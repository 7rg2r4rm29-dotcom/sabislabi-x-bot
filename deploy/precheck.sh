#!/usr/bin/env bash
# Vérifie, AVANT toute installation, ce qui marche déjà avec les clés présentes
# sur le serveur (Telegram, API X, Claude), avec le code de la NOUVELLE version.
# Ne touche pas au bot en service (seules les bibliothèques Python sont mises à
# jour, comme le ferait deploy.sh).
#   bash precheck.sh DOSSIER_DE_LA_NOUVELLE_VERSION
# Dernière ligne : « PARTS telegram=… x=… claude=… » (lue par push_from_mac.sh).
main() {
  set -u
  local STAGE="${1:-}"
  STAGE="${STAGE%/}"
  local APP_DIR="${XBOT_APP_DIR:-$HOME/x-degen-bot}"
  local PY="$APP_DIR/.venv/bin/python"
  if [ ! -f "$STAGE/bot/main.py" ]; then
    echo "Nouvelle version introuvable ($STAGE)."
    echo "PARTS server=stage"
    return 1
  fi
  if [ ! -x "$PY" ]; then
    python3 -m venv "$APP_DIR/.venv" || { echo "PARTS server=venv"; return 1; }
  fi
  if [ -z "${XBOT_SKIP_PIP:-}" ]; then
    # État des bibliothèques avant toute mise à jour (remis si l'installation est annulée).
    "$PY" -m pip freeze --disable-pip-version-check > "$APP_DIR/.pip_freeze_avant" 2>/dev/null || true
    local plog
    plog=$(mktemp)
    if ! "$PY" -m pip install -q --disable-pip-version-check -r "$STAGE/requirements.txt" >"$plog" 2>&1; then
      tail -n 5 "$plog"
      rm -f "$plog"
      echo "Mise à jour des bibliothèques impossible (réseau du serveur ?)."
      echo "PARTS server=pip"
      return 1
    fi
    rm -f "$plog"
  fi
  local extra=""
  if ! grep -qE '^[[:space:]]*(export[[:space:]]+)?X_HANDLE=.' "$APP_DIR/config/.env" 2>/dev/null; then
    extra="X_HANDLE=sabislabi"
  fi
  # Vérification « comme en production » : publication réelle, validation Telegram.
  (cd "$STAGE" && env XBOT_ENV_FILE="$APP_DIR/config/.env" DRY_RUN=false APPROVAL_MODE=telegram $extra \
     PYTHONDONTWRITEBYTECODE=1 timeout 300 "$PY" -m bot.main --check 2>&1) \
    | grep -E '^(✅|❌|⚠️|PARTS |  - )'
  return 0
}
main "$@"
exit $?
