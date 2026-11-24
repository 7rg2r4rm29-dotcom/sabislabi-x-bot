"""Tests d'Analytics (bot/analytics.py) : aucun appel réseau."""
from __future__ import annotations

from datetime import timedelta

import pytest

from bot import analytics as amod
from bot.analytics import Analytics
from bot import utils
from bot.utils import iso, now_local
from bot.x_api import XError
from tests.fakes import FakeX


@pytest.fixture(autouse=True)
def _frozen(clock):
    return clock


def m(imp: int, likes: int = 0, replies: int = 0, reposts: int = 0, bookmarks: int = 0, quotes: int = 0) -> dict:
    return {"impressions": imp, "likes": likes, "replies": replies, "reposts": reposts, "quotes": quotes,
            "bookmarks": bookmarks, "fetched_at": iso(now_local())}


def pub(state, tid: str, kind: str, text: str, *, days: float = 2, hour: int = 14, minute: int = 0,
        metrics: dict | None = None, item_id: str | None = None) -> dict:
    dt = (now_local() - timedelta(days=days)).replace(hour=hour, minute=minute, second=0, microsecond=0)
    p = {"tweet_id": tid, "kind": kind, "text": text, "item_id": item_id, "slot_key": None,
         "created_at": iso(dt), "metrics": metrics}
    state.data["published"].append(p)
    return p


def api_tweet(tid: str, imp: int, likes: int = 0, replies: int = 0, reposts: int = 0, bookmarks: int = 0) -> dict:
    return {"id": tid, "text": "x", "created_at": "2026-10-13T12:00:00.000Z",
            "public_metrics": {"impression_count": imp, "like_count": likes, "reply_count": replies,
                               "retweet_count": reposts, "quote_count": 0, "bookmark_count": bookmarks}}


@pytest.fixture
def an(settings, state, fake_x, budget):
    return Analytics(settings, state, fake_x, budget)


# ---------------------------------------------------------------------------
# Relevé
# ---------------------------------------------------------------------------
def test_refresh_metrics_matches_published(an, state, fake_x):
    pub(state, "1", "tip", "tip one")
    pub(state, "2", "news", "news two")
    pub(state, "3", "gm", "gm three")
    fake_x.recent_posts = [api_tweet("1", 1000, likes=10, replies=2, bookmarks=5),
                           api_tweet("2", 500, likes=3), api_tweet("999", 50)]
    assert an.refresh_metrics() == 2
    p1 = state.data["published"][0]
    assert p1["metrics"]["impressions"] == 1000 and p1["metrics"]["likes"] == 10
    assert p1["metrics"]["replies"] == 2 and p1["metrics"]["bookmarks"] == 5
    assert set(p1["metrics"]) == {"impressions", "likes", "replies", "reposts", "quotes", "bookmarks",
                                  "fetched_at"}
    assert state.data["published"][2]["metrics"] is None
    assert state.last_run("metrics") is not None
    assert state.data["performance"]["settled_posts"] == 2


class _ErrX(FakeX):
    def my_recent_posts(self, max_results=50):
        self.calls.append(("my_recent_posts",))
        raise XError("server", "down")


def test_refresh_metrics_x_error(settings, state, budget):
    pub(state, "1", "tip", "tip one", metrics=m(10))
    an = Analytics(settings, state, _ErrX(), budget)
    assert an.refresh_metrics() == 0
    assert state.data["published"][0]["metrics"]["impressions"] == 10


def test_maybe_refresh_interval(an, state, fake_x, clock):
    assert an.maybe_refresh() == 0                      # aucun post : pas de lecture
    assert ("my_recent_posts",) not in fake_x.calls
    pub(state, "1", "tip", "tip one")
    fake_x.recent_posts = [api_tweet("1", 100)]
    clock(10, 0)
    assert an.maybe_refresh() == 1
    clock(15, 49)
    assert an.maybe_refresh() == 0                      # 349 min < 350 : environ toutes les 6 h
    assert len([c for c in fake_x.calls if c == ("my_recent_posts",)]) == 1
    clock(15, 51)
    assert an.maybe_refresh() == 1
    assert len([c for c in fake_x.calls if c == ("my_recent_posts",)]) == 2


def test_refresh_remembers_format_from_item(an, state, fake_x):
    it = state.new_item("tip", ["t"], image_path="/tmp/a.jpg", image_kind="shot",
                        meta={"used_notes": ["I tried X"], "angle": "", "hook": "THE NUMBER",
                              "card": {"type": "steps", "title": "T"}})          # vieille carte : ignorée
    th = state.new_item("thread", ["a", "b", "c"], meta={"angle": "future", "style_test": True})
    pub(state, "1", "tip", "tip", item_id=it["id"])
    pub(state, "2", "thread", "thread", item_id=th["id"])
    fake_x.recent_posts = [api_tweet("1", 10), api_tweet("2", 20)]
    an.refresh_metrics()
    f1, f2 = state.data["published"][0]["fmt"], state.data["published"][1]["fmt"]
    assert set(f1) == {"media", "image_kind", "genre", "hook", "notes", "fallback", "tweets",
                       "test"}                                                       # plus de « card »
    assert f1["test"] is False and f2["test"] is True        # post d'essai (pas « ce qui marche »)
    assert f1["media"] == "image" and f1["notes"] is True and f1["image_kind"] == "shot"
    assert f1["hook"] == "THE NUMBER" and f2["hook"] == ""
    assert f1["genre"] == ""                                 # le genre ne concerne que les threads
    assert f2["genre"] == "future" and f2["tweets"] == 3 and f2["media"] == "text"


