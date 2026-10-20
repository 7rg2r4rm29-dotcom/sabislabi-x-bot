"""Tests des scripts d'installation (deploy/*.sh), dans des dossiers jetables.

Aucun réseau, aucun systemd : XBOT_SERVICE="" coupe la gestion du service,
XBOT_SKIP_PIP / XBOT_SKIP_TESTS / XBOT_SKIP_CHECK sautent les étapes lourdes,
et un faux `ssh` exécute les commandes « distantes » en local.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "deploy"
SCRIPTS = sorted(DEPLOY.glob("*.sh"))
SECRET = "sk-ant-SECRET-never-print-me-123"
V1_ENV = (f"ANTHROPIC_API_KEY={SECRET}\nTELEGRAM_BOT_TOKEN=123:tok-SECRET\nTELEGRAM_CHAT_ID=777\n"
          "X_HANDLE=sabislabi\nTREND_LLM_MODEL=claude-sonnet-5\nPOST_SLOTS=gm@09:00\n")
STAGED = ("bot", "deploy", "tests", "config/settings.json", "config/content.json", "requirements.txt",
          "README.md", "DESIGN.md")

pytestmark = pytest.mark.skipif(shutil.which("bash") is None or shutil.which("flock") is None,
                                reason="bash et flock nécessaires")


def run(script: str | Path, *args: str, env: dict | None = None, stdin: str = "",
        home: Path | None = None) -> subprocess.CompletedProcess:
    base = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8",
            "HOME": str(home or Path.home())}
    base.update(env or {})
    return subprocess.run(["bash", str(script), *args], input=stdin, capture_output=True, text=True,
                          env=base, timeout=120)


def out(res: subprocess.CompletedProcess) -> str:
    return res.stdout + res.stderr


def make_v1_app(app: Path) -> Path:
    """Dossier du bot tel que la v1 l'a laissé sur le serveur."""
    (app / "bot").mkdir(parents=True)
    (app / "bot" / "__init__.py").write_text("")
    (app / "bot" / "main.py").write_text("print('v1')\n")
    (app / "bot" / "x_browser.py").write_text("# v1\n")
    (app / "deploy").mkdir()
    (app / "deploy" / "relaunch_env.sh").write_text("echo v1\n")
    (app / "config").mkdir()
    (app / "config" / "settings.json").write_text('{"v1": true}')
    env = app / "config" / ".env"
    env.write_text(V1_ENV)
    env.chmod(0o600)
    (app / "state").mkdir()
    (app / "state" / "state.json").write_text('{"posted": []}')
    (app / "auth").mkdir()
    (app / "auth" / "storage_state.json").write_text("{}")
    (app / ".venv" / "bin").mkdir(parents=True)
    (app / ".venv" / "bin" / "python").symlink_to(sys.executable)
    return app


def make_stage(stage: Path) -> Path:
    for p in STAGED:
        src, dst = ROOT / p, stage / p
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__"))
        elif src.exists():
            shutil.copy2(src, dst)
    return stage


def deploy_env(app: Path, **extra: str) -> dict:
    env = {"XBOT_APP_DIR": str(app), "XBOT_SERVICE": "", "XBOT_SKIP_PIP": "1", "XBOT_SKIP_TESTS": "1",
           "XBOT_SKIP_CHECK": "1", "XBOT_CLOCK": "1000 4"}
    env.update(extra)
    return env


def env_lines(app: Path) -> list[str]:
    return (app / "config" / ".env").read_text().splitlines()


# ---------------------------------------------------------------------------
def test_scripts_are_valid_bash():
    assert {p.name for p in SCRIPTS} >= {"deploy.sh", "push_from_mac.sh", "set_secrets.sh", "migrate_env_v2.sh",
                                         "restore_backup.sh", "cleanup_v1.sh"}
    for script in SCRIPTS:
        res = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
        assert res.returncode == 0, f"{script.name}: {res.stderr}"


