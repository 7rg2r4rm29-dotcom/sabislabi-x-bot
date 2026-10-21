"""Boucle principale (bot/scheduler.py) : créneaux, réactif, pause, rapports.

Doublures maison pour Writer / NewsHub / Approvals / Publisher / CommentDesk /
Radar / Analytics : ces tests ne dépendent pas des autres modules (écrits en
parallèle). Un test d'intégration utilise les vrais Approvals / Publisher
s'ils s'importent.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta

import pytest

from bot import scheduler as sched
from bot import utils
from bot.config import Slot, parse_post_slots, parse_thread_slots
from bot.models import Draft, NewsItem, Visual
from bot.scheduler import SLOT_FAILED, SLOT_PENDING, SLOT_SKIPPED, Bot, NullTelegram
from bot.utils import iso, parse_iso
from tests.fakes import FakeTelegram, FakeX, callback_data_for

from datetime import time as dtime


# ---------------------------------------------------------------------------
# Doublures
# ---------------------------------------------------------------------------
def _news_item(i: int = 1) -> NewsItem:
    return NewsItem(id=f"n{i}", title=f"Title {i}", summary="summary", url=f"https://openai.com/{i}",
                    source="OpenAI", published=utils.now_local() - timedelta(hours=1), official=True)


class StubNews:
    def __init__(self, items=None) -> None:
        self.items = items if items is not None else [_news_item(1), _news_item(2)]
        self.calls: list[dict] = []

    def collect(self, max_age_hours: float = 36, use_x: bool = True):
        self.calls.append({"max_age_hours": max_age_hours, "use_x": use_x})
        return list(self.items)


class StubWriter:
    """Chaque méthode de rédaction renvoie la prochaine valeur de sa file
    (ou un brouillon par défaut quand la file est vide)."""

    def __init__(self) -> None:
        self.queues: dict[str, list] = {"post": [], "thread": [], "reactive": []}
        self.default_post = True
        self.default_thread = True
        self.default_reactive = False
        self.calls: list[tuple] = []
        self.during_draft = None   # callable() appelé pendant une rédaction

    def _next(self, family: str, kind: str):
        if self.during_draft:
            self.during_draft()
        q = self.queues[family]
        if q:
            v = q.pop(0)
            return v(kind) if callable(v) else v
        enabled = {"post": self.default_post, "thread": self.default_thread,
                   "reactive": self.default_reactive}[family]
        if not enabled:
            return None
        texts = [f"{kind} tweet {i}" for i in range(1, 6)] if family == "thread" else [f"{kind} text"]
        return Draft(kind=kind, texts=texts, angle="tutorial" if family == "thread" else "",
                     reason="stub")

    def draft_post(self, kind, news, notes, perf):
        self.calls.append(("draft_post", kind, len(news), tuple(notes), perf))
        return self._next("post", kind)

    def draft_thread(self, news, notes, perf):
        self.calls.append(("draft_thread", "thread", len(news), tuple(notes), perf))
        return self._next("thread", "thread")

    def draft_reactive(self, news, notes, perf):
        self.calls.append(("draft_reactive", "reactive", len(news), tuple(notes), perf))
        return self._next("reactive", "reactive")

    def fallback_post(self, kind):
        self.calls.append(("fallback_post", kind))
        return Draft(kind=kind, texts=[f"fallback {kind}"], from_fallback=True, reason="secours")

    def draft_comment_reply(self, my_post_text, commenter, comment_text):
        self.calls.append(("draft_comment_reply", my_post_text, commenter, comment_text))
        return "thanks!"

    def draft_radar_reply(self, author, post_text, age_min, likes):
        self.calls.append(("draft_radar_reply", author, post_text, age_min, likes))
        return "good point", "add"

    def visual_matches(self, post_text, url, visible_text):
        return True, "ok"

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]


class StubApprovals:
    def __init__(self, state) -> None:
        self.state = state
        self.created: list[dict] = []
        self.notes: list[str] = []
        self.polls = 0
        self.expires = 0

    def create_post_item(self, draft, *, slot_key, slot_date, publish_at, expires_at,
                         image_path=None, image_kind=None):
        item = self.state.new_item(draft.kind, draft.texts, slot_key=slot_key, slot_date=slot_date,
                                   publish_at=iso(publish_at), expires_at=iso(expires_at),
                                   image_path=image_path, image_kind=image_kind, poll=draft.poll,
                                   meta={"from_fallback": draft.from_fallback, "angle": draft.angle})
        self.created.append(item)
        return item

    def poll(self):
        self.polls += 1
        return 0

    def expire_due(self):
        self.expires += 1
        return 0

    def notify(self, text, silent=False):
        self.notes.append(text)
        return 1

    def refresh_card(self, item):
        pass


class Counter:
    def __init__(self, name: str = "", fail: bool = False) -> None:
        self.n = 0
        self.fail = fail
        self.name = name

    def __call__(self):
        self.n += 1
        if self.fail:
            raise RuntimeError(f"{self.name} en panne")
        return 0


class StubPublisher:
    def __init__(self) -> None:
        self.publish_due = Counter("publish")


class StubDesk:
    def __init__(self, fail: bool = False) -> None:
        self.check = Counter("check", fail)


class StubAnalytics:
    def __init__(self) -> None:
        self.refreshes = 0
        self.daily = 0
        self.weekly = 0

    def refresh_metrics(self):
        self.refreshes += 1
        return 3

    def performance_notes(self):
        return "tutorials do 2x better"

    def daily_report(self):
        self.daily += 1
        return "☀️ Récap"

    def weekly_report(self):
        self.weekly += 1
        return "📊 Bilan"


class VisualRecorder:
    def __init__(self, result=(None, None)) -> None:
        self.calls: list[tuple] = []
        self.result = result

    def __call__(self, settings, draft, writer, tag):
        self.calls.append((draft.kind, tag))
        return self.result


@pytest.fixture
def parts(state, budget):
    return {
        "writer": StubWriter(), "news": StubNews(), "approvals": StubApprovals(state),
        "publisher": StubPublisher(), "comments": StubDesk(), "radar": StubDesk(),
        "analytics": StubAnalytics(), "visual": VisualRecorder(),
    }


def make_bot(settings, state, budget, parts, **over) -> Bot:
    p = dict(parts)
    p.update(over)
    return Bot(settings, llm=object(), x=FakeX(), telegram=FakeTelegram(), news=p["news"], state=state,
               writer=p["writer"], approvals=p["approvals"], publisher=p["publisher"],
               comments=p["comments"], radar=p["radar"], analytics=p["analytics"], budget=budget,
               prepare_visual=p["visual"], rng=random.Random(0))


# ---------------------------------------------------------------------------
# Créneaux
# ---------------------------------------------------------------------------
class TestPlanSlots:
    def test_gm_drafted_evening_before_at_lead_time(self, settings, state, budget, parts, clock):
        # gm@08:00, avance par défaut 660 min → rédigé la veille à 21:00.
        assert settings.lead_for("gm") == 660
        bot = make_bot(settings, state, budget, parts)
        clock(20, 59)
        assert bot.plan_slots() == 0
        assert parts["approvals"].created == []
        clock(21, 0)
        assert bot.plan_slots() == 1
        item = parts["approvals"].created[0]
        assert item["kind"] == "gm"
        assert item["slot_date"] == "2026-10-16"
        assert item["slot_key"] == "gm-0800"
        assert parse_iso(item["publish_at"]) == datetime(2026, 10, 16, 8, 0, tzinfo=utils.tz())
        assert parse_iso(item["expires_at"]) == datetime(2026, 10, 16, 10, 0, tzinfo=utils.tz())
        assert state.slot_item_id("2026-10-16", "gm-0800") == item["id"]
        # notes perso et notes de performance transmises au Writer
        assert parts["writer"].calls[0][4] == "tutorials do 2x better"

    def test_no_duplicate_drafting(self, settings, state, budget, parts, clock):
        bot = make_bot(settings, state, budget, parts)
        clock(21, 0)
        assert bot.plan_slots() == 1
        clock(21, 1)
        assert bot.plan_slots() == 0
        clock(23, 59)
        assert bot.plan_slots() == 0
        assert parts["writer"].names().count("draft_post") == 1
        assert len(parts["approvals"].created) == 1

    def test_slot_marked_pending_before_drafting(self, settings, state, budget, parts, clock):
        seen = []
        parts["writer"].during_draft = lambda: seen.append(state.slot_item_id("2026-10-16", "gm-0800"))
        bot = make_bot(settings, state, budget, parts)
        clock(21, 0)
        bot.plan_slots()
        assert seen == [SLOT_PENDING]

    def test_fallback_when_writer_fails_twice(self, settings, state, budget, parts, clock):
        parts["writer"].default_post = False
        bot = make_bot(settings, state, budget, parts)
        clock(21, 0)
        assert bot.plan_slots() == 1
        assert parts["writer"].names() == ["draft_post", "draft_post", "fallback_post"]
        item = parts["approvals"].created[0]
        assert item["texts"] == ["fallback gm"]
        assert item["meta"]["from_fallback"] is True

    def test_second_try_succeeds_without_fallback(self, settings, state, budget, parts, clock):
        parts["writer"].queues["post"] = [None, Draft(kind="gm", texts=["gm second try"])]
        bot = make_bot(settings, state, budget, parts)
        clock(21, 0)
        bot.plan_slots()
        assert parts["writer"].names() == ["draft_post", "draft_post"]
        assert parts["approvals"].created[0]["texts"] == ["gm second try"]

    def test_window_closes_after_grace(self, settings, state, budget, parts, clock):
        bot = make_bot(settings, state, budget, parts)
        # tip@13:30 : rédaction dès 11:00, plus rien après 15:30 (13:30 + 120 min).
        clock(15, 31)
        assert bot.plan_slots() == 0
        clock(15, 29)
        assert bot.plan_slots() == 1
        assert parts["approvals"].created[0]["kind"] == "tip"

    def test_late_drafting_keeps_publish_time_and_expiry(self, settings, state, budget, parts, clock):
        bot = make_bot(settings, state, budget, parts)
        clock(14, 0)  # bot redémarré après l'heure du tip de 13:30
        bot.plan_slots()
        item = parts["approvals"].created[0]
        assert item["kind"] == "tip"
        assert parse_iso(item["publish_at"]) == datetime(2026, 10, 15, 13, 30, tzinfo=utils.tz())
        assert parse_iso(item["expires_at"]) == datetime(2026, 10, 15, 15, 30, tzinfo=utils.tz())

    def test_jitter_within_bounds(self, settings, state, budget, parts, clock):
        settings.publish_jitter_minutes = 4
        bot = make_bot(settings, state, budget, parts)
        clock(21, 0)
        bot.plan_slots()
        pa = parse_iso(parts["approvals"].created[0]["publish_at"])
        occ = datetime(2026, 10, 16, 8, 0, tzinfo=utils.tz())
        assert occ <= pa <= occ + timedelta(minutes=4)

    def test_thread_slot_only_on_its_weekday(self, settings, state, budget, parts, clock):
        settings.post_slots = []
        bot = make_bot(settings, state, budget, parts)
        # Jeudi 15/10 à 11:00 : pas de thread (tue@15:00, sun@18:00).
        clock(11, 0, day=15)
        assert bot.plan_slots() == 0
        # Mardi 20/10 : thread de 15:00 rédigé dès 11:00 (avance 240 min).
        clock(10, 59, day=20)
        assert bot.plan_slots() == 0
        clock(11, 0, day=20)
        assert bot.plan_slots() == 1
        item = parts["approvals"].created[0]
        assert item["kind"] == "thread"
        assert item["slot_key"] == "thread-tue-1500"
        assert item["slot_date"] == "2026-10-20"
        assert parts["writer"].names() == ["draft_thread"]
        # Dimanche 18/10 à 14:00 : thread de 18:00.
        clock(14, 0, day=18)
        assert bot.plan_slots() == 1
        assert parts["approvals"].created[-1]["slot_key"] == "thread-sun-1800"

    def test_thread_skipped_when_nothing_solid(self, settings, state, budget, parts, clock):
        settings.post_slots = []
        parts["writer"].default_thread = False
        bot = make_bot(settings, state, budget, parts)
        clock(11, 0, day=20)
        assert bot.plan_slots() == 1
        assert parts["approvals"].created == []
        assert parts["writer"].names() == ["draft_thread", "draft_thread"]   # jamais de texte de secours
        assert state.slot_item_id("2026-10-20", "thread-tue-1500") == SLOT_SKIPPED
        assert any("Thread" in n and "sauté" in n for n in parts["approvals"].notes)
        clock(11, 30, day=20)
        assert bot.plan_slots() == 0

    def test_one_drafting_per_call(self, settings, state, budget, parts, clock):
        # Mardi 20/10 à 13:30 : le tip de 13:30 ET le thread de 17:30 sont dus.
        settings.thread_slots = parse_thread_slots("tue@17:30")
        bot = make_bot(settings, state, budget, parts)
        clock(13, 30, day=20)
        assert bot.plan_slots() == 1
        assert [i["kind"] for i in parts["approvals"].created] == ["tip"]
        assert bot.plan_slots() == 1
        assert [i["kind"] for i in parts["approvals"].created] == ["tip", "thread"]
        assert bot.plan_slots() == 0

    def test_unexpected_error_marks_slot_failed(self, settings, state, budget, parts, clock):
        def boom(kind):
            raise RuntimeError("bug")
        parts["writer"].default_post = False
        parts["writer"].fallback_post = lambda kind: boom(kind)
        bot = make_bot(settings, state, budget, parts)
        clock(21, 0)
        assert bot.plan_slots() == 1
        assert state.slot_item_id("2026-10-16", "gm-0800") == SLOT_FAILED
        assert any("rédaction impossible" in n for n in parts["approvals"].notes)
        assert bot.plan_slots() == 0

    def test_visual_prepared_and_attached(self, settings, state, budget, parts, clock):
        parts["writer"].queues["post"] = [Draft(kind="gm", texts=["gm with shot"],
                                                visual=Visual("screenshot", "https://openai.com/x"))]
        parts["visual"].result = ("/tmp/shot.png", "screenshot")
        bot = make_bot(settings, state, budget, parts)
        clock(21, 0)
        bot.plan_slots()
        item = parts["approvals"].created[0]
        assert item["image_path"] == "/tmp/shot.png"
        assert item["image_kind"] == "screenshot"
        assert parts["visual"].calls == [("gm", "gm_2026-10-16")]

    def test_no_visual_for_poll(self, settings, state, budget, parts, clock):
        parts["writer"].queues["post"] = [Draft(kind="gm", texts=["gm poll"], poll={"options": ["a", "b"]},
                                                visual=Visual("ai", "a calm desk"))]
        parts["visual"].result = ("/tmp/img.png", "ai")
        bot = make_bot(settings, state, budget, parts)
        clock(21, 0)
        bot.plan_slots()
        assert parts["visual"].calls == []
        assert parts["approvals"].created[0]["image_path"] is None

    def test_visual_crash_never_blocks_the_post(self, settings, state, budget, parts, clock):
        def crash(*a):
            raise RuntimeError("chromium")
        parts["writer"].queues["post"] = [Draft(kind="gm", texts=["gm"], visual=Visual("screenshot", "u"))]
        bot = make_bot(settings, state, budget, parts, visual=crash)
        clock(21, 0)
        bot.plan_slots()
        assert parts["approvals"].created[0]["image_path"] is None

    def test_slot_late_evening_survives_midnight(self, settings, state, budget, parts, clock):
        settings.post_slots = parse_post_slots("news@23:30")
        settings.thread_slots = []
        bot = make_bot(settings, state, budget, parts)
        clock(0, 30, day=16)   # créneau de la veille, encore dans son délai de validation
        assert bot.plan_slots() == 1
        assert parts["approvals"].created[0]["slot_date"] == "2026-10-15"


class TestRecoverPendingSlots:
    def test_pending_without_item_is_retried_once(self, settings, state, budget, parts, clock):
        clock(21, 5)
        state.mark_slot("2026-10-16", "gm-0800", SLOT_PENDING)
        make_bot(settings, state, budget, parts)
        assert state.slot_item_id("2026-10-16", "gm-0800") is None
        # nouveau plantage pendant la même rédaction → abandon
        state.mark_slot("2026-10-16", "gm-0800", SLOT_PENDING)
        make_bot(settings, state, budget, parts)
        assert state.slot_item_id("2026-10-16", "gm-0800") == SLOT_FAILED

    def test_pending_with_existing_item_is_attached(self, settings, state, budget, parts, clock):
        clock(21, 5)
        it = state.new_item("gm", ["gm"], slot_key="gm-0800", slot_date="2026-10-16")
        state.mark_slot("2026-10-16", "gm-0800", SLOT_PENDING)
        make_bot(settings, state, budget, parts)
        assert state.slot_item_id("2026-10-16", "gm-0800") == it["id"]


# ---------------------------------------------------------------------------
# Réactif
# ---------------------------------------------------------------------------
class TestReactive:
    def _bot(self, settings, state, budget, parts):
        parts["writer"].default_reactive = True
        return make_bot(settings, state, budget, parts)

    def test_creates_item_between_slots(self, settings, state, budget, parts, clock):
        bot = self._bot(settings, state, budget, parts)
        now = clock(16, 0)   # 150 min après 13:30, 120 min avant 18:00
        assert bot.maybe_reactive() == 1
        item = parts["approvals"].created[0]
        assert item["kind"] == "reactive"
        assert item["slot_key"] is None
        assert parse_iso(item["publish_at"]) == now
        assert parse_iso(item["expires_at"]) == now + timedelta(minutes=180)
        assert state.count("reactive") == 1

    def test_disabled(self, settings, state, budget, parts, clock):
        settings.reactive_enabled = False
        bot = self._bot(settings, state, budget, parts)
        clock(16, 0)
        assert bot.maybe_reactive() == 0
        assert parts["writer"].calls == []

    def test_outside_window(self, settings, state, budget, parts, clock):
        bot = self._bot(settings, state, budget, parts)
        clock(9, 0)   # fenêtre 09:30-22:00
        assert bot.maybe_reactive() == 0
        clock(22, 30)
        assert bot.maybe_reactive() == 0
        assert parts["writer"].calls == []

    def test_too_close_to_a_slot(self, settings, state, budget, parts, clock):
        bot = self._bot(settings, state, budget, parts)
        clock(13, 0)    # 30 min avant le tip de 13:30 (écart mini 90)
        assert bot.maybe_reactive() == 0
        clock(14, 30)   # 60 min après
        assert bot.maybe_reactive() == 0
        assert parts["writer"].calls == []

    def test_interval_between_checks(self, settings, state, budget, parts, clock):
        parts["writer"].default_reactive = False   # Claude dit « non »
        bot = make_bot(settings, state, budget, parts)
        clock(16, 0)
        assert bot.maybe_reactive() == 0
        assert parts["writer"].names() == ["draft_reactive"]
        parts["writer"].default_reactive = True
        clock(16, 30)   # < 150 min depuis le dernier essai
        assert bot.maybe_reactive() == 0
        assert parts["writer"].names() == ["draft_reactive"]

    def test_daily_cap(self, settings, state, budget, parts, clock):
        bot = self._bot(settings, state, budget, parts)
        clock(10, 0)
        state.incr("reactive")
        assert bot.maybe_reactive() == 0
        assert parts["writer"].calls == []

    def test_recent_reactive_item_blocks(self, settings, state, budget, parts, clock):
        settings.reactive_max_per_day = 3
        bot = self._bot(settings, state, budget, parts)
        clock(15, 50)
        state.new_item("reactive", ["earlier"])
        clock(16, 0)
        assert bot.maybe_reactive() == 0
        assert parts["writer"].calls == []

    def test_force_ignores_window_but_not_cap(self, settings, state, budget, parts, clock):
        bot = self._bot(settings, state, budget, parts)
        clock(23, 0)
        assert bot.maybe_reactive(force=True) == 1
        assert bot.maybe_reactive(force=True) == 0   # plafond 1/jour


# ---------------------------------------------------------------------------
# Tour de boucle
# ---------------------------------------------------------------------------
class TestTick:
    def test_order_and_double_poll(self, settings, state, budget, parts, clock):
        bot = make_bot(settings, state, budget, parts)
        clock(10, 0)
        bot.tick()
        # début + après chaque étape lente (créneaux, réactif, commentaires, radar) + fin
        assert parts["approvals"].polls == 6
        assert parts["publisher"].publish_due.n == 6
        assert parts["approvals"].expires == 1
        assert parts["comments"].check.n == 1
        assert parts["radar"].check.n == 1

    def test_paused_drafts_nothing(self, settings, state, budget, parts, clock):
        state.data["paused"] = True
        parts["writer"].default_reactive = True
        bot = make_bot(settings, state, budget, parts)
        clock(21, 0)   # gm de demain dû
        bot.tick()
        assert parts["writer"].calls == []
        assert parts["comments"].check.n == 0
        assert parts["radar"].check.n == 0
        # Telegram et publication tournent toujours (pour /reprendre)
        assert parts["approvals"].polls == 2
        assert parts["publisher"].publish_due.n == 2

    def test_failing_step_does_not_stop_the_tick(self, settings, state, budget, parts, clock):
        bot = make_bot(settings, state, budget, parts, comments=StubDesk(fail=True))
        clock(10, 0)
        res = bot.tick()
        assert res["comments"] is None
        assert parts["radar"].check.n == 1
        assert parts["approvals"].polls == 6

    def test_long_tick_postpones_optional_steps(self, settings, state, budget, parts, clock, monkeypatch):
        bot = make_bot(settings, state, budget, parts)
        clock(10, 0)
        t = iter([0.0] + [1000.0] * 50)
        monkeypatch.setattr(sched.time, "monotonic", lambda: next(t))
        bot.tick()
        assert parts["comments"].check.n == 0 and parts["radar"].check.n == 0
        assert parts["publisher"].publish_due.n >= 2

    def test_one_drafting_per_tick_skips_reactive(self, settings, state, budget, parts, clock):
        settings.reactive_window = (dtime(0, 0), dtime(23, 59))
        settings.reactive_min_gap_min = 0
        parts["writer"].default_reactive = True
        settings.thread_slots = parse_thread_slots("tue@17:30")
        bot = make_bot(settings, state, budget, parts)
        clock(13, 30, day=20)   # tip et thread dus
        bot.tick()
        assert parts["writer"].names() == ["draft_post"]
        bot.tick()
        assert parts["writer"].names() == ["draft_post", "draft_thread"]
        bot.tick()
        assert parts["writer"].names() == ["draft_post", "draft_thread", "draft_reactive"]

    def test_analytics_every_three_hours_without_maybe_refresh(self, settings, state, budget, parts, clock):
        bot = make_bot(settings, state, budget, parts)
        clock(10, 0)
        bot.tick()
        clock(12, 0)
        bot.tick()
        assert parts["analytics"].refreshes == 1
        clock(13, 1)
        bot.tick()
        assert parts["analytics"].refreshes == 2

    def test_analytics_maybe_refresh_preferred(self, settings, state, budget, parts, clock):
        an = StubAnalytics()
        calls = []
        an.maybe_refresh = lambda: calls.append(1) or 0
        bot = make_bot(settings, state, budget, parts, analytics=an)
        clock(10, 0)
        bot.tick()
        bot.tick()
        assert len(calls) == 2
        assert an.refreshes == 0


class TestReports:
    def test_daily_report_once_a_day(self, settings, state, budget, parts, clock):
        bot = make_bot(settings, state, budget, parts)
        clock(8, 29)
        assert bot.maybe_daily_report() is False
        clock(8, 30)
        assert bot.maybe_daily_report() is True
        assert parts["approvals"].notes == ["☀️ Récap"]
        clock(9, 0)
        assert bot.maybe_daily_report() is False
        clock(8, 31, day=16)
        assert bot.maybe_daily_report() is True
        assert parts["analytics"].daily == 2

    def test_daily_report_mentions_pause(self, settings, state, budget, parts, clock):
        state.data["paused"] = True
        bot = make_bot(settings, state, budget, parts)
        clock(8, 30)
        bot.tick()
        assert parts["approvals"].notes and "pause" in parts["approvals"].notes[0]

    def test_daily_report_skipped_when_too_late(self, settings, state, budget, parts, clock):
        bot = make_bot(settings, state, budget, parts)
        clock(21, 0)
        assert bot.maybe_daily_report() is False
        assert parts["analytics"].daily == 0
        clock(21, 5)
        assert bot.maybe_daily_report() is False

    def test_weekly_report_once(self, settings, state, budget, parts, clock):
        settings.weekly_report = Slot("weekly", dtime(20, 30), 6)   # dimanche 20:30
        bot = make_bot(settings, state, budget, parts)
        clock(20, 29, day=18)
        assert bot.maybe_weekly_report() is False
        clock(20, 30, day=18)
        assert bot.maybe_weekly_report() is True
        clock(22, 0, day=18)
        assert bot.maybe_weekly_report() is False
        clock(10, 0, day=19)
        assert bot.maybe_weekly_report() is False
        assert parts["analytics"].weekly == 1
        assert parts["approvals"].notes == ["📊 Bilan"]

    def test_weekly_report_not_sent_days_later(self, settings, state, budget, parts, clock):
        settings.weekly_report = Slot("weekly", dtime(20, 30), 6)
        bot = make_bot(settings, state, budget, parts)
        clock(10, 0, day=20)   # mardi : le dimanche est passé depuis plus de 24 h
        assert bot.maybe_weekly_report() is False
        assert parts["analytics"].weekly == 0

    def test_gc_once_a_day(self, settings, state, budget, parts, clock, monkeypatch):
        calls = []
        monkeypatch.setattr(state, "gc", lambda: calls.append(1) or 0)
        bot = make_bot(settings, state, budget, parts)
        clock(10, 0)
        bot.maybe_gc()
        clock(20, 0)
        bot.maybe_gc()
        assert calls == [1]
        clock(10, 1, day=16)
        bot.maybe_gc()
        assert calls == [1, 1]


# ---------------------------------------------------------------------------
# Crochets (commandes, nouvelle version)
# ---------------------------------------------------------------------------
class TestHooks:
    def test_hooks_keys(self, settings, state, budget, parts):
        bot = make_bot(settings, state, budget, parts)
        assert set(bot.hooks()) == {"regenerate", "regen_comment", "regen_radar", "status", "plan", "budget"}

    def test_regenerate_uses_same_kind_and_no_paid_x_reads(self, settings, state, budget, parts, clock):
        bot = make_bot(settings, state, budget, parts)
        clock(10, 0)
        d = bot.regenerate({"kind": "tip"})
        assert d.kind == "tip"
        t = bot.regenerate({"kind": "thread"})
        assert t.kind == "thread"
        assert parts["writer"].names() == ["draft_post", "draft_thread"]
        assert all(c["use_x"] is False for c in parts["news"].calls)
        assert bot.regenerate({"kind": "radar"}) is None

    def test_regen_comment_and_radar_pass_meta(self, settings, state, budget, parts):
        bot = make_bot(settings, state, budget, parts)
        assert bot.regen_comment({"meta": {"my_post_text": "mine", "commenter": "bob",
                                           "comment_text": "nice"}}) == "thanks!"
        assert parts["writer"].calls[-1] == ("draft_comment_reply", "mine", "bob", "nice")
        assert bot.regen_radar({"meta": {"author": "sama", "post_text": "p", "age_min": 5.0,
                                         "likes": 40}}) == ("good point", "add")
        assert parts["writer"].calls[-1] == ("draft_radar_reply", "sama", "p", 5.0, 40)

    def test_status_text(self, settings, state, budget, parts, clock):
        bot = make_bot(settings, state, budget, parts)
        clock(10, 0)
        state.new_item("tip", ["a <b>tip</b> waiting"], status="awaiting")
        txt = bot.status_text()
        assert "actif" in txt
        assert "À valider (1)" in txt
        assert "&lt;b&gt;tip" in txt          # texte échappé pour le HTML Telegram
        assert "API X" in txt and "Claude" in txt
        state.data["paused"] = True
        assert "en pause" in bot.status_text()

    def test_plan_text(self, settings, state, budget, parts, clock):
        bot = make_bot(settings, state, budget, parts)
        clock(21, 0)
        bot.plan_slots()
        txt = bot.plan_text()
        assert "demain 08:00" in txt and "attend ta validation" in txt
        assert "13:30" in txt and "rédaction demain vers 11:00" in txt

    def test_budget_text(self, settings, state, budget, parts, clock):
        clock(10, 0)
        state.data["llm_usage"] = {"days": {"2026-10-15": {"calls": 3, "usd": 0.42},
                                            "2026-10-01": {"calls": 1, "usd": 1.0}}}
        bot = make_bot(settings, state, budget, parts)
        txt = bot.budget_text()
        assert "0.42 $ aujourd'hui (3 appel(s)), 1.42 $ ce mois" in txt
        assert "API X" in txt


# ---------------------------------------------------------------------------
# Construction et boucle infinie
# ---------------------------------------------------------------------------
class TestConstructionAndLoop:
    def test_null_telegram_when_not_configured(self, settings, state, budget, parts):
        settings.telegram_token = ""
        p = dict(parts)
        bot = Bot(settings, llm=object(), x=FakeX(), news=p["news"], state=state, writer=p["writer"],
                  approvals=p["approvals"], publisher=p["publisher"], comments=p["comments"],
                  radar=p["radar"], analytics=p["analytics"], budget=budget)
        assert isinstance(bot.telegram, NullTelegram)
        assert bot.telegram.send("hello") is None
        assert bot.telegram.updates(None) == []

    def test_run_forever_survives_errors_and_stops(self, settings, state, budget, parts, monkeypatch):
        monkeypatch.setattr(utils, "start_watchdog", lambda *a, **k: None)
        bot = make_bot(settings, state, budget, parts)
        sleeps: list[float] = []
        monkeypatch.setattr(bot, "_sleep", lambda s: sleeps.append(s))
        monkeypatch.setattr(bot, "_install_sigterm", lambda: None)
        calls = {"n": 0}

        def fake_tick():
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")
            if calls["n"] == 3:
                bot.stop()
            return {}
        monkeypatch.setattr(bot, "tick", fake_tick)
        bot.run_forever()
        assert calls["n"] == 3
        assert sleeps[0] == sched.ERROR_SLEEP_S
        assert sleeps[1] == sched.TICK_SLEEP_S

    def test_run_forever_keyboard_interrupt(self, settings, state, budget, parts, monkeypatch):
        monkeypatch.setattr(utils, "start_watchdog", lambda *a, **k: None)
        bot = make_bot(settings, state, budget, parts)
        monkeypatch.setattr(bot, "_install_sigterm", lambda: None)

        def interrupt():
            raise KeyboardInterrupt
        monkeypatch.setattr(bot, "tick", interrupt)
        bot.run_forever()   # ne lève pas

    def test_startup_notice_not_repeated(self, settings, state, budget, parts, clock):
        bot = make_bot(settings, state, budget, parts)
        clock(10, 0)
        bot._startup_notice()
        bot._startup_notice()
        assert len(parts["approvals"].notes) == 1


# ---------------------------------------------------------------------------
# Intégration avec les vrais Approvals / Publisher (s'ils s'importent)
# ---------------------------------------------------------------------------
def _real_modules():
    try:
        from bot.approvals import Approvals  # noqa: F401
        from bot.publisher import Publisher  # noqa: F401
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"Approvals/Publisher indisponibles : {e}")


def test_integration_draft_approve_publish(settings, state, budget, parts, clock):
    _real_modules()
    tg = FakeTelegram()
    x = FakeX()
    bot = Bot(settings, llm=object(), x=x, telegram=tg, news=parts["news"], state=state,
              writer=parts["writer"], comments=StubDesk(), radar=StubDesk(), analytics=StubAnalytics(),
              budget=budget, prepare_visual=VisualRecorder(), rng=random.Random(0))
    assert bot.approvals.hooks["status"] == bot.status_text
    clock(21, 0)
    bot.tick()
    assert tg.sent, "la carte du gm doit partir sur Telegram"
    gm_cards = [i for i, m in enumerate(tg.sent) if "gm text" in m["text"]]
    assert gm_cards
    tg.push_callback(callback_data_for(tg, "✅", gm_cards[0]), message_id=tg.sent[gm_cards[0]]["id"])
    clock(21, 1)
    bot.tick()
    gm = [it for it in state.items(kinds=("gm",))][0]
    assert gm["status"] == "approved"
    assert x.posts == []          # rien avant l'heure
    clock(8, 0, day=16)
    bot.tick()
    assert [p["text"] for p in x.posts] == ["gm text"]
    assert state.item(gm["id"])["status"] == "published"


def test_default_construction_wires_real_modules(settings, state, budget, fake_llm, clock):
    try:
        bot = Bot(settings, llm=fake_llm, x=FakeX(), telegram=FakeTelegram(), state=state, budget=budget)
    except ImportError as e:
        pytest.skip(f"module manquant : {e}")
    assert bot.approvals.hooks["plan"] == bot.plan_text
    assert bot.publisher.approvals is bot.approvals



def test_thread_day_drops_the_post_slot_next_to_the_thread(settings, state, budget, parts, clock):
    settings.post_slots = parse_post_slots("gm@08:30,tip@14:00,news@18:30")
    settings.thread_slots = parse_thread_slots("tue@15:00,sun@17:00")
    bot = make_bot(settings, state, budget, parts)
    clock(7, 0, day=20)                                   # mardi
    kinds = [sl.kind for occ, sl in bot._occurrences((0,))]
    assert kinds == ["gm", "thread", "news"]              # le tip de 14 h laisse la place au thread
    clock(7, 0, day=25)                                   # dimanche
    assert [sl.kind for occ, sl in bot._occurrences((0,))] == ["gm", "tip", "thread"]
    clock(7, 0, day=21)                                   # mercredi : journée normale
    assert [sl.kind for occ, sl in bot._occurrences((0,))] == ["gm", "tip", "news"]


def test_failed_news_slot_is_skipped_not_filled(settings, state, budget, parts, clock):
    parts["writer"].default_post = False
    bot = make_bot(settings, state, budget, parts)
    clock(16, 30)
    bot.plan_slots()
    assert parts["approvals"].created == []
    assert any("Actu" in n for n in parts["approvals"].notes)
