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


@pytest.fixture(autouse=True)
def _no_real_browser(monkeypatch):
    """--check fait une vraie capture d'essai (visuals.self_test) : ici, aucun navigateur."""
    from bot import visuals
    monkeypatch.setattr(visuals, "self_test", lambda: (True, ""))


def _check(settings, **kw):
    lines: list[str] = []
    kw.setdefault("telegram", FakeTelegram())
    kw.setdefault("x", FakeXMe())
    kw.setdefault("llm", FakeLLM({"check": {"ok": True}}))
    kw.setdefault("videos", lambda: (True, ""))      # le vrai découpage a ses propres tests
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

    def test_photo_preparation_works(self, secret_settings):
        code, out, _ = _check(secret_settings)
        assert code == 0 and "✅ Images : la mise au propre des photos fonctionne" in out
        assert "capture" not in out.lower()                     # plus de captures d'écran
        assert "carte" not in out.lower()                       # plus de cartes dessinées
        assert out.splitlines()[-1].startswith("PARTS telegram=ok x=ok claude=ok images=ok videos=")

    def test_check_really_tries_a_photo(self, secret_settings, monkeypatch):
        """Par défaut, --check passe une photo d'essai par la mise au propre de bot/visuals.py."""
        from bot import visuals
        calls: list[int] = []
        monkeypatch.setattr(visuals, "self_test",
                            lambda: calls.append(1) or (False, "Pillow n'est pas installé"))
        code, out, _ = _check(secret_settings)
        assert calls == [1]
        assert code == 0
        assert "⚠️ Images : la mise au propre des photos ne marche pas (Pillow n'est pas installé)" in out
        assert out.splitlines()[-1].startswith("PARTS telegram=ok x=ok claude=ok images=bad videos=")

    def test_images_impossible_is_a_warning_not_a_failure(self, secret_settings):
        """Sans photo possible, le bot publie quand même (texte seul) : on prévient, on ne bloque pas."""
        code, out, _ = _check(secret_settings, images=lambda: (False, "cannot write mode RGBA as JPEG"))
        assert code == 0
        assert "⚠️ Images : la mise au propre des photos ne marche pas (cannot write mode RGBA as JPEG)" in out
        assert "partiraient SANS image" in out
        assert "pip install Pillow" in out and "tout est OK" in out
        assert out.splitlines()[-1].startswith("PARTS telegram=ok x=ok claude=ok images=bad videos=")

    def test_video_clips_are_checked_too_and_never_block(self, secret_settings):
        code, out, _ = _check(secret_settings, images=lambda: (True, ""), videos=lambda: (True, ""))
        assert code == 0 and "✅ Vidéos : le découpage des extraits fonctionne" in out
        assert out.splitlines()[-1] == "PARTS telegram=ok x=ok claude=ok images=ok videos=ok"
        code, out, _ = _check(secret_settings, images=lambda: (True, ""), videos=lambda: (False, "ffmpeg absent"))
        assert code == 0 and "⚠️ Vidéos : le découpage des extraits ne marche pas (ffmpeg absent)" in out
        assert "garderaient la photo" in out and "pip install imageio-ffmpeg" in out
        assert out.splitlines()[-1] == "PARTS telegram=ok x=ok claude=ok images=ok videos=bad"

    def test_the_voice_is_checked_when_it_is_on_and_never_blocks(self, secret_settings):
        code, out, _ = _check(secret_settings, images=lambda: (True, ""), voice=lambda s: pytest.fail("coupée"))
        assert code == 0 and "Voix off" not in out                         # voix coupée : rien à vérifier
        secret_settings.explainer_voice_enabled = True
        code, out, _ = _check(secret_settings, images=lambda: (True, ""), voice=lambda s: (True, "af_heart, 1.2 s"))
        assert code == 0 and "✅ Voix off : prête (af_heart, 1.2 s)" in out
        code, out, _ = _check(secret_settings, images=lambda: (True, ""),
                              voice=lambda s: (False, "paquet kokoro-onnx absent"))
        assert code == 0 and "⚠️ Voix off : paquet kokoro-onnx absent. Les vidéos explicatives partiraient sans voix." in out
        assert out.splitlines()[-1] == "PARTS telegram=ok x=ok claude=ok images=ok videos=ok"   # l'installateur lit ça

    def test_check_really_cuts_a_test_clip(self, secret_settings, monkeypatch):
        from bot import visuals
        calls: list[int] = []
        monkeypatch.setattr(visuals, "video_self_test", lambda: calls.append(1) or (True, ""))
        _check(secret_settings, images=lambda: (True, ""), videos=None)
        assert calls == [1]

        def boom():
            raise RuntimeError("no display")
        code, out, _ = _check(secret_settings, images=boom)
        assert code == 0 and "(RuntimeError)" in out

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
        monkeypatch.setattr(bmain, "run_preview", lambda s, kinds, state: seen.append(("preview", kinds)) or 0)
        assert bmain.main(["--check"]) == 0
        assert bmain.main(["--once", "radar", "--state", "/tmp/s.json"]) == 0
        assert bmain.main(["--log-level", "DEBUG"]) == 0
        assert bmain.main(["--preview"]) == 0
        assert bmain.main(["--preview", "thread"]) == 0
        assert seen == ["check", ("once", "radar", "/tmp/s.json"), ("run", None),
                        ("preview", "tip,news"), ("preview", "thread")]

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

    def test_config_summary_speaks_of_photos(self, settings):
        txt = "\n".join(bmain.config_summary(settings))
        assert "Images : vidéo oui" in txt and "photo oui (pages officielles et articles)" in txt
        assert "2e image (logo, drapeau) oui" in txt
        assert "photos IA non" in txt and "carte" not in txt and "capture" not in txt
        settings.photos_enabled = settings.videos_enabled = settings.second_image_enabled = False
        settings.press_media_enabled = False
        txt = "\n".join(bmain.config_summary(settings))
        assert "vidéo non" in txt and "photo non" in txt and "(pages officielles seulement)" in txt
        assert "2e image (logo, drapeau) non" in txt

    def test_config_summary_shows_the_quality_settings(self, settings):
        txt = "\n".join(bmain.config_summary(settings))
        assert "effort medium (threads : high)" in txt and "versions par post" not in txt
        assert "actu regardée toutes les 30 min, Claude interrogé 6 fois/jour au plus" in txt
        settings.post_versions, settings.llm_effort_post = 3, "high"
        settings.reactive_check_every_min, settings.reactive_max_checks_per_day = 15, 30
        txt = "\n".join(bmain.config_summary(settings))
        assert "effort medium (threads : high · posts : high) · 3 versions par post, la meilleure est gardée" in txt
        assert "actu regardée toutes les 15 min, Claude interrogé 30 fois/jour au plus)" in txt
        settings.reactive_slot_gap_min = 10
        txt = "\n".join(bmain.config_summary(settings))
        assert "Claude interrogé 30 fois/jour au plus · à 10 min au moins d’un post prévu)" in txt

    def test_config_summary_shows_the_explainer_videos(self, settings):
        from bot.config import parse_explainer_slots
        assert "vidéos explicatives : aucune ·" in "\n".join(bmain.config_summary(settings))
        settings.explainer_slots = parse_explainer_slots("mon@14:30,wed@14:30,fri@14:30")
        txt = "\n".join(bmain.config_summary(settings))
        assert ("vidéos explicatives : lun. 14:30, mer. 14:30, ven. 14:30 (à la place du post prévu le plus "
                "proche)") in txt

    def test_config_summary_shows_the_voice(self, settings):
        from bot.config import parse_explainer_slots
        settings.explainer_slots = parse_explainer_slots("mon@14:30")
        assert "voix off" not in "\n".join(bmain.config_summary(settings))
        settings.explainer_voice_enabled = True
        assert "(à la place du post prévu le plus proche), voix off af_heart" in "\n".join(
            bmain.config_summary(settings))

    def test_config_summary_shows_how_formats_are_chosen(self, settings):
        assert "Formats : jours fixes (FORMAT_AUTO=false)" in bmain.config_summary(settings)
        settings.format_auto = True
        txt = "\n".join(bmain.config_summary(settings))
        assert "Formats : choisis chaque jour d'après les résultats" in txt and "jours fixes" not in txt

    def test_config_summary_shows_what_works_and_trends(self, settings):
        txt = "\n".join(bmain.config_summary(settings))
        assert ("tendances du jour : oui (Google, Wikipédia, Hacker News, annonces X déjà lues) · "
                "veille du marché : non · 3 posts prévus sur 4 suivent ce qui marche") in txt
        settings.market_watch_enabled = True
        assert "veille du marché : 5 comptes, une fois par jour" in "\n".join(bmain.config_summary(settings))
        settings.trends_enabled = False
        assert "tendances du jour : non" in "\n".join(bmain.config_summary(settings))

    def test_config_summary_shows_the_claude_cap(self, settings):
        assert "Claude : 35 $/mois au plus" in "\n".join(bmain.config_summary(settings))
        settings.claude_budget_monthly_usd = 0
        assert "Claude : sans plafond" in "\n".join(bmain.config_summary(settings))

    def test_manual_runs_use_a_claude_client_that_counts_in_the_monthly_cap(self, settings, tmp_path,
                                                                           monkeypatch):
        """--preview et --check créent leur client Claude en mode « essai à la main »."""
        from bot import llm as llm_mod
        from bot.models import Draft
        made: list[bool] = []

        class Recorder:
            def __init__(self, settings, state=None, client=None, manual=False):
                made.append(manual)

            def json(self, *a, **k):
                return {"ok": True}
        monkeypatch.setattr(llm_mod, "LLM", Recorder)
        bmain.run_preview(settings, "tip", tmp_path / "s.json", news=PreviewNews(), telegram=None,
                          writer=PreviewWriter({"tip": Draft(kind="tip", texts=["A tip."])}),
                          prepare_visual=lambda *a: (None, None, None), out=lambda line: None)
        bmain.run_check(settings, telegram=FakeTelegram(), x=FakeXMe(), images=lambda: (True, ""),
                        videos=lambda: (True, ""), out=lambda line: None)
        assert made == [True, True]

    def test_post_labels_follow_the_new_line(self):
        assert bmain.KIND_FR["tip"] == "le futur arrive"
        assert bmain.KIND_FR["news"] == "l'histoire du jour"
        assert bmain.KIND_FR["reactive"] == "ça vient de tomber"



