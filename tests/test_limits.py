"""Le nombre de posts par jour réglé par l'agent d'analyse (bot/limits.py) : bornes, un cran à la
fois, application le lendemain, et jamais au-delà de ce que le budget permet."""
from __future__ import annotations

from datetime import timedelta

import pytest

from bot import limits as lmod
from bot import utils
from bot.config import parse_post_slots
from bot.limits import PostLimits


class FakeBudget:
    def __init__(self, last7: float = 4.2, monthly: float = 30.0) -> None:
        self.last7, self.monthly_usd = last7, monthly

    def spent_last_days(self, n: int) -> float:
        return self.last7


@pytest.fixture
def lim(settings, state, clock):
    clock(10, 0)                                              # jeudi 15/10/2026
    settings.post_slots = parse_post_slots("tip@14:30,news@19:00,news@23:00")
    settings.reactive_max_per_day = 3
    settings.claude_budget_monthly_usd = 195.0
    spend(state, 3.0)
    return PostLimits(settings, state, llm=None, budget=FakeBudget())


def spend(state, usd_per_day: float, days: int = 7) -> None:
    today = utils.now_local()
    state.data["llm_usage"] = {"days": {(today - timedelta(days=i)).strftime("%Y-%m-%d"): {"usd": usd_per_day, "calls": 9}
                                        for i in range(1, days + 1)}}


def keys(lim, day=None) -> list[str]:
    return [s.key for s in lim.post_slots(day)]


def test_without_a_decision_the_settings_apply(lim):
    assert lim.decision() is None and lim.target() == (3, 3)
    assert (lim.scheduled(), lim.reactive(), lim.scheduled_range()) == (3, 3, (2, 4))
    assert keys(lim) == ["tip-1430", "news-1900", "news-2300"]


def test_a_decision_starts_tomorrow(lim, state):
    row = lim.adopt(4, 3, "morning posts 1.27x on 21 posts", "Un post de plus le matin.")
    assert row["from"] == "2026-10-16" and row["before"] == {"scheduled": 3, "reactive": 3}
    today, tomorrow = utils.now_local(), utils.now_local() + timedelta(days=1)
    assert keys(lim, today) == ["tip-1430", "news-1900", "news-2300"] and lim.scheduled(today) == 3
    assert keys(lim, tomorrow) == ["news-0900", "tip-1430", "news-1900", "news-2300"]
    assert lim.target() == (4, 3)                              # c'est d'elle que part la décision suivante
    assert state.data["post_limits"][-1]["fr"] == "Un post de plus le matin."


def test_one_post_less_drops_the_last_news_post(lim):
    lim.adopt(2, 3, "fewer, better posts", "Un post de moins.")
    assert keys(lim, utils.now_local() + timedelta(days=1)) == ["tip-1430", "news-1900"]


def test_the_breaking_news_cap_moves_too(lim, clock):
    lim.adopt(3, 4, "breaking posts 1.38x on 49 posts", "Un post à chaud de plus.")
    assert lim.reactive() == 3
    clock(0, 5, day=16)
    assert lim.reactive() == 4 and lim.scheduled() == 3


@pytest.mark.parametrize("scheduled, reactive, why", [
    (5, 3, "posts prévus hors bornes (2 à 4)"), (1, 3, "posts prévus hors bornes (2 à 4)"),
    (3, 6, "posts à chaud hors bornes (1 à 5)"), (3, 0, "posts à chaud hors bornes (1 à 5)"),
    (3, 5, "plus d'un cran à la fois"), (True, 3, "valeurs illisibles"), ("4", 3, "valeurs illisibles"),
    (None, None, "valeurs illisibles"),
])
def test_bounds_and_one_step_at_a_time(lim, scheduled, reactive, why):
    assert lim.problem(scheduled, reactive) == why
    assert lim.adopt(scheduled, reactive, "x", "y") is None and lim.target() == (3, 3)


def test_nothing_changes_nothing_is_stored(lim, state):
    assert lim.changed(3, 3) is False and lim.adopt(3, 3, "same", "pareil") is None
    assert "post_limits" not in state.data


def test_careful_with_money_a_raise_the_budget_cannot_carry_is_refused(lim, state):
    assert lim.afford(1) == (True, "")                         # 90 $ + 13,5 $ par mois : sous 156 $
    spend(state, 5.0)                                          # 150 $ par mois au rythme actuel
    ok, why = lim.afford(1)
    assert ok is False and "au-dessus de 156 $, soit 80 % du plafond de 195 $" in why
    assert lim.problem(4, 3).startswith("le budget ne le permet pas : Claude coûterait environ 164 $ par mois")
    assert lim.adopt(4, 3, "more posts", "plus de posts") is None and lim.target() == (3, 3)
    assert lim.adopt(2, 3, "fewer posts", "moins de posts") is not None     # baisser est toujours permis


def test_the_x_bill_counts_too(lim):
    lim.budget.last7 = 6.5 * 7                                 # 195 $ par mois chez X pour un plafond de 30 $
    ok, why = lim.afford(1)
    assert ok is False and why.startswith("l'API X coûterait environ") and "90 % du plafond de 30 $" in why


