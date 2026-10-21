#!/usr/bin/env bash
# Installe une nouvelle version du bot SUR LE SERVEUR, sans casser ce qui tourne.
#   bash deploy.sh DOSSIER_DE_LA_NOUVELLE_VERSION
# 1. refuse si une publication est en cours ou imminente (rien n'est modifié) ;
# 2. met à jour les bibliothèques et lance TOUS les tests sur la nouvelle version ;
# 3. arrête le bot, sauvegarde (code, réglages, état), installe,
#    complète config/.env avec les réglages v2 (sans rien écraser) ;
# 4. vérifie Telegram, l'API X et Claude (--check) ;
# 5. relance le bot.
# Au moindre échec (ou interruption) après l'arrêt : l'ancienne version est remise
# et relancée, et tu es prévenu sur Telegram. Tes modifications de
# config/settings.json et content.json sont gardées.
# Ne touche jamais : tes clés, state/, logs/, media/, auth/.
# Dernière ligne : « FIN_DEPLOIEMENT <code> » (suivie par push_from_mac.sh).
# Tout est dans main() : bash lit la fonction en entier avant de l'exécuter.
main() {
  set -u
  local STAGE="${1:-$HOME/xbv2_stage}"
  STAGE="${STAGE%/}"
  local APP_DIR="${XBOT_APP_DIR:-$HOME/x-degen-bot}"
  local SERVICE="${XBOT_SERVICE-x-degen-bot}"
  local SUDO="${XBOT_SUDO-sudo -n}"   # -n : jamais de mot de passe demandé (sinon tout resterait bloqué)
  local PY="$APP_DIR/.venv/bin/python"
  # Ce qui est sauvegardé puis remplacé (le reste n'est jamais touché).
  local SAVED="bot deploy tests config/settings.json config/content.json config/.shipped config/.env state/state.json requirements.txt README.md DESIGN.md"
  local INSTALL_DIRS="bot deploy tests"
  local INSTALL_FILES="requirements.txt README.md DESIGN.md"
  local KEEP_BACKUPS=5
  local BACKUP=""
  ROLLBACK_INCOMPLETE=0

  svc() { [ -n "$SERVICE" ] || return 0; $SUDO systemctl "$@" "$SERVICE"; }
  svc_active() { [ -z "$SERVICE" ] || systemctl is-active --quiet "$SERVICE"; }
  journal() { [ -z "$SERVICE" ] || $SUDO journalctl -u "$SERVICE" -n "$1" --no-pager 2>/dev/null || true; }

  # Message Telegram de secours (le jeton n'est jamais affiché).
  tg_alert() {
    [ -z "${XBOT_NO_TELEGRAM:-}" ] || return 0
    "$PY" - "$APP_DIR/config/.env" "$1" >/dev/null 2>&1 <<'PY' || true
import sys

import requests
from dotenv import dotenv_values

v = dotenv_values(sys.argv[1])
token, chat = (v.get("TELEGRAM_BOT_TOKEN") or "").strip(), (v.get("TELEGRAM_CHAT_ID") or "").strip()
if token and chat:
    requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                  json={"chat_id": chat, "text": sys.argv[2]}, timeout=15)
PY
  }

  # Bibliothèques Python d'avant la mise à jour (le service partage le même .venv).
  restore_pip() {
    [ -z "${XBOT_SKIP_PIP:-}" ] && [ -s "$APP_DIR/.pip_freeze_avant" ] || return 0
    echo "Remise des bibliothèques Python d'avant…"
    "$PY" -m pip install -q --disable-pip-version-check -r "$APP_DIR/.pip_freeze_avant" >/dev/null 2>&1 \
      || echo "(certaines bibliothèques n'ont pas pu être remises à l'identique)"
  }

  # Réglages JSON : ceux que tu as modifiés sont gardés.
  install_config() {
    local f="$1" new="$STAGE/config/$1" cur="$APP_DIR/config/$1" shipped="$APP_DIR/config/.shipped/$1"
    [ -f "$new" ] || return 0
    mkdir -p "$APP_DIR/config/.shipped" || return 1
    if [ -f "$cur" ] && [ -f "$shipped" ] && ! cmp -s "$cur" "$shipped"; then
      cp "$new" "$cur.nouveau" || return 1
      echo "config/$f : tes modifications sont gardées (nouvelle version par défaut : config/$f.nouveau)"
    else
      cp "$new" "$cur" || return 1
    fi
    cp "$new" "$shipped"
  }

  # Annule tout et relance l'ancienne version.
  rollback() {
    trap - INT TERM HUP
    echo "$1"
    if XBOT_APP_DIR="$APP_DIR" XBOT_RESTORE_ENV=1 XBOT_RESTORE_MANAGE_SERVICE=0 \
        bash "$STAGE/deploy/restore_backup.sh" "$BACKUP"; then
      restore_pip
      svc restart
      if [ -f "$APP_DIR/bot/x_browser.py" ]; then
        echo "ÉCHEC : rien n'a changé, l'ancienne version tourne comme avant."
      else
        echo "ÉCHEC : la version précédente est remise et relancée. Rien n'est perdu."
      fi
      tg_alert "❌ Nouvelle version NON installée ($1) L'ancienne version tourne comme avant."
    else
      ROLLBACK_INCOMPLETE=1
      if svc stop 2>/dev/null; then
        echo "⚠️ RESTAURATION INCOMPLÈTE : bot arrêté par sécurité. Sauvegarde intacte : backup_deploy/$(basename "$BACKUP")"
        tg_alert "⚠️ Installation annulée mais restauration incomplète : bot arrêté par sécurité. Sauvegarde : backup_deploy/$(basename "$BACKUP")."
      else
        echo "⚠️ RESTAURATION INCOMPLÈTE : le bot tourne encore, mais ses fichiers sont un mélange des deux versions."
        echo "   Ne le relance pas. Commande pour finir la restauration :"
        echo "   bash ~/x-degen-bot/deploy/restore_backup.sh $BACKUP"
        tg_alert "⚠️ Installation annulée mais restauration incomplète : ne relance pas le bot. Sauvegarde : backup_deploy/$(basename "$BACKUP")."
      fi
    fi
  }

  if [ ! -f "$STAGE/bot/main.py" ] || [ ! -f "$STAGE/deploy/restore_backup.sh" ]; then
    echo "Nouvelle version introuvable dans $STAGE. Rien n'a été modifié."
    return 1
  fi
  if [ ! -d "$APP_DIR" ]; then
    echo "Dossier du bot introuvable ($APP_DIR). Rien n'a été modifié."
    return 1
  fi
  # Un seul déploiement à la fois.
  exec 9>"$APP_DIR/.deploy.lock"
  if ! flock -n 9; then
    echo "Un déploiement est déjà en cours. Rien n'a été modifié."
    return 1
  fi

  # --- 1. Jamais pendant une publication -------------------------------------
  echo "=== 1/5 Publication en cours ? ==="
  if [ -f "$APP_DIR/bot/x_browser.py" ]; then
    # Ancienne version (navigateur) : ses horaires fixes, heure de Paris.
    local hm dow busy=""
    if [ -n "${XBOT_CLOCK:-}" ]; then
      read -r hm dow <<<"$XBOT_CLOCK"   # tests : "HHMM JOUR" (1 = lundi)
    else
      hm=$(TZ=Europe/Paris date +%H%M)
      dow=$(TZ=Europe/Paris date +%u)
    fi
    hm=$((10#$hm))
    if [ "$hm" -ge 725 ] && [ "$hm" -le 805 ]; then busy="post GM (7h25-8h05)"; fi
    if [ "$hm" -ge 1155 ] && [ "$hm" -le 1240 ]; then busy="post de 12h (11h55-12h40)"; fi
    if [ "$hm" -ge 1855 ] && [ "$hm" -le 1940 ]; then busy="post de 19h (18h55-19h40)"; fi
    if [ "$dow" = 2 ] && [ "$hm" -ge 1755 ] && [ "$hm" -le 1840 ]; then busy="thread du mardi (17h55-18h40)"; fi
    if [ -n "$busy" ]; then
      echo "PAS MAINTENANT : $busy de l'ancienne version. Relance la même commande dans un moment. Rien n'a été modifié."
      return 3
    fi
    echo "Libre (ancienne version, hors de ses horaires de publication)."
  else
    local msg rc
    msg=$(cd "$STAGE" && PYTHONDONTWRITEBYTECODE=1 "$PY" -m bot.main --busy-check \
          --state "$APP_DIR/state/state.json" 2>&1)
    rc=$?
    echo "$msg"
    if [ "$rc" = 3 ]; then
      echo "PAS MAINTENANT : relance la même commande dans 15-20 minutes. Rien n'a été modifié."
      return 3
    fi
  fi

  # --- 2. Bibliothèques + tests sur la nouvelle version ----------------------
  echo "=== 2/5 Bibliothèques et tests (quelques minutes) ==="
  if [ ! -x "$PY" ]; then
    python3 -m venv "$APP_DIR/.venv" || { echo "Impossible de créer .venv. Rien n'a été modifié."; return 1; }
  fi
  if [ -z "${XBOT_SKIP_PIP:-}" ]; then
    # Instantané d'avant (sauf s'il vient d'être pris par precheck.sh).
    if [ -z "$(find "$APP_DIR/.pip_freeze_avant" -mmin -180 2>/dev/null)" ]; then
      "$PY" -m pip freeze --disable-pip-version-check > "$APP_DIR/.pip_freeze_avant" 2>/dev/null || true
    fi
    if ! "$PY" -m pip install -q --disable-pip-version-check -r "$STAGE/requirements.txt"; then
      restore_pip
      echo "MISE À JOUR DES BIBLIOTHÈQUES IMPOSSIBLE : nouvelle version NON installée, le bot continue sur la version actuelle."
      return 1
    fi
    # Navigateur des captures d'écran (déjà présent avec l'ancienne version ; optionnel).
    "$PY" -m playwright install chromium >/dev/null 2>&1 || true
    echo "BIBLIOTHEQUES_OK"
  fi
  if [ -z "${XBOT_SKIP_TESTS:-}" ]; then
    local tlog
    tlog=$(mktemp)
    if (cd "$STAGE" && PYTHONDONTWRITEBYTECODE=1 timeout 900 "$PY" -m pytest -q -p no:cacheprovider tests \
          >"$tlog" 2>&1); then
      echo "TESTS $(tail -n 1 "$tlog")"
      rm -f "$tlog"
    else
      grep -E "^(FAILED|ERROR)|Error|error" "$tlog" | tail -n 30
      tail -n 1 "$tlog"
      rm -f "$tlog"
      restore_pip
      echo "TESTS EN ÉCHEC : nouvelle version NON installée, le bot continue sur la version actuelle."
      return 1
    fi
  fi

  # --- 3. Arrêt, sauvegarde, installation ------------------------------------
  echo "=== 3/5 Sauvegarde et installation ==="
  # Si le serveur n'autorise que « restart », on installe pendant qu'il tourne
  # (comme la v1) : le redémarrage final charge la nouvelle version.
  if ! svc stop 2>/dev/null; then
    echo "(Arrêt non autorisé : installation pendant que le bot tourne, redémarrage à la fin.)"
  fi
  BACKUP="$APP_DIR/backup_deploy/$(date +%Y%m%d-%H%M%S)"
  (umask 077 && mkdir -p "$BACKUP") || { svc restart; echo "Sauvegarde impossible : bot relancé tel quel."; return 1; }
  chmod 700 "$APP_DIR/backup_deploy"
  local p
  for p in $SAVED; do
    if [ -e "$APP_DIR/$p" ]; then
      mkdir -p "$(dirname "$BACKUP/$p")"
      if ! cp -a "$APP_DIR/$p" "$BACKUP/$p"; then
        svc restart
        echo "Sauvegarde impossible ($p) : bot relancé tel quel, rien installé."
        return 1
      fi
      echo "have $p" >>"$BACKUP/.xbot_backup"
    else
      echo "absent $p" >>"$BACKUP/.xbot_backup"
    fi
  done
  echo "Sauvegarde : backup_deploy/$(basename "$BACKUP")"
  # Coupure ou arrêt demandé à partir d'ici : on remet l'ancienne version.
  trap 'rollback "Installation interrompue."; if [ "$ROLLBACK_INCOMPLETE" = 1 ]; then echo "FIN_DEPLOIEMENT 5"; exit 5; fi; echo "FIN_DEPLOIEMENT 1"; exit 1' INT TERM HUP

  local d f ok=1
  for d in $INSTALL_DIRS; do
    rm -rf "$APP_DIR/$d.new" \
      && cp -a "$STAGE/$d" "$APP_DIR/$d.new" \
      && find "$APP_DIR/$d.new" -name __pycache__ -type d -prune -exec rm -rf {} + \
      && rm -rf "$APP_DIR/$d" \
      && mv "$APP_DIR/$d.new" "$APP_DIR/$d" \
      || { ok=0; break; }
  done
  if [ "$ok" = 1 ]; then
    mkdir -p "$APP_DIR/config" "$APP_DIR/state" "$APP_DIR/logs" "$APP_DIR/media" || ok=0
    for f in $INSTALL_FILES; do
      [ -f "$STAGE/$f" ] || continue
      cp "$STAGE/$f" "$APP_DIR/$f" || { ok=0; break; }
    done
    for f in settings.json content.json; do
      install_config "$f" || { ok=0; break; }
    done
  fi
  if [ "$ok" != 1 ]; then
    rollback "Copie des nouveaux fichiers impossible (disque plein ?)."
    return 1
  fi
  if ! XBOT_APP_DIR="$APP_DIR" bash "$APP_DIR/deploy/migrate_env_v2.sh"; then
    rollback "Mise à jour de config/.env impossible."
    return 1
  fi
  echo "INSTALL_OK"

  # --- 4. Vérification réelle : Telegram, API X, Claude ----------------------
  echo "=== 4/5 Vérification (Telegram, API X, Claude) ==="
  if [ -z "${XBOT_SKIP_CHECK:-}" ]; then
    if ! (cd "$APP_DIR" && timeout 300 "$PY" -m bot.main --check); then
      rollback "La vérification a échoué : lis les lignes ❌ ci-dessus (souvent une clé ou des crédits)."
      return 1
    fi
  fi

  # --- 5. Redémarrage ---------------------------------------------------------
  echo "=== 5/5 Redémarrage ==="
  if ! svc restart; then
    rollback "Redémarrage du bot impossible (droits sudo du serveur ?)."
    return 1
  fi
  if [ -n "$SERVICE" ]; then
    sleep "${XBOT_WAIT:-12}"
    if ! svc_active; then
      journal 30
      rollback "Le bot ne démarre pas (voir le journal ci-dessus)."
      return 1
    fi
    journal 15
  fi
  trap - INT TERM HUP

  # Ménage : 5 dernières sauvegardes datées, anciens dossiers d'envoi et journaux.
  ls -1d "$APP_DIR"/backup_deploy/20*/ 2>/dev/null | sort | head -n "-$KEEP_BACKUPS" | xargs -r rm -rf
  # Seulement les envois PLUS ANCIENS que celui-ci, et seulement s'il en fait partie
  # (jamais le dossier d'où l'on tourne, ni rien pendant les tests).
  local old base
  base="$(basename "$STAGE")"
  case "$base" in
    xbv2_stage_*)
      for old in "$(dirname "$STAGE")"/xbv2_stage_*; do
        if [ -d "$old" ] && [[ "$(basename "$old")" < "$base" ]]; then rm -rf "$old"; fi
      done
      ls -1t "$(dirname "$STAGE")"/xbv2_deploy_*.log 2>/dev/null | tail -n +6 | xargs -r rm -f ;;
  esac
  echo "V2_OK : installation terminée, le bot tourne."
}
main "$@"
rc=$?
# 5 = restauration incomplète (le message ci-dessus dit quoi faire).
if [ "$rc" != 0 ] && [ "${ROLLBACK_INCOMPLETE:-0}" = 1 ]; then rc=5; fi
echo "FIN_DEPLOIEMENT $rc"
exit $rc