def test_report_shows_recent_posts_without_secrets(tmp_path, capsys):
    from bot import main as m
    state = {"version": 2, "items": {
        "a": {"id": "a", "kind": "gm", "status": "published", "created_at": "2026-10-28T06:30:00+01:00",
              "texts": ["gm. one small thing\ntoday"], "meta": {"from_fallback": True, "reason": "secours"}},
        "b": {"id": "b", "kind": "news", "status": "failed", "created_at": "2026-10-28T16:30:00+01:00",
              "texts": ["voice mode is free now"], "last_error": "Plus de crédits API X",
              "meta": {"reason": "actu du jour", "sources": [{"title": "OpenAI makes voice free", "url": "u"}]}},
        "c": {"id": "c", "kind": "comment_reply", "status": "expired", "created_at": "2026-10-28T10:00:00+01:00",
              "texts": ["thanks"], "meta": {}}},
        "costs": {"days": {"2026-10-28": {"total": 1.25}}}, "llm_usage": {"days": {"2026-10-28": {"usd": 0.5}}}}
    sp = tmp_path / "state.json"
    sp.write_text(json.dumps(state))
    log = tmp_path / "bot.log"
    log.write_text("2026-10-28 07:58:43 | ERROR   | xbot.publisher | Item 23ea (gm) en échec : crédits\n")
    out = m.report(sp, log)
    assert "gm [published] (TEXTE DE SECOURS)" in out and "gm. one small thing / today" in out
    assert "OpenAI makes voice free" in out and "Plus de crédits API X" in out
    assert "Réponses aux commentaires : expired 1" in out
    assert "en échec : crédits" in out
    assert "aucun post mesuré" in out and "Abonnés : aucun relevé" in out
    assert "chiffres : pas encore relevés" in out          # publié, mais pas encore mesuré
    assert m.main(["--report", "--state", str(sp)]) == 0