def test_video_posts_are_their_own_format(an, state, fake_x):
    it = state.new_item("news", ["t"], image_path="/tmp/clip.mp4", image_kind="video", meta={})
    pub(state, "1", "news", "news", item_id=it["id"])
    fake_x.recent_posts = [api_tweet("1", 10)]
    an.refresh_metrics()
    assert state.data["published"][0]["fmt"]["media"] == "video"


class _MeX(FakeX):
    def __init__(self, followers=1000, fail=False):
        super().__init__()
        self.followers = followers
        self.fail = fail

    def me(self):
        self.calls.append(("me",))
        if self.fail:
            raise XError("auth", "no keys")
        return {"id": "42", "username": "sabislabi", "public_metrics": {"followers_count": self.followers}}


def test_followers_recorded_once_per_day(settings, state, budget, clock):
    x = _MeX(1234)
    an = Analytics(settings, state, x, budget)
    an.refresh_metrics()
    an.refresh_metrics()
    assert state.data["followers"] == [{"date": "2026-10-15", "count": 1234}]
    assert x.calls.count(("me",)) == 1
    clock(10, 0, day=16)
    x.followers = 1300
    an.refresh_metrics()
    assert state.data["followers"][-1] == {"date": "2026-10-16", "count": 1300}


def test_followers_optional_and_errors(settings, state, budget, fake_x):
    class _NoMeX(FakeX):
        me = None                                                     # client sans me()
    Analytics(settings, state, _NoMeX(), budget).refresh_metrics()
    assert not state.data.get("followers")
    Analytics(settings, state, _MeX(fail=True), budget).refresh_metrics()
    assert not state.data.get("followers")


# ---------------------------------------------------------------------------
# Agrégats
# ---------------------------------------------------------------------------
def test_compute_aggregates(an, state):
    pub(state, "1", "tip", "tip A", days=2, hour=14, metrics=m(1000, likes=20, replies=5, bookmarks=10))
    pub(state, "2", "tip", "tip B", days=3, hour=14, metrics=m(3000, likes=40, replies=5, bookmarks=30))
    pub(state, "3", "news", "news C", days=4, hour=18, metrics=m(200, likes=2))
    pub(state, "4", "gm", "too fresh", days=0, hour=8, metrics=m(99999))          # < 24 h : ignoré
    pub(state, "5", "gm", "too old", days=40, hour=8, metrics=m(99999))           # > 30 j : ignoré
    pub(state, "6", "comment_reply", "a reply", days=2, metrics=m(99999))         # pas un post
    pub(state, "7", "news", "no metrics", days=2)
    perf = an.recompute()
    assert perf["settled_posts"] == 3
    assert perf["by_kind"]["tip"]["count"] == 2
    assert perf["by_kind"]["tip"]["avg_impressions"] == 2000
    assert perf["by_kind"]["tip"]["avg_bookmarks"] == 20
    assert perf["by_kind"]["tip"]["engagement_rate"] == pytest.approx((60 + 10 + 40) / 4000, abs=1e-4)
    assert set(perf["by_kind"]) == {"tip", "news"}
    assert perf["by_hour"]["14"]["count"] == 2 and perf["by_hour"]["18"]["count"] == 1
    assert perf["best"]["tweet_id"] == "2" and perf["best"]["impressions"] == 3000
    assert perf["worst"]["tweet_id"] == "3"
    assert state.data["performance"] == perf


def test_best_worst_limited_to_14_days(an, state):
    pub(state, "1", "tip", "old star", days=20, metrics=m(50000))
    pub(state, "2", "tip", "recent", days=2, metrics=m(300))
    perf = an.compute()
    assert perf["best"]["tweet_id"] == "2"
    assert perf["by_kind"]["tip"]["count"] == 2


# ---------------------------------------------------------------------------
# Notes pour Claude
# ---------------------------------------------------------------------------
def test_performance_notes_empty_below_five_posts(an, state):
    for i in range(4):
        pub(state, str(i), "tip", f"tip {i}", days=2 + i, metrics=m(1000))
    assert an.performance_notes() == ""


def test_performance_notes_content(an, state):
    for i in range(3):
        pub(state, f"t{i}", "tip", f"Tip number {i}: paste the whole error", days=2 + i, hour=14,
            metrics=m(3000 + i, likes=30, replies=6, bookmarks=20))
    for i in range(3):
        pub(state, f"n{i}", "news", f"News {i}", days=2 + i, hour=8, metrics=m(500, likes=2, replies=0))
    notes = an.performance_notes()
    lines = notes.splitlines()
    assert 3 <= len(lines) <= 6
    assert lines[0].startswith("Last 30 days, 6 posts")
    assert "over-performing: tip posts" in notes
    assert "under-performing: news posts" in notes
    assert "best publishing hour 14:00" in notes.lower()
    assert "Top post" in notes and "Tip number 2" in notes
    assert "vues" not in notes   # anglais


