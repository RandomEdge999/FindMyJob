#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ -f "$ROOT/.venv312/Scripts/python.exe" ]]; then
  PYTHON_BIN="$ROOT/.venv312/Scripts/python.exe"
elif [[ -f "$ROOT/.venv/Scripts/python.exe" ]]; then
  PYTHON_BIN="$ROOT/.venv/Scripts/python.exe"
elif [[ -f "$ROOT/.venv312/bin/python" ]]; then
  PYTHON_BIN="$ROOT/.venv312/bin/python"
elif [[ -f "$ROOT/.venv/bin/python" ]]; then
  PYTHON_BIN="$ROOT/.venv/bin/python"
else
  echo "Python 3.12 was not found in .venv312 or .venv. Create a repo-local Python 3.12 virtual environment first." >&2
  exit 1
fi

PYTHON_VERSION="$("$PYTHON_BIN" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
if [[ "$PYTHON_VERSION" != "3.12" ]]; then
  echo "Unsupported Python at $PYTHON_BIN. FindMyJob requires a repo-local Python 3.12 virtual environment." >&2
  exit 1
fi

export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
"$PYTHON_BIN" -m findmyjob auto run --loop --workspace "$ROOT" "$@"
