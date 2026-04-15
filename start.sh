#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

UNAME_S="$(uname -s 2>/dev/null | tr '[:upper:]' '[:lower:]')"
if [[ -z "${MSYSTEM:-}" && ( -n "${WSL_DISTRO_NAME:-}" || -n "${WSL_INTEROP:-}" || "$UNAME_S" == linux* ) ]]; then
  GIT_BASH_ROOT="$ROOT"
  if [[ "$GIT_BASH_ROOT" =~ ^/mnt/([a-zA-Z])/(.*)$ ]]; then
    GIT_BASH_ROOT="/${BASH_REMATCH[1],,}/${BASH_REMATCH[2]}"
  fi
  GIT_BASH_ROOT="${GIT_BASH_ROOT//\\//}"
  cat >&2 <<EOF
Unsupported shell runtime: WSL/Linux bash.

start.sh is supported only from Git Bash or from PowerShell via Git bash.exe.

Use one of these launch paths instead:
  1. Open Git Bash in the repo and run: ./start.sh
  2. From PowerShell run: ./start.ps1
  3. From PowerShell run:
       & 'C:\\Program Files\\Git\\bin\\bash.exe' -lc "cd '$GIT_BASH_ROOT' && ./start.sh"
EOF
  exit 1
fi

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
  echo "Python 3.12 was not found in .venv312 or .venv. Create a repo-local Python 3.12 virtual environment first." >&2
  exit 1
fi
echo "Python: $PYTHON_BIN"

PYTHON_VERSION="$("$PYTHON_BIN" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
if [[ "$PYTHON_VERSION" != "3.12" ]]; then
  echo "Unsupported Python at $PYTHON_BIN. FindMyJob requires a repo-local Python 3.12 virtual environment." >&2
  exit 1
fi

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

# -- Frontend build --
if [[ "$SKIP_FRONTEND_BUILD" != "1" ]]; then
  if ! command -v npm >/dev/null 2>&1; then
    echo "npm is required to build the React frontend." >&2
    exit 1
  fi
  if [[ ! -d "$ROOT/frontend/node_modules" ]]; then
    npm --prefix "$ROOT/frontend" install
  fi
  npm --prefix "$ROOT/frontend" run build
fi

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