def test_performance_notes_mentions_real_notes(an, state):
    for i in range(3):
        pub(state, f"a{i}", "tip", f"with note {i}", days=2 + i, metrics=m(1000, likes=80))
        state.data["published"][-1]["fmt"] = {"media": "text", "notes": True}
    for i in range(3):
        pub(state, f"b{i}", "tip", f"without {i}", days=2 + i, metrics=m(1000, likes=5))
        state.data["published"][-1]["fmt"] = {"media": "text", "notes": False}
    assert "real notes" in an.performance_notes()


# ---------------------------------------------------------------------------
# Récap du matin
# ---------------------------------------------------------------------------
def test_daily_report(an, settings, state, budget, clock):
    now = clock(8, 30)
    pub(state, "1", "gm", "gm <friends> & builders", days=1, hour=8, minute=2,
        metrics=m(1500, likes=12, replies=3))
    pub(state, "2", "tip", "Tip: paste the full error", days=1, hour=13, minute=31)
    pub(state, "3", "tip", "older post", days=3, metrics=m(10))
    # Créneau gm d'aujourd'hui déjà rédigé, en attente.
    gm_slot = next(s for s in settings.post_slots if s.kind == "gm")
    it = state.new_item("gm", ["gm"], slot_key=gm_slot.key, slot_date=now.strftime("%Y-%m-%d"))
    state.mark_slot(now.strftime("%Y-%m-%d"), gm_slot.key, it["id"])
    state.new_item("radar", ["r"], status="suggested")
    state.data["llm_usage"] = {"days": {"2026-10-14": {"calls": 37, "usd": 0.42}}}
    state.incr("comment_drafts", 4, date="2026-10-14")
    budget.record("post", 2)
    txt = an.daily_report()
    assert "Récap du jeudi 15/10" in txt
    assert "2 post(s) publié(s)" in txt
    assert "08:02 gm" in txt and "1 500 vues" in txt and "12 ❤️" in txt
    assert "stats pas encore relevées" in txt
    assert "older post" not in txt
    assert "&lt;friends&gt; &amp; builders" in txt and "<friends>" not in txt
    assert "08:00 gm — ⏳ attend ta validation" in txt
    assert "13:30 futur — 📝 pas encore rédigé" in txt
    assert "thread" not in txt                      # jeudi : pas de thread prévu
    assert "1 brouillon(s) attendent ta validation" in txt
    assert "1 suggestion(s) radar ouvertes" in txt
    assert "4 brouillon(s)" in txt
    assert "API X" in txt
    assert "Claude hier : 0.42 $ (37 appel(s)) · 0.42 $ ce mois sur 35 $" in txt
    assert "plafond atteint" not in txt


def test_daily_report_recalls_the_claude_cap_when_it_is_reached(an, settings, state, clock):
    from bot import llm as llm_mod
    clock(8, 30)
    state.data["llm_usage"] = {"days": {"2026-10-14": {"calls": 37, "usd": 0.42},
                                        "2026-10-02": {"calls": 900, "usd": 33.0},
                                        "2026-09-30": {"calls": 9, "usd": 20.0}}}       # septembre : pas compté
    utils.save_json(llm_mod.manual_ledger_path(), {"days": {"2026-10-05": {"calls": 9, "usd": 1.6}}})
    txt = an.daily_report()
    assert "· 35.02 $ ce mois sur 35 $ — plafond atteint : plus rien n'est rédigé jusqu'au 1er" in txt
    settings.claude_budget_monthly_usd = 0                 # sans plafond : la ligne d'avant, telle quelle
    assert "Claude hier : 0.42 $ (37 appel(s))\n" in an.daily_report() + "\n"


def test_daily_report_shows_thread_on_thread_day(an, clock):
    clock(8, 30, day=20)                            # mardi
    txt = an.daily_report()
    assert "Récap du mardi 20/10" in txt
    assert "15:00 thread" in txt
    # Comme le planificateur : pas d'autre post (hors gm) à moins de 3 h du thread.
    assert "• 13:30" not in txt and "• 08:00" in txt and "• 18:00 actu" in txt


def test_daily_report_shows_skipped_and_pending_slots(an, settings, state, clock):
    now = clock(8, 30)
    today = now.strftime("%Y-%m-%d")
    tip, news = [s for s in settings.post_slots if s.kind in ("tip", "news")]
    state.mark_slot(today, tip.key, "skipped")
    state.mark_slot(today, news.key, "pending")
    txt = an.daily_report()
    assert "⏭️ sauté (rien d'assez solide)" in txt and "✍️ rédaction en cours" in txt


def test_daily_report_statuses(an, settings, state, clock):
    now = clock(8, 30)
    today = now.strftime("%Y-%m-%d")
    for slot, status in zip(settings.post_slots, ("published", "approved", "expired")):
        it = state.new_item(slot.kind, ["x"], status=status, publish_at=iso(now.replace(hour=13, minute=34)))
        state.mark_slot(today, slot.key, it["id"])
    txt = an.daily_report()
    assert "✅ publié" in txt
    assert "🕓 validé, part à 13:34" in txt
    assert "⌛ expiré, rien publié" in txt


