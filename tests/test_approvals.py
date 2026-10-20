"""Tests de la validation Telegram (cartes, boutons, modification, commandes)."""
from __future__ import annotations

from datetime import time as dtime, timedelta

import pytest

from bot.approvals import MAX_REGEN, Approvals, check_human_text, split_thread
from bot.models import Draft, NewsItem
from bot.publisher import Publisher
from bot.state import StateStore
from bot.utils import iso, now_local, parse_iso
from bot.x_api import intent_reply_url
from tests.fakes import callback_data_for

TIP = "Ask Claude to explain its answer like you're 12. You'll spot the gaps fast."


@pytest.fixture
def app(settings, state, fake_tg, clock):
    # Offset déjà initialisé : le premier poll() traite les messages (voir test_backlog).
    state.data["telegram"]["offset"] = 0
    return Approvals(settings, state, fake_tg, hooks={})


def labels(buttons) -> list[str]:
    return [b["text"] for row in (buttons or []) for b in row]


def url_of(buttons, label_start: str) -> str:
    for row in buttons or []:
        for b in row:
            if b["text"].startswith(label_start) and "url" in b:
                return b["url"]
    raise AssertionError(f"bouton URL {label_start!r} introuvable")


def at(h: int, m: int = 0, day: int = 15):
    return now_local().replace(day=day, hour=h, minute=m, second=0, microsecond=0)


def make_post(app, kind="tip", texts=None, publish=(14, 0), expires=(16, 0), **draft_kw):
    d = Draft(kind=kind, texts=list(texts or [TIP]), reason=draft_kw.pop("reason", "Une astuce concrète"),
              **draft_kw)
    return app.create_post_item(d, slot_key=f"{kind}-1400", slot_date="2026-10-15",
                                publish_at=at(*publish), expires_at=at(*expires))


def click(fake_tg, app, data: str, item=None) -> int:
    fake_tg.push_callback(data, message_id=(item or {}).get("tg_message_id"))
    return app.poll()


# ---------------------------------------------------------------------------
# Création des cartes
# ---------------------------------------------------------------------------
def test_create_post_item_card(app, state, fake_tg, clock):
    clock(11, 30)
    src = NewsItem(id="n1", title="Claude gets project memory", summary="", url="https://anthropic.com/news/x",
                   source="Anthropic", published=None)
    item = make_post(app, sources=[src], used_notes=["I automated my invoices"], angle="")
    assert item["status"] == "awaiting"
    assert item["kind"] == "tip"
    assert item["slot_key"] == "tip-1400" and item["slot_date"] == "2026-10-15"
    assert item["publish_at"] == iso(at(14)) and item["expires_at"] == iso(at(16))
    assert item["meta"]["reason"] == "Une astuce concrète"
    assert item["meta"]["sources"] == [src.brief()]
    assert item["meta"]["used_notes"] == ["I automated my invoices"]
    assert item["meta"]["from_fallback"] is False
    assert item["tg_message_id"] == fake_tg.sent[-1]["id"]
    assert state.item(item["id"])["tg_message_id"] == fake_tg.sent[-1]["id"]

    card = fake_tg.sent[-1]["text"]
    assert "💡 <b>Astuce IA</b>" in card
    assert "prévu à 14:00" in card
    assert "Ask Claude to explain its answer like you're 12" in card
    assert "Pourquoi : Une astuce concrète" in card
    assert "Claude gets project memory (Anthropic)" in card
    assert "anthropic.com" not in card  # les sources sont citées sans lien
    assert "/280" in card
    assert "À valider (avant 16:00)" in card
    assert labels(fake_tg.sent[-1]["buttons"]) == [
        "✅ Valider", "✏️ Modifier", "🔄 Autre version", "❌ Annuler", "📲 Publier moi-même"]
    assert url_of(fake_tg.sent[-1]["buttons"], "📲").startswith("https://x.com/intent/post?text=Ask%20Claude")
    assert callback_data_for(fake_tg, "✅") == f"ok:{item['id']}"
    for b in fake_tg.buttons_of():
        if "callback_data" in b:
            assert len(b["callback_data"].encode()) <= 64


