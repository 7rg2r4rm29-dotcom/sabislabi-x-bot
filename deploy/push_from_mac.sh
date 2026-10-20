#!/usr/bin/env bash
# À lancer SUR TON MAC, dans le Terminal :
#   bash ~/Downloads/sabislabi-x-bot-main/deploy/push_from_mac.sh
# 1. envoie le code au serveur (jamais tes clés : elles ne sont pas dans ce dossier) ;
# 2. si tu as de nouvelles clés, te les fait coller DIRECTEMENT sur le serveur ;
# 3. installe la nouvelle version (tests, sauvegarde, vérification, retour arrière si besoin).
main() {
  set -u
  local HOST="${XBOT_HOST:-botuser@178.128.194.190}"
  local SSH="${XBOT_SSH:-ssh}"
  local STAGE="xbv2_stage"
  local HERE
  HERE="$(cd "$(dirname "$0")/.." && pwd)"
  if [ ! -f "$HERE/bot/main.py" ] || [ ! -f "$HERE/deploy/deploy.sh" ]; then
    echo "Ce script doit rester dans le dossier deploy/ du code téléchargé (bot/main.py introuvable)."
    return 1
  fi

  echo "=== Envoi du code vers $HOST ==="
  local items=() p
  for p in bot deploy tests config/settings.json config/content.json requirements.txt README.md DESIGN.md; do
    [ -e "$HERE/$p" ] && items+=("$p")
  done
  # COPYFILE_DISABLE : pas de fichiers ._ du Mac ; les attributs macOS sont ignorés côté serveur.
  if ! COPYFILE_DISABLE=1 tar -C "$HERE" --exclude=__pycache__ --exclude=.DS_Store -czf - "${items[@]}" \
      | $SSH "$HOST" "rm -rf ~/$STAGE && mkdir -p ~/$STAGE && tar --warning=no-unknown-keyword -xzf - -C ~/$STAGE \
                      && echo ENVOI_OK"; then
    echo "Envoi impossible : vérifie ta connexion internet, puis relance la même commande."
    return 1
  fi

  local ans="${XBOT_ASK_SECRETS:-}"
  if [ -z "$ans" ]; then
    if [ -r /dev/tty ]; then
      read -r -p "As-tu de nouvelles clés à enregistrer (API X, Claude, Telegram) ? (o/N) " ans </dev/tty || ans=n
    else
      ans=n
    fi
  fi
  case "$ans" in
    o|O|oui|OUI|y|Y|yes)
      echo "=== Enregistrement des clés sur le serveur ==="
      $SSH -t "$HOST" "bash ~/$STAGE/deploy/set_secrets.sh" || {
        echo "Enregistrement des clés interrompu. Relance la même commande."
        return 1
      }
      ;;
  esac

  echo "=== Installation sur le serveur ==="
  $SSH -t "$HOST" "bash ~/$STAGE/deploy/deploy.sh ~/$STAGE"
}
main "$@"
exit $?