def test_daily_report_tip_when_drafts_ignored(an, state):
    for i in range(6):
        state.new_item("comment_reply", ["r"], status="expired" if i < 5 else "published")
    txt = an.daily_report()
    assert "💡 5/6 brouillons de réponse aux commentaires" in txt
    an2_state = state
    for it in list(an2_state.data["items"].values()):
        it["status"] = "published"
    assert "💡" not in an.daily_report()


def test_daily_report_tip_for_radar(an, state):
    for i in range(5):
        state.new_item("radar", ["r"], status="dismissed")
    assert "suggestions radar" in an.daily_report()


def test_reports_never_raise_on_bad_data(an, state):
    state.data["published"].append({"tweet_id": "x", "kind": "tip", "text": None, "created_at": "garbage",
                                    "metrics": "oops"})
    state.data["published"].append({"tweet_id": "y", "kind": "tip", "text": "ok",
                                    "created_at": iso(now_local() - timedelta(days=1)),
                                    "metrics": {"impressions": "n/a"}})
    assert "Récap" in an.daily_report()
    assert "Bilan" in an.weekly_report()
    assert isinstance(an.performance_notes(), str)


# ---------------------------------------------------------------------------
# Bilan hebdo
# ---------------------------------------------------------------------------
def test_weekly_report(an, state, budget, clock):
    clock(20, 30, day=18)                            # dimanche
    pub(state, "1", "tip", "Best tip <ever>", days=2, hour=14, metrics=m(5000, likes=60, replies=9, bookmarks=40))
    pub(state, "2", "tip", "tip two", days=3, hour=14, metrics=m(1000, likes=10, replies=1))
    pub(state, "3", "thread", "My thread hook", days=4, hour=15, metrics=m(2000, likes=20, replies=4))
    pub(state, "4", "news", "fresh news", days=0, hour=18)
    pub(state, "5", "gm", "two weeks ago", days=14, metrics=m(99999))
    state.data["llm_usage"] = {"days": {"2026-10-18": {"calls": 10, "usd": 0.5},
                                        "2026-10-13": {"calls": 10, "usd": 0.25},
                                        "2026-10-01": {"calls": 10, "usd": 9.0}}}
    budget.record("post", 10)
    state.data["followers"] = [{"date": "2026-10-10", "count": 900}, {"date": "2026-10-11", "count": 950},
                               {"date": "2026-10-18", "count": 1000}]
    txt = an.weekly_report()
    assert "Bilan de la semaine" in txt and "du 12/10 au 18/10" in txt
    assert "4 post(s) publié(s) dont 1 thread(s)" in txt
    assert "8 000 vues" in txt
    assert "1 post(s) pas encore mesuré(s)" in txt
    assert "Meilleur post" in txt and "Best tip &lt;ever&gt;" in txt and "5 000 vues" in txt
    assert "two weeks ago" not in txt
    assert "<pre>" in txt and "futur" in txt and "thread" in txt and "astuce" not in txt
    assert "API X 0.15 $" in txt
    assert "Claude 0.75 $" in txt
    assert "Abonnés : 1 000 (+50 depuis le 11/10)" in txt
    assert "/note" in txt


def test_weekly_report_best_hours_and_empty_week(an, state, clock):
    clock(20, 30, day=18)
    txt = an.weekly_report()
    assert "Aucun post publié ces 7 derniers jours." in txt
    assert "/note" in txt
    for i in range(2):
        pub(state, f"a{i}", "tip", "a", days=2 + i, hour=14, metrics=m(3000))
        pub(state, f"b{i}", "news", "b", days=2 + i, hour=8, metrics=m(500))
    txt = an.weekly_report()
    assert "Meilleures heures (30 j, moyenne 1 750 vues) : 14h" in txt
    assert "8h" not in txt.split("Meilleures heures")[1].split("\n")[0]


def test_followers_line_single_entry(an, state):
    state.data["followers"] = [{"date": "2026-10-15", "count": 321}]
    assert "👥 Abonnés : 321" in an.weekly_report()
    assert "(" not in an._followers_line()


def test_kind_labels_cover_all_kinds():
    for k in amod.KINDS:
        assert k in amod.KIND_FR
    assert amod.KIND_FR["tip"] == "futur" and amod.KIND_FR["reactive"] == "à chaud"
    assert all(len(v) <= 9 for v in amod.KIND_FR.values())   # colonne du tableau du bilan hebdo


def _hooked_week(state, fake_x) -> None:
    """3 posts « THE NUMBER » qui marchent, 3 « THE MYSTERY » qui marchent moins, 1 sans forme notée."""
    tweets = []
    for i in range(3):
        it = state.new_item("tip", [f"number post {i}"], image_path=f"/tmp/p{i}.jpg", image_kind="shot",
                            meta={"hook": "THE NUMBER"})
        pub(state, f"p{i}", "tip", f"Number post {i}", days=2 + i, item_id=it["id"])
        tweets.append(api_tweet(f"p{i}", 4000, likes=40, bookmarks=30))
    for i in range(3):
        it = state.new_item("news", [f"mystery post {i}"], meta={"hook": "THE MYSTERY"})
        pub(state, f"n{i}", "news", f"Mystery post {i}", days=2 + i, hour=19, item_id=it["id"])
        tweets.append(api_tweet(f"n{i}", 400, likes=1))
    plain = state.new_item("tip", ["no hook noted"], meta={})
    pub(state, "x1", "tip", "Old post without a hook", days=3, item_id=plain["id"])
    tweets.append(api_tweet("x1", 2200))
    fake_x.recent_posts = tweets


