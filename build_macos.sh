#!/usr/bin/env bash
# Build dist/Whoosh Clicker.app. Run from repo root with the venv active
# (or pass the path to a venv as the first argument).
set -euo pipefail

if [[ -n "${1:-}" ]]; then
    PY="$1/bin/python"
else
    PY=".venv/bin/python"
fi

if [[ ! -x "$PY" ]]; then
    echo "No python at $PY — activate your venv or pass its path as arg 1." >&2
    exit 1
fi

"$PY" -m pip install -q -e ".[dev]"
"$PY" -m PyInstaller --noconfirm clickwhoosh.spec

echo
echo "Built: dist/Whoosh Clicker.app"
echo "Move it to /Applications, then grant Accessibility in System Settings."
