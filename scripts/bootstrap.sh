#!/usr/bin/env sh
set -eu

for dependency in ffmpeg ffprobe curl; do
  if ! command -v "$dependency" >/dev/null 2>&1; then
    echo "missing required dependency: $dependency" >&2
    exit 1
  fi
done

PLUGIN_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
python3 -m venv "$PLUGIN_ROOT/.venv"
"$PLUGIN_ROOT/.venv/bin/python" -m pip install -e "${1:-$PLUGIN_ROOT}"
echo "Installed in $PLUGIN_ROOT/.venv; the bundled bin/audiobook-curator wrapper uses it automatically."
echo "Optional acoustic matching: '$PLUGIN_ROOT/.venv/bin/python' -m pip install -e '$PLUGIN_ROOT[acoustic]'"
echo "Whisper checks additionally require whisper-cli and a local ggml model."
echo "Audiobook Forge is optional; when installed, convert can use --engine audiobook-forge --quality source semantics."
