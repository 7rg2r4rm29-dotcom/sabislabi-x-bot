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
    # Un lien symbolique vers le python d'un venv perd ses bibliothèques (Python 3.12) :
    # un petit relais appelle le vrai chemin.
    py = app / ".venv" / "bin" / "python"
    py.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    py.chmod(0o755)
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
           "XBOT_SKIP_CHECK": "1", "XBOT_CLOCK": "1000 4", "XBOT_NO_TELEGRAM": "1"}
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
    # Revenir à la version navigateur demande une confirmation explicite.
    res = run(app / "deploy" / "restore_backup.sh", env={"XBOT_APP_DIR": str(app), "XBOT_SERVICE": ""},
              stdin="non\n")
    assert res.returncode == 1 and "Rien n'a été modifié" in res.stdout
    assert (app / "bot" / "publisher.py").exists()
    res = run(app / "deploy" / "restore_backup.sh", env={"XBOT_APP_DIR": str(app), "XBOT_SERVICE": ""},
              stdin="OUI\n")
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
    assert "TELEGRAM_CHAT_ID" in res.stdout and "l'ancienne version tourne comme avant" in res.stdout
    assert SECRET not in out(res)
    assert "V2_OK" not in res.stdout
    assert (app / "bot" / "x_browser.py").exists() and not (app / "bot" / "publisher.py").exists()
    assert not (app / "tests").exists()
    assert json.loads((app / "config" / "settings.json").read_text()) == {"v1": True}
    assert env_file.read_text() == original_env               # réglages v2 retirés
    assert (app / "state" / "state.json").read_text() == '{"posted": []}'


# ---------------------------------------------------------------------------
# push_from_mac.sh : l'assistant complet, avec un faux ssh
# ---------------------------------------------------------------------------
FAKE_SSH = """#!/usr/bin/env python3
# Faux ssh : ignore les options et l'hôte. La vérification des clés et l'aide
# Telegram renvoient des réponses préparées par le test (pas de réseau) ; tout le
# reste s'exécute vraiment, en local.
import os, subprocess, sys
args = sys.argv[1:]
while args and args[0].startswith("-"):
    args.pop(0)
cmd = " ".join(args[1:])
fake = os.environ["FAKE_DIR"]
def canned(name):
    n_file = os.path.join(fake, name + ".n")
    n = int(open(n_file).read()) if os.path.exists(n_file) else 0
    open(n_file, "w").write(str(n + 1))
    files = sorted(f for f in os.listdir(fake) if f.startswith(name + "_"))
    print(open(os.path.join(fake, files[min(n, len(files) - 1)])).read(), end="")
    with open(os.path.join(fake, "log"), "a") as f:
        f.write(cmd + "\\n")
if "precheck.sh" in cmd:
    canned("precheck"); sys.exit(0)
if "telegram_setup.sh check" in cmd:
    canned("tgcheck"); sys.exit(0)
if "telegram_setup.sh chatid" in cmd:
    canned("tgchat"); sys.exit(0)
with open(os.path.join(fake, "log"), "a") as f:
    f.write(cmd + "\\n")
sys.exit(subprocess.call(["bash", "-c", cmd]))
"""

ALL_OK = "✅ Telegram : message de test envoyé\n✅ API X : connecté\n✅ Claude : répond\nPARTS telegram=ok x=ok claude=ok\n"