def test_hook_shape_is_remembered_and_compared(an, state, fake_x):
    """Quelle forme d'accroche marche sur CE compte : notée avec le post, puis dite à Claude."""
    _hooked_week(state, fake_x)
    an.refresh_metrics()
    fmts = {p["tweet_id"]: p["fmt"] for p in state.data["published"]}
    assert fmts["p0"]["hook"] == "THE NUMBER" and fmts["p0"]["image_kind"] == "shot"
    assert fmts["n0"]["hook"] == "THE MYSTERY" and fmts["x1"]["hook"] == ""
    perf = an.compute()
    assert "by_card" not in perf
    assert set(perf["by_hook"]) == {"THE NUMBER", "THE MYSTERY"} and perf["by_hook"]["THE NUMBER"]["count"] == 3
    notes = an.performance_notes()
    hook_line = next(line for line in notes.splitlines() if line.startswith("Hook shapes on this account: "))
    assert '"THE NUMBER" hooks do 1.8x avg impressions (n=3)' in hook_line      # 4 000 vues / 2 200
    assert '"THE MYSTERY" only 0.2x (n=3)' in hook_line
    assert hook_line.endswith("Lean toward what works, but keep varying.")
    assert "card" not in notes


def test_hook_note_needs_two_posts_per_shape(an, state, fake_x):
    _hooked_week(state, fake_x)
    for p in state.data["published"]:
        if p["tweet_id"] in ("p1", "p2", "n1", "n2"):
            p["item_id"] = None                      # forme d'accroche inconnue pour ces posts-là
    an.refresh_metrics()
    assert "Hook shapes" not in an.performance_notes()


def test_image_posts_are_named_as_such_in_the_notes(an, state, fake_x):
    """Photos de l'actu, et avant elles captures ou cartes : des « posts with an image »."""
    _hooked_week(state, fake_x)
    an.refresh_metrics()
    notes = an.performance_notes()
    assert "Posts with an image do " in notes and "text-only posts only" in notes
    assert "screenshot" not in notes


def test_performance_notes_keep_their_promised_length(an, state, fake_x):
    _hooked_week(state, fake_x)
    for tw in fake_x.recent_posts[:3]:                # les posts « THE NUMBER » font aussi parler
        tw["public_metrics"]["reply_count"] = 9
    for i in range(2):                                # un 2e type qui sous-performe et une heure faible
        pub(state, f"g{i}", "gm", f"gm post {i}", days=2 + i, hour=8, metrics=m(100, likes=0))
    an.refresh_metrics()
    notes = an.performance_notes()
    # résumé, types, réponses, formats, accroches, heures, meilleur post : 7 lignes au plus
    assert len(notes.splitlines()) <= 7, notes
    assert "≤ 7 lignes" in an.performance_notes.__doc__


# ---------------------------------------------------------------------------
# Ce qui marche (consigne du propriétaire, 23/11/2026)
# ---------------------------------------------------------------------------
def topic_pub(state, tid: str, topic: str, imp: int, *, days: float = 2, kind: str = "news",
              text: str | None = None, **eng) -> dict:
    p = pub(state, tid, kind, text or f"Story {tid}", days=days, metrics=m(imp, **eng))
    p["topic"] = topic
    return p


def _mixed_month(state):
    # Jeux vidéo : 3 posts à 200 vues ; business : 4 posts à 20 vues ; applis d'IA : 4 posts à 40.
    for i in range(3):
        topic_pub(state, f"g{i}", "games", 200, days=2 + i, text=f"GTA VI is finally out, take {i}.")
    for i in range(4):
        topic_pub(state, f"b{i}", "business", 20, days=2 + i)
    for i in range(4):
        topic_pub(state, f"a{i}", "ai-apps", 40, days=2 + i)


def test_post_score_counts_interactions():
    p = {"metrics": m(100, likes=2, replies=1, reposts=1, bookmarks=1, quotes=1)}
    assert amod.post_score(p) == 100 + 30 * 6
    assert amod.post_score({}) == 0


def test_what_works_needs_five_measured_posts(an, state):
    for i in range(4):
        topic_pub(state, f"p{i}", "games", 100, days=2 + i)
    assert an.what_works() == {}
    assert an.editor_notes() == "" and an.style_notes() == ""


def test_topics_that_work_and_topics_that_fall_flat(an, state):
    _mixed_month(state)
    ww = an.what_works()
    assert ww["baseline"] == 40
    assert ww["topics"]["games"]["lift"] > 2.0 and ww["topics"]["games"]["count"] == 3
    assert ww["topics"]["business"]["lift"] < amod.FLOPS
    assert 0.9 <= ww["topics"]["ai-apps"]["lift"] <= 1.1
    assert ww["best"][0]["text"].startswith("GTA VI is finally out")