# ---------------------------------------------------------------------------
# migrate_env_v2.sh
# ---------------------------------------------------------------------------
def test_migrate_adds_v2_settings_without_overwriting(tmp_path):
    app = tmp_path / "app"
    (app / "config").mkdir(parents=True)
    env = app / "config" / ".env"
    env.write_text(V1_ENV.rstrip("\n"))  # pas de saut de ligne final
    res = run(DEPLOY / "migrate_env_v2.sh", env={"XBOT_APP_DIR": str(app)})
    assert res.returncode == 0, out(res)
    assert "REGLAGES_OK (ajouté :" in res.stdout
    assert SECRET not in out(res) and "tok-SECRET" not in out(res)
    lines = env_lines(app)
    assert "POST_SLOTS=gm@09:00" in lines                    # réglage perso conservé
    assert sum(line.startswith("POST_SLOTS=") for line in lines) == 1
    assert "TELEGRAM_CHAT_ID=777" in lines                   # dernière ligne d'origine intacte
    assert "X_HANDLE=sabislabi" in lines and sum(line.startswith("X_HANDLE=") for line in lines) == 1
    assert {"DRY_RUN=false", "APPROVAL_MODE=telegram", "LLM_MODEL=claude-opus-5-5",
            "X_BUDGET_MONTHLY_USD=30", "TIMEZONE=Europe/Paris"} <= set(lines)
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    assert (app / "config" / ".env.avant_v2").read_text() == V1_ENV.rstrip("\n")

    before = env.read_text()
    res2 = run(DEPLOY / "migrate_env_v2.sh", env={"XBOT_APP_DIR": str(app)})
    assert res2.returncode == 0 and "ajouté" not in res2.stdout
    assert env.read_text() == before                          # idempotent
    assert (app / "config" / ".env.avant_v2").read_text() == V1_ENV.rstrip("\n")


# ---------------------------------------------------------------------------
# set_secrets.sh
# ---------------------------------------------------------------------------
def test_set_secrets_from_stdin(tmp_path):
    app = tmp_path / "app"
    (app / "config").mkdir(parents=True)
    env = app / "config" / ".env"
    env.write_text("X_API_KEY=old\nexport ANTHROPIC_API_KEY=old-anthropic\nTELEGRAM_CHAT_ID=777\n")
    weird = 'ab"c\\d e'
    answers = ["new-key", "s3cr3t/with+chars=", "", "tok:abc.def", weird, "", ""]
    res = run(DEPLOY / "set_secrets.sh", env={"XBOT_APP_DIR": str(app)}, stdin="\n".join(answers) + "\n")
    assert res.returncode == 0, out(res)
    for value in ("new-key", "s3cr3t", "tok:abc", "ab\"c"):
        assert value not in out(res)
    assert "X_API_KEY : mis à jour" in res.stdout and "X_ACCESS_TOKEN : inchangé" in res.stdout
    lines = env_lines(app)
    assert lines.count("X_API_KEY=new-key") == 1 and not any(line == "X_API_KEY=old" for line in lines)
    assert "X_API_SECRET=s3cr3t/with+chars=" in lines
    assert "X_ACCESS_TOKEN_SECRET=tok:abc.def" in lines
    assert not any("old-anthropic" in line for line in lines)  # ancienne ligne « export » remplacée
    assert "TELEGRAM_CHAT_ID=777" in lines
    assert not any(line.startswith(("X_ACCESS_TOKEN=", "TELEGRAM_BOT_TOKEN=", "OPENAI_API_KEY=")) for line in lines)
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    from dotenv import dotenv_values
    assert dotenv_values(env)["ANTHROPIC_API_KEY"] == weird


