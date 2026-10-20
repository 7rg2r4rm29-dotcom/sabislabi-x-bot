#!/usr/bin/env bash
# Nettoyage FINAL de l'ancienne version, à lancer SUR LE SERVEUR
# 2-3 jours après l'installation de la v2, quand tout va bien :
#   bash ~/x-degen-bot/deploy/cleanup_v1.sh
# Supprime :
#   - auth/ : la session de connexion à X de l'ancien navigateur
#     (équivaut à ton mot de passe X : elle ne doit pas traîner) ;
#   - les copies de config/.env faites avant la v2 (elles contiennent tes ANCIENNES clés) ;
#   - les restes de l'ancienne version et les sauvegardes, sauf la plus récente ;
#   - toute clé SSH privée oubliée dans le dossier du bot (jamais celles de ~/.ssh).
# Après ça, revenir à l'ancienne version n'est plus possible (elle n'est plus connectée à X).
main() {
  set -u
  local APP_DIR="${XBOT_APP_DIR:-$HOME/x-degen-bot}"
  local SERVICE="${XBOT_SERVICE-x-degen-bot}"
  local SUDO="${XBOT_SUDO-sudo}"
  cd "$APP_DIR" || return 1

  # Garde-fous : la v2 doit être installée, avoir tourné, et tourner encore.
  if [ -f bot/x_browser.py ] || [ ! -f bot/publisher.py ]; then
    echo "La v2 n'est pas installée ici : rien supprimé."
    return 1
  fi
  if ! python3 -c 'import json,sys; sys.exit(0 if json.load(open(sys.argv[1])).get("version") == 2 else 1)' \
      state/state.json 2>/dev/null; then
    echo "La v2 n'a pas encore tourné ici (état encore en v1) : rien supprimé. Réessaie dans un jour ou deux."
    return 1
  fi
  if [ -n "$SERVICE" ] && ! $SUDO systemctl is-active --quiet "$SERVICE"; then
    echo "Le bot ne tourne pas en ce moment : rien supprimé. Vérifie d'abord /statut sur Telegram."
    return 1
  fi

  echo "=== Nettoyage de l'ancienne version ==="
  local p
  for p in auth _deploy_new config/gm_replies.json config/.env.avant_nouvelle_ligne config/.env.avant_v2 \
           config/.env.bak config/.env.backup; do
    if [ -e "$p" ]; then
      rm -rf "$p" && echo "  supprimé : $p"
    fi
  done
  # Sauvegardes de l'ancienne version (fichiers à plat) puis sauvegardes datées sauf la plus récente.
  if [ -d backup_deploy ]; then
    find backup_deploy -maxdepth 1 -type f -print -delete | sed 's/^/  supprimé : /'
    ls -1d backup_deploy/20*/ 2>/dev/null | sort | head -n -1 | while read -r p; do
      rm -rf "$p" && echo "  supprimé : ${p%/}"
    done
  fi
  # Clés SSH privées égarées dans le dossier du bot (et leur .pub).
  find . -path ./.venv -prune -o -type f -size -20k -print 2>/dev/null \
    | while read -r p; do
        if grep -qE -- '-----BEGIN (OPENSSH|RSA|EC|DSA) PRIVATE KEY-----' "$p" 2>/dev/null; then
          rm -f "$p" "$p.pub" && echo "  supprimé (clé SSH privée) : ${p#./}"
        fi
      done
  echo "NETTOYAGE_OK"
}
main "$@"
exit $?
