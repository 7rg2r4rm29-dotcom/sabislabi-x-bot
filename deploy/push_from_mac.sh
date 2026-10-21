#!/usr/bin/env bash
# À lancer SUR TON MAC, dans le Terminal. C'est la seule commande : ensuite,
# l'assistant te guide pas à pas.
#   bash ~/Downloads/sabislabi-x-bot-main/deploy/push_from_mac.sh
# 1. envoie le code au serveur (jamais tes clés : elles ne sont pas dans ce dossier) ;
# 2. essaie les clés DÉJÀ sur le serveur et ne te demande que ce qui manque, en
#    t'ouvrant la bonne page ; ce que tu colles va directement sur le serveur ;
# 3. installe la nouvelle version (tests, sauvegarde, vérification, retour arrière
#    automatique). L'installation continue même si la connexion se coupe.
# Compatible avec le bash 3.2 du Mac.
main() {
  set -u
  local HOST="${XBOT_HOST:-botuser@178.128.194.190}"
  local SSH="${XBOT_SSH:-ssh}"
  local OPEN="${XBOT_OPEN:-open}"
  local RESTART="${XBOT_RESTART_CMD:-sudo -n systemctl restart x-degen-bot}"
  local STAMP
  STAMP=$(date +%Y%m%d%H%M%S)
  local STAGE="xbv2_stage_$STAMP"
  local LOG="xbv2_deploy_$STAMP.log"
  local HERE
  HERE="$(cd "$(dirname "$0")/.." && pwd)"
  if [ ! -f "$HERE/bot/main.py" ] || [ ! -f "$HERE/deploy/deploy.sh" ]; then
    echo "Ce script doit rester dans le dossier deploy/ du code téléchargé (bot/main.py introuvable)."
    return 1
  fi

  ask() {  # ask "question" -> la réponse tapée (clavier du Mac ; entrée standard pour les tests)
    local a=""
    if [ -z "${XBOT_NO_TTY:-}" ] && { exec 3</dev/tty; } 2>/dev/null; then
      read -r -p "$1" a <&3 || a=""
      exec 3<&-
    else
      printf '%s' "$1" >&2
      read -r a || a=""
    fi
    printf '%s' "$a"
  }
  pause() { ask "$1 appuie sur Entrée. " >/dev/null; echo; }
  remote() { $SSH -n "$HOST" "$@"; }
  remote_tty() { $SSH -t "$HOST" "$@"; }
  open_page() {
    echo "   → J'ouvre $1"
    $OPEN "$1" >/dev/null 2>&1 || echo "   (ouvre toi-même cette adresse dans ton navigateur)"
  }
  part() { printf '%s\n' "$2" | tr ' ' '\n' | sed -n "s/^$1=//p" | head -n 1; }

  fix_x() {
    echo
    echo "━━━━━━━━ Clés de l'API X ━━━━━━━━"
    case "$1" in
      credits)
        echo "Ton compte développeur X n'a pas (ou plus) de crédit."
        open_page "https://console.x.com"
        echo "   1. Menu « Billing »."
        echo "   2. Ajoute une carte bancaire (X offre 20 \$ de crédit). Conseillé : un plafond de 30 \$ par mois."
        pause "Quand c'est fait,"
        return 0 ;;
      error)
        echo "X n'a pas répondu (réseau ou limite passagère) : nouvel essai dans 30 secondes."
        sleep "${XBOT_RETRY_SLEEP:-30}"
        return 0 ;;
      wrong_account)
        echo "⚠️ Les clés actuelles appartiennent à un AUTRE compte X que @sabislabi." ;;
    esac
    open_page "https://console.x.com"
    echo "   1. Connecte-toi avec le compte @sabislabi, puis clique sur ton app (colonne de gauche)."
    echo "   2. « Settings » → « User authentication settings » → App permissions : « Read and write » → Save."
    echo "   3. « Keys and tokens » → « API Key and Secret » → Regenerate : garde les 2 valeurs."
    echo "   4. « Keys and tokens » → « Access Token and Secret » → Regenerate : garde les 2 valeurs."
    pause "Quand tu as les 4 valeurs,"
    remote_tty "bash ~/$STAGE/deploy/set_secrets.sh X_API_KEY X_API_SECRET X_ACCESS_TOKEN X_ACCESS_TOKEN_SECRET"
  }

  fix_claude() {
    echo
    echo "━━━━━━━━ Clé Claude (Anthropic) ━━━━━━━━"
    open_page "https://console.anthropic.com/settings/keys"
    echo "   1. « Create Key », nom : sabislabi-bot, puis copie la clé."
    if [ "${1:-}" = rotate ]; then
      echo "   2. Dans la même liste, supprime l'ANCIENNE clé (icône poubelle)."
    else
      echo "   2. Si ton compte n'a plus de crédit : menu « Billing », ajoute du crédit."
    fi
    pause "Quand tu as copié la nouvelle clé,"
    remote_tty "bash ~/$STAGE/deploy/set_secrets.sh ANTHROPIC_API_KEY"
  }

  fix_telegram() {
    echo
    echo "━━━━━━━━ Telegram ━━━━━━━━"
    local t
    t=$(remote "bash ~/$STAGE/deploy/telegram_setup.sh check" 2>&1 | tail -n 1)
    if [ "${1:-}" = rotate ] || [ "${t%% *}" != TOKEN_OK ]; then
      open_page "https://t.me/BotFather"
      echo "   1. Dans Telegram, ouvre la conversation avec @BotFather."
      if [ "${1:-}" = rotate ]; then
        echo "   2. Envoie /revoke puis choisis ton bot : BotFather te donne un NOUVEAU jeton (token). Copie-le."
      else
        echo "   2. Envoie /mybots → choisis ton bot → « API Token » : copie le jeton."
      fi
      pause "Quand tu as copié le jeton,"
      remote_tty "bash ~/$STAGE/deploy/set_secrets.sh TELEGRAM_BOT_TOKEN" || return 1
      t=$(remote "bash ~/$STAGE/deploy/telegram_setup.sh check" 2>&1 | tail -n 1)
      if [ "${t%% *}" != TOKEN_OK ]; then
        echo "Telegram refuse ce jeton (copie incomplète ?). Relance la même commande pour le recoller."
        return 1
      fi
    fi
    [ "${1:-}" = rotate ] && return 0
    echo "   Dans Telegram, ouvre ta conversation avec ${t#TOKEN_OK } et envoie-lui : /start"
    pause "Quand c'est envoyé,"
    remote "bash ~/$STAGE/deploy/telegram_setup.sh chatid"
  }

  check_keys() {  # affiche la vérification ; 0 si tout est OK, 2 si quelque chose manque, 1 si erreur
    local out parts
    out=$(remote "bash ~/$STAGE/deploy/precheck.sh ~/$STAGE" 2>&1)
    printf '%s\n' "$out" | grep -v '^PARTS '
    parts=$(printf '%s\n' "$out" | grep '^PARTS ' | tail -n 1)
    case "$parts" in
      "") echo "Le serveur n'a pas répondu. Rien n'a été modifié : relance la même commande dans un moment."
          return 1 ;;
      *server=*) echo "Le serveur n'a pas pu préparer la vérification (voir ci-dessus). Rien n'a été modifié."
          return 1 ;;
    esac
    TG=$(part telegram "$parts"); XS=$(part x "$parts"); CL=$(part claude "$parts")
    [ "$TG" = ok ] && [ "$XS" = ok ] && [ "$CL" = ok ] && return 0
    return 2
  }

  echo "=== 1/4 Envoi du code au serveur ==="
  local items=() p
  for p in bot deploy tests config/settings.json config/content.json requirements.txt README.md DESIGN.md; do
    [ -e "$HERE/$p" ] && items+=("$p")
  done
  # COPYFILE_DISABLE : pas de fichiers ._ du Mac ; les attributs macOS sont ignorés côté serveur.
  if ! COPYFILE_DISABLE=1 tar -C "$HERE" --exclude=__pycache__ --exclude=.DS_Store -czf - "${items[@]}" \
      | $SSH "$HOST" "mkdir -p ~/$STAGE && tar --warning=no-unknown-keyword -xzf - -C ~/$STAGE && echo ENVOI_OK"; then
    echo "Envoi impossible : vérifie ta connexion internet, puis relance la même commande."
    return 1
  fi

  echo "=== 2/4 Vérification de tes clés (celles déjà sur le serveur) ==="
  local round=0 rc
  TG="" XS="" CL="" KEYS_CHANGED=0
  while :; do
    check_keys
    rc=$?
    [ "$rc" = 0 ] && break
    [ "$rc" = 1 ] && return 1
    round=$((round + 1))
    if [ "$round" -gt 3 ]; then
      echo "Il reste un problème avec une clé (voir les lignes ❌). Rien n'a été modifié sur ton bot."
      return 1
    fi
    case "$XS" in ok|credits|error) ;; *) KEYS_CHANGED=1 ;; esac
    [ "$CL" = ok ] || KEYS_CHANGED=1
    [ "$TG" = ok ] || KEYS_CHANGED=1
    [ "$XS" = ok ] || fix_x "$XS" || return 1
    [ "$CL" = ok ] || fix_claude || return 1
    [ "$TG" = ok ] || fix_telegram "$TG" || return 1
    echo "=== 2/4 Nouvelle vérification ==="
  done
  echo "✅ Telegram, l'API X et Claude fonctionnent."

  echo "=== 3/4 Installation (5 à 10 minutes, tu peux laisser cette fenêtre ouverte) ==="
  if ! remote "nohup setsid bash ~/$STAGE/deploy/deploy.sh ~/$STAGE > ~/$LOG 2>&1 < /dev/null & echo LANCEMENT_OK"; then
    echo "Le serveur n'a pas pu lancer l'installation. Rien n'a été modifié : relance la même commande."
    return 1
  fi
  remote "bash ~/$STAGE/deploy/follow.sh ~/$LOG"
  rc=$?
  echo
  echo "=== 4/4 Résultat ==="
  case "$rc" in
    0) echo "✅ C'est fait : la nouvelle version tourne. Sur Telegram, envoie /statut pour voir le planning." ;;
    3) echo "⏳ Une publication était en cours : relance la même commande dans 20 minutes. Le bot n'a pas changé." ;;
    4|255) echo "La connexion s'est coupée (ou l'installation est longue), mais elle continue toute seule sur le serveur."
         echo "Dans 15 minutes, regarde Telegram : « Bot v2 démarré » = c'est bon ; un message ❌ sinon." ;;
    5) echo "⚠️ L'installation a échoué ET la restauration est incomplète : suis les instructions juste au-dessus" ;;
    *) echo "❌ L'installation n'a pas abouti : l'ancienne version tourne comme avant (détails ci-dessus et sur Telegram)." ;;
  esac
  if [ "$rc" = 1 ] && [ "$KEYS_CHANGED" = 1 ]; then
    # Les clés corrigées sont déjà sur le serveur : on relance le bot pour qu'il les prenne.
    remote "$RESTART" >/dev/null 2>&1 \
      && echo "Le bot a été relancé pour prendre en compte les clés que tu viens d'enregistrer."
  fi
  [ "$rc" = 0 ] || return "$rc"

  # Après une installation réussie seulement : remplacer aussi les clés qui marchent.
  local rot
  echo
  rot=$(ask "Dernière étape, recommandée (vol du 9 octobre) : remplacer aussi tes clés actuelles, environ 5 min ? (o/N) ")
  case "$rot" in
    o|O|oui|OUI|y|Y|yes)
      fix_x rotate && fix_claude rotate && fix_telegram rotate || {
        remote "$RESTART" >/dev/null 2>&1
        echo "Remplacement interrompu. Le bot a été relancé avec les clés enregistrées ; relance la commande pour finir."
        return 1
      }
      echo "Relance du bot avec les nouvelles clés…"
      remote "$RESTART" >/dev/null 2>&1 \
        || echo "(relance impossible d'ici : le bot prendra les nouvelles clés à son prochain redémarrage)"
      echo "=== Vérification des nouvelles clés ==="
      if check_keys; then
        echo "✅ Nouvelles clés en place. Pense à supprimer les anciennes clés X et Claude sur leurs sites."
      else
        echo "⚠️ Une nouvelle clé ne marche pas (voir ❌) : relance la même commande, l'assistant te guidera."
        return 1
      fi ;;
    *) echo "D'accord. Tu pourras le faire plus tard en relançant la même commande." ;;
  esac
  return 0
}
main "$@"
exit $?