def test_one_lucky_post_is_not_a_rule(an, state):
    topic_pub(state, "v", "robots", 400, days=2, text="A tiny machine did something wild.")
    for i in range(6):
        topic_pub(state, f"n{i}", "ai-apps", 40, days=2 + i)
    lift = an.what_works()["topics"]["robots"]["lift"]
    assert 1.5 < lift < 2.5                       # 10 fois la normale → compté 4 fois, puis tempéré
    assert "Stories our readers react to" not in an.editor_notes()   # un seul post : pas encore une règle


def test_what_stopped_working_fades(an, state):
    """« Tant que ça marche » : les jeux marchaient il y a trois semaines, plus maintenant."""
    for i in range(3):
        topic_pub(state, f"old{i}", "games", 300, days=20 + i)
    for i in range(3):
        topic_pub(state, f"new{i}", "games", 10, days=2 + i)
    for i in range(4):
        topic_pub(state, f"n{i}", "ai-apps", 40, days=2 + i)
    assert an.what_works()["topics"]["games"]["lift"] < 1.0


def test_editor_notes_follow_what_works(an, state):
    _mixed_month(state)
    notes = an.editor_notes()
    assert notes.startswith("WHAT WORKS ON THIS ACCOUNT")
    assert "Stories our readers react to: video games & play (" in notes
    assert "Stories that fall flat with them: tech business & money raised (" in notes
    assert "GTA VI is finally out" in notes
    assert "THIS PICK FOLLOWS WHAT WORKS" in notes and "TEST" not in notes
    test = an.editor_notes(explore=True)
    assert "THIS PICK IS A TEST" in test and "FOLLOWS WHAT WORKS" not in test


def test_style_notes_show_our_best_posts_in_full(an, state):
    _mixed_month(state)
    for p in state.data["published"]:
        if p["tweet_id"].startswith("g"):
            p["fmt"] = {"media": "image", "hook": "THE NUMBER"}
    state.data["published"][0]["metrics"]["likes"] = 3
    state.data["published"][1]["metrics"].update(likes=1, replies=1, bookmarks=2)
    notes = an.style_notes()
    assert notes.startswith("OUR BEST RECENT POSTS — insist on what works")
    assert "  • (200 views, 3 likes) GTA VI is finally out, take 0." in notes
    assert "  • (200 views, 1 like, 1 reply, 2 bookmarks) GTA VI is finally out, take 1." in notes
    assert notes.count("  • ") == 3
    assert 'First-line shapes that work best here: "THE NUMBER"' in notes
    assert "vues" not in notes                                    # anglais
    test = an.style_notes(explore=True)
    assert test.startswith("THIS POST IS A TEST") and "GTA" not in test


def test_only_posts_better_than_usual_are_style_models(an, state):
    for i in range(6):
        topic_pub(state, f"n{i}", "ai-apps", 40, days=2 + i)
    assert an.what_works()["best"] == []
    assert "OUR BEST RECENT POSTS" not in an.style_notes()


def test_ensure_topics_labels_each_post_once(an, state, clock):
    pub(state, "1", "news", "GTA VI is out", metrics=m(10))
    pub(state, "2", "tip", "A robot folds laundry", metrics=None)      # pas encore mesuré : classé aussi
    p3 = pub(state, "3", "news", "Already sorted", metrics=m(10))
    p3["topic"] = "science"
    seen = []

    def labeler(texts):
        seen.append(list(texts))
        return ["games", "robots"]

    assert an.ensure_topics(labeler) == 2
    assert seen == [["GTA VI is out", "A robot folds laundry"]]
    assert [p.get("topic") for p in state.data["published"]] == ["games", "robots", "science"]
    pub(state, "4", "news", "A new one", metrics=m(10))
    assert an.ensure_topics(labeler) == 0                        # 6 h entre deux classements
    clock(17, 0)
    assert an.ensure_topics(lambda texts: ["nonsense"]) == 0     # sujet inconnu : ignoré
    assert state.data["published"][3].get("topic") is None


def test_ensure_topics_survives_claude_errors(an, state):
    pub(state, "1", "news", "GTA VI is out", metrics=m(10))

    def boom(texts):
        raise RuntimeError("claude down")

    assert an.ensure_topics(boom) == 0
    assert an.ensure_topics(None) == 0


def test_developer_stories_are_never_pushed_even_when_they_work(an, state):
    """La ligne éditoriale vise le grand public : les sujets pour initiés ne sont jamais
    « ce qui marche », ni des modèles de style (23/11/2026 : 1,3x, l'ancien public tech)."""
    for i in range(3):
        topic_pub(state, f"d{i}", "insider", 200, days=2 + i, text=f"A new SDK for agents, take {i}.")
    for i in range(4):
        topic_pub(state, f"a{i}", "ai-apps", 40, days=2 + i)
    assert an.what_works()["topics"]["insider"]["lift"] > 2.0
    notes = an.editor_notes()
    assert "developers" not in notes and "SDK" not in notes
    assert "SDK" not in an.style_notes()


