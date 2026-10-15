"""Point d'entrée (bot/main.py) : --busy-check, --check, validation, aiguillage."""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from bot import main as bmain
from bot import utils
from bot.config import Settings
from bot.utils import iso
from bot.x_api import XError
from tests.conftest import PARIS
from tests.fakes import FakeLLM, FakeTelegram, FakeX


NOW = datetime(2026, 10, 15, 10, 0, tzinfo=PARIS)


def _write_state(path, items: dict, **extra) -> None:
    data = {"version": 2, "items": items, "paused": False}
    data.update(extra)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _item(status: str, publish_at: datetime, kind: str = "tip", **meta) -> dict:
    return {"id": "a1", "kind": kind, "status": status, "publish_at": iso(publish_at), "meta": meta}


# ---------------------------------------------------------------------------
# --busy-check
# ---------------------------------------------------------------------------
class TestBusyCheck:
    def test_missing_file_is_free(self, tmp_path):
        code, msg = bmain.busy_check(tmp_path / "nope.json", now=NOW)
        assert code == 0 and "Libre" in msg

    def test_v1_state_is_free(self, tmp_path):
        p = tmp_path / "state.json"
        p.write_text(json.dumps({"replied_ids": ["1"], "pending_drafts": []}), encoding="utf-8")
        assert bmain.busy_check(p, now=NOW)[0] == 0

    def test_corrupt_state_is_free(self, tmp_path):
        p = tmp_path / "state.json"
        p.write_text("{not json", encoding="utf-8")
        assert bmain.busy_check(p, now=NOW)[0] == 0

    def test_publishing_is_busy(self, tmp_path):
        p = tmp_path / "state.json"
        _write_state(p, {"a1": _item("publishing", NOW - timedelta(minutes=1), "thread",
                                     publishing_at=iso(NOW - timedelta(minutes=1)))})
        code, msg = bmain.busy_check(p, now=NOW)
        assert code == 3 and "thread" in msg

    def test_stale_publishing_is_not_busy(self, tmp_path):
        p = tmp_path / "state.json"
        _write_state(p, {"a1": _item("publishing", NOW - timedelta(hours=3),
                                     publishing_at=iso(NOW - timedelta(hours=3)))})
        assert bmain.busy_check(p, now=NOW)[0] == 0

    @pytest.mark.parametrize("delta_min,expected", [(10, 3), (15, 3), (0, 3), (-5, 3), (16, 0), (120, 0),
                                                    (-120, 0)])
    def test_approved_window(self, tmp_path, delta_min, expected):
        p = tmp_path / "state.json"
        _write_state(p, {"a1": _item("approved", NOW + timedelta(minutes=delta_min))})
        assert bmain.busy_check(p, now=NOW)[0] == expected

    def test_paused_approved_is_not_busy(self, tmp_path):
        p = tmp_path / "state.json"
        _write_state(p, {"a1": _item("approved", NOW + timedelta(minutes=5))}, paused=True)
        assert bmain.busy_check(p, now=NOW)[0] == 0

    def test_awaiting_is_not_busy(self, tmp_path):
        p = tmp_path / "state.json"
        _write_state(p, {"a1": _item("awaiting", NOW + timedelta(minutes=5))})
        assert bmain.busy_check(p, now=NOW)[0] == 0

    def test_via_main_exit_codes_and_read_only(self, tmp_path, capsys, clock):
        clock(10, 0)
        p = tmp_path / "live" / "state.json"
        _write_state(p, {"a1": _item("approved", NOW + timedelta(minutes=5))})
        before = p.read_bytes()
        assert bmain.main(["--busy-check", "--state", str(p)]) == 3
        assert "Occupé" in capsys.readouterr().out
        assert p.read_bytes() == before                      # lecture seule
        assert not (p.parent / "state_v1_backup.json").exists()
        _write_state(p, {})
        assert bmain.main(["--busy-check", "--state", str(p)]) == 0

    def test_v1_state_untouched(self, tmp_path):
        p = tmp_path / "state.json"
        p.write_text(json.dumps({"replied_ids": ["1"]}), encoding="utf-8")
        before = p.read_bytes()
        assert bmain.main(["--busy-check", "--state", str(p)]) == 0
        assert p.read_bytes() == before


