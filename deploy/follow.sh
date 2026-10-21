#!/usr/bin/env bash
# Affiche en direct le journal d'une installation lancée en arrière-plan, jusqu'à
# la fin (« FIN_DEPLOIEMENT <code> »), puis sort avec ce code.
#   bash follow.sh ~/xbv2_deploy_XXXX.log
# Si la connexion se coupe, l'installation continue quand même sur le serveur.
main() {
  set -u
  local log="$1" waited=0 max="${XBOT_FOLLOW_MAX_S:-2700}"
  while [ ! -f "$log" ] && [ "$waited" -lt 60 ]; do sleep 1; waited=$((waited + 1)); done
  # 4 = on ne sait pas (encore) : l'installation continue peut-être sur le serveur.
  [ -f "$log" ] || { echo "Journal d'installation introuvable ($log)."; return 4; }
  tail -n +1 -f "$log" &
  local tailpid=$! start=$SECONDS
  while ! grep -q '^FIN_DEPLOIEMENT' "$log" 2>/dev/null; do
    if [ $((SECONDS - start)) -ge "$max" ]; then
      kill "$tailpid" 2>/dev/null
      echo "L'installation est anormalement longue : elle continue sur le serveur, regarde Telegram."
      return 4
    fi
    sleep 2
  done
  sleep 1
  kill "$tailpid" 2>/dev/null
  wait "$tailpid" 2>/dev/null
  local rc
  rc=$(grep '^FIN_DEPLOIEMENT' "$log" | tail -n 1 | awk '{print $2}')
  return "${rc:-1}"
}
main "$@"
exit $?