# ---------------------------------------------------------------------------
# deploy.sh + restore_backup.sh
# ---------------------------------------------------------------------------
def test_deploy_installs_v2_over_v1(tmp_path):
    app = make_v1_app(tmp_path / "app")
    stage = make_stage(tmp_path / "stage")
    res = run(DEPLOY / "deploy.sh", str(stage), env=deploy_env(app))
    assert res.returncode == 0, out(res)
    assert "V2_OK" in res.stdout and "INSTALL_OK" in res.stdout
    assert SECRET not in out(res)
    assert not (app / "bot" / "x_browser.py").exists()
    assert (app / "bot" / "publisher.py").is_file() and (app / "deploy" / "deploy.sh").is_file()
    assert not (app / "deploy" / "relaunch_env.sh").exists()
    assert not list(app.rglob("__pycache__"))
    assert json.loads((app / "config" / "settings.json").read_text()).get("radar_accounts")
    lines = env_lines(app)
    assert f"ANTHROPIC_API_KEY={SECRET}" in lines and "APPROVAL_MODE=telegram" in lines
    # Jamais touchés
    assert (app / "state" / "state.json").read_text() == '{"posted": []}'
    assert (app / "auth" / "storage_state.json").exists()
    backups = list((app / "backup_deploy").iterdir())
    assert len(backups) == 1
    manifest = (backups[0] / ".xbot_backup").read_text().splitlines()
    assert {"have bot", "absent tests", "have config/.env", "have state/state.json",
            "absent config/content.json"} <= set(manifest)
    assert (backups[0] / "bot" / "x_browser.py").exists()
    assert stat.S_IMODE((app / "backup_deploy").stat().st_mode) == 0o700

    # Retour manuel à la v1 : code remis, clés actuelles gardées, état v2 mis de côté.
    (app / "state" / "state.json").write_text('{"version": 2, "items": {}}')
    res = run(app / "deploy" / "restore_backup.sh", env={"XBOT_APP_DIR": str(app), "XBOT_SERVICE": ""})
    assert res.returncode == 0, out(res)
    assert "RESTAURÉ" in res.stdout
    assert (app / "bot" / "x_browser.py").exists() and not (app / "bot" / "publisher.py").exists()
    assert not (app / "tests").exists() and not (app / "config" / "content.json").exists()
    assert (app / "deploy" / "relaunch_env.sh").exists()
    assert "APPROVAL_MODE=telegram" in env_lines(app)
    assert (app / "state" / "state.json").read_text() == '{"posted": []}'
    assert json.loads((app / "state" / "state_avant_restauration.json").read_text())["version"] == 2


def test_deploy_refuses_during_v1_post_window(tmp_path):
    app = make_v1_app(tmp_path / "app")
    stage = make_stage(tmp_path / "stage")
    res = run(DEPLOY / "deploy.sh", str(stage), env=deploy_env(app, XBOT_CLOCK="0730 4"))
    assert res.returncode == 3
    assert "PAS MAINTENANT" in res.stdout and "Rien n'a été modifié" in res.stdout
    assert (app / "bot" / "x_browser.py").exists() and not (app / "backup_deploy").exists()


def test_deploy_refuses_when_v2_is_about_to_publish(tmp_path):
    app = make_v1_app(tmp_path / "app")
    stage = make_stage(tmp_path / "stage")
    assert run(DEPLOY / "deploy.sh", str(stage), env=deploy_env(app)).returncode == 0
    soon = datetime.now(ZoneInfo("Europe/Paris")) + timedelta(minutes=5)
    (app / "state" / "state.json").write_text(json.dumps({"version": 2, "items": {
        "abc": {"id": "abc", "kind": "tip", "status": "approved", "publish_at": soon.isoformat()}}}))
    res = run(DEPLOY / "deploy.sh", str(stage), env=deploy_env(app))
    assert res.returncode == 3, out(res)
    assert "Occupé" in res.stdout and "PAS MAINTENANT" in res.stdout
    assert len(list((app / "backup_deploy").iterdir())) == 1


def test_failed_check_rolls_everything_back(tmp_path):
    app = make_v1_app(tmp_path / "app")
    env_file = app / "config" / ".env"
    env_file.write_text(V1_ENV.replace("TELEGRAM_CHAT_ID=777\n", ""))  # --check échouera (sans réseau)
    original_env = env_file.read_text()
    stage = make_stage(tmp_path / "stage")
    res = run(DEPLOY / "deploy.sh", str(stage), env=deploy_env(app, XBOT_SKIP_CHECK=""))
    assert res.returncode == 1, out(res)
    assert "TELEGRAM_CHAT_ID" in res.stdout and "ancienne version remise" in res.stdout
    assert SECRET not in out(res)
    assert "V2_OK" not in res.stdout
    assert (app / "bot" / "x_browser.py").exists() and not (app / "bot" / "publisher.py").exists()
    assert not (app / "tests").exists()
    assert json.loads((app / "config" / "settings.json").read_text()) == {"v1": True}
    assert env_file.read_text() == original_env               # réglages v2 retirés
    assert (app / "state" / "state.json").read_text() == '{"posted": []}'