def test_report_shows_numbers_screenshots_and_followers(tmp_path, clock):
    """Ce que les posts ont vraiment donné : c'est ce qui permet de corriger le tir."""
    from bot import main as m
    clock(10, 0, day=30)
    state = {"version": 2, "items": {
        "a": {"id": "a", "kind": "tip", "status": "published", "created_at": "2026-10-28T14:00:00+01:00",
              "texts": ["A robot just folded a whole basket of laundry, alone."], "image_kind": "shot",
              "meta": {"reason": "le robot", "hook": "IT CAN DO WHAT?"}},
        # Item d'avant novembre 2026 : sa vieille « carte » n'est plus affichée nulle part.
        "b": {"id": "b", "kind": "news", "status": "published", "created_at": "2026-10-29T19:00:00+01:00",
              "texts": ["ChatGPT can now talk to you. For free."], "image_kind": "phone",
              "meta": {"card": {"type": "phone", "title": "Voice is free"}}},
        "c": {"id": "c", "kind": "tip", "status": "approved", "created_at": "2026-10-30T09:00:00+01:00",
              "texts": ["Scheduled, not out yet."], "meta": {}}},
        "published": [
            {"tweet_id": "1", "kind": "tip", "item_id": "a", "created_at": "2026-10-28T14:02:00+01:00",
             "metrics": {"impressions": 812, "likes": 9, "replies": 2, "reposts": 1, "quotes": 1, "bookmarks": 14}},
            {"tweet_id": "2", "kind": "news", "item_id": "b", "created_at": "2026-10-29T19:01:00+01:00",
             "metrics": None},
            {"tweet_id": "0", "kind": "tip", "item_id": "old", "created_at": "2026-08-01T14:00:00+02:00",
             "metrics": {"impressions": 99999, "likes": 500}}],
        "followers": [{"date": "2026-10-20", "count": 4520}, {"date": "2026-10-29", "count": 4498},
                      {"date": "2026-10-30", "count": 4501}]}
    sp = tmp_path / "state.json"
    sp.write_text(json.dumps(state))
    out = m.report(sp, tmp_path / "missing.log")
    assert "tip [published] (image shot)" in out and "raison : le robot" in out
    # L'heure affichée est celle de la publication, pas celle de la rédaction.
    assert "- publié 2026-10-28 14:02 tip [published]" in out
    assert "- rédigé 2026-10-30 09:00 tip [approved]" in out
    assert "carte :" not in out and "Voice is free" not in out
    assert "chiffres : 812 vues · 9 j'aime · 2 réponse(s) · 2 repartage(s) · 14 signet(s)" in out
    assert out.count("chiffres :") == 2 and "chiffres : pas encore relevés" in out   # rien pour le post programmé
    assert "30 derniers jours : 1 posts mesurés, 812 vues en moyenne, 27 interactions au total" in out
    assert "Abonnés : 4501 le 2026-10-30 (-19 depuis le 2026-10-20)" in out
    assert "10-29=4498" in out


