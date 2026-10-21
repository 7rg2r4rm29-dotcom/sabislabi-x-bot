"""Tests d'Analytics (bot/analytics.py) : aucun appel réseau."""
from __future__ import annotations

from datetime import timedelta

import pytest

from bot import analytics as amod
from bot.analytics import Analytics
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
    clock(12, 0)
    assert an.maybe_refresh() == 0                      # 120 min < 170
    assert len([c for c in fake_x.calls if c == ("my_recent_posts",)]) == 1
    clock(12, 51)
    assert an.maybe_refresh() == 1
    assert len([c for c in fake_x.calls if c == ("my_recent_posts",)]) == 2


def test_refresh_remembers_format_from_item(an, state, fake_x):
    it = state.new_item("tip", ["t"], image_path="/tmp/a.png", image_kind="screenshot",
                        meta={"used_notes": ["I tried X"], "angle": ""})
    th = state.new_item("thread", ["a", "b", "c"], meta={"angle": "tutorial"})
    pub(state, "1", "tip", "tip", item_id=it["id"])
    pub(state, "2", "thread", "thread", item_id=th["id"])
    fake_x.recent_posts = [api_tweet("1", 10), api_tweet("2", 20)]
    an.refresh_metrics()
    f1, f2 = state.data["published"][0]["fmt"], state.data["published"][1]["fmt"]
    assert f1["media"] == "image" and f1["notes"] is True and f1["image_kind"] == "screenshot"
    assert f2["genre"] == "tutorial" and f2["tweets"] == 3 and f2["media"] == "text"


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
    assert "13:30 astuce — 📝 pas encore rédigé" in txt
    assert "thread" not in txt                      # jeudi : pas de thread prévu
    assert "1 brouillon(s) attendent ta validation" in txt
    assert "1 suggestion(s) radar ouvertes" in txt
    assert "4 brouillon(s)" in txt
    assert "API X" in txt
    assert "Claude hier : 0.42 $ (37 appel(s))" in txt


def test_daily_report_shows_thread_on_thread_day(an, clock):
    clock(8, 30, day=20)                            # mardi
    txt = an.daily_report()
    assert "Récap du mardi 20/10" in txt
    assert "15:00 thread" in txt


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
    assert "<pre>" in txt and "astuce" in txt and "thread" in txt
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
