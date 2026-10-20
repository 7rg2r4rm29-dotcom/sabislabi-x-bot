"""Une journée complète simulée avec les VRAIS modules du bot.

Seuls Claude (FakeLLM), l'API X (FakeX) et Telegram (FakeTelegram) sont
simulés. On fait tourner `Bot.tick()` toutes les 10 minutes du jeudi 06:00
au vendredi 09:00 et on vérifie les règles qui protègent le compte :
  - rien ne part sur X sans un ✅ du propriétaire ;
  - le radar ne publie jamais (il propose un lien, c'est toi qui publies) ;
  - /pause bloque tout, /reprendre laisse 3 minutes pour annuler ;
  - un post non validé expire, sans jamais partir.
"""
from __future__ import annotations

import random
from datetime import timedelta, timezone

import pytest

from bot import utils
from bot.scheduler import Bot
from tests.fakes import FakeLLM, FakeTelegram, FakeX, callback_data_for, make_tweet

ME = "42"
GM = "gm. one small automation today beats the big plan you never start."
TIP = ('before you trust an AI answer, ask it: "what would make this answer wrong?"\n'
       "you'll catch more mistakes than you think.")
REPLY = "Yes: paste the full stack trace, not a summary. It changes everything."
RADAR = "Tried this yesterday: it works best when you paste the full error, not a summary."
RADAR_POST = "We just shipped a new way to build small apps directly inside the chat window."


def responder(label, system, user, schema):
    def post(text):
        return {"text": text, "source_ids": [], "used_note_ids": [], "visual_url": "", "image_prompt": "",
                "poll_options": [], "reason": "raison"}
    if label == "post-gm":
        return post(GM)
    if label == "post-tip":
        return post(TIP)
    if label == "reactive":
        return {"post": False, "reason": "rien d'assez chaud"}
    if label == "comment-reply":
        return {"reply": True, "reason": "vraie question", "text": REPLY}
    if label == "radar-reply":
        return {"reply": True, "reason": "vrai apport", "text": RADAR, "angle": "test perso"}
    return None  # actu : pas de source -> texte de secours


def iso_utc(minutes_ago: float) -> str:
    dt = utils.now_local().astimezone(timezone.utc) - timedelta(minutes=minutes_ago)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


class Day:
    def __init__(self, settings, state, budget, clock) -> None:
        self.clock = clock
        self.tg = FakeTelegram()
        self.x = FakeX()
        self.x.ids_map = {"sama": "1", "karpathy": "2", "claudeai": "3"}
        self.state = state
        self.bot = Bot(settings, llm=FakeLLM({"": responder}), x=self.x, telegram=self.tg, state=state,
                       budget=budget, prepare_visual=lambda *a: (None, None), rng=random.Random(0))
        self.approved_ids: set[str] = set()

    def card_index(self, needle: str) -> int:
        idx = [i for i, m in enumerate(self.tg.sent) if needle in m["text"] and self.tg.sent[i].get("buttons")]
        assert idx, f"aucune carte avec « {needle[:30]} »"
        return idx[-1]

    def item_for_card(self, i: int) -> dict:
        mid = self.tg.sent[i]["id"]
        return next(it for it in self.state.data["items"].values() if it.get("tg_message_id") == mid)

    def approve(self, needle: str) -> dict:
        i = self.card_index(needle)
        item = self.item_for_card(i)
        self.tg.push_callback(callback_data_for(self.tg, "✅", i), message_id=self.tg.sent[i]["id"])
        self.approved_ids.add(item["id"])
        return item

    def run(self, start: tuple[int, int, int], end: tuple[int, int, int], events: dict) -> None:
        """Un tour toutes les 10 min ; `events[(jour, h, m)]` est appelé juste avant le tour."""
        day, h, m = start
        t = self.clock(h, m, day=day)
        stop = self.clock(end[1], end[2], day=end[0])
        self.clock(h, m, day=day)
        while t <= stop:
            key = (t.day, t.hour, t.minute)
            if key in events:
                events[key]()
            self.bot.tick()
            t = t + timedelta(minutes=10)
            utils.set_clock(t)


