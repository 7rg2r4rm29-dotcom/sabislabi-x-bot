"""Tests du Radar (bot/radar.py) : aucun appel réseau, jamais de publication."""
from __future__ import annotations

from datetime import timedelta, timezone

import pytest

from bot import utils
from bot.radar import Radar, parse_created
from bot.x_api import XError, tweet_url
from tests.fakes import FakeX, make_tweet


# ---------------------------------------------------------------------------
# Doublures locales
# ---------------------------------------------------------------------------
class StubWriter:
    def __init__(self, result=("Tried this yesterday: it works best when you paste the full error.", "own test")):
        self.result = result
        self.calls: list[tuple] = []

    def draft_radar_reply(self, author, post_text, age_min, likes):
        self.calls.append((author, post_text, age_min, likes))
        if callable(self.result):
            return self.result(author, post_text, age_min, likes)
        return self.result


class StubApprovals:
    def __init__(self):
        self.radar_items: list[dict] = []

    def create_radar_item(self, **kw):
        self.radar_items.append(kw)
        return {"id": f"r{len(self.radar_items)}", **kw}


@pytest.fixture(autouse=True)
def _frozen(clock):
    return clock


def ago(minutes: float) -> str:
    """created_at façon API X, `minutes` avant l'horloge figée."""
    dt = utils.now_local().astimezone(timezone.utc) - timedelta(minutes=minutes)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


TEXT = "We just shipped a new way to build small apps directly inside the chat window."


def post(tid: str, minutes: float, likes: int, text: str = TEXT, author: str = "1") -> dict:
    return make_tweet(tid, text, author_id=author, created_at=ago(minutes), likes=likes)


@pytest.fixture
def radar_env(settings, state, fake_x):
    settings.radar_accounts = ["sama", "karpathy", "claudeai"]
    settings.radar_accounts_per_check = 3
    fake_x.ids_map = {"sama": "1", "karpathy": "2", "claudeai": "3"}
    writer, approvals = StubWriter(), StubApprovals()
    radar = Radar(settings, state, fake_x, writer, approvals)
    return radar, writer, approvals


def never_posted(fake_x: FakeX) -> None:
    assert fake_x.posts == []
    assert not any(c[0] in ("post", "upload") for c in fake_x.calls)


# ---------------------------------------------------------------------------
def test_parse_created():
    assert parse_created("2026-10-15T08:00:00.000Z").utcoffset() == timedelta(0)
    assert parse_created("") is None and parse_created("garbage") is None


def test_suggestion_for_fast_fresh_post(radar_env, state, fake_x, clock):
    radar, writer, approvals = radar_env
    now = clock(10, 0)
    fake_x.user_posts_map["1"] = [post("5001", 10, 80)]
    assert radar.check() == 1
    assert writer.calls[0][0] == "sama" and writer.calls[0][1] == TEXT
    assert writer.calls[0][2] == pytest.approx(10.0) and writer.calls[0][3] == 80
    kw = approvals.radar_items[0]
    assert kw["tweet_id"] == "5001" and kw["author"] == "sama"
    assert kw["text"].startswith("Tried this yesterday") and kw["angle"] == "own test"
    assert kw["post_text"] == TEXT
    assert kw["post_url"] == tweet_url("sama", "5001")
    assert kw["likes"] == 80 and kw["age_min"] == pytest.approx(10.0)
    assert kw["expires_at"] == now + timedelta(minutes=90)
    assert state.count("radar_suggestions") == 1
    assert state.is_handled("5001")
    assert state.data["radar"]["since_ids"]["sama"] == "5001"
    assert "sama" in state.data["radar"]["done_today"][utils.today_str()]
    assert state.recent_texts("radar") == [kw["text"]]
    never_posted(fake_x)


def test_user_ids_resolved_once_and_cached(radar_env, state, fake_x, clock):
    radar, _, _ = radar_env
    fake_x.ids_map = {"sama": "1", "karpathy": "2"}   # claudeai introuvable
    radar.check()
    assert [c for c in fake_x.calls if c[0] == "user_ids"] == [("user_ids", ("sama", "karpathy", "claudeai"))]
    assert state.data["radar"]["user_ids"] == {"sama": "1", "karpathy": "2"}
    # Passage suivant le même jour : rien à résoudre (claudeai retenté demain seulement).
    clock(10, 31)
    radar.check()
    assert len([c for c in fake_x.calls if c[0] == "user_ids"]) == 1
    clock(10, 0, day=16)
    radar.check()
    assert [c for c in fake_x.calls if c[0] == "user_ids"][-1] == ("user_ids", ("claudeai",))