def test_card_labels_thread_poll_image_fallback(app, fake_tg, tmp_path, clock):
    clock(9, 0)
    th = make_post(app, kind="thread", texts=["Hook tweet", "Second tweet", "Third tweet"], angle="tutorial")
    card = fake_tg.sent[-1]["text"]
    assert "🧵 <b>Thread (tuto)</b>" in card
    assert "<b>1/</b> Hook tweet" in card and "<b>3/</b> Third tweet" in card
    assert "📲 Publier moi-même" not in labels(fake_tg.sent[-1]["buttons"])
    assert th["kind"] == "thread"

    make_post(app, kind="news", texts=["Which one do you use?"],
              poll={"options": ["Claude", "ChatGPT"], "duration_minutes": 1440})
    card = fake_tg.sent[-1]["text"]
    assert "📰 <b>Actu IA décryptée</b>" in card
    assert "📊 Sondage (24 h) : Claude · ChatGPT" in card
    assert "📲 Publier moi-même" not in labels(fake_tg.sent[-1]["buttons"])

    img = tmp_path / "shot.png"
    img.write_bytes(b"\x89PNG fake")
    d = Draft(kind="gm", texts=["gm. one small thing to try today"], reason="gm")
    item = app.create_post_item(d, slot_key="gm-0830", slot_date="2026-10-16", publish_at=at(8, 30, day=16),
                                expires_at=at(10, 30, day=16), image_path=str(img), image_kind="screenshot")
    assert fake_tg.photos and fake_tg.photos[-1]["path"] == str(img)
    assert fake_tg.photos[-1]["id"] < item["tg_message_id"]  # la photo arrive avant la carte
    card = fake_tg.sent[-1]["text"]
    assert "☀️ <b>GM</b>" in card and "prévu demain à 08:30" in card
    assert "Capture d'écran jointe" in card
    assert "📲 Publier moi-même" not in labels(fake_tg.sent[-1]["buttons"])
    assert item["image_path"] == str(img) and item["image_kind"] == "screenshot"

    make_post(app, kind="reactive", texts=["fallback text here"], from_fallback=True)
    card = fake_tg.sent[-1]["text"]
    assert "⚡ <b>Post réactif</b>" in card
    assert "🧯 Texte de secours" in card


def test_late_draft_says_publish_now(app, fake_tg, clock):
    clock(14, 20)
    make_post(app)
    assert "L'heure prévue est passée" in fake_tg.sent[-1]["text"]


def test_long_thread_card_fits_telegram(app, fake_tg, clock):
    texts = [("word " * 54).strip() for _ in range(20)]
    make_post(app, kind="thread", texts=texts, angle="timely")
    assert len(fake_tg.sent[-1]["text"]) <= 3900


# ---------------------------------------------------------------------------
# Boutons
# ---------------------------------------------------------------------------
def test_ok_before_time_schedules_then_publisher_publishes(app, settings, state, fake_tg, fake_x, clock):
    clock(11, 30)
    item = make_post(app)
    assert click(fake_tg, app, f"ok:{item['id']}", item) == 1
    assert item["status"] == "approved"
    assert item["approved_at"] == iso(now_local())
    assert item["publish_at"] == iso(at(14))
    edit = fake_tg.edits[-1]
    assert edit["id"] == item["tg_message_id"]
    assert "✅ Validé — publication à 14:00" in edit["text"]
    assert labels(edit["buttons"]) == ["✏️ Modifier", "❌ Annuler"]
    assert fake_tg.answers[-1][1].startswith("✅ Validé")

    pub = Publisher(settings, state, fake_x, app)
    assert pub.publish_due() == 0
    assert fake_x.posts == []
    clock(14, 0)
    assert pub.publish_due() == 1
    assert item["status"] == "published"
    assert fake_x.posts[0]["text"] == TIP
    edit = fake_tg.edits[-1]
    assert edit["id"] == item["tg_message_id"]
    assert "✅ Publié" in edit["text"]
    assert "https://x.com/sabislabi/status/1000" in edit["text"]
    assert labels(edit["buttons"]) == ["🔗 Voir sur X"]


def test_ok_after_time_is_immediate(app, settings, state, fake_tg, fake_x, clock):
    clock(11, 30)
    item = make_post(app)
    clock(14, 10)
    click(fake_tg, app, f"ok:{item['id']}", item)
    assert item["status"] == "approved"
    assert item["publish_at"] == iso(now_local())
    assert "✅ Validé — publication immédiate" in fake_tg.edits[-1]["text"]
    assert Publisher(settings, state, fake_x, app).publish_due() == 1


