#!/usr/bin/env bash
# runs the vr spike with your system python (pyside6, numpy, ytmusicapi,
# onnxruntime come from pacman) plus pyopenvr in a private venv.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
VENV="${XDG_CACHE_HOME:-$HOME/.cache}/tide-vr-spike/venv"
if [ ! -x "$VENV/bin/python" ]; then
    echo "first run: making $VENV and installing openvr (pyopenvr, ~6 MB)"
    python3 -m venv --system-site-packages "$VENV"
    "$VENV/bin/pip" install --quiet "openvr==2.12.1401"
fi
exec "$VENV/bin/python" "$HERE/spike.py" "$@"
