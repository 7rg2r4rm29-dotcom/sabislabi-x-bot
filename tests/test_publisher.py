"""Tests de la publication : posts, threads (reprise), réponses, erreurs API X."""
from __future__ import annotations

from datetime import timedelta

import pytest

from bot.approvals import Approvals
from bot.publisher import MAX_ATTEMPTS, Publisher
from bot.utils import iso, now_local, parse_iso
from bot.x_api import XError, intent_reply_url

HOOK = "Most people use AI like a search bar. Here's the one habit that changed my results:"


@pytest.fixture
def app(settings, state, fake_tg, clock):
    state.data["telegram"]["offset"] = 0
    return Approvals(settings, state, fake_tg)


@pytest.fixture
def pub(settings, state, fake_x, app):
    return Publisher(settings, state, fake_x, app)


def approved(state, kind="tip", texts=None, publish_in_min: float = 0, meta=None, **fields):
    now = now_local()
    return state.new_item(kind, list(texts or ["A tip worth trying today."]), status="approved",
                          publish_at=iso(now + timedelta(minutes=publish_in_min)), approved_at=iso(now),
                          meta=dict(meta or {}), **fields)


def labels(buttons) -> list[str]:
    return [b["text"] for row in (buttons or []) for b in row]


# ---------------------------------------------------------------------------
# Cas nominal
# ---------------------------------------------------------------------------
def test_publish_simple_post(pub, state, fake_x, fake_tg, clock):
    item = approved(state, slot_key="tip-1400")
    assert pub.publish_due() == 1
    assert item["status"] == "published"
    assert item["published_ids"] == ["1000"]
    assert [{k: v for k, v in p.items() if k != "created_at"} for p in fake_x.posts] == [
        {"id": "1000", "text": "A tip worth trying today.", "reply_to": None,
         "media_ids": None, "poll": None, "made_with_ai": False}]
    last = state.data["published"][-1]
    assert last["tweet_id"] == "1000" and last["kind"] == "tip" and last["item_id"] == item["id"]
    assert last["slot_key"] == "tip-1400"
    assert state.recent_texts("tip") == ["A tip worth trying today."]
    assert state.count("published_tip") == 1
    # Pas de carte existante : le résultat est envoyé dans une nouvelle carte, avec le lien
    assert "https://x.com/sabislabi/status/1000" in fake_tg.sent[-1]["text"]
    assert item["tg_message_id"] == fake_tg.sent[-1]["id"]
    # Rien n'est republié au tour suivant
    assert pub.publish_due() == 0
    assert len(fake_x.posts) == 1


def test_not_due_and_order(pub, state, fake_x, clock):
    later = approved(state, texts=["second"], publish_in_min=-1)
    first = approved(state, texts=["first"], publish_in_min=-5)
    future = approved(state, texts=["future"], publish_in_min=30)
    assert pub.publish_due() == 2
    assert [p["text"] for p in fake_x.posts] == ["first", "second"]
    assert future["status"] == "approved"
    assert first["status"] == later["status"] == "published"


def test_late_approved_post_is_not_published_hours_later(pub, state, fake_x, fake_tg, clock):
    clock(8, 30)
    gm = approved(state, kind="gm", texts=["gm. ship one small thing today."])
    clock(12, 0)  # bot arrêté de 8 h 30 à midi
    assert pub.publish_due() == 0
    assert fake_x.posts == []
    assert gm["status"] == "expired"
    assert "08:30" in gm["last_error"]
    assert any("n'a pas pu partir à temps" in m["text"] for m in fake_tg.sent)
    assert pub.publish_due() == 0  # une seule notification


def test_stale_limits_per_kind(pub, state, fake_x, clock):
    clock(9, 0)
    reactive = approved(state, kind="reactive", texts=["Big news: a model just shipped."])
    reply = approved(state, kind="comment_reply", texts=["thanks!"], meta={"comment_id": "555"})
    tip = approved(state, texts=["A tip, 3h30 late."])
    clock(12, 30)
    assert pub.publish_due() == 1
    assert reactive["status"] == tip["status"] == "expired"   # 3 h max (comme le délai de la carte)
    assert reply["status"] == "published"                     # une réponse reste utile 12 h


