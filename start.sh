#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

# Cross-platform launcher: supports macOS, Linux, and Git Bash on Windows.
# WSL is supported as a Linux runtime. Native Windows uses start.ps1 / start.bat.

HOST="${FMJ_HOST:-127.0.0.1}"
PORT="${FMJ_PORT:-8765}"
PAGE="${FMJ_PAGE:-}"
OPEN_BROWSER="${FMJ_OPEN_BROWSER:-1}"
SKIP_FRONTEND_BUILD="${SKIP_FRONTEND_BUILD:-0}"
REQUIRE_LMSTUDIO_PREFLIGHT="${FMJ_REQUIRE_LMSTUDIO_PREFLIGHT:-0}"
FORWARD_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-frontend-build)
      SKIP_FRONTEND_BUILD=1
      shift
      ;;
    --)
      shift
      while [[ $# -gt 0 ]]; do
        if [[ -n "$1" ]]; then
          FORWARD_ARGS+=("$1")
        fi
        shift
      done
      ;;
    *)
      if [[ -n "$1" ]]; then
        FORWARD_ARGS+=("$1")
      fi
      shift
      ;;
  esac
done

bootstrap_python=()

run_bootstrap() {
  local script_path="$ROOT/scripts/bootstrap_env.py"
  if [[ ${#bootstrap_python[@]} -eq 0 ]]; then
    if [[ -f "$ROOT/.venv312/Scripts/python.exe" ]]; then
      bootstrap_python=("$ROOT/.venv312/Scripts/python.exe")
    elif [[ -f "$ROOT/.venv/Scripts/python.exe" ]]; then
      bootstrap_python=("$ROOT/.venv/Scripts/python.exe")
    elif command -v py >/dev/null 2>&1 && py -3.12 -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)' >/dev/null 2>&1; then
      bootstrap_python=(py -3.12)
    elif command -v python.exe >/dev/null 2>&1 && python.exe -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)' >/dev/null 2>&1; then
      bootstrap_python=(python.exe)
    elif command -v python3.12 >/dev/null 2>&1; then
      bootstrap_python=(python3.12)
    elif command -v python >/dev/null 2>&1 && python -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)' >/dev/null 2>&1; then
      bootstrap_python=(python)
    else
      echo "Python 3.12 was not found. Install Python 3.12 or create .venv312 manually before launching FindMyJob." >&2
      exit 1
    fi
  fi

  "${bootstrap_python[@]}" "$script_path" --project-root "$ROOT"
}

# -- Choose Python from the virtual environment without sourcing activate --
# Sourcing the Windows bash activate script under Git Bash can fail on `uname`.
if [[ -f "$ROOT/.venv312/Scripts/python.exe" ]]; then
  PYTHON_BIN="$ROOT/.venv312/Scripts/python.exe"
elif [[ -f "$ROOT/.venv/Scripts/python.exe" ]]; then
  PYTHON_BIN="$ROOT/.venv/Scripts/python.exe"
elif [[ -f "$ROOT/.venv312/bin/python" ]]; then
  PYTHON_BIN="$ROOT/.venv312/bin/python"
elif [[ -f "$ROOT/.venv/bin/python" ]]; then
  PYTHON_BIN="$ROOT/.venv/bin/python"
else
  run_bootstrap
  if [[ -f "$ROOT/.venv312/Scripts/python.exe" ]]; then
    PYTHON_BIN="$ROOT/.venv312/Scripts/python.exe"
  elif [[ -f "$ROOT/.venv/Scripts/python.exe" ]]; then
    PYTHON_BIN="$ROOT/.venv/Scripts/python.exe"
  elif [[ -f "$ROOT/.venv312/bin/python" ]]; then
    PYTHON_BIN="$ROOT/.venv312/bin/python"
  elif [[ -f "$ROOT/.venv/bin/python" ]]; then
    PYTHON_BIN="$ROOT/.venv/bin/python"
  else
    echo "Bootstrap completed, but the repo-local Python environment was still not found under .venv312 or .venv." >&2
    exit 1
  fi
fi
echo "Python: $PYTHON_BIN"

PYTHON_VERSION="$("$PYTHON_BIN" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
if [[ "$PYTHON_VERSION" != "3.12" ]]; then
  echo "Unsupported Python at $PYTHON_BIN. FindMyJob requires a repo-local Python 3.12 virtual environment." >&2
  exit 1
fi

bootstrap_python=("$PYTHON_BIN")
run_bootstrap

if ! "$PYTHON_BIN" "$ROOT/scripts/lmstudio_preflight.py" --workspace "$ROOT"; then
  case "${REQUIRE_LMSTUDIO_PREFLIGHT,,}" in
    1|true|yes)
      echo "LM Studio preflight failed. Start LM Studio, load the configured models, then retry." >&2
      exit 1
      ;;
    *)
      echo "Warning: LM Studio preflight failed. Starting the web console anyway so readiness, settings, and review pages stay available." >&2
      ;;
  esac
fi

BUILD_CMD=("$PYTHON_BIN" -m findmyjob build --workspace "$ROOT")
if [[ "$SKIP_FRONTEND_BUILD" == "1" ]]; then
  BUILD_CMD+=(--skip-frontend-build)
  export SKIP_FRONTEND_BUILD=1
fi

"${BUILD_CMD[@]}"

"$PYTHON_BIN" - <<'PY' "$HOST" "$PORT"
import socket
import sys

host = sys.argv[1]
port = int(sys.argv[2])
family = socket.AF_INET6 if ":" in host and host != "0.0.0.0" else socket.AF_INET
bind_target = (host, port, 0, 0) if family == socket.AF_INET6 else (host, port)
with socket.socket(family, socket.SOCK_STREAM) as probe:
    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        probe.bind(bind_target)
    except OSError:
        print(
            f"Port {port} is already in use on {host}. Stop the previous backend before starting a new run. "
            f"Run ./scripts/kill_servers.sh or .\\scripts\\kill_servers.ps1 first.",
            file=sys.stderr,
        )
        raise SystemExit(1)
PY

export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

CMD=("$PYTHON_BIN" -m findmyjob start --workspace "$ROOT" --host "$HOST" --port "$PORT")
if [[ -n "$PAGE" ]]; then
  CMD+=(--page "$PAGE")
fi
if [[ "$OPEN_BROWSER" == "0" || "$OPEN_BROWSER" == "false" ]]; then
  CMD+=(--no-open)
fi
if [[ ${#FORWARD_ARGS[@]} -gt 0 ]]; then
  CMD+=("${FORWARD_ARGS[@]}")
fi

"${CMD[@]}"