# ---------------------------------------------------------------------------
# Le format suit les résultats (consigne du propriétaire, 24/11/2026 : « visuel ou textuel, peu
# importe : choisis la meilleure option à chaque fois »)
# ---------------------------------------------------------------------------
def _formats_month(state, *, threads=5, thread_views=100, tips=6, tip_views=30, news=8, news_views=40,
                   explainers=0, explainer_views=40):
    for i in range(threads):
        pub(state, f"th{i}", "thread", f"Thread {i}", days=2 + i, metrics=m(thread_views))
    for i in range(tips):
        pub(state, f"tp{i}", "tip", f"Tip {i}", days=2 + i, metrics=m(tip_views))
    for i in range(news):
        pub(state, f"nw{i}", "news", f"News {i}", days=2 + i, metrics=m(news_views))
    for i in range(explainers):
        pub(state, f"ex{i}", "explainer", f"Video {i}", days=2 + i, metrics=m(explainer_views))


ALL_FORMATS = ["explainer", "thread", "tip"]


def test_without_figures_nothing_is_chosen(an):
    import random
    assert an.format_table() == {}
    assert an.choose_format(ALL_FORMATS, random.Random(1)) is None
    assert an.second_format("tip", ["explainer", "thread"]) is None
    assert an.photo_advice() == (True, "")


def test_format_table_counts_measured_and_published_posts(an, state):
    _formats_month(state)
    pub(state, "fresh", "explainer", "Video of the day", days=0, hour=8, metrics=None)   # publié, pas encore mesuré
    table = an.format_table()
    assert table["thread"]["count"] == 5 and table["thread"]["tries"] == 5 and table["thread"]["lift"] > 1.5
    assert table["tip"]["lift"] < 1.0 and table["news"]["count"] == 8
    assert table["explainer"] == {"lift": 1.0, "count": 0, "tries": 1}


def test_a_format_hardly_tried_goes_first(an, state):
    import random
    _formats_month(state)                                    # aucune vidéo encore
    kind, why = an.choose_format(ALL_FORMATS, random.Random(1))
    assert kind == "explainer" and "0 sur 3" in why
    # Ce qui est déjà prévu sans être publié compte comme un essai : pas cinq jours de suite.
    assert an.choose_format(ALL_FORMATS, random.Random(1), {"explainer": 2})[0] == "explainer"
    kind, why = an.choose_format(ALL_FORMATS, random.Random(1), {"explainer": 3})
    assert "tiré selon les résultats" in why and "thread" in why
    # Un format que les réglages ne permettent pas n'est jamais choisi.
    assert an.choose_format(["thread", "tip"], random.Random(1))[0] in ("thread", "tip")
    assert an.choose_format([], random.Random(1)) is None and an.choose_format(["news"], random.Random(1)) is None


def test_the_draw_follows_the_results_and_keeps_testing_the_rest(an, state):
    import random
    _formats_month(state, explainers=3)
    table = an.format_table()
    shares = an.format_shares(table, ALL_FORMATS)
    assert abs(sum(shares.values()) - 1.0) < 1e-9
    assert shares["thread"] > 0.6 > shares["explainer"] > shares["tip"] >= amod.FORMAT_MIN_SHARE
    picks = [an.choose_format(ALL_FORMATS, random.Random(f"format:2026-11-{d:02d}"))[0] for d in range(1, 29)]
    assert picks.count("thread") > 14 and set(picks) == set(ALL_FORMATS)       # le meilleur, mais pas que lui
    # Le même jour donne toujours le même tirage (un redémarrage ne change rien).
    assert an.choose_format(ALL_FORMATS, random.Random("format:2026-11-25")) \
        == an.choose_format(ALL_FORMATS, random.Random("format:2026-11-25"))


def test_a_flopping_format_keeps_one_slot_in_ten():
    table = {"thread": {"lift": 2.5}, "explainer": {"lift": 2.0}, "tip": {"lift": 0.2}}
    shares = Analytics.format_shares(table, ALL_FORMATS)
    assert shares["tip"] == pytest.approx(amod.FORMAT_MIN_SHARE)
    assert sum(shares.values()) == pytest.approx(1.0) and shares["thread"] > shares["explainer"] > shares["tip"]
    assert Analytics.format_shares(table, ["tip"]) == {"tip": 1.0}
    assert Analytics.format_shares({}, ALL_FORMATS) == pytest.approx({k: 1 / 3 for k in ALL_FORMATS})
    assert Analytics.format_shares(table, ["news"]) == {}


def test_a_proven_format_also_replaces_a_news_post(an, state):
    _formats_month(state)                                    # threads : 100 vues contre 40 pour l'actu
    kind, why = an.second_format("explainer", ["explainer", "thread"])
    assert kind == "thread" and "posts mesurés" in why
    assert an.second_format("thread", ["explainer", "thread"]) is None     # déjà au grand créneau
    assert an.second_format("explainer", ["explainer"]) is None            # pas permis par les réglages


@pytest.mark.parametrize("threads, views", [(3, 100), (5, 45)])
def test_a_format_is_not_proven_on_thin_or_flat_figures(an, state, threads, views):
    _formats_month(state, threads=threads, thread_views=views)
    assert an.second_format("explainer", ["explainer", "thread"]) is None