def test_no_rejects_and_second_click_is_ignored(app, fake_tg, clock):
    item = make_post(app)
    click(fake_tg, app, f"no:{item['id']}", item)
    assert item["status"] == "rejected"
    edit = fake_tg.edits[-1]
    assert "❌ Annulé — rien n'a été publié" in edit["text"]
    assert edit["buttons"] == []
    assert fake_tg.answers[-1][1] == "Annulé"
    click(fake_tg, app, f"ok:{item['id']}", item)
    assert item["status"] == "rejected"
    assert fake_tg.answers[-1][1] == "Déjà traité"


def test_cancel_after_approval(app, fake_tg, clock):
    clock(11, 0)
    item = make_post(app)
    click(fake_tg, app, f"ok:{item['id']}", item)
    click(fake_tg, app, f"no:{item['id']}", item)
    assert item["status"] == "rejected"


def test_unknown_item_and_action(app, fake_tg):
    fake_tg.push_callback("ok:deadbeef")
    app.poll()
    assert "Introuvable" in fake_tg.answers[-1][1]
    item = make_post(app)
    fake_tg.push_callback(f"zap:{item['id']}")
    app.poll()
    assert fake_tg.answers[-1][1] == "Action inconnue"
    assert item["status"] == "awaiting"


def test_ok_while_paused_says_so(app, state, fake_tg):
    item = make_post(app)
    state.data["paused"] = True
    click(fake_tg, app, f"ok:{item['id']}", item)
    assert item["status"] == "approved"
    assert "pause" in fake_tg.answers[-1][1]
    assert "Bot en pause" in fake_tg.edits[-1]["text"]


# ---------------------------------------------------------------------------
# Nouvelle version
# ---------------------------------------------------------------------------
def test_regen_replaces_texts_and_is_limited(app, fake_tg, clock):
    item = make_post(app)
    calls = []

    def regen(it):
        calls.append(it["id"])
        n = len(calls)
        return Draft(kind="tip", texts=[f"Version {n}: ask Claude for the strongest counter-argument first."],
                     reason=f"raison v{n}", poll={"options": ["A", "B"], "duration_minutes": 60} if n == 2 else None)

    app.hooks["regenerate"] = regen
    for n in range(1, MAX_REGEN + 1):
        click(fake_tg, app, f"regen:{item['id']}", item)
        assert item["texts"] == [f"Version {n}: ask Claude for the strongest counter-argument first."]
        assert item["meta"]["reason"] == f"raison v{n}"
        assert item["regen_count"] == n
        assert f"Version {n}:" in fake_tg.edits[-1]["text"]
    assert item["poll"] is None  # le sondage de la v2 a disparu avec la v3
    assert fake_tg.answers[-1][1].startswith("🔄")
    assert "🔄 Autre version" not in labels(fake_tg.edits[-1]["buttons"])
    assert "✅ Valider" in labels(fake_tg.edits[-1]["buttons"])
    click(fake_tg, app, f"regen:{item['id']}", item)
    assert len(calls) == MAX_REGEN
    assert "Maximum 3" in fake_tg.answers[-1][1]
    assert item["status"] == "awaiting"


def test_regen_failure_keeps_text(app, fake_tg):
    item = make_post(app)
    app.hooks["regenerate"] = lambda it: None
    click(fake_tg, app, f"regen:{item['id']}", item)
    assert item["texts"] == [TIP]
    assert "Pas de nouvelle version" in fake_tg.edits[-1]["text"]
    assert item["regen_count"] == 1

    def boom(it):
        raise RuntimeError("claude down")

    app.hooks["regenerate"] = boom
    click(fake_tg, app, f"regen:{item['id']}", item)
    assert item["texts"] == [TIP] and item["status"] == "awaiting"


def test_regen_without_hook(app, fake_tg):
    item = make_post(app)
    click(fake_tg, app, f"regen:{item['id']}", item)
    assert "indisponible" in fake_tg.answers[-1][1]
    assert item["regen_count"] == 0