# ---------------------------------------------------------------------------
# --check
# ---------------------------------------------------------------------------
class FakeXMe(FakeX):
    def __init__(self, me=None, error=None) -> None:
        super().__init__()
        self._me = me if me is not None else {"id": "42", "username": "sabislabi",
                                               "public_metrics": {"followers_count": 1234}}
        self._error = error
        self.me_calls = 0

    def me(self):
        self.me_calls += 1
        if self._error:
            raise self._error
        return self._me


class SilentTelegram(FakeTelegram):
    def send(self, *a, **k):
        return None


@pytest.fixture
def secret_settings(settings):
    settings.x_api_key = "XKEY-SECRET-111"
    settings.x_api_secret = "XSEC-SECRET-222"
    settings.x_access_token = "XTOK-SECRET-333"
    settings.x_access_token_secret = "XTS-SECRET-444"
    settings.anthropic_api_key = "sk-ant-SECRET-555"
    settings.telegram_token = "123:TG-SECRET-666"
    settings.openai_api_key = ""
    return settings


def _check(settings, **kw):
    lines: list[str] = []
    kw.setdefault("telegram", FakeTelegram())
    kw.setdefault("x", FakeXMe())
    kw.setdefault("llm", FakeLLM({"check": {"ok": True}}))
    code = bmain.run_check(settings, out=lines.append, **kw)
    return code, "\n".join(lines), kw


class TestCheck:
    def test_all_ok(self, secret_settings):
        code, out, kw = _check(secret_settings)
        assert code == 0
        assert "@sabislabi" in out and "1234 abonnés" in out
        assert kw["telegram"].sent and "Test v2 OK" in kw["telegram"].sent[0]["text"]
        assert kw["llm"].labels() == ["check"]
        assert kw["x"].me_calls == 1

    def test_never_prints_secrets(self, secret_settings):
        _, out, _ = _check(secret_settings)
        for secret in ("XKEY-SECRET-111", "XSEC-SECRET-222", "XTOK-SECRET-333", "XTS-SECRET-444",
                       "sk-ant-SECRET-555", "TG-SECRET-666"):
            assert secret not in out
        assert "X_API_KEY défini" in out and "OPENAI_API_KEY manquant" in out

    def test_wrong_account(self, secret_settings):
        code, out, _ = _check(secret_settings, x=FakeXMe(me={"id": "42", "username": "someone_else"}))
        assert code == 1 and "@someone_else" in out

    def test_user_id_mismatch(self, secret_settings):
        code, out, _ = _check(secret_settings, x=FakeXMe(me={"id": "999", "username": "sabislabi"}))
        assert code == 1 and "X_USER_ID" in out

    def test_x_auth_error(self, secret_settings):
        code, out, _ = _check(secret_settings, x=FakeXMe(error=XError("auth", "Unauthorized", 401)))
        assert code == 1 and "clés refusées" in out

    def test_telegram_failure(self, secret_settings):
        code, out, _ = _check(secret_settings, telegram=SilentTelegram())
        assert code == 1 and "Telegram" in out

    def test_claude_failure(self, secret_settings):
        code, out, _ = _check(secret_settings, llm=FakeLLM({}))
        assert code == 1 and "Claude" in out

    def test_missing_x_keys_in_dry_run_is_only_a_warning(self, secret_settings):
        secret_settings.x_api_key = ""
        secret_settings.dry_run = True
        lines: list[str] = []
        code = bmain.run_check(secret_settings, telegram=FakeTelegram(), llm=FakeLLM({"check": {"ok": True}}),
                               out=lines.append)
        assert code == 0
        assert any("DRY_RUN" in line for line in lines)

    def test_missing_x_keys_for_real_is_an_error(self, secret_settings):
        secret_settings.x_api_key = ""
        secret_settings.dry_run = False
        lines: list[str] = []
        code = bmain.run_check(secret_settings, telegram=FakeTelegram(), llm=FakeLLM({"check": {"ok": True}}),
                               out=lines.append)
        assert code == 1