def _media_posts(state, media: str, n: int, views: int):
    for i in range(n):
        pub(state, f"{media}{i}", "news", f"{media} post {i}", days=2 + i % 6, metrics=m(views))["fmt"] = {"media": media}


@pytest.mark.parametrize("images, image_views, keep", [(12, 20, False), (11, 20, True), (12, 50, True)])
def test_photos_are_dropped_only_when_the_figures_are_clear(an, state, images, image_views, keep):
    _media_posts(state, "image", images, image_views)
    _media_posts(state, "text", 12, 60)
    ok, why = an.photo_advice()
    assert ok is keep
    assert ("avec photo" in why and "texte seul" in why) if not keep else why == ""
    assert bool((state.data.get("photo_policy") or {}).get("off")) is (not keep)


def test_photos_do_not_flip_back_and_forth(an, state):
    """Coupées sur un écart net, elles ne reviennent que quand l'écart s'est vraiment refermé."""
    def figures(image_views: int, images: int = 12) -> bool:
        state.data["published"].clear()
        _media_posts(state, "image", images, image_views)
        _media_posts(state, "text", 12, 60)
        return an.photo_advice()[0]

    assert figures(52) is True                    # un petit écart : rien ne change
    assert figures(20) is False                   # un écart net : coupées
    since = state.data["photo_policy"]["since"]
    assert figures(46) is False                   # un peu mieux, pas assez : toujours coupées
    assert state.data["photo_policy"]["since"] == since
    assert figures(58) is True                    # l'écart s'est refermé : de retour
    assert figures(20) is False
    assert figures(20, images=5) is True          # plus assez de posts avec photo pour juger : on réessaie


# ---------------------------------------------------------------------------
# La feuille de chiffres de l'agent d'analyse
# ---------------------------------------------------------------------------
def test_figures_sheet_for_the_analyst(an, state):
    assert an.figures_sheet() == ("", 0)                      # pas assez de chiffres
    _formats_month(state)
    pub(state, "q1", "news", "GTA VI is finally out.\n\nWould you buy a console for it?", days=3,
        metrics=m(200, likes=2))["fmt"] = {"media": "image", "hook": "NO X, NO Y", "test": False}
    state.data["published"][-1]["topic"] = "games"
    sheet, n = an.figures_sheet()
    assert n == 20 and sheet.startswith("OUR NUMBERS — the 20 posts of the last 30 days")
    for label in ("- Totals: ", "- Last 7 days: ", "- By topic: games ", "- By kind of post", "thread ", "- By media: ",
                  "- By shape of the first line: NO X, NO Y", "- By time of day (Paris time", "- By day: ",
                  "- By length of the post: ", "- By last line: ", "ends with a question", "- First line has a number: ",
                  "- First line names something everybody knows", "- First line sounds like breaking news",
                  "OUR BEST POSTS:", "OUR WEAKEST POSTS:"):
        assert label in sheet, label
    assert "200 views, 2 reactions |" in sheet and "| news | games | image | GTA VI is finally out." in sheet
    assert "x (" in sheet                                     # chaque chiffre dit sur combien de posts il repose


def test_figures_sheet_never_raises(an, state, monkeypatch):
    _formats_month(state)
    monkeypatch.setattr(amod, "post_score", lambda p: 1 / 0)
    assert an.figures_sheet() == ("", 0)


def test_the_trend_flag_is_remembered_only_when_it_was_noted(an, state, fake_x):
    """Depuis le 24/11/2026 : `fmt.trend` dit si le post collait à une tendance du jour. Les posts
    d'avant n'ont pas l'information : ils ne comptent ni pour ni contre."""
    old = state.new_item("news", ["old post"], meta={"hook": ""})
    linked = state.new_item("news", ["GTA post"], meta={"trend": "gta 6"})
    plain = state.new_item("news", ["robot post"], meta={"trend": ""})
    for tid, it in (("1", old), ("2", linked), ("3", plain)):
        pub(state, tid, "news", it["texts"][0], item_id=it["id"])
    fake_x.recent_posts = [api_tweet("1", 10), api_tweet("2", 20), api_tweet("3", 30)]
    an.refresh_metrics()
    f = [p["fmt"] for p in state.data["published"]]
    assert "trend" not in f[0] and f[1]["trend"] is True and f[2]["trend"] is False


def test_figures_sheet_says_whether_following_trends_pays(an, state):
    for i in range(6):
        pub(state, f"t{i}", "news", f"GTA story {i}", days=2 + i, metrics=m(120))["fmt"] = {"media": "text", "trend": True}
    for i in range(6):
        pub(state, f"n{i}", "news", f"Other story {i}", days=2 + i, metrics=m(40))["fmt"] = {"media": "text", "trend": False}
    for i in range(6):
        pub(state, f"o{i}", "news", f"Old story {i}", days=2 + i, metrics=m(40))["fmt"] = {"media": "text"}
    sheet, _n = an.figures_sheet()
    line = next(l for l in sheet.splitlines() if l.startswith("- Linked to something people were looking up that day"))
    assert "linked to a trend" in line and "(6), not linked" in line and line.index("linked to a trend") < line.index("not linked")
