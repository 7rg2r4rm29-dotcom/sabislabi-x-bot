#!/usr/bin/env bash
# Enregistre tes NOUVELLES clés dans config/.env, sur le serveur.
# - ce que tu tapes ne s'affiche pas ;
# - Entrée sans rien taper = on garde la clé actuelle ;
# - les clés ne passent jamais sur une ligne de commande ni dans les logs.
set -u
APP_DIR="${XBOT_APP_DIR:-$HOME/x-degen-bot}"
ENV_FILE="$APP_DIR/config/.env"
umask 077
mkdir -p "$APP_DIR/config"
[ -f "$ENV_FILE" ] || : > "$ENV_FILE"
chmod 600 "$ENV_FILE"

# Écrit CLE=valeur (valeur lue sur l'entrée standard) de façon atomique.
write_key() {
  python3 -c '
import os, re, sys, tempfile
path, key = sys.argv[1], sys.argv[2]
value = sys.stdin.read().strip()
if re.fullmatch(r"[A-Za-z0-9_\-:.+/=@]*", value):
    line = f"{key}={value}"
else:
    esc = value.replace("\\", "\\\\").replace("\"", "\\\"")
    line = f"{key}=\"{esc}\""
with open(path, encoding="utf-8") as f:
    lines = f.read().splitlines()
pat = re.compile(r"^\s*(export\s+)?" + re.escape(key) + r"=")
out, done = [], False
for l in lines:
    if pat.match(l):
        if not done:
            out.append(line)
            done = True
        continue
    out.append(l)
if not done:
    out.append(line)
d = os.path.dirname(os.path.abspath(path))
fd, tmp = tempfile.mkstemp(dir=d, prefix=".env.")
with os.fdopen(fd, "w", encoding="utf-8") as f:
    f.write("\n".join(out) + "\n")
os.chmod(tmp, 0o600)
os.replace(tmp, path)
' "$ENV_FILE" "$1"
}

ask() {  # ask CLE "explication"
  local key="$1" help="$2" val=""
  printf '\n%s\n  %s\n' "$key" "$help" >&2
  if [ -t 0 ]; then
    IFS= read -r -s -p "  Colle la valeur (rien = garder l'actuelle) : " val
    printf '\n' >&2
  else
    IFS= read -r val || true
  fi
  val="${val//$'\r'/}"
  if [ -z "${val// /}" ]; then
    echo "  -> $key : inchangé"
  else
    printf '%s' "$val" | write_key "$key" && echo "  -> $key : mis à jour"
  fi
  val=""
}

help_for() {
  case "$1" in
    X_API_KEY) echo "console.x.com > ton app > Keys and tokens > API Key (Consumer Key)" ;;
    X_API_SECRET) echo "console.x.com > ton app > Keys and tokens > API Key Secret" ;;
    X_ACCESS_TOKEN) echo "console.x.com > Access Token (généré APRÈS « Read and write », connecté en @sabislabi)" ;;
    X_ACCESS_TOKEN_SECRET) echo "console.x.com > Access Token Secret" ;;
    ANTHROPIC_API_KEY) echo "console.anthropic.com > API Keys > la clé que tu viens de créer" ;;
    TELEGRAM_BOT_TOKEN) echo "Telegram > @BotFather > le jeton (token) de ton bot" ;;
    OPENAI_API_KEY) echo "platform.openai.com > API keys (seulement pour les photos IA ; sinon Entrée)" ;;
    *) echo "" ;;
  esac
}

# Sans argument : toutes les clés. Avec des noms : seulement celles-là.
KEYS="$*"
[ -n "$KEYS" ] || KEYS="X_API_KEY X_API_SECRET X_ACCESS_TOKEN X_ACCESS_TOKEN_SECRET ANTHROPIC_API_KEY TELEGRAM_BOT_TOKEN OPENAI_API_KEY"
echo "=== Enregistrement des clés (rien ne s'affiche quand tu colles, c'est normal) ==="
for k in $KEYS; do
  case "$k" in
    X_API_KEY|X_API_SECRET|X_ACCESS_TOKEN|X_ACCESS_TOKEN_SECRET|ANTHROPIC_API_KEY|TELEGRAM_BOT_TOKEN|OPENAI_API_KEY)
      ask "$k" "$(help_for "$k")" ;;
    *) echo "  (clé inconnue ignorée : $k)" ;;
  esac
done

chmod 600 "$ENV_FILE"
echo
echo "C'est enregistré, directement sur le serveur."
