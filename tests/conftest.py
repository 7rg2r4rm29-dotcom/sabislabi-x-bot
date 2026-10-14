from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot import utils  # noqa: E402
from bot.config import Settings, parse_post_slots, parse_thread_slots  # noqa: E402
from bot.costs import Budget  # noqa: E402
from bot.state import StateStore  # noqa: E402
from tests.fakes import FakeLLM, FakeTelegram, FakeX  # noqa: E402

PARIS = ZoneInfo("Europe/Paris")


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("TIMEZONE", "Europe/Paris")
    # Aucun module ne doit écrire dans le vrai dossier du projet pendant les tests.
    monkeypatch.setattr(utils, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(utils, "LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(utils, "MEDIA_DIR", tmp_path / "media")
    yield
    utils.set_clock(None)


@pytest.fixture
def clock():
    """clock(h, m, day=15) fige l'horloge au 2026-10-<day> h:m (Paris). Le 15/10/2026 est un jeudi."""
    def _set(h: int = 10, m: int = 0, day: int = 15, month: int = 10) -> datetime:
        dt = datetime(2026, month, day, h, m, tzinfo=PARIS)
        utils.set_clock(dt)
        return dt
    _set()
    return _set


@pytest.fixture
def settings():
    return Settings(
        x_api_key="k", x_api_secret="s", x_access_token="t", x_access_token_secret="ts",
        x_user_id="42", x_handle="sabislabi", anthropic_api_key="a",
        telegram_token="tg", telegram_chat_id="777", dry_run=False,
        post_slots=parse_post_slots("gm@08:00,tip@13:30,news@18:00"),
        thread_slots=parse_thread_slots("tue@15:00,sun@18:00"),
        publish_jitter_minutes=0,
        radar_accounts=["sama", "karpathy", "claudeai"],
        x_news_accounts=["OpenAI", "claudeai"],
        news_feeds=[],
        fallback_captions={"gm": ["gm. fallback one", "gm. fallback two"],
                           "tip": ["fallback tip"], "news": ["fallback news"]},
        evergreen_topics=["TUTORIAL — topic A", "BEGINNER — topic B", "STORY — topic C", "plain topic D"],
    )


@pytest.fixture
def state(tmp_path):
    return StateStore(tmp_path / "state" / "state.json")


@pytest.fixture
def budget(state, settings):
    return Budget(state, settings.x_budget_daily_usd, settings.x_budget_monthly_usd)


@pytest.fixture
def fake_llm():
    return FakeLLM()


@pytest.fixture
def fake_x():
    return FakeX()


@pytest.fixture
def fake_tg():
    return FakeTelegram()