def test_without_a_cap_or_without_figures_money_does_not_block(settings, state, clock):
    settings.post_slots = parse_post_slots("tip@14:30,news@19:00,news@23:00")
    settings.claude_budget_monthly_usd = 0
    lim = PostLimits(settings, state)                          # ni Claude ni budget X fournis
    assert lim.afford(2) == (True, "") and lim.afford(0) == (True, "") and lim.afford(-1) == (True, "")


def test_a_second_decision_the_same_day_replaces_the_first(lim, state):
    lim.adopt(4, 3, "a", "a")
    assert lim.problem(5, 3) == "posts prévus hors bornes (2 à 4)"
    assert lim.adopt(3, 3, "back", "retour") is not None
    assert len(state.data["post_limits"]) == 1 and lim.target() == (3, 3)
    assert keys(lim, utils.now_local() + timedelta(days=1)) == ["tip-1430", "news-1900", "news-2300"]


def test_yesterdays_slots_keep_the_limits_of_yesterday(lim, clock):
    lim.adopt(4, 3, "a", "a")                                  # s'applique le 16/10
    clock(10, 0, day=17)
    lim.adopt(3, 3, "b", "b")                                  # s'applique le 18/10
    day = utils.now_local()
    assert keys(lim, day - timedelta(days=2)) == ["tip-1430", "news-1900", "news-2300"]          # le 15
    assert keys(lim, day - timedelta(days=1))[0] == "news-0900" and keys(lim, day)[0] == "news-0900"   # les 16 et 17
    assert keys(lim, day + timedelta(days=1)) == ["tip-1430", "news-1900", "news-2300"]          # le 18


def test_the_extra_post_needs_a_free_hour(settings, state, clock):
    tomorrow = utils.now_local() + timedelta(days=1)
    settings.post_slots = parse_post_slots("gm@08:00,tip@12:00,news@18:00")
    lim = PostLimits(settings, state)
    assert lim.adopt(3, settings.reactive_max_per_day, "a", "a") is not None
    # 9 h est à 3 h des autres posts ; le « gm » de 8 h n'entre pas dans le calcul et n'est jamais touché.
    assert [s.key for s in lim.post_slots(tomorrow)] == ["gm-0800", "news-0900", "tip-1200", "news-1800"]


def test_no_free_hour_no_extra_post(settings, state, clock):
    tomorrow = utils.now_local() + timedelta(days=1)
    settings.post_slots = parse_post_slots("gm@08:00,tip@10:00,news@13:00,news@19:00")
    lim = PostLimits(settings, state)
    assert lim.adopt(4, settings.reactive_max_per_day, "a", "a") is not None
    # 9 h, 11 h, 17 h et 21 h sont toutes à moins de 3 h d'un post prévu : rien n'est ajouté.
    assert [s.key for s in lim.post_slots(tomorrow)] == ["gm-0800", "tip-1000", "news-1300", "news-1900"]


def test_the_tip_slot_is_never_dropped(settings, state, clock):
    settings.post_slots = parse_post_slots("tip@14:30,news@19:00")
    lim = PostLimits(settings, state)
    assert lim.scheduled_range() == (1, 3)
    lim.adopt(1, settings.reactive_max_per_day, "a", "a")
    assert [s.key for s in lim.post_slots(utils.now_local() + timedelta(days=1))] == ["tip-1430"]


def test_what_the_analyst_is_told(lim):
    sheet = lim.sheet()
    assert sheet.startswith("POSTING LIMITS — the owner lets you change them")
    assert "Scheduled posts per day: 3 now (you may choose 2 to 4)" in sheet
    assert "Breaking-news posts per day, at most: 3 now (you may choose 1 to 5)" in sheet
    assert "one more post a day costs about $14 a month" in sheet
    assert "costs about $90 a month at the pace of the last 7 days, for a cap of $195" in sheet
    assert "X's API about $18 a month for a cap of $30" in sheet
    assert "A raise the budget cannot carry is refused whatever you decide." in sheet


def test_unreadable_state_falls_back_to_the_settings(lim, state):
    state.data["post_limits"] = "n'importe quoi"
    assert lim.target() == (3, 3) and keys(lim) == ["tip-1430", "news-1900", "news-2300"]
    state.data["post_limits"] = [{"from": "2026-10-01", "scheduled": "beaucoup", "reactive": None}, "x"]
    assert (lim.scheduled(), lim.reactive()) == (3, 3)
    state.data["post_limits"] = [{"from": "2026-10-01", "scheduled": 99, "reactive": 99}]
    assert (lim.scheduled(), lim.reactive()) == (4, 5)         # toujours dans les bornes


def test_history_is_kept_short(lim, state, clock):
    for i in range(20):
        clock(10, 0, day=1 + i)
        spend(state, 1.0)
        lim.adopt(3, 4 if i % 2 == 0 else 3, "x", "y")
    assert len(state.data["post_limits"]) == lmod.KEEP