# ---------------------------------------------------------------------------
# Modification
# ---------------------------------------------------------------------------
def test_edit_flow(app, state, fake_tg):
    item = make_post(app)
    click(fake_tg, app, f"edit:{item['id']}", item)
    prompt = fake_tg.sent[-1]
    assert prompt["force_reply"] is True
    assert "Réponds à CE message" in prompt["text"]
    assert prompt["reply_to"] == item["tg_message_id"]
    assert "<code>" in prompt["text"]
    assert state.data["telegram"]["edit_waiting"] == {str(prompt["id"]): item["id"]}

    new = "My own version: test one prompt, then tweak a single word."
    fake_tg.push_message(new, reply_to=prompt["id"])
    assert app.poll() == 1
    assert item["texts"] == [new]
    assert item["status"] == "awaiting"
    assert item["meta"]["edited"] is True
    assert state.data["telegram"]["edit_waiting"] == {}
    assert "✏️ Modifié par toi" in fake_tg.edits[-1]["text"]
    assert new in fake_tg.edits[-1]["text"]
    assert "Texte mis à jour" in fake_tg.sent[-1]["text"]
    # Le lien « Publier moi-même » suit le nouveau texte
    assert "My%20own%20version" in url_of(fake_tg.edits[-1]["buttons"], "📲")


def test_edit_thread_split(app, fake_tg):
    item = make_post(app, kind="thread", texts=["Hook", "Two", "Three"], angle="practical")
    click(fake_tg, app, f"edit:{item['id']}", item)
    prompt = fake_tg.sent[-1]
    assert "---" in prompt["text"]
    fake_tg.push_message("New hook line\nsecond line of hook\n---\nNew two\n\n—-\nNew three\n---\n",
                         reply_to=prompt["id"])
    app.poll()
    assert item["texts"] == ["New hook line\nsecond line of hook", "New two", "New three"]
    assert "(3 tweets)" in fake_tg.sent[-1]["text"]


def test_edit_url_rejected_then_fixed(app, state, fake_tg):
    item = make_post(app)
    click(fake_tg, app, f"edit:{item['id']}", item)
    prompt = fake_tg.sent[-1]
    fake_tg.push_message("Try it today on claude.ai, it's free", reply_to=prompt["id"])
    app.poll()
    assert item["texts"] == [TIP]
    assert "⛔ Texte refusé" in fake_tg.sent[-1]["text"]
    assert "lien" in fake_tg.sent[-1]["text"]
    assert str(prompt["id"]) in state.data["telegram"]["edit_waiting"]
    # Nouvelle réponse au même message ✏️ : acceptée
    fake_tg.push_message("Try it today, it's free.", reply_to=prompt["id"])
    app.poll()
    assert item["texts"] == ["Try it today, it's free."]


def test_edit_thread_one_bad_tweet_blocks_all(app, fake_tg):
    item = make_post(app, kind="thread", texts=["Hook", "Two"], angle="timely")
    click(fake_tg, app, f"edit:{item['id']}", item)
    prompt = fake_tg.sent[-1]
    fake_tg.push_message("Good hook\n---\n" + "x" * 300, reply_to=prompt["id"])
    app.poll()
    assert item["texts"] == ["Hook", "Two"]
    assert "tweet 2" in fake_tg.sent[-1]["text"] and "trop long" in fake_tg.sent[-1]["text"]


def test_edit_off_limits_is_warned_but_accepted(app, fake_tg):
    item = make_post(app)
    click(fake_tg, app, f"edit:{item['id']}", item)
    prompt = fake_tg.sent[-1]
    fake_tg.push_message("Stop gambling your time on random prompts.", reply_to=prompt["id"])
    app.poll()
    assert item["texts"] == ["Stop gambling your time on random prompts."]
    assert item["meta"]["warnings"] and "hors ligne" in item["meta"]["warnings"][0]
    assert "⚠️" in fake_tg.sent[-1]["text"]
    assert "⚠️ hors ligne" in fake_tg.edits[-1]["text"]


def test_edit_too_late(app, state, fake_tg):
    item = make_post(app)
    click(fake_tg, app, f"edit:{item['id']}", item)
    prompt = fake_tg.sent[-1]
    state.update_item(item["id"], status="published")
    fake_tg.push_message("New text", reply_to=prompt["id"])
    app.poll()
    assert item["texts"] == [TIP]
    assert "Trop tard" in fake_tg.sent[-1]["text"]
    assert state.data["telegram"]["edit_waiting"] == {}


