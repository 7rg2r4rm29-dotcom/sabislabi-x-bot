#!/usr/bin/env bash
# Remet une sauvegarde faite par deploy.sh : code, réglages (settings.json,
# content.json) et, si besoin, l'état. Puis relance le bot.
#   bash ~/x-degen-bot/deploy/restore_backup.sh            -> la plus récente
#   bash ~/x-degen-bot/deploy/restore_backup.sh DOSSIER    -> une sauvegarde précise
# config/.env n'est PAS remis (tes clés actuelles restent), sauf quand c'est
# deploy.sh qui annule une installation ratée (XBOT_RESTORE_ENV=1).
# Remettre l'ANCIENNE version (navigateur sur x.com, risquée pour le compte)
# demande de taper OUI.
# Tout est dans main() : bash lit la fonction en entier avant de l'exécuter,
# donc remplacer ce fichier pendant la restauration ne pose pas de problème.
main() {
  set -u
  local APP_DIR="${XBOT_APP_DIR:-$HOME/x-degen-bot}"
  local SERVICE="${XBOT_SERVICE-x-degen-bot}"
  local SUDO="${XBOT_SUDO-sudo -n}"
  local MANAGE_SERVICE="${XBOT_RESTORE_MANAGE_SERVICE:-1}"
  local WITH_ENV="${XBOT_RESTORE_ENV:-}"
  local B="${1:-}"
  if [ -z "$B" ]; then
    B=$(ls -1d "$APP_DIR"/backup_deploy/20*/ 2>/dev/null | sort | tail -n 1)
  fi
  B="${B%/}"
  if [ -z "$B" ] || [ ! -f "$B/.xbot_backup" ]; then
    echo "Aucune sauvegarde trouvée (dossier backup_deploy/ vide ?). Rien n'a été modifié."
    return 1
  fi

  svc() { [ -n "$SERVICE" ] || return 0; $SUDO systemctl "$@" "$SERVICE"; }
  if [ "$MANAGE_SERVICE" = 1 ] && [ -f "$B/bot/x_browser.py" ] && [ "${XBOT_RESTORE_V1:-}" != 1 ]; then
    local answer=""
    echo "⚠️ Cette sauvegarde est l'ANCIENNE version (navigateur sur x.com) : X peut suspendre le compte"
    echo "   pour ce type d'automatisation. Pour mettre le bot en pause, préfère /pause sur Telegram."
    if { exec 3</dev/tty; } 2>/dev/null; then
      read -r -p "   Tape OUI pour la remettre quand même : " answer <&3 || answer=""
      exec 3<&-
    else
      read -r answer || answer=""
    fi
    if [ "$answer" != "OUI" ]; then
      echo "Rien n'a été modifié."
      return 1
    fi
  fi
  # Version du fichier d'état (2 = v2, autre = v1 ou absent)
  state_version() {
    python3 -c 'import json,sys
try:
    print(json.load(open(sys.argv[1], encoding="utf-8")).get("version", 1))
except Exception:
    print(0)' "$1"
  }

  if [ "$MANAGE_SERVICE" = 1 ]; then svc stop 2>/dev/null || true; fi
  local kind path failed=""
  while read -r kind path; do
    [ -n "$path" ] || continue
    case "$path" in
      config/.env) [ -n "$WITH_ENV" ] || continue ;;
      state/state.json)
        # Retour v2 -> v2 : on garde l'état actuel (plus récent, même format).
        if [ "$(state_version "$APP_DIR/$path")" = 2 ] && [ "$kind" = have ] \
           && [ "$(state_version "$B/$path")" = 2 ]; then
          continue
        fi
        # L'état v2 n'est jamais effacé sans copie.
        [ -f "$APP_DIR/$path" ] && cp -p "$APP_DIR/$path" "$APP_DIR/state/state_avant_restauration.json"
        ;;
    esac
    case "$kind" in
      have)
        mkdir -p "$(dirname "$APP_DIR/$path")"
        rm -rf "$APP_DIR/$path.restore" \
          && cp -a "$B/$path" "$APP_DIR/$path.restore" \
          && rm -rf "$APP_DIR/$path" \
          && mv "$APP_DIR/$path.restore" "$APP_DIR/$path" \
          || { echo "ERREUR en remettant $path"; failed="$failed $path"; }
        ;;
      absent)
        [ "$path" = state/state.json ] || rm -rf "$APP_DIR/$path"
        ;;
    esac
  done < "$B/.xbot_backup"
  if [ -n "$failed" ]; then
    echo "RESTAURATION INCOMPLÈTE :$failed (sauvegarde intacte : $B)"
    return 1
  fi
  if [ "$MANAGE_SERVICE" = 1 ]; then svc restart || echo "Relance impossible : sudo systemctl restart $SERVICE"; fi
  echo "RESTAURÉ : $(basename "$B")"
}
main "$@"
exit $?