def test_half_published_thread_is_finished_even_late(pub, state, fake_x, clock):
    clock(9, 0)
    th = approved(state, kind="thread", texts=[HOOK, "two", "three"], meta={"partial_ids": ["900"]})
    clock(15, 0)
    assert pub.publish_due() == 1
    assert th["status"] == "published"
    assert th["published_ids"][0] == "900"


# ---------------------------------------------------------------------------
# Réponse de X perdue : jamais de doublon
# ---------------------------------------------------------------------------
def test_lost_response_is_checked_on_profile_not_reposted(pub, state, fake_x, fake_tg, clock):
    clock(14, 0)
    item = approved(state, texts=["A tip worth trying today."])
    fake_x.lose_response_next = 1          # X crée le post mais la réponse se perd
    assert pub.publish_due() == 0
    assert item["status"] == "approved" and item["meta"]["uncertain"] is True
    assert len(fake_x.posts) == 1
    clock(14, 4)
    assert pub.publish_due() == 1
    assert len(fake_x.posts) == 1          # retrouvé sur le profil, pas republié
    assert item["status"] == "published" and item["published_ids"] == [fake_x.posts[0]["id"]]
    assert "uncertain" not in item["meta"]
    assert state.data["published"][-1]["tweet_id"] == fake_x.posts[0]["id"]


def test_lost_response_really_failed_is_posted_once(pub, state, fake_x, clock):
    clock(14, 0)
    item = approved(state, texts=["A tip worth trying today."])
    fake_x.lose_response_next = 1
    pub.publish_due()
    fake_x.posts.clear()                    # en réalité rien n'était parti
    clock(14, 4)
    assert pub.publish_due() == 1
    assert [p["text"] for p in fake_x.posts] == ["A tip worth trying today."]
    assert item["status"] == "published"


def test_duplicate_after_lost_response_is_not_reported_as_failure(pub, state, fake_x, fake_tg, clock):
    clock(14, 0)
    item = approved(state, texts=["A tip worth trying today."])
    fake_x.fail_next = [XError("network", "coupure")]
    pub.publish_due()                       # 1er essai raté (réseau) -> nouvel essai
    clock(14, 11)
    fake_x.fail_next = [XError("duplicate", "duplicate content")]
    fake_x.posts.append({"id": "777", "text": "A tip worth trying today.", "reply_to": None,
                         "media_ids": None, "poll": None, "made_with_ai": False,
                         "created_at": "2026-10-15T12:05:00.000Z"})
    pub.publish_due()
    assert item["status"] == "approved" and item["meta"].get("uncertain")
    assert not any("n'a pas été publiée" in m["text"] for m in fake_tg.sent)
    clock(14, 15)
    assert pub.publish_due() == 1
    assert item["published_ids"] == ["777"]


def test_thread_lost_response_mid_thread_resumes_without_duplicate(pub, state, fake_x, clock):
    clock(15, 0)
    item = approved(state, kind="thread", texts=[HOOK, "two", "three"])
    fake_x.fail_next = []
    real_post = fake_x.post
    calls = {"n": 0}

    def post(text, **kw):
        calls["n"] += 1
        if calls["n"] == 2:
            fake_x.lose_response_next = 1
        return real_post(text, **kw)

    fake_x.post = post
    pub.publish_due()
    assert item["meta"]["partial_ids"] == ["1000"] and item["meta"]["uncertain"]
    clock(15, 4)
    assert pub.publish_due() == 1
    assert [p["text"] for p in fake_x.posts] == [HOOK, "two", "three"]
    assert item["published_ids"] == ["1000", "1001", "1002"]