def test_simulated_day(settings, state, budget, clock):
    settings.radar_accounts = ["sama", "karpathy", "claudeai"]
    settings.radar_accounts_per_check = 3
    d = Day(settings, state, budget, clock)
    seen = {}

    def approve_gm():
        seen["gm"] = d.approve("one small automation")

    def note():
        d.tg.push_message("/note I automated my invoices with Claude in one evening")

    def comment_and_radar():
        gm_id = seen["gm"].get("published_ids", [None])[0]
        assert gm_id, "le GM validé doit être publié à 08:00"
        reply = make_tweet("901", "@sabislabi Does this also work with stack traces?", author_id="7",
                           created_at=iso_utc(5), in_reply_to_user_id=ME, conversation_id=gm_id,
                           referenced_reply_to=gm_id)
        d.x.mention_batches.append(([reply], {"7": {"id": "7", "username": "alice"}}, "901"))
        d.x.user_posts_map["1"] = [make_tweet("5001", RADAR_POST, author_id="1", created_at=iso_utc(10),
                                              likes=80)]

    def approve_reply():
        seen["reply"] = d.approve("stack traces")

    def approve_news_then_pause():
        seen["news"] = d.approve("fallback news")
        d.tg.push_message("/pause")

    def resume():
        assert [p for p in d.x.posts if p["text"] == "fallback news"] == [], "rien pendant la pause"
        d.tg.push_message("/reprendre")

    d.run((15, 6, 0), (16, 9, 0), {
        (15, 7, 0): approve_gm,
        (15, 9, 0): note,
        (15, 10, 0): comment_and_radar,
        (15, 10, 40): approve_reply,
        (15, 17, 0): approve_news_then_pause,
        (15, 18, 30): resume,
    })

    posts = d.x.posts
    texts = [p["text"] for p in posts]
    # 1. Rien sans ✅ : chaque post vient d'un item validé à la main.
    published = [it for it in state.data["items"].values() if it.get("status") == "published"]
    assert {it["id"] for it in published} <= d.approved_ids
    assert len(posts) == len(published) == 3
    # GM à l'heure, réponse au commentaire, actu après la pause
    assert texts[0] == GM
    assert posts[1]["text"] == REPLY and posts[1]["reply_to"] == "901"
    assert texts[2] == "fallback news"
    news = state.item(seen["news"]["id"])
    assert utils.parse_iso(news["publish_at"]) == clock(18, 33)   # /reprendre à 18:30 + 3 min
    # 2. L'astuce jamais validée a expiré sans partir ; le GM de vendredi attend toujours.
    tips = state.items(kinds=("tip",))
    assert tips and all(t["status"] == "expired" for t in tips)
    assert TIP not in texts
    gm_friday = [it for it in state.items(kinds=("gm",)) if it.get("slot_date") == "2026-10-16"]
    assert gm_friday and gm_friday[0]["status"] in ("awaiting", "expired")
    # 3. Le radar a proposé une réponse… que le bot n'a jamais publiée.
    radar = state.items(kinds=("radar",))
    assert radar and radar[0]["texts"][0] == RADAR
    assert RADAR not in texts and all(p["reply_to"] != "5001" for p in posts)
    assert any("x.com/intent/post" in str(b.get("url", "")) for m in d.tg.sent for row in (m.get("buttons") or [])
               for b in row)
    # 4. /note gardée pour Claude
    assert state.data["notes"][-1]["text"].startswith("I automated my invoices")
    assert state.data.get("paused") is False


def test_auto_mode_requires_explicit_label_ack(settings):
    settings.approval_mode = "auto"
    settings.automated_label_ack = False
    assert any("Automated" in e for e in settings.validate())
    settings.automated_label_ack = True
    assert settings.validate() == []


@pytest.mark.parametrize("hours_down", [1, 4])
def test_bot_down_during_slot(settings, state, budget, clock, hours_down):
    """Bot arrêté au moment du GM validé : moins de 3 h de retard -> il part ; plus -> il ne part pas."""
    d = Day(settings, state, budget, clock)
    d.run((15, 6, 0), (15, 7, 0), {(15, 7, 0): lambda: d.approve("one small automation")})
    gm = state.items(kinds=("gm",))[0]
    assert gm["status"] == "approved" and d.x.posts == []
    clock(8 + hours_down, 0)
    d.bot.tick()
    if hours_down < 3:
        assert [p["text"] for p in d.x.posts] == [GM]
    else:
        assert d.x.posts == []
        assert state.item(gm["id"])["status"] == "expired"
        assert any("n'a pas pu partir à temps" in m["text"] for m in d.tg.sent)
