#!/usr/bin/env bash
# Sur ton Mac, dans le Terminal : télécharge la DERNIÈRE version depuis GitHub, puis
# lance l'assistant d'installation (push_from_mac.sh). Une seule ligne à coller :
#   curl -fsSL https://raw.githubusercontent.com/7rg2r4rm29-dotcom/sabislabi-x-bot/main/deploy/install_from_github.sh | bash
# Rien d'autre à télécharger ni à décompresser. Compatible avec le bash 3.2 du Mac.
main() {
  set -u
  local zip_url="${XBOT_ZIP_URL:-https://github.com/7rg2r4rm29-dotcom/sabislabi-x-bot/archive/refs/heads/main.zip}"
  local dir script
  dir=$(mktemp -d "${TMPDIR:-/tmp}/xbot_maj.XXXXXX") || { echo "Impossible de créer un dossier temporaire."; return 1; }
  echo "Téléchargement de la dernière version…"
  if ! curl -fsSL "$zip_url" -o "$dir/main.zip"; then
    echo "Téléchargement impossible (connexion ?). Rien n'a changé : relance la même commande."
    return 1
  fi
  if ! unzip -q "$dir/main.zip" -d "$dir"; then
    echo "Archive illisible. Rien n'a changé : relance la même commande."
    return 1
  fi
  script=$(ls "$dir"/*/deploy/push_from_mac.sh 2>/dev/null | head -n 1)
  if [ -z "$script" ]; then
    echo "Installateur introuvable dans l'archive. Rien n'a changé."
    return 1
  fi
  # Lancé par « curl … | bash » : le clavier est sur /dev/tty, pas sur l'entrée standard.
  if (exec </dev/tty) 2>/dev/null; then
    bash "$script" </dev/tty
  else
    bash "$script"
  fi
}
main "$@"
exit $?