def test_uncertain_forever_ends_with_check_your_profile(pub, state, fake_x, fake_tg, clock):
    clock(14, 0)
    item = approved(state, texts=["A tip worth trying today."])
    for minute in (0, 4, 8, 12, 16):
        clock(14, minute)
        fake_x.fail_next = [XError("uncertain", "perdu")]
        fake_x.posts.clear()
        pub.publish_due()
    assert item["status"] == "failed"
    assert "profil" in item["last_error"]


def test_awaiting_items_are_never_published(pub, state, fake_x):
    state.new_item("tip", ["waiting for you"], status="awaiting")
    assert pub.publish_due() == 0
    assert fake_x.posts == []


def test_paused(pub, state, fake_x):
    item = approved(state)
    state.data["paused"] = True
    assert pub.publish_due() == 0
    assert item["status"] == "approved" and fake_x.posts == []
    state.data["paused"] = False
    assert pub.publish_due() == 1


def test_dry_run_card(pub, settings, state, fake_tg):
    settings.dry_run = True
    approved(state)
    pub.publish_due()
    assert "DRY_RUN" in fake_tg.sent[-1]["text"]


# ---------------------------------------------------------------------------
# Médias / sondages
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("image_kind, ai", [("screenshot", False), ("ai", True)])
def test_image_upload(pub, state, fake_x, tmp_path, image_kind, ai):
    img = tmp_path / "img.png"
    img.write_bytes(b"\x89PNG fake")
    item = approved(state, image_path=str(img), image_kind=image_kind)
    assert pub.publish_due() == 1
    assert fake_x.calls[0] == ("upload", str(img))
    assert fake_x.posts[0]["media_ids"] == ["1000"]
    assert fake_x.posts[0]["made_with_ai"] is ai
    assert item["published_ids"] == ["1001"]


def test_poll_without_media(pub, state, fake_x, tmp_path):
    img = tmp_path / "img.png"
    img.write_bytes(b"\x89PNG fake")
    poll = {"options": ["Claude", "ChatGPT", "Gemini"], "duration_minutes": 1440}
    approved(state, kind="news", texts=["Which one do you use daily?"], image_path=str(img),
             image_kind="screenshot", poll=poll)
    assert pub.publish_due() == 1
    assert fake_x.uploads == []
    assert fake_x.posts[0]["poll"] == poll
    assert fake_x.posts[0]["media_ids"] is None


def test_missing_or_refused_image_posts_without_it(pub, state, fake_x, tmp_path):
    item = approved(state, image_path=str(tmp_path / "gone.png"), image_kind="screenshot")
    assert pub.publish_due() == 1
    assert fake_x.posts[0]["media_ids"] is None and item["status"] == "published"

    img = tmp_path / "bad.png"
    img.write_bytes(b"x")
    item2 = approved(state, texts=["another tip"], image_path=str(img), image_kind="ai")
    fake_x.fail_next = [XError("invalid", "unsupported media", 400)]
    assert pub.publish_due() == 1
    assert item2["status"] == "published"
    assert fake_x.posts[-1]["media_ids"] is None and fake_x.posts[-1]["made_with_ai"] is False


def test_upload_rate_limited_retries_whole_post(pub, state, fake_x, tmp_path):
    img = tmp_path / "img.png"
    img.write_bytes(b"x")
    item = approved(state, image_path=str(img), image_kind="screenshot")
    fake_x.fail_next = [XError("rate_limited", "too many", 429)]
    assert pub.publish_due() == 0
    assert item["status"] == "approved" and fake_x.posts == []