def test_edit_approved_scheduled_post_keeps_approval(app, fake_tg, clock):
    clock(11, 0)
    item = make_post(app)
    click(fake_tg, app, f"ok:{item['id']}", item)
    click(fake_tg, app, f"edit:{item['id']}", item)
    fake_tg.push_message("Edited after approval.", reply_to=fake_tg.sent[-1]["id"])
    app.poll()
    assert item["status"] == "approved" and item["texts"] == ["Edited after approval."]
    assert "partira à 14:00" in fake_tg.sent[-1]["text"]


def test_helpers_split_and_check():
    assert split_thread("a\n---\nb\n - - - \nc") == ["a", "b", "c"]
    assert split_thread("---\n\n---") == []
    assert check_human_text("visit https://foo.com")[0]
    assert check_human_text('{"text": "hi"}')[0]
    assert check_human_text("a" * 281)[0].startswith("trop long")
    block, warn = check_human_text("Nice wine tasting with AI")
    assert block is None and warn
    assert check_human_text("A clean sentence.") == (None, None)


# ---------------------------------------------------------------------------
# Commandes
# ---------------------------------------------------------------------------
def test_commands(app, state, fake_tg):
    app.hooks.update(status=lambda: "STATUS-OK", plan=lambda: "PLAN-OK", budget=lambda: "BUDGET-OK")
    for text, expected in [("/aide", "Commandes"), ("aide", "Commandes"), ("/start", "Commandes"),
                           ("/statut", "STATUS-OK"), ("Statut", "STATUS-OK"), ("/plan", "PLAN-OK"),
                           ("/budget@sabis_bot", "BUDGET-OK"), ("BUDGET", "BUDGET-OK")]:
        fake_tg.push_message(text)
        assert app.poll() == 1
        assert expected in fake_tg.sent[-1]["text"], text

    fake_tg.push_message("/pause")
    app.poll()
    assert state.data["paused"] is True and "Pause" in fake_tg.sent[-1]["text"]
    fake_tg.push_message("Reprendre")
    app.poll()
    assert state.data["paused"] is False and "reparti" in fake_tg.sent[-1]["text"]
    fake_tg.push_message("PAUSE")
    app.poll()
    assert state.data["paused"] is True
    fake_tg.push_message("/reprendre")
    app.poll()
    assert state.data["paused"] is False
    # Sans « / », un mot-commande suivi d'autre chose n'est pas une commande
    fake_tg.push_message("pause café")
    app.poll()
    assert state.data["paused"] is False and "pas compris" in fake_tg.sent[-1]["text"]

    fake_tg.push_message("/note I tested Claude on 200 support emails")
    app.poll()
    assert state.data["notes"][-1]["text"] == "I tested Claude on 200 support emails"
    assert "Noté" in fake_tg.sent[-1]["text"]
    fake_tg.push_message("note: built my landing page in one evening")
    app.poll()
    assert state.data["notes"][-1]["text"] == "built my landing page in one evening"
    n = len(state.data["notes"])
    fake_tg.push_message("/note")
    app.poll()
    assert len(state.data["notes"]) == n and "Usage" in fake_tg.sent[-1]["text"]

    fake_tg.push_message("hello there")
    app.poll()
    assert "pas compris" in fake_tg.sent[-1]["text"] and "/note" in fake_tg.sent[-1]["text"]


def test_commands_without_hooks_and_failing_hook(app, state, fake_tg):
    make_post(app)
    fake_tg.push_message("/statut")
    app.poll()
    assert "Bot ▶️ actif" in fake_tg.sent[-1]["text"] and "1 brouillon(s) à valider" in fake_tg.sent[-1]["text"]
    fake_tg.push_message("/plan")
    app.poll()
    assert "indisponible" in fake_tg.sent[-1]["text"]

    def boom():
        raise ValueError("x")

    app.hooks["budget"] = boom
    fake_tg.push_message("/budget")
    app.poll()
    assert "erreur" in fake_tg.sent[-1]["text"]


def test_resume_mentions_late_posts(app, state, fake_tg, clock):
    clock(11, 0)
    item = make_post(app)
    click(fake_tg, app, f"ok:{item['id']}", item)
    state.data["paused"] = True
    clock(15, 0)
    fake_tg.push_message("/reprendre")
    app.poll()
    assert "1 post(s) validé(s) en retard partiront à 15:03" in fake_tg.sent[-1]["text"]
    # Le temps d'annuler : il ne part pas tout de suite
    assert item["status"] == "approved"
    assert parse_iso(item["publish_at"]) == at(15, 3)