def wizard(tmp_path, prechecks, stdin, tgcheck=("TOKEN_OK @testbot\n",), tgchat=("CHATID_OK\n",), **extra):
    home = tmp_path / "home"
    app = make_v1_app(home / "x-degen-bot")
    fake = tmp_path / "fake"
    fake.mkdir()
    for i, txt in enumerate(prechecks):
        (fake / f"precheck_{i}").write_text(txt)
    for i, txt in enumerate(tgcheck):
        (fake / f"tgcheck_{i}").write_text(txt)
    for i, txt in enumerate(tgchat):
        (fake / f"tgchat_{i}").write_text(txt)
    ssh = tmp_path / "fake_ssh"
    ssh.write_text(FAKE_SSH)
    ssh.chmod(0o755)
    env = deploy_env(app, XBOT_SSH=str(ssh), XBOT_HOST="botuser@example", XBOT_OPEN="true",
                     XBOT_NO_TTY="1", FAKE_DIR=str(fake), XBOT_RETRY_SLEEP="0", XBOT_NO_TELEGRAM="1", **extra)
    del env["XBOT_APP_DIR"]  # chemin par défaut : ~/x-degen-bot
    res = run(DEPLOY / "push_from_mac.sh", env=env, stdin=stdin, home=home)
    log = (fake / "log").read_text() if (fake / "log").exists() else ""
    return res, app, home, log


@pytest.mark.skipif(shutil.which("tar") is None or shutil.which("setsid") is None, reason="tar/setsid")
def test_wizard_asks_only_for_missing_x_keys_then_installs(tmp_path):
    missing_x = ("✅ Telegram : message de test envoyé\n❌ API X : clés absentes\n✅ Claude : répond\n"
                 "PARTS telegram=ok x=missing claude=ok\n")
    answers = "\n".join(["", "k1", "k2", "k3", "k4", "n"]) + "\n"   # Entrée, 4 clés, pas de rotation
    res, app, home, log = wizard(tmp_path, [missing_x, ALL_OK], answers)
    assert res.returncode == 0, out(res)
    o = out(res)
    assert "ENVOI_OK" in o and "console.x.com" in o and "Read and write" in o
    assert "V2_OK" in o and "C'est fait" in o
    assert "anthropic" not in o.split("ENVOI_OK")[1].split("Installation")[0].lower()  # Claude marchait déjà
    lines = env_lines(app)
    assert {"X_API_KEY=k1", "X_API_SECRET=k2", "X_ACCESS_TOKEN=k3", "X_ACCESS_TOKEN_SECRET=k4",
            f"ANTHROPIC_API_KEY={SECRET}"} <= set(lines)
    assert (app / "bot" / "publisher.py").is_file() and not (app / "bot" / "x_browser.py").exists()
    stages = list(home.glob("xbv2_stage_*"))
    assert len(stages) == 1
    assert not list(stages[0].rglob("__pycache__")) and not (stages[0] / "config" / ".env").exists()
    assert "nohup setsid bash" in log          # l'installation survit à une coupure


@pytest.mark.skipif(shutil.which("tar") is None, reason="tar nécessaire")
def test_wizard_stops_without_touching_anything_when_server_cannot_check(tmp_path):
    res, app, home, log = wizard(tmp_path, ["Mise à jour des bibliothèques impossible\nPARTS server=pip\n"], "\n")
    assert res.returncode == 1
    assert "Rien n'a été modifié" in out(res)
    assert (app / "bot" / "x_browser.py").exists() and "deploy.sh" not in log


@pytest.mark.skipif(shutil.which("tar") is None or shutil.which("setsid") is None, reason="tar/setsid")
def test_wizard_fixes_telegram_chat_then_installs(tmp_path):
    bad_tg = ("❌ Telegram : envoi impossible\n✅ API X : connecté\n✅ Claude : répond\n"
              "PARTS telegram=bad x=ok claude=ok\n")
    res, app, home, log = wizard(tmp_path, [bad_tg, ALL_OK], "\n\nn\n")
    assert res.returncode == 0, out(res)
    assert "/start" in out(res) and "telegram_setup.sh chatid" in log
    assert "BotFather" not in out(res)       # le jeton marchait : on ne le redemande pas


@pytest.mark.skipif(shutil.which("tar") is None, reason="tar nécessaire")
def test_wizard_gives_up_after_three_rounds(tmp_path):
    still_bad = "❌ Claude : pas de réponse\nPARTS telegram=ok x=ok claude=bad\n"
    res, app, home, log = wizard(tmp_path, [still_bad], "\n" * 20)
    assert res.returncode == 1
    assert "Rien n'a été modifié" in out(res)
    assert (app / "bot" / "x_browser.py").exists()


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