# ---------------------------------------------------------------------------
# Threads
# ---------------------------------------------------------------------------
def test_thread_published(pub, state, fake_x, tmp_path):
    img = tmp_path / "img.png"
    img.write_bytes(b"x")
    item = approved(state, kind="thread", texts=[HOOK, "Step one.", "Step two."], image_path=str(img),
                    image_kind="screenshot", meta={"angle": "tutorial", "used_notes": []},
                    slot_key="thread-tue-1500")
    assert pub.publish_due() == 1
    assert item["status"] == "published"
    assert item["published_ids"] == ["1001", "1002", "1003"]
    assert [p["reply_to"] for p in fake_x.posts] == [None, "1001", "1002"]
    assert fake_x.posts[0]["media_ids"] == ["1000"] and fake_x.posts[1]["media_ids"] is None
    last = state.data["published"][-1]
    assert last["kind"] == "thread" and last["text"] == HOOK and last["tweet_id"] == "1001"
    assert state.recent_texts("thread") == [HOOK]
    assert state.data["thread_recent_kinds"][-1] == "tutorial"
    assert item["id"] in state.data["thread_kinds_seen"]
    assert state.count("published_thread") == 1


def test_thread_kind_not_double_counted_by_writer(pub, settings, state, fake_llm):
    writer_mod = pytest.importorskip("bot.writer")
    state.data["thread_recent_kinds"] = ["story"] * 6
    approved(state, kind="thread", texts=[HOOK, "Two."], meta={"angle": "beginner"})
    pub.publish_due()
    kinds = writer_mod.Writer(settings, fake_llm, state).recent_thread_kinds()
    assert kinds[-1] == "beginner" and kinds.count("beginner") == 1


def test_thread_resume_from_partial_ids(pub, state, fake_x):
    item = approved(state, kind="thread", texts=[HOOK, "Two.", "Three.", "Four."],
                    meta={"angle": "timely", "partial_ids": ["900", "901"]})
    assert pub.publish_due() == 1
    assert [p["text"] for p in fake_x.posts] == ["Three.", "Four."]
    assert [p["reply_to"] for p in fake_x.posts] == ["901", "1000"]
    assert item["published_ids"] == ["900", "901", "1000", "1001"]
    assert "partial_ids" not in item["meta"]
    assert state.data["published"][-1]["tweet_id"] == "900"


def test_thread_failure_midway_then_resume(pub, state, fake_x, app, fake_tg, clock):
    item = approved(state, kind="thread", texts=[HOOK, "Two.", "Three."], meta={"angle": "practical"})
    real_post = fake_x.post
    calls = {"n": 0}

    def flaky(text, **kw):
        calls["n"] += 1
        if calls["n"] == 2:
            raise XError("server", "Service Unavailable", 503)
        return real_post(text, **kw)

    fake_x.post = flaky
    assert pub.publish_due() == 0
    assert item["status"] == "approved"
    assert item["meta"]["partial_ids"] == ["1000"]
    assert item["attempts"] == 1
    assert parse_iso(item["publish_at"]) == now_local() + timedelta(minutes=10)
    assert "Déjà en ligne : 1/3" in fake_tg.sent[-1]["text"]
    assert "Nouvel essai" in fake_tg.sent[-1]["text"]

    clock(10, 10)
    assert pub.publish_due() == 1
    assert item["status"] == "published"
    assert item["published_ids"] == ["1000", "1001", "1002"]
    assert [p["text"] for p in fake_x.posts] == [HOOK, "Two.", "Three."]
    assert [p["reply_to"] for p in fake_x.posts] == [None, "1000", "1001"]


def test_thread_fatal_midway_keeps_partial(pub, state, fake_x, fake_tg):
    item = approved(state, kind="thread", texts=[HOOK, "Two.", "Three."], meta={"angle": "story"})
    real_post = fake_x.post
    calls = {"n": 0}

    def flaky(text, **kw):
        calls["n"] += 1
        if calls["n"] == 3:
            raise XError("credits", "Payment required", 402)
        return real_post(text, **kw)

    fake_x.post = flaky
    pub.publish_due()
    assert item["status"] == "failed"
    assert item["meta"]["partial_ids"] == ["1000", "1001"]
    notif = fake_tg.sent[-1]["text"]
    assert "console.x.com" in notif and "2/3 tweets" in notif