def test_rotation_and_accounts_per_check(radar_env, settings, state, fake_x, clock):
    radar, _, _ = radar_env
    settings.radar_accounts_per_check = 2
    radar.check()
    read = [c[1] for c in fake_x.calls if c[0] == "user_posts"]
    assert read == ["1", "2"]
    assert state.data["radar"]["rotation"] == 2
    clock(10, 31)
    radar.check()
    read = [c[1] for c in fake_x.calls if c[0] == "user_posts"]
    assert read == ["1", "2", "3", "1"]
    assert state.data["radar"]["rotation"] == 1


def test_rotation_skips_accounts_done_today(radar_env, settings, state, fake_x, clock):
    radar, _, _ = radar_env
    settings.radar_accounts_per_check = 2
    state.data["radar"]["done_today"] = {utils.today_str(): ["sama"], "2026-10-14": ["karpathy"]}
    radar.check()
    read = [c[1] for c in fake_x.calls if c[0] == "user_posts"]
    assert read == ["2", "3"]
    assert list(state.data["radar"]["done_today"]) == [utils.today_str()]   # vieux jours purgés


def test_since_id_passed_to_api(radar_env, state, fake_x):
    radar, _, _ = radar_env
    state.data["radar"]["since_ids"] = {"sama": "4000"}
    radar.check()
    assert ("user_posts", "1", "4000") in fake_x.calls


@pytest.mark.parametrize("args,why", [
    ((("6001", 50, 500), {}), "trop vieux (> 45 min)"),
    ((("6002", 10, 50), {"text": "Short one https://t.co/abcdefghij"}), "trop court"),
    ((("6003", 10, 50), {"text": "Huge giveaway today: like and rt to enter, winners announced tonight"}), "bruit"),
    ((("6004", 10, 50), {"text": "Our thoughts on the election results and what comes next for everyone"}),
     "politique"),
    ((("6005", 10, 50), {"text": "Why we think bitcoin and AI agents are a perfect match for the future"}),
     "crypto"),
    ((("6006", 10, 50), {"text": "RT @someone: this is a repost of a long enough post about AI tools"}), "repost"),
])
def test_candidate_filters(radar_env, state, fake_x, args, why):
    # Construit APRÈS le figeage de l'horloge (sinon created_at dépend de l'heure réelle).
    tweet = post(*args[0], **args[1])
    radar, writer, approvals = radar_env
    fake_x.user_posts_map["1"] = [tweet]
    assert radar.check() == 0, why
    assert writer.calls == [], why
    assert state.is_handled(tweet["id"]), why


def test_velocity_threshold_and_priority_accounts(settings, state, fake_x, clock):
    # 12 comptes : les 10 premiers sont prioritaires, pas les deux derniers.
    accounts = [f"acc{i}" for i in range(12)]
    settings.radar_accounts = accounts
    settings.radar_accounts_per_check = 12
    settings.radar_min_likes_per_min = 1.0
    fake_x.ids_map = {a: str(100 + i) for i, a in enumerate(accounts)}
    fake_x.user_posts_map = {
        "100": [post("7001", 10, 2)],    # prioritaire, < 20 min, lent -> gardé
        "101": [post("7002", 30, 5)],    # prioritaire mais 30 min et lent -> refusé
        "110": [post("7003", 10, 3)],    # non prioritaire, lent (0,3/min) -> refusé
        "111": [post("7004", 10, 40)],   # non prioritaire, rapide (4/min) -> gardé
    }
    writer, approvals = StubWriter(), StubApprovals()
    radar = Radar(settings, state, fake_x, writer, approvals)
    assert radar.check() == 2
    assert {c[0] for c in writer.calls} == {"acc0", "acc11"}
    # Le plus rapide d'abord
    assert writer.calls[0][0] == "acc11"
    for tid in ("7001", "7002", "7003", "7004"):
        assert state.is_handled(tid)


def test_max_two_per_check_sorted_by_velocity(radar_env, settings, state, fake_x):
    radar, writer, approvals = radar_env
    fake_x.user_posts_map = {
        "1": [post("8001", 10, 20)],     # 2/min
        "2": [post("8002", 10, 200)],    # 20/min
        "3": [post("8003", 10, 90)],     # 9/min
    }
    assert radar.check() == 2
    assert [k["tweet_id"] for k in approvals.radar_items] == ["8002", "8003"]
    assert state.is_handled("8001")   # examiné : ne repassera pas


def test_one_suggestion_per_account_per_day(radar_env, state, fake_x, clock):
    radar, writer, approvals = radar_env
    fake_x.user_posts_map = {"1": [post("8101", 5, 100), post("8102", 8, 100)]}
    assert radar.check() == 1
    assert len(writer.calls) == 1
    # Passage suivant : sama est déjà servi aujourd'hui, il n'est même pas relu.
    clock(10, 31)
    fake_x.user_posts_map = {"1": [post("8103", 2, 100)]}
    calls_before = len([c for c in fake_x.calls if c[0] == "user_posts" and c[1] == "1"])
    radar.check()
    assert len([c for c in fake_x.calls if c[0] == "user_posts" and c[1] == "1"]) == calls_before