# ---------------------------------------------------------------------------
# Service systemd simulé : sudo n'autorise que « restart » (comme pour la v1)
# ---------------------------------------------------------------------------
def fake_service_bin(tmp_path: Path, active: bool) -> tuple[Path, Path]:
    bindir, log = tmp_path / "bin", tmp_path / "systemctl.log"
    bindir.mkdir()
    (bindir / "sudo").write_text('#!/usr/bin/env bash\n[ "$1" = -n ] && shift\nexec "$@"\n')
    (bindir / "systemctl").write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$*" >> "{log}"\n'
        'case "$1" in\n'
        '  stop) echo "Interactive authentication required." >&2; exit 1 ;;\n'
        f'  is-active) exit {0 if active else 3} ;;\n'
        "esac\n")
    (bindir / "journalctl").write_text('#!/usr/bin/env bash\necho "journal: bot démarré"\n')
    for f in bindir.iterdir():
        f.chmod(0o755)
    return bindir, log


@pytest.mark.parametrize("active", [True, False])
def test_deploy_with_restart_only_service(tmp_path, active):
    app = make_v1_app(tmp_path / "app")
    stage = make_stage(tmp_path / "stage")
    bindir, log = fake_service_bin(tmp_path, active)
    env = deploy_env(app, XBOT_SERVICE="x-degen-bot", XBOT_WAIT="0",
                     PATH=f"{bindir}:{os.environ.get('PATH', '/usr/bin:/bin')}")
    res = run(DEPLOY / "deploy.sh", str(stage), env=env)
    calls = log.read_text().splitlines()
    assert "Arrêt non autorisé" in res.stdout
    assert calls[0] == "stop x-degen-bot"
    if active:
        assert res.returncode == 0, out(res)
        assert "V2_OK" in res.stdout and "journal: bot démarré" in res.stdout
        assert calls[1:] == ["restart x-degen-bot", "is-active --quiet x-degen-bot"]
        assert (app / "bot" / "publisher.py").exists()
    else:
        assert res.returncode == 1
        assert "ne démarre pas" in res.stdout and "l'ancienne version tourne comme avant" in res.stdout
        assert calls[1:] == ["restart x-degen-bot", "is-active --quiet x-degen-bot", "restart x-degen-bot"]
        assert (app / "bot" / "x_browser.py").exists() and not (app / "bot" / "publisher.py").exists()


# ---------------------------------------------------------------------------
# Mises à jour : réglages gardés, interruption, restauration ratée
# ---------------------------------------------------------------------------
def test_update_keeps_owner_settings(tmp_path):
    app = make_v1_app(tmp_path / "app")
    stage = make_stage(tmp_path / "stage")
    assert run(DEPLOY / "deploy.sh", str(stage), env=deploy_env(app)).returncode == 0
    custom = json.loads((app / "config" / "settings.json").read_text())
    custom["radar_accounts"] = ["my_custom_account"]
    (app / "config" / "settings.json").write_text(json.dumps(custom))
    res = run(DEPLOY / "deploy.sh", str(stage), env=deploy_env(app))
    assert res.returncode == 0, out(res)
    assert "tes modifications sont gardées" in res.stdout
    assert json.loads((app / "config" / "settings.json").read_text())["radar_accounts"] == ["my_custom_account"]
    assert (app / "config" / "settings.json.nouveau").exists()
    # content.json non modifié : mis à jour normalement, sans fichier .nouveau
    assert not (app / "config" / "content.json.nouveau").exists()


