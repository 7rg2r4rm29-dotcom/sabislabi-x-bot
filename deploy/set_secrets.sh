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

echo "=== Enregistrement des clés (rien ne s'affiche quand tu colles, c'est normal) ==="
ask X_API_KEY "console.x.com > ton app > Keys and tokens > API Key (Consumer Key)"
ask X_API_SECRET "console.x.com > ton app > Keys and tokens > API Key Secret"
ask X_ACCESS_TOKEN "console.x.com > Access Token (généré APRÈS avoir mis l'app en « Read and write », pour @sabislabi)"
ask X_ACCESS_TOKEN_SECRET "console.x.com > Access Token Secret"
ask ANTHROPIC_API_KEY "console.anthropic.com > API Keys (nouvelle clé, puis révoque l'ancienne)"
ask TELEGRAM_BOT_TOKEN "Telegram > @BotFather > /revoke sur ton bot > nouveau jeton"
ask OPENAI_API_KEY "platform.openai.com > API keys (seulement pour les photos IA ; sinon Entrée)"

chmod 600 "$ENV_FILE"
echo
echo "C'est enregistré. Pense à RÉVOQUER les anciennes clés sur chaque site (console.x.com, console.anthropic.com, @BotFather, platform.openai.com)."