# ---------------------------------------------------------------------------
# Réponses aux commentaires
# ---------------------------------------------------------------------------
def test_comment_reply_published(pub, state, fake_x):
    item = approved(state, kind="comment_reply", texts=["Glad it helped! Try it on your inbox next."],
                    meta={"comment_id": "555", "commenter": "bob", "post_url": "https://x.com/bob/status/555"})
    assert pub.publish_due() == 1
    assert fake_x.posts[0]["reply_to"] == "555"
    assert item["status"] == "published"
    assert state.count("published_comment_reply") == 1
    # Mes RÉPONSES ne remplissent pas la liste de mes posts (statistiques)
    assert state.data["published"] == []
    assert state.recent_texts("comment_reply") == ["Glad it helped! Try it on your inbox next."]


def test_comment_reply_text_not_remembered_twice(pub, state):
    text = "Same reply text"
    state.remember_text("comment_reply", text)  # déjà noté par CommentDesk au brouillon
    approved(state, kind="comment_reply", texts=[text], meta={"comment_id": "1"})
    pub.publish_due()
    assert state.recent_texts("comment_reply") == [text]


def test_not_summoned_offers_manual_reply(pub, state, fake_x, fake_tg):
    text = "Glad it helped!"
    item = approved(state, kind="comment_reply", texts=[text],
                    meta={"comment_id": "555", "commenter": "bob", "post_url": "https://x.com/bob/status/555"})
    fake_x.fail_next = [XError("not_summoned", "You are not allowed to reply", 403)]
    assert pub.publish_due() == 0
    assert item["status"] == "failed"
    cards = [s for s in fake_tg.sent if s["buttons"]]
    card = cards[-1]
    assert "❌ Échec" in card["text"]
    btn = [b for row in card["buttons"] for b in row if b["text"].startswith("📲 Répondre moi-même")][0]
    assert btn["url"] == intent_reply_url("555", text)
    assert "🔗 Voir" in labels(card["buttons"])
    notif = fake_tg.sent[-1]
    assert notif["silent"] is True and "@bob" in notif["text"]


def test_comment_without_id_fails(pub, state, fake_x):
    item = approved(state, kind="comment_reply", texts=["hi"], meta={})
    pub.publish_due()
    assert item["status"] == "failed" and fake_x.posts == []


# ---------------------------------------------------------------------------
# Erreurs API X
# ---------------------------------------------------------------------------
def test_credits_fail_immediately_and_notify(pub, state, fake_x, fake_tg):
    item = approved(state)
    fake_x.fail_next = [XError("credits", "Your account has no credits", 402)]
    assert pub.publish_due() == 0
    assert item["status"] == "failed"
    assert item["attempts"] == 0
    assert "console.x.com" in item["last_error"]
    assert "Plus de crédits API X — recharge sur console.x.com" in fake_tg.sent[-1]["text"]
    assert any("❌ Échec" in s["text"] for s in fake_tg.sent)
    # Pas de nouvel essai
    assert pub.publish_due() == 0
    assert len(fake_x.calls) == 1


@pytest.mark.parametrize("kind", ["auth", "budget", "forbidden", "invalid", "duplicate"])
def test_other_fatal_errors(pub, state, fake_x, fake_tg, kind):
    item = approved(state)
    fake_x.fail_next = [XError(kind, "nope", 403)]
    pub.publish_due()
    assert item["status"] == "failed"
    assert fake_tg.sent[-1]["text"].startswith("❌ L'astuce IA n'a pas été publiée")


def test_rate_limited_with_reset(pub, state, fake_x, fake_tg, clock):
    item = approved(state)
    reset = now_local() + timedelta(minutes=7)
    fake_x.fail_next = [XError("rate_limited", "Too Many Requests", 429, reset_at=reset.timestamp())]
    assert pub.publish_due() == 0
    assert item["status"] == "approved"
    assert item["attempts"] == 0
    when = parse_iso(item["publish_at"])
    assert reset <= when <= reset + timedelta(minutes=1)
    assert item["meta"]["retry_at"] == item["publish_at"]
    assert any("Nouvel essai à 10:07" in m["text"] for m in fake_tg.sent)
    assert "X limite le nombre de publications" in fake_tg.sent[-1]["text"]
    assert pub.publish_due() == 0  # pas avant l'heure
    clock(10, 8)
    assert pub.publish_due() == 1
    assert item["status"] == "published" and item["last_error"] == ""
    assert "retry_at" not in item["meta"]