# ---------------------------------------------------------------------------
# push_from_mac.sh (faux ssh)
# ---------------------------------------------------------------------------
FAKE_SSH = """#!/usr/bin/env bash
# faux ssh : ignore les options et l'hôte, exécute la commande en local
while [ "${1#-}" != "$1" ]; do shift; done
shift
exec bash -c "$*"
"""


@pytest.mark.skipif(shutil.which("tar") is None, reason="tar nécessaire")
def test_push_from_mac_end_to_end(tmp_path):
    home = tmp_path / "home"
    app = make_v1_app(home / "x-degen-bot")
    ssh = tmp_path / "fake_ssh"
    ssh.write_text(FAKE_SSH)
    ssh.chmod(0o755)
    env = deploy_env(app, XBOT_SSH=str(ssh), XBOT_HOST="botuser@example", XBOT_ASK_SECRETS="o")
    del env["XBOT_APP_DIR"]  # chemin par défaut : ~/x-degen-bot
    answers = "\n".join(["k1", "k2", "k3", "k4", "", "", ""]) + "\n"
    res = run(DEPLOY / "push_from_mac.sh", env=env, stdin=answers, home=home)
    assert res.returncode == 0, out(res)
    assert "ENVOI_OK" in res.stdout and "V2_OK" in res.stdout
    staged = home / "xbv2_stage"
    assert (staged / "bot" / "main.py").is_file() and (staged / "deploy" / "deploy.sh").is_file()
    assert not list(staged.rglob("__pycache__")) and not (staged / ".venv").exists()
    assert not (staged / "config" / ".env").exists()
    lines = env_lines(app)
    assert {"X_API_KEY=k1", "X_API_SECRET=k2", "X_ACCESS_TOKEN=k3", "X_ACCESS_TOKEN_SECRET=k4",
            f"ANTHROPIC_API_KEY={SECRET}"} <= set(lines)
    assert (app / "bot" / "publisher.py").is_file()


# ---------------------------------------------------------------------------
# cleanup_v1.sh
# ---------------------------------------------------------------------------
def test_cleanup_v1(tmp_path):
    app = make_v1_app(tmp_path / "app")
    stage = make_stage(tmp_path / "stage")
    assert run(DEPLOY / "deploy.sh", str(stage), env=deploy_env(app)).returncode == 0
    env = {"XBOT_APP_DIR": str(app), "XBOT_SERVICE": ""}

    # La v2 n'a pas encore tourné : on ne touche à rien.
    res = run(app / "deploy" / "cleanup_v1.sh", env=env)
    assert res.returncode == 1 and "rien supprimé" in res.stdout
    assert (app / "auth").exists()

    (app / "state" / "state.json").write_text('{"version": 2, "items": {}}')
    (app / "backup_deploy" / "x_browser.py").write_text("# v1 backup")
    (app / "backup_deploy" / "20250101-000000").mkdir()
    latest = sorted((app / "backup_deploy").glob("20*"))[-1]
    key = "-----BEGIN OPENSSH " + "PRIVATE KEY-----\nabc\n-----END OPENSSH " + "PRIVATE KEY-----\n"
    (app / "id_ed25519").write_text(key)
    (app / "id_ed25519.pub").write_text("ssh-ed25519 AAAA test")
    res = run(app / "deploy" / "cleanup_v1.sh", env=env)
    assert res.returncode == 0, out(res)
    assert "NETTOYAGE_OK" in res.stdout
    assert not (app / "auth").exists()
    assert not (app / "config" / ".env.avant_v2").exists()
    assert (app / "config" / ".env").exists() and f"ANTHROPIC_API_KEY={SECRET}" in env_lines(app)
    assert not (app / "id_ed25519").exists() and not (app / "id_ed25519.pub").exists()
    assert not (app / "backup_deploy" / "x_browser.py").exists()
    assert not (app / "backup_deploy" / "20250101-000000").exists() and latest.exists()
    assert (app / "bot" / "publisher.py").exists() and (app / "tests" / "test_deploy_scripts.py").exists()
