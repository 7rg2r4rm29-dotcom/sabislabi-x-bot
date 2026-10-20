#!/usr/bin/env bash
# Remet une sauvegarde faite par deploy.sh : code, réglages (settings.json,
# content.json) et, si besoin, l'état. Puis relance le bot.
#   bash ~/x-degen-bot/deploy/restore_backup.sh            -> la plus récente
#   bash ~/x-degen-bot/deploy/restore_backup.sh DOSSIER    -> une sauvegarde précise
# config/.env n'est PAS remis (tes clés actuelles restent), sauf quand c'est
# deploy.sh qui annule une installation ratée (XBOT_RESTORE_ENV=1).
# Tout est dans main() : bash lit la fonction en entier avant de l'exécuter,
# donc remplacer ce fichier pendant la restauration ne pose pas de problème.
main() {
  set -u
  local APP_DIR="${XBOT_APP_DIR:-$HOME/x-degen-bot}"
  local SERVICE="${XBOT_SERVICE-x-degen-bot}"
  local SUDO="${XBOT_SUDO-sudo}"
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
  # Version du fichier d'état (2 = v2, autre = v1 ou absent)
  state_version() {
    python3 -c 'import json,sys
try:
    print(json.load(open(sys.argv[1], encoding="utf-8")).get("version", 1))
except Exception:
    print(0)' "$1"
  }

  [ "$MANAGE_SERVICE" = 1 ] && svc stop
  local kind path
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
          || { echo "ERREUR en remettant $path"; return 1; }
        ;;
      absent)
        [ "$path" = state/state.json ] || rm -rf "$APP_DIR/$path"
        ;;
    esac
  done < "$B/.xbot_backup"
  [ "$MANAGE_SERVICE" = 1 ] && svc start
  echo "RESTAURÉ : $(basename "$B")"
}
main "$@"
exit $?