def test_resume_drops_posts_that_waited_too_long(app, state, fake_tg, clock):
    clock(11, 0)
    item = make_post(app)
    click(fake_tg, app, f"ok:{item['id']}", item)
    state.data["paused"] = True
    clock(18, 0)  # prévu à 14:00 : 4 h de retard
    fake_tg.push_message("/reprendre")
    app.poll()
    assert item["status"] == "expired"
    assert "trop ancien" in fake_tg.sent[-1]["text"]
    assert "partiront à" not in fake_tg.sent[-1]["text"]


# ---------------------------------------------------------------------------
# Sécurité / offset
# ---------------------------------------------------------------------------
def test_non_owner_chat_ignored(app, state, fake_tg):
    item = make_post(app)
    sent_before = len(fake_tg.sent)
    fake_tg.push_message("/pause", chat_id="999")
    fake_tg.pending_updates.append({
        "update_id": 10_000_000,
        "callback_query": {"id": "evil", "data": f"ok:{item['id']}",
                           "message": {"message_id": item["tg_message_id"], "chat": {"id": "999"}}},
    })
    assert app.poll() == 0
    assert state.data["paused"] is False
    assert item["status"] == "awaiting"
    assert len(fake_tg.sent) == sent_before
    assert ("evil", "") in fake_tg.answers


def test_backlog_skipped_on_first_poll(settings, state, fake_tg, clock):
    assert state.data["telegram"]["offset"] is None
    app = Approvals(settings, state, fake_tg)
    item = make_post(app)
    fake_tg.push_callback(f"ok:{item['id']}")
    fake_tg.push_message("/pause")
    last_id = fake_tg.pending_updates[-1]["update_id"]
    assert app.poll() == 0
    assert item["status"] == "awaiting"
    assert state.data["paused"] is False
    assert state.data["telegram"]["offset"] == last_id + 1
    # Ensuite, tout est traité normalement, et l'offset est persisté sur disque
    fake_tg.push_callback(f"ok:{item['id']}")
    uid = fake_tg.pending_updates[-1]["update_id"]
    assert app.poll() == 1
    assert item["status"] == "approved"
    assert StateStore(state.path).data["telegram"]["offset"] == uid + 1


def test_first_poll_without_backlog_initialises_offset(settings, state, fake_tg):
    app = Approvals(settings, state, fake_tg)
    assert app.poll() == 0
    assert state.data["telegram"]["offset"] == 0
    fake_tg.push_message("/aide")
    assert app.poll() == 1


def test_missing_card_is_resent_with_backoff(app, fake_tg, monkeypatch, clock):
    real_send = fake_tg.send
    attempts = []

    def down(*a, **k):
        attempts.append(1)
        return None

    monkeypatch.setattr(fake_tg, "send", down)
    item = make_post(app)
    assert item["tg_message_id"] is None
    app.poll()
    app.poll()  # pas de nouvel essai avant 5 min
    assert len(attempts) == 2
    monkeypatch.setattr(fake_tg, "send", real_send)
    clock(10, 6)
    app.poll()
    assert item["tg_message_id"] == fake_tg.sent[-1]["id"]
    assert "Astuce IA" in fake_tg.sent[-1]["text"]


# ---------------------------------------------------------------------------
# Expiration, mode auto, notifications
# ---------------------------------------------------------------------------
def test_expire_due(app, state, fake_tg, clock):
    clock(10, 0)
    post = make_post(app, publish=(11, 0), expires=(12, 0))
    approved = make_post(app, publish=(13, 0), expires=(11, 0))
    click(fake_tg, app, f"ok:{approved['id']}", approved)
    radar = app.create_radar_item(text="Good point, and the cheaper model wins here too.", angle="nuance",
                                  tweet_id="77", author="sama", post_text="big launch",
                                  post_url="https://x.com/sama/status/77", age_min=5, likes=120,
                                  expires_at=at(11, 30))
    clock(11, 45)
    assert app.expire_due() == 1
    assert radar["status"] == "expired" and post["status"] == "awaiting"
    assert "⌛ Plus d'actualité" in fake_tg.edits[-1]["text"] and fake_tg.edits[-1]["buttons"] == []
    clock(12, 1)
    assert app.expire_due() == 1
    assert post["status"] == "expired"
    assert "⌛ Expiré — rien n'a été publié" in fake_tg.edits[-1]["text"]
    assert fake_tg.edits[-1]["buttons"] == []
    assert approved["status"] == "approved"
    assert app.expire_due() == 0


