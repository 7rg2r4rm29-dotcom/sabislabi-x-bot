#!/usr/bin/env bash
# Voix off des vidéos explicatives : installe UNE FOIS les fichiers du modèle Kokoro (licence
# Apache 2.0) dans ~/xbot-models/kokoro, hors du dossier du bot (une installation ne les efface
# jamais), et le paquet kokoro-onnx dans l'environnement du bot.
# Source : github.com/thewh1teagle/kokoro-onnx, version « model-files-v1.0 » (354 Mo en tout).
# Tailles et empreintes vérifiées ; rien à saisir, aucune clé.
# Sur le serveur :  bash ~/x-degen-bot/deploy/install_voice.sh
set -euo pipefail
DIR="${VOICE_MODEL_DIR:-$HOME/xbot-models/kokoro}"
URL="https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
APP="${XBOT_APP_DIR:-$HOME/x-degen-bot}"
mkdir -p "$DIR"

fetch() {
  local name="$1" sha="$2"
  if [ -s "$DIR/$name" ] && echo "$sha  $DIR/$name" | sha256sum -c --quiet - >/dev/null 2>&1; then
    echo "OK   $name (déjà installé)"
    return 0
  fi
  curl -fsSL --retry 2 -m 900 -o "$DIR/$name.part" "$URL/$name"
  if ! echo "$sha  $DIR/$name.part" | sha256sum -c --quiet - >/dev/null 2>&1; then
    rm -f "$DIR/$name.part"
    echo "ÉCHEC $name : fichier différent de celui attendu (rien n'est installé)"
    exit 1
  fi
  mv "$DIR/$name.part" "$DIR/$name"
  echo "OK   $name"
}

fetch kokoro-v1.0.onnx 7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5
fetch voices-v1.0.bin bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d
"$APP/.venv/bin/python" -m pip install -q "kokoro-onnx==0.6.1"
echo "VOIX_OK : modèle dans $DIR"