# ---------------------------------------------------------------------------
# --preview : de vrais posts avec leur capture d'écran, sans rien publier
# ---------------------------------------------------------------------------
class PreviewWriter:
    def __init__(self, drafts: dict) -> None:
        self.drafts = drafts
        self.calls: list[tuple] = []
        self.seen: list[list] = []          # les actus données à chaque rédaction

    def draft_post(self, kind, news, notes, perf):
        self.perfs = getattr(self, "perfs", []) + [perf]
        self.calls.append((kind, len(news), tuple(notes)))
        self.seen.append(list(news))
        return self.drafts.get(kind)

    def draft_thread(self, news, notes, perf):
        self.calls.append(("thread", len(news), tuple(notes)))
        self.seen.append(list(news))
        return self.drafts.get("thread")

    def draft_reactive(self, news, notes, perf):
        self.calls.append(("reactive", len(news), tuple(notes)))
        self.seen.append(list(news))
        return self.drafts.get("reactive")


class PreviewNews:
    """`max_age_hours=None` : fenêtre pas précisée (celle par défaut du NewsHub, l'actu fraîche)."""

    def __init__(self, items=None, fail=False) -> None:
        self.items = items or []
        self.fail = fail
        self.calls: list[dict] = []

    def collect(self, max_age_hours=None, use_x: bool = True):
        self.calls.append({"max_age_hours": max_age_hours, "use_x": use_x})
        if self.fail:
            raise RuntimeError("network down")
        return list(self.items)