def test_rate_limit_for_hours_does_not_publish_a_stale_gm(pub, state, fake_x, fake_tg, clock):
    clock(8, 30)
    gm = approved(state, kind="gm", texts=["gm. one small thing today."])
    reset = now_local() + timedelta(hours=20)
    fake_x.fail_next = [XError("rate_limited", "Too Many Requests", 429, reset_at=reset.timestamp())]
    pub.publish_due()
    clock(4, 31, day=16)
    assert pub.publish_due() == 0
    assert fake_x.posts == [] and gm["status"] == "expired"
    assert "08:30" in gm["last_error"]


def test_rate_limited_without_reset(pub, state, fake_x):
    item = approved(state)
    fake_x.fail_next = [XError("rate_limited", "Too Many Requests", 429)]
    pub.publish_due()
    assert item["status"] == "approved"
    assert parse_iso(item["publish_at"]) == now_local() + timedelta(minutes=15)


def test_transient_errors_retry_then_fail(pub, state, fake_x, fake_tg, clock):
    item = approved(state)
    fake_x.fail_next = [XError("server", "503", 503), XError("network", "down"), XError("not_found", "gone", 404)]
    assert pub.publish_due() == 0
    assert item["status"] == "approved" and item["attempts"] == 1
    assert parse_iso(item["publish_at"]) == now_local() + timedelta(minutes=10)
    notifications_before = len([s for s in fake_tg.sent if not s["buttons"]])
    clock(10, 5)
    assert pub.publish_due() == 0 and item["attempts"] == 1  # pas encore l'heure
    clock(10, 10)
    pub.publish_due()
    assert item["status"] == "approved" and item["attempts"] == 2
    clock(10, 20)
    pub.publish_due()
    assert item["status"] == "failed" and item["attempts"] == MAX_ATTEMPTS
    assert f"après {MAX_ATTEMPTS} essais" in item["last_error"]
    notifications = [s for s in fake_tg.sent if not s["buttons"]]
    assert len(notifications) == notifications_before + 1
    assert "n'a pas été publié" in notifications[-1]["text"]
    assert fake_x.posts == []


def test_transient_then_success(pub, state, fake_x, clock):
    item = approved(state)
    fake_x.fail_next = [XError("network", "down")]
    pub.publish_due()
    clock(10, 10)
    assert pub.publish_due() == 1
    assert item["status"] == "published" and item["attempts"] == 1


def test_unexpected_exception_never_escapes(pub, state, fake_x):
    item = approved(state)

    def broken(*a, **k):
        raise RuntimeError("bug")

    fake_x.post = broken
    assert pub.publish_due() == 0
    assert item["status"] == "approved" and item["attempts"] == 1


# ---------------------------------------------------------------------------
# Divers
# ---------------------------------------------------------------------------
def test_notes_marked_used(pub, state):
    state.add_note("I cut my weekly reporting from 3h to 40 min with Claude")
    approved(state, meta={"used_notes": ["I cut my weekly reporting from 3h to 40 min with Claude"]})
    pub.publish_due()
    assert state.data["notes"][0]["uses"] == 1


def test_radar_never_published(pub, state, fake_x):
    item = state.new_item("radar", ["suggested reply"], status="approved", meta={"tweet_id": "1"})
    assert pub.publish_due() == 0
    assert pub.publish_item(item) is False
    assert fake_x.calls == []
    assert item["status"] == "approved"


def test_publish_item_requires_approved(pub, state, fake_x):
    item = state.new_item("tip", ["x"], status="awaiting")
    assert pub.publish_item(item) is False
    assert fake_x.calls == []


