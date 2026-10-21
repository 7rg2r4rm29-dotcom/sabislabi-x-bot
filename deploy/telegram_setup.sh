#!/usr/bin/env bash
# Aide Telegram, sur le serveur (le jeton n'est jamais affiché) :
#   bash telegram_setup.sh check    -> « TOKEN_OK @nom_du_bot » ou « TOKEN_INVALIDE »
#   bash telegram_setup.sh chatid   -> retrouve ton chat PRIVÉ avec le bot (après un /start)
#                                      et l'enregistre dans config/.env (TELEGRAM_CHAT_ID)
main() {
  set -u
  local APP_DIR="${XBOT_APP_DIR:-$HOME/x-degen-bot}"
  local PY="$APP_DIR/.venv/bin/python"
  [ -x "$PY" ] || PY=python3
  local api="${XBOT_TELEGRAM_API:-https://api.telegram.org}"
  "$PY" - "$1" "$APP_DIR/config/.env" "$api" <<'PY'
import os, re, sys, tempfile

import requests

mode, env_path, api = sys.argv[1], sys.argv[2], sys.argv[3].rstrip("/")
try:
    from dotenv import dotenv_values
    values = dotenv_values(env_path)
except Exception:  # noqa: BLE001
    values = {}
token = (values.get("TELEGRAM_BOT_TOKEN") or "").strip()


def call(method, **params):
    try:
        r = requests.get(f"{api}/bot{token}/{method}", params=params, timeout=20)
        return r.json()
    except Exception as e:  # noqa: BLE001 — jamais l'URL (elle contient le jeton)
        return {"ok": False, "description": type(e).__name__}


me = call("getMe") if token else {"ok": False}
if not me.get("ok"):
    print("TOKEN_INVALIDE")
    sys.exit(2)
bot_name = me["result"].get("username", "?")
if mode == "check":
    print(f"TOKEN_OK @{bot_name}")
    sys.exit(0)

res = call("getUpdates", timeout=0, allowed_updates='["message"]')
if not res.get("ok"):
    print(f"Lecture des messages impossible ({str(res.get('description'))[:120]}).")
    print("CHATID_ABSENT")
    sys.exit(3)
chat_id = None
for upd in res.get("result") or []:
    msg = upd.get("message") or {}
    chat, sender = msg.get("chat") or {}, msg.get("from") or {}
    if chat.get("type") == "private" and chat.get("id") == sender.get("id"):
        chat_id = str(chat["id"])
if not chat_id:
    print(f"Aucun message privé trouvé : envoie /start à @{bot_name} dans Telegram, puis réessaie.")
    print("CHATID_ABSENT")
    sys.exit(3)

lines = open(env_path, encoding="utf-8").read().splitlines() if os.path.exists(env_path) else []
pat = re.compile(r"^\s*(export\s+)?TELEGRAM_CHAT_ID=")
out = [l for l in lines if not pat.match(l)] + [f"TELEGRAM_CHAT_ID={chat_id}"]
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(env_path)), prefix=".env.")
with os.fdopen(fd, "w", encoding="utf-8") as f:
    f.write("\n".join(out) + "\n")
os.chmod(tmp, 0o600)
os.replace(tmp, env_path)
print(f"CHATID_OK (chat privé avec @{bot_name})")
PY
}
main "$@"
exit $?