def test_writer_refusal_tries_next_candidate(radar_env, state, fake_x):
    radar, writer, approvals = radar_env
    writer.result = lambda author, *a: None if author == "karpathy" else ("Good add here, tested it.", "test")
    fake_x.user_posts_map = {"2": [post("8201", 10, 300)], "1": [post("8202", 10, 30)]}
    assert radar.check() == 1
    assert approvals.radar_items[0]["author"] == "sama"
    assert state.is_handled("8201")
    assert "karpathy" not in state.data["radar"]["done_today"][utils.today_str()]


def test_daily_cap(radar_env, settings, state, fake_x, clock):
    radar, writer, _ = radar_env
    settings.radar_max_per_day = 3
    state.incr("radar_suggestions", 2)
    fake_x.user_posts_map = {"1": [post("8301", 10, 300)], "2": [post("8302", 10, 200)]}
    assert radar.check() == 1
    clock(10, 31)
    assert radar.check() == 0
    assert state.count("radar_suggestions") == 3


class _ErrX(FakeX):
    def __init__(self, errors: dict[str, str]):
        super().__init__()
        self.errors = errors

    def user_posts(self, user_id, since_id=None, max_results=5, optional=True):
        assert optional is True and max_results == 5
        if user_id in self.errors:
            self.calls.append(("user_posts", user_id, since_id))
            raise XError(self.errors[user_id], "nope")
        return super().user_posts(user_id, since_id, max_results, optional)


def test_budget_error_stops_reading_quietly(settings, state, clock):
    settings.radar_accounts = ["sama", "karpathy", "claudeai"]
    settings.radar_accounts_per_check = 3
    x = _ErrX({"2": "budget"})
    x.ids_map = {"sama": "1", "karpathy": "2", "claudeai": "3"}
    x.user_posts_map = {"1": [post("8401", 10, 300)], "3": [post("8402", 10, 300)]}
    writer, approvals = StubWriter(), StubApprovals()
    radar = Radar(settings, state, x, writer, approvals)
    assert radar.check() == 1                  # sama (lu avant l'erreur) est quand même proposé
    assert [c[1] for c in x.calls if c[0] == "user_posts"] == ["1", "2"]   # claudeai pas lu


def test_other_error_skips_account(settings, state, clock):
    settings.radar_accounts = ["sama", "karpathy", "claudeai"]
    settings.radar_accounts_per_check = 3
    x = _ErrX({"1": "not_found"})
    x.ids_map = {"sama": "1", "karpathy": "2", "claudeai": "3"}
    x.user_posts_map = {"2": [post("8501", 10, 300)]}
    writer, approvals = StubWriter(), StubApprovals()
    radar = Radar(settings, state, x, writer, approvals)
    assert radar.check() == 1
    assert [c[1] for c in x.calls if c[0] == "user_posts"] == ["1", "2", "3"]


def test_gating(radar_env, settings, state, fake_x, clock):
    radar, _, _ = radar_env
    fake_x.user_posts_map = {"1": [post("8601", 5, 300)]}

    def reads():
        return len([c for c in fake_x.calls if c[0] == "user_posts"])

    settings.radar_enabled = False
    assert radar.check() == 0 and reads() == 0
    settings.radar_enabled = True
    state.data["paused"] = True
    assert radar.check() == 0 and reads() == 0
    state.data["paused"] = False
    clock(23, 0)                       # hors fenêtre 08:00-22:30
    assert radar.check() == 0 and reads() == 0
    clock(10, 0)
    fake_x.configured = False
    assert radar.check() == 0 and reads() == 0
    fake_x.configured = True
    settings.radar_max_per_day = 0
    assert radar.check() == 0 and reads() == 0
    settings.radar_max_per_day = 8
    assert radar.check() == 1
    clock(10, 20)                      # 20 min < 30 min
    assert radar.check() == 0


def test_never_raises_and_never_posts(radar_env, state, fake_x):
    radar, writer, _ = radar_env

    def boom(*a):
        raise RuntimeError("writer exploded")

    writer.result = boom
    fake_x.user_posts_map = {"1": [post("8701", 5, 300)]}
    assert radar.check() == 0
    never_posted(fake_x)


def test_own_handle_is_ignored(settings, state, fake_x):
    settings.radar_accounts = ["sabislabi", "sama"]
    fake_x.ids_map = {"sabislabi": "42", "sama": "1"}
    radar = Radar(settings, state, fake_x, StubWriter(), StubApprovals())
    radar.check()
    assert all(c[1] != "42" for c in fake_x.calls if c[0] == "user_posts")