class PickingNews(PreviewNews):
    """Avec top_for, comme le vrai NewsHub : on note le type de sélection demandé."""

    def __init__(self, items=None, fail=False) -> None:
        super().__init__(items, fail)
        self.picks: list[tuple[str, int]] = []

    def top_for(self, kind, items, n):
        self.picks.append((kind, n))
        return list(items)[:n]


SHOT_URL = "https://www.theverge.com/2026/10/15/robot-laundry"


def _story(i: int, title: str):
    from bot.models import NewsItem
    return NewsItem(id=f"s{i}", title=title, summary="", url=f"https://news.example.com/{i}", source="Example",
                    published=utils.now_local() - timedelta(hours=2), official=True)


class TestPreview:
    def _run(self, settings, tmp_path, drafts, kinds="tip,news", **kw):
        lines: list[str] = []
        img = tmp_path / "photo.jpg"
        img.write_bytes(b"\xff\xd8\xff fake jpeg")
        visuals_seen: list[tuple] = []

        def fake_visual(s, draft, writer, tag):
            visuals_seen.append((draft.kind, tag))
            return (str(img), "photo") if getattr(draft, "visual", None) else (None, None)
        tg = kw.pop("telegram", FakeTelegram())
        writer = PreviewWriter(drafts)
        news = kw.pop("news", PreviewNews(["n1", "n2"]))
        code = bmain.run_preview(settings, kinds, kw.pop("state_path", tmp_path / "state" / "state.json"),
                                 llm=object(), news=news, writer=writer, telegram=tg,
                                 prepare_visual=fake_visual, out=lines.append)
        return code, "\n".join(lines), tg, writer, news, visuals_seen

    def test_preview_gives_claude_the_same_performance_notes_as_the_live_bot(self, settings, tmp_path, clock,
                                                                             monkeypatch):
        from bot import analytics
        monkeypatch.setattr(analytics.Analytics, "performance_notes", lambda self: "Last 30 days, 6 posts: …")
        _c, _o, _t, writer, _n, _v = self._run(settings, tmp_path, {}, kinds="tip")
        assert writer.perfs == ["Last 30 days, 6 posts: …"]

    def test_preview_makes_the_explainer_video(self, settings, tmp_path, clock):
        from bot.models import Draft
        lines: list[str] = []
        video = tmp_path / "explainer.mp4"
        video.write_bytes(b"mp4")
        made: list[tuple] = []

        class VideoPreviewWriter(PreviewWriter):
            def draft_explainer(self, news, notes, perf):
                self.calls.append(("explainer", len(news), tuple(notes)))
                return Draft(kind="explainer", texts=["Your car can now tell you're not watching the road."],
                             video_plan={"title": "How?", "scenes": [], "ending": "?"})

        def make(s, draft, writer, tag):
            made.append((draft.kind, tag))
            return str(video), "video", None

        tg = FakeTelegram()
        news = PreviewNews(["n1", "n2"])
        writer = VideoPreviewWriter({})
        code = bmain.run_preview(settings, "explainer", tmp_path / "state" / "state.json", llm=object(), news=news,
                                 writer=writer, telegram=tg, prepare_visual=lambda *a: pytest.fail("photo"),
                                 prepare_explainer=make, out=lines.append)
        out = "\n".join(lines)
        assert code == 0 and writer.calls == [("explainer", 2, ())]
        assert made == [("explainer", "apercu_explainer")]
        assert "--- vidéo explicative ---" in out and "Your car can now tell" in out
        assert tg.photos and tg.photos[0].get("video") is True and tg.photos[0]["path"] == str(video)
        assert news.calls[0]["max_age_hours"] is None and news.calls[1]["max_age_hours"] == 96

    def test_preview_shows_the_editor_what_works_and_the_trends(self, settings, tmp_path, clock, monkeypatch):
        from bot import analytics, trends
        monkeypatch.setattr(analytics.Analytics, "editor_notes", lambda self, explore=False: "WHAT WORKS: games")
        monkeypatch.setattr(analytics.Analytics, "style_notes", lambda self, explore=False: "OUR BEST RECENT POSTS")
        monkeypatch.setattr(trends.TrendWatch, "block", lambda self: "TRENDING TODAY — GTA VI")
        contexts: list[str] = []
        PreviewWriter.set_editor_context = lambda self, text: contexts.append(text)
        try:
            _c, _o, _t, writer, _n, _v = self._run(settings, tmp_path, {}, kinds="tip")
        finally:
            del PreviewWriter.set_editor_context
        assert contexts == ["WHAT WORKS: games\n\nTRENDING TODAY — GTA VI"]
        assert writer.perfs[0].endswith("OUR BEST RECENT POSTS")

    def test_drafts_are_shown_and_sent_but_nothing_is_published(self, settings, tmp_path, clock):
        from bot.models import Draft, Visual
        from bot.state import StateStore
        real = tmp_path / "state" / "state.json"
        store = StateStore(real)
        store.add_note("I tested voice mode while cooking")
        store.remember_text("tip", "an older post")
        before = real.read_text()
        drafts = {"tip": Draft(kind="tip", texts=["A robot just folded a whole basket of laundry, alone."],
                               visual=Visual("photo", SHOT_URL), reason="le robot", hook="IT CAN DO WHAT?"),
                  "news": Draft(kind="news", texts=["Which one would you trust?"],
                                poll={"options": ["A robot", "A human"], "duration_minutes": 1440})}
        code, out, tg, writer, news, visuals_seen = self._run(settings, tmp_path, drafts)
        assert code == 0
        assert "RIEN n'est publié" in out and "Aperçu terminé : 2/2 post(s) envoyé(s) sur Telegram" in out
        assert "--- le futur arrive ---" in out and "--- l'histoire du jour ---" in out
        assert "A robot just folded" in out and "pourquoi : le robot" in out
        assert "[sondage] A robot · A human" in out
        assert f"photo : {tmp_path / 'photo.jpg'} (photo)" in out
        assert out.count("photo : aucune (le post partirait en texte seul)") == 1     # le sondage
        assert "carte" not in out                                      # plus de cartes dessinées
        assert all(c["use_x"] is False for c in news.calls)            # jamais de lecture payante de X
        assert writer.calls[0] == ("tip", 2, ("I tested voice mode while cooking",))
        assert visuals_seen == [("tip", "apercu_tip"), ("news", "apercu_news")]
        assert len(tg.photos) == 1 and "APERÇU" in tg.photos[0]["caption"]
        assert tg.photos[0]["path"] == str(tmp_path / "photo.jpg")
        assert len(tg.sent) == 2 and all("rien n'est publié" in m["text"] for m in tg.sent)
        assert "📊 A robot · A human" in tg.sent[1]["text"]
        assert real.read_text() == before                              # l'état du bot en service n'a pas bougé

    def test_reads_the_same_news_windows_as_the_live_bot(self, settings, tmp_path, clock):
        """Comme le bot en service : 4 jours d'actu pour « le futur arrive » et les threads,
        l'actu fraîche pour l'histoire du jour et le réactif, et la même sélection (top_for)."""
        from bot.scheduler import FUTURE_NEWS_HOURS, NEWS_PER_POST, NEWS_PER_THREAD
        clock(10, 0, day=15)                                           # jeudi : thread « normal »
        news = PickingNews(["n1", "n2", "n3"])
        code, _out, _tg, writer, _n, _v = self._run(settings, tmp_path, {}, kinds="tip,news,thread,reactive",
                                                    news=news)
        assert code == 1                                               # rien de rédigé ici
        assert news.calls == [{"max_age_hours": None, "use_x": False},                 # l'actu fraîche
                              {"max_age_hours": FUTURE_NEWS_HOURS, "use_x": False},    # tip
                              {"max_age_hours": FUTURE_NEWS_HOURS, "use_x": False}]    # thread
        assert news.picks == [("tip", NEWS_PER_POST), ("news", NEWS_PER_POST),
                              ("thread", NEWS_PER_THREAD), ("reactive", NEWS_PER_POST)]
        assert [c[0] for c in writer.calls] == ["tip", "news", "thread", "reactive"]

    @pytest.mark.parametrize("day", [16, 17, 18])                     # vendredi, samedi, dimanche
    def test_end_of_week_thread_reads_seven_days_ranked_by_appeal(self, settings, tmp_path, clock, day):
        clock(10, 0, day=day)
        news = PickingNews(["n1", "n2"])
        self._run(settings, tmp_path, {}, kinds="thread", news=news)
        assert news.calls[-1] == {"max_age_hours": 168, "use_x": False}
        assert news.picks == [("weekly", 120)]

    def test_thread_and_breaking_news(self, settings, tmp_path, clock):
        from bot.models import Draft, Visual
        drafts = {"thread": Draft(kind="thread", texts=["Hook tweet", "Second tweet"],
                                  visual=Visual("photo", SHOT_URL))}
        code, out, tg, writer, _news, _ = self._run(settings, tmp_path, drafts, kinds="thread, reactive")
        assert code == 0 and "1/ Hook tweet" in out and "2/ Second tweet" in out
        assert "--- ça vient de tomber : rien, dans l'actu fraîche, ne mérite un post à chaud" in out
        assert "Aperçu terminé : 1/2" in out
        assert [c[0] for c in writer.calls] == ["thread", "reactive"]

    def test_breaking_news_preview_offers_the_editor_what_the_live_bot_does(self, settings, tmp_path, clock):
        insider = _story(1, "OpenAI launches a new API for developers")
        everyday = _story(2, "A humanoid robot folds laundry in a real home")
        _c, _o, _t, writer, _n, _v = self._run(settings, tmp_path, {}, kinds="reactive",
                                               news=PreviewNews([insider, everyday]))
        assert [it.id for it in writer.seen[0]] == ["s2"]

    def test_nothing_drafted_is_a_failure(self, settings, tmp_path):
        code, out, tg, _w, _n, _ = self._run(settings, tmp_path, {})
        assert code == 1 and "Claude n'a rien proposé de publiable" in out and tg.sent == [] and tg.photos == []

    def test_works_without_news_state_or_telegram(self, settings, tmp_path):
        from bot.models import Draft
        drafts = {"tip": Draft(kind="tip", texts=["A tip."])}
        code, out, _tg, writer, _n, _ = self._run(settings, tmp_path, drafts, kinds="tip",
                                                 news=PreviewNews(fail=True), telegram=None,
                                                 state_path=tmp_path / "nowhere" / "state.json")
        assert code == 0 and "0 actus lues" in out and writer.calls == [("tip", 0, ())]
        assert "photo : aucune (le post partirait en texte seul)" in out
        assert not (tmp_path / "nowhere").exists()

    def test_kinds_parsing(self, settings, tmp_path):
        assert bmain.preview_kinds("tip,news") == ["tip", "news"]
        assert bmain.preview_kinds(" Thread ; tip tip,gm,unknown") == ["thread", "tip"]
        assert bmain.preview_kinds("") == [] and bmain.preview_kinds(None) == []
        lines: list[str] = []
        assert bmain.run_preview(settings, "gm", tmp_path / "s.json", out=lines.append) == 2
        assert "Types inconnus" in lines[0]