def test_auto_mode(settings, state, fake_tg, fake_x, clock):
    settings.approval_mode = "auto"
    state.data["telegram"]["offset"] = 0
    app = Approvals(settings, state, fake_tg)
    clock(11, 0)
    item = make_post(app)
    assert item["status"] == "approved" and item["approved_at"] == iso(now_local())
    card = fake_tg.sent[-1]
    assert "publication automatique à 14:00" in card["text"]
    assert labels(card["buttons"]) == ["✏️ Modifier", "❌ Annuler"]
    click(fake_tg, app, f"ok:{item['id']}", item)
    assert fake_tg.answers[-1][1] == "Déjà traité"
    clock(14, 0)
    assert Publisher(settings, state, fake_x, app).publish_due() == 1
    assert item["status"] == "published"


def test_auto_mode_cancel(settings, state, fake_tg, clock):
    settings.approval_mode = "auto"
    state.data["telegram"]["offset"] = 0
    app = Approvals(settings, state, fake_tg)
    clock(11, 0)
    item = make_post(app)
    click(fake_tg, app, f"no:{item['id']}", item)
    assert item["status"] == "rejected"


def test_notify_silent_during_quiet_hours(app, settings, fake_tg, clock):
    settings.quiet_hours = (dtime(23, 30), dtime(7, 30))
    clock(23, 45)
    app.notify("hello")
    assert fake_tg.sent[-1]["silent"] is True
    make_post(app)
    assert fake_tg.sent[-1]["silent"] is True
    clock(12, 0)
    app.notify("hello")
    assert fake_tg.sent[-1]["silent"] is False
    app.notify("hello", silent=True)
    assert fake_tg.sent[-1]["silent"] is True


def test_notify_retries_as_plain_text(app, fake_tg, monkeypatch):
    real_send = fake_tg.send
    calls = []

    def flaky(text, **kw):
        calls.append(text)
        return None if len(calls) == 1 else real_send(text, **kw)

    monkeypatch.setattr(fake_tg, "send", flaky)
    assert app.notify("cost < 5 $") is not None
    assert calls[1] == "cost &lt; 5 $"


def test_refresh_card_failed_offers_manual_publish(app, state, fake_tg):
    item = make_post(app)
    state.update_item(item["id"], status="failed", last_error="boom <x>")
    app.refresh_card(item)
    edit = fake_tg.edits[-1]
    assert "❌ Échec : boom &lt;x&gt;" in edit["text"]
    assert labels(edit["buttons"]) == ["📲 Publier moi-même"]


def test_refresh_card_publishing_has_no_buttons(app, state, fake_tg):
    item = make_post(app)
    state.update_item(item["id"], status="publishing")
    app.refresh_card(item)
    assert "Publication en cours" in fake_tg.edits[-1]["text"]
    assert fake_tg.edits[-1]["buttons"] == []


def test_no_telegram_does_not_crash(settings, state, clock):
    settings.approval_mode = "auto"
    app = Approvals(settings, state, None)
    item = make_post(app)
    assert item["status"] == "approved"
    assert app.poll() == 0
    assert app.notify("x") is None
    app.refresh_card(item)


# ---------------------------------------------------------------------------
# Réponses aux commentaires
# ---------------------------------------------------------------------------
def make_comment(app, text="Thanks! Start by asking it for 3 options, then pick one."):
    return app.create_comment_item(text=text, comment_id="555", comment_text="How do you start with this?",
                                   commenter="bob", my_post_text="My post about prompts",
                                   post_url="https://x.com/bob/status/555",
                                   expires_at=now_local() + timedelta(hours=24))


