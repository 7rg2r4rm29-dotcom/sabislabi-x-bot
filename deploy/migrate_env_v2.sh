#!/usr/bin/env bash
# Ajoute à config/.env les réglages de la v2 — SANS JAMAIS écraser une valeur
# déjà présente (tu as peut-être réglé des horaires à ta main).
# Idempotent : on peut le relancer autant qu'on veut.
# 1re sauvegarde conservée dans config/.env.avant_v2. N'affiche jamais un secret.
set -u
APP_DIR="${XBOT_APP_DIR:-$HOME/x-degen-bot}"
ENV_FILE="$APP_DIR/config/.env"
umask 077
mkdir -p "$APP_DIR/config"
[ -f "$ENV_FILE" ] || : > "$ENV_FILE"
[ -f "$ENV_FILE.avant_v2" ] || cp -p "$ENV_FILE" "$ENV_FILE.avant_v2"
# Le fichier doit finir par un saut de ligne avant qu'on ajoute quoi que ce soit.
if [ -s "$ENV_FILE" ] && [ -n "$(tail -c 1 "$ENV_FILE")" ]; then
  printf '\n' >> "$ENV_FILE"
fi

ADDED=""
setkey_default() {  # setkey_default CLE VALEUR : ajoutée seulement si la clé est absente
  if ! grep -qE "^[[:space:]]*(export[[:space:]]+)?$1=" "$ENV_FILE"; then
    printf '%s=%s\n' "$1" "$2" >> "$ENV_FILE"
    ADDED="$ADDED $1"
  fi
}

# La v1 publiait en vrai : on garde ce comportement si rien n'est indiqué.
setkey_default X_HANDLE sabislabi
setkey_default TIMEZONE Europe/Paris
setkey_default DRY_RUN false
setkey_default APPROVAL_MODE telegram
setkey_default POST_SLOTS "gm@08:30,tip@14:00,news@18:30"
setkey_default THREAD_SLOTS "tue@15:00,sun@17:00"
setkey_default DRAFT_LEAD_MINUTES "gm:660,tip:150,news:120,thread:240"
setkey_default APPROVAL_GRACE_MINUTES 120
setkey_default PUBLISH_JITTER_MINUTES 4
setkey_default QUIET_HOURS "23:30-07:30"
setkey_default REACTIVE_ENABLED true
setkey_default REACTIVE_MAX_PER_DAY 1
setkey_default REACTIVE_CHECK_EVERY_MINUTES 150
setkey_default REACTIVE_WINDOW "09:30-22:00"
setkey_default COMMENTS_ENABLED true
setkey_default COMMENTS_CHECK_EVERY_MINUTES 10
setkey_default COMMENTS_MAX_DRAFTS_PER_DAY 40
setkey_default RADAR_ENABLED true
setkey_default RADAR_MAX_PER_DAY 8
setkey_default RADAR_CHECK_EVERY_MINUTES 30
setkey_default X_BUDGET_DAILY_USD 1.50
setkey_default X_BUDGET_MONTHLY_USD 30
setkey_default HEALTH_REPORT_AT "08:30"
setkey_default WEEKLY_REPORT_AT "sun@20:30"
setkey_default SCREENSHOTS_ENABLED true
setkey_default IMAGE_AI_ENABLED false
setkey_default LLM_MODEL claude-opus-5-5
setkey_default LLM_EFFORT medium
setkey_default LLM_EFFORT_THREAD high

chmod 600 "$ENV_FILE"

echo "REGLAGES_OK${ADDED:+ (ajouté :$ADDED)}"
# Uniquement des réglages NON secrets (liste explicite).
grep -E '^(DRY_RUN|APPROVAL_MODE|POST_SLOTS|THREAD_SLOTS|DRAFT_LEAD_MINUTES|APPROVAL_GRACE_MINUTES|QUIET_HOURS|REACTIVE_ENABLED|REACTIVE_MAX_PER_DAY|COMMENTS_ENABLED|RADAR_ENABLED|RADAR_MAX_PER_DAY|X_BUDGET_DAILY_USD|X_BUDGET_MONTHLY_USD|LLM_MODEL|X_HANDLE|TIMEZONE)=' "$ENV_FILE" | tr '\n' ' '
echo