def test_empty_texts_fail(pub, state, fake_x):
    item = approved(state, texts=["   "])
    pub.publish_due()
    assert item["status"] == "failed" and fake_x.calls == []


def test_stuck_publishing_is_reported_not_republished(pub, state, fake_x, fake_tg, clock):
    item = approved(state)
    state.update_item(item["id"], status="publishing", meta={"publishing_at": iso(now_local())})
    clock(10, 5)
    pub.publish_due()
    assert item["status"] == "publishing"
    clock(10, 30)
    pub.publish_due()
    assert item["status"] == "failed"
    assert "interrompue" in item["last_error"]
    assert fake_x.calls == []
    assert "interrompue" in fake_tg.sent[-1]["text"]


def test_full_flow_through_telegram(settings, state, fake_tg, fake_x, clock):
    """Carte -> ✅ -> publication dans le même tour, carte mise à jour avec le lien."""
    state.data["telegram"]["offset"] = 0
    app = Approvals(settings, state, fake_tg)
    pub = Publisher(settings, state, fake_x, app)
    from bot.models import Draft
    clock(13, 0)
    item = app.create_post_item(Draft(kind="tip", texts=["One prompt to try tonight."], reason="r"),
                                slot_key="tip-1300", slot_date="2026-10-15", publish_at=now_local(),
                                expires_at=now_local() + timedelta(hours=2))
    fake_tg.push_callback(f"ok:{item['id']}", message_id=item["tg_message_id"])
    app.poll()
    assert pub.publish_due() == 1
    assert fake_tg.edits[-1]["id"] == item["tg_message_id"]
    assert "https://x.com/sabislabi/status/1000" in fake_tg.edits[-1]["text"]



def test_poll_is_counted_when_published(pub, state, fake_x, clock):
    item = approved(state, texts=["quick one: where do you draft your emails?"],
                    poll={"options": ["Claude", "ChatGPT"], "duration_minutes": 1440})
    assert pub.publish_due() == 1
    assert fake_x.posts[0]["poll"]["options"] == ["Claude", "ChatGPT"]
    assert len(state.data["poll_dates"]) == 1 and item["status"] == "published"



def test_uncertain_comment_reply_found_despite_leading_mention(pub, state, fake_x, clock):
    """Sur X, le texte d'une réponse commence par « @pseudo » : la réponse déjà en
    ligne doit quand même être retrouvée, pas renvoyée."""
    clock(14, 0)
    item = approved(state, kind="comment_reply", texts=["Glad it helped!"], meta={"comment_id": "555"})
    fake_x.lose_response_next = 1
    pub.publish_due()
    fake_x.posts[0]["text"] = "@alice Glad it helped!"          # forme réelle renvoyée par X
    clock(14, 4)
    assert pub.publish_due() == 1
    assert len(fake_x.posts) == 1 and item["published_ids"] == [fake_x.posts[0]["id"]]


def test_edit_prompt_opened_before_publication_is_dropped(pub, app, state, fake_x, fake_tg, clock):
    clock(14, 0)
    item = approved(state, kind="thread", texts=[HOOK, "two", "three"], publish_in_min=0)
    app._waiting()["999"] = item["id"]                 # invite ✏️ ouverte avant 14:00
    fake_x.lose_response_next = 0
    real_post = fake_x.post
    calls = {"n": 0}

    def post(text, **kw):
        calls["n"] += 1
        if calls["n"] == 2:
            fake_x.lose_response_next = 1
        return real_post(text, **kw)

    fake_x.post = post
    pub.publish_due()                                  # tweet 1 en ligne, tweet 2 incertain
    assert "999" not in app._waiting()
    fake_tg.push_message("totally new version\n---\nnew two", reply_to=999)
    app.poll()
    assert item["texts"] == [HOOK, "two", "three"]     # rien n'a changé
    assert any("plus modifiable" in m["text"] or "pas compris" in m["text"] for m in fake_tg.sent)