def test_comment_card_and_ok(app, settings, state, fake_tg, fake_x, clock):
    clock(10, 0)
    item = make_comment(app)
    assert item["kind"] == "comment_reply" and item["status"] == "awaiting"
    assert item["publish_at"] == iso(now_local())
    assert item["meta"]["comment_id"] == "555" and item["meta"]["commenter"] == "bob"
    assert item["meta"]["my_post_text"] == "My post about prompts"
    card = fake_tg.sent[-1]
    assert "💬 <b>@bob</b> a répondu à ton post : «How do you start with this?»" in card["text"]
    assert "Réponse proposée :" in card["text"]
    assert labels(card["buttons"]) == ["✅ Envoyer", "✏️ Modifier", "🔄 Autre", "❌ Ignorer", "🔗 Voir"]
    assert url_of(card["buttons"], "🔗") == "https://x.com/bob/status/555"

    clock(10, 30)
    click(fake_tg, app, f"ok:{item['id']}", item)
    assert item["status"] == "approved" and item["publish_at"] == iso(now_local())
    assert "envoi immédiat" in fake_tg.edits[-1]["text"]
    assert Publisher(settings, state, fake_x, app).publish_due() == 1
    assert fake_x.posts[-1]["reply_to"] == "555"
    assert "Réponse envoyée" in fake_tg.edits[-1]["text"]


def test_comment_regen_and_ignore(app, fake_tg):
    item = make_comment(app)
    app.hooks["regen_comment"] = lambda it: "Another angle: show it one example first."
    click(fake_tg, app, f"regen:{item['id']}", item)
    assert item["texts"] == ["Another angle: show it one example first."]
    click(fake_tg, app, f"no:{item['id']}", item)
    assert item["status"] == "rejected"
    assert "❌ Ignoré" in fake_tg.edits[-1]["text"] and fake_tg.edits[-1]["buttons"] == []


# ---------------------------------------------------------------------------
# Radar
# ---------------------------------------------------------------------------
def make_radar(app):
    return app.create_radar_item(text="Underrated part: it also cuts the cost of long chats.",
                                 angle="le coût, pas la vitesse", tweet_id="777", author="sama",
                                 post_text="We just shipped a faster model.", post_url="https://x.com/sama/status/777",
                                 age_min=12.4, likes=1450, expires_at=now_local() + timedelta(minutes=90))


def test_radar_card(app, state, fake_tg, fake_x, settings):
    item = make_radar(app)
    assert item["kind"] == "radar" and item["status"] == "suggested"
    text = item["texts"][0]
    assert item["meta"]["intent_url"] == intent_reply_url("777", text)
    assert item["meta"]["tweet_id"] == "777" and item["meta"]["likes"] == 1450
    card = fake_tg.sent[-1]
    assert "🎯 <b>@sama</b> vient de poster (12 min, 1 450 likes)" in card["text"]
    assert "Réponse suggérée — c'est toi qui la publies :" in card["text"]
    assert labels(card["buttons"]) == ["📲 Répondre dans X", "🔄 Autre réponse", "❌ Passer", "🔗 Voir le post"]
    assert url_of(card["buttons"], "📲") == intent_reply_url("777", text)
    assert url_of(card["buttons"], "🔗") == "https://x.com/sama/status/777"
    # Jamais publié par l'API, même si on essaie
    assert Publisher(settings, state, fake_x, app).publish_due() == 0
    assert fake_x.calls == []


def test_radar_regen_updates_intent_and_dismiss(app, state, fake_tg):
    item = make_radar(app)
    app.hooks["regen_radar"] = lambda it: ("Fair, but the real win is fewer retries.", "nuance")
    click(fake_tg, app, f"regen:{item['id']}", item)
    assert item["texts"] == ["Fair, but the real win is fewer retries."]
    assert item["meta"]["angle"] == "nuance"
    assert item["meta"]["intent_url"] == intent_reply_url("777", "Fair, but the real win is fewer retries.")
    assert url_of(fake_tg.edits[-1]["buttons"], "📲") == item["meta"]["intent_url"]
    assert "Fair, but the real win is fewer retries." in state.recent_texts("radar")
    click(fake_tg, app, f"ok:{item['id']}", item)
    assert fake_tg.answers[-1][1] == "Déjà traité" and item["status"] == "suggested"
    click(fake_tg, app, f"no:{item['id']}", item)
    assert item["status"] == "dismissed"
    assert "❌ Passé" in fake_tg.edits[-1]["text"] and fake_tg.edits[-1]["buttons"] == []