# ---------------------------------------------------------------------------
# main() : validation et aiguillage
# ---------------------------------------------------------------------------
@pytest.fixture
def quiet_main(monkeypatch):
    monkeypatch.setattr(bmain, "load_env", lambda: None)
    monkeypatch.setattr(utils, "setup_logging", lambda *a, **k: None)


class TestMain:
    def test_blocking_config_errors_exit_2(self, quiet_main, monkeypatch, capsys):
        import bot.config as cfg
        monkeypatch.setattr(cfg, "load_settings", lambda: Settings(anthropic_api_key="", x_handle=""))
        assert bmain.main(["--check"]) == 2
        out = capsys.readouterr().out
        assert "ANTHROPIC_API_KEY" in out and "X_HANDLE" in out

    def test_invalid_config_value_exit_2(self, quiet_main, monkeypatch, capsys):
        import bot.config as cfg

        def bad():
            raise cfg.ConfigError("POST_SLOTS : type inconnu 'foo'")
        monkeypatch.setattr(cfg, "load_settings", bad)
        assert bmain.main([]) == 2
        assert "POST_SLOTS" in capsys.readouterr().out

    def test_dispatch(self, quiet_main, monkeypatch, settings):
        import bot.config as cfg
        monkeypatch.setattr(cfg, "load_settings", lambda: settings)
        seen = []
        monkeypatch.setattr(bmain, "run_check", lambda s: seen.append("check") or 0)
        monkeypatch.setattr(bmain, "run_once", lambda s, job, state: seen.append(("once", job, state)) or 0)
        monkeypatch.setattr(bmain, "run_bot", lambda s, state: seen.append(("run", state)) or 0)
        assert bmain.main(["--check"]) == 0
        assert bmain.main(["--once", "radar", "--state", "/tmp/s.json"]) == 0
        assert bmain.main(["--log-level", "DEBUG"]) == 0
        assert seen == ["check", ("once", "radar", "/tmp/s.json"), ("run", None)]

    def test_unknown_job_rejected(self):
        with pytest.raises(SystemExit) as e:
            bmain.parse_args(["--once", "tweet"])
        assert e.value.code == 2

    def test_run_once_refuses_when_service_holds_the_lock(self, settings, tmp_path):
        p = tmp_path / "state" / "state.json"
        held = bmain.acquire_lock(p)
        assert held
        lines: list[str] = []
        assert bmain.run_once(settings, "plan", p, out=lines.append) == 1
        assert "systemctl stop" in lines[0]
        assert bmain.run_bot(settings, p) == 1
        held.close()

    def test_run_once_dispatches_to_bot(self, settings, tmp_path, monkeypatch):
        import bot.scheduler as sched
        calls = []

        class FakeBot:
            def __init__(self, s, state=None):
                self.state = state

            def plan_slots(self):
                calls.append("plan")
                return 1

            def maybe_reactive(self, force=False):
                calls.append(("reactive", force))
                return 0
        monkeypatch.setattr(sched, "Bot", FakeBot)
        lines: list[str] = []
        assert bmain.run_once(settings, "plan", tmp_path / "s1" / "state.json", out=lines.append) == 0
        assert bmain.run_once(settings, "reactive", tmp_path / "s2" / "state.json", out=lines.append) == 0
        assert calls == ["plan", ("reactive", True)]
        assert lines[0].startswith("plan : 1")

    def test_config_summary_lists_slots(self, settings):
        txt = "\n".join(bmain.config_summary(settings))
        assert "gm 08:00" in txt and "mar. 15:00" in txt and "dim. 18:00" in txt