def test_interrupted_install_is_rolled_back(tmp_path):
    import signal
    import time
    app = make_v1_app(tmp_path / "app")
    stage = make_stage(tmp_path / "stage")
    slow = app / ".venv" / "bin" / "python"
    slow.unlink()
    slow.write_text(f'#!/usr/bin/env bash\ncase "$*" in *--check*) sleep 4 ;; esac\nexec {sys.executable} "$@"\n')
    slow.chmod(0o755)
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8", "HOME": str(tmp_path),
           **deploy_env(app, XBOT_SKIP_CHECK="")}
    proc = subprocess.Popen(["bash", str(DEPLOY / "deploy.sh"), str(stage)], env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, start_new_session=True)
    deadline = time.time() + 20
    while time.time() < deadline and not (app / "bot" / "publisher.py").exists():
        time.sleep(0.2)
    time.sleep(1)
    os.killpg(proc.pid, signal.SIGTERM)       # coupure pendant la vérification
    output, _ = proc.communicate(timeout=60)
    assert "Installation interrompue" in output and "FIN_DEPLOIEMENT 1" in output, output
    assert (app / "bot" / "x_browser.py").exists() and not (app / "bot" / "publisher.py").exists()
    assert (app / "config" / ".env").read_text() == V1_ENV


def test_incomplete_restore_is_reported_not_hidden(tmp_path):
    app = make_v1_app(tmp_path / "app")
    (app / "config" / ".env").write_text(V1_ENV.replace("TELEGRAM_CHAT_ID=777\n", ""))
    stage = make_stage(tmp_path / "stage")
    real = (DEPLOY / "restore_backup.sh").read_text()
    (stage / "deploy" / "restore_backup.sh").write_text(
        real.replace('main "$@"\nexit $?', 'echo "ERREUR en remettant bot"; echo "RESTAURATION INCOMPLÈTE : bot"\nexit 1'))
    res = run(DEPLOY / "deploy.sh", str(stage), env=deploy_env(app, XBOT_SKIP_CHECK=""))
    assert res.returncode == 1
    assert "RESTAURATION INCOMPLÈTE" in res.stdout and "Rien n'est perdu" not in res.stdout


# ---------------------------------------------------------------------------
# precheck.sh et telegram_setup.sh (sans réseau)
# ---------------------------------------------------------------------------
def test_precheck_reports_what_is_missing(tmp_path):
    app = make_v1_app(tmp_path / "app")
    (app / "config" / ".env").write_text("X_HANDLE=sabislabi\n")   # aucune clé : aucun appel réseau
    stage = make_stage(tmp_path / "stage")
    res = run(DEPLOY / "precheck.sh", str(stage), env={"XBOT_APP_DIR": str(app), "XBOT_SKIP_PIP": "1"})
    assert res.returncode == 0, out(res)
    assert "PARTS telegram=missing x=missing claude=missing" in res.stdout
    assert "❌" in res.stdout
    assert (app / "bot" / "x_browser.py").exists()     # rien n'est installé


def test_telegram_setup_finds_private_chat(tmp_path):
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            if "getMe" in self.path:
                body = {"ok": True, "result": {"username": "testbot"}}
            else:
                body = {"ok": True, "result": [
                    {"update_id": 1, "message": {"chat": {"id": -100, "type": "group"}, "from": {"id": 5}}},
                    {"update_id": 2, "message": {"chat": {"id": 4242, "type": "private"}, "from": {"id": 4242}}}]}
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        app = make_v1_app(tmp_path / "app")
        env = {"XBOT_APP_DIR": str(app), "XBOT_TELEGRAM_API": f"http://127.0.0.1:{srv.server_port}"}
        res = run(DEPLOY / "telegram_setup.sh", "check", env=env)
        assert res.stdout.strip() == "TOKEN_OK @testbot", out(res)
        assert "tok-SECRET" not in out(res)
        res = run(DEPLOY / "telegram_setup.sh", "chatid", env=env)
        assert "CHATID_OK" in res.stdout, out(res)
        lines = env_lines(app)
        assert "TELEGRAM_CHAT_ID=4242" in lines and "TELEGRAM_CHAT_ID=777" not in lines
        assert stat.S_IMODE((app / "config" / ".env").stat().st_mode) == 0o600
    finally:
        srv.shutdown()
