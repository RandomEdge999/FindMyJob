#!/usr/bin/env bash
# FindMyJob managed installer for macOS and Linux.
#
# This script clones (or updates) FindMyJob into a per-user managed location,
# bootstraps the repo-local Python 3.12 virtualenv via start.sh, and writes
# stable launcher shims (`findmyjob`, `findmyjob-update`) on the user's PATH.
#
# Public one-liner:
#   curl -fsSL https://raw.githubusercontent.com/RandomEdge999/FindMyJob/main/install.sh | bash
#
# Flags:
#   --install-dir <path>   Install into <path> instead of the per-OS default.
#   --repo-url <url>       Override the upstream Git URL (defaults to RandomEdge999/FindMyJob).
#   --branch <name>        Override the branch (default: main).
#   --no-launch            Skip launching FindMyJob after install.
#   --no-open              Pass through to start.sh (do not open browser).
#   --skip-frontend-build  Pass through to start.sh.
#   --no-path-update       Do not modify the user's shell rc PATH.
#   --force-archive        Use GitHub archive download even if git is available.
#   --yes                  Non-interactive: accept the default install location.
#
# Environment overrides:
#   FMJ_INSTALL_ROOT, FMJ_REPO_URL, FMJ_BRANCH

set -Eeuo pipefail

REPO_URL_DEFAULT="https://github.com/RandomEdge999/FindMyJob.git"
BRANCH_DEFAULT="main"

INSTALL_ROOT="${FMJ_INSTALL_ROOT:-}"
REPO_URL="${FMJ_REPO_URL:-$REPO_URL_DEFAULT}"
BRANCH="${FMJ_BRANCH:-$BRANCH_DEFAULT}"
NO_LAUNCH=0
NO_OPEN=0
SKIP_FRONTEND_BUILD=0
NO_PATH_UPDATE=0
FORCE_ARCHIVE=0
ASSUME_YES=0
FORWARD_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --install-dir) INSTALL_ROOT="$2"; shift 2 ;;
    --repo-url) REPO_URL="$2"; shift 2 ;;
    --branch) BRANCH="$2"; shift 2 ;;
    --no-launch) NO_LAUNCH=1; shift ;;
    --no-open) NO_OPEN=1; shift ;;
    --skip-frontend-build) SKIP_FRONTEND_BUILD=1; shift ;;
    --no-path-update) NO_PATH_UPDATE=1; shift ;;
    --force-archive) FORCE_ARCHIVE=1; shift ;;
    --yes|-y) ASSUME_YES=1; shift ;;
    --) shift; while [[ $# -gt 0 ]]; do FORWARD_ARGS+=("$1"); shift; done ;;
    *) FORWARD_ARGS+=("$1"); shift ;;
  esac
done

OS_KIND="$(uname -s 2>/dev/null || echo unknown)"

default_install_root() {
  case "$OS_KIND" in
    Darwin) echo "$HOME/Library/Application Support/FindMyJob" ;;
    Linux) echo "${XDG_DATA_HOME:-$HOME/.local/share}/findmyjob" ;;
    *) echo "$HOME/.findmyjob" ;;
  esac
}

python_install_hint() {
  case "$OS_KIND" in
    Darwin) echo "  Install with Homebrew:  brew install python@3.12" ;;
    Linux)
      echo "  Debian/Ubuntu:  sudo apt-get install -y python3.12 python3.12-venv"
      echo "  Fedora/RHEL:    sudo dnf install -y python3.12"
      echo "  Arch:           sudo pacman -S python"
      ;;
    *) echo "  Install Python 3.12 from https://www.python.org/downloads/ and rerun." ;;
  esac
}

resolve_python312() {
  local candidates=(python3.12 python3 python)
  if command -v pyenv >/dev/null 2>&1; then
    if pyenv shims 2>/dev/null | grep -q '^python3\.12$'; then
      candidates=(python3.12 "${candidates[@]}")
    fi
  fi
  for cand in "${candidates[@]}"; do
    if command -v "$cand" >/dev/null 2>&1; then
      if "$cand" -c 'import sys; sys.exit(0 if sys.version_info[:2]==(3,12) else 1)' >/dev/null 2>&1; then
        echo "$cand"
        return 0
      fi
    fi
  done
  echo "Python 3.12 is required for FindMyJob but was not found on PATH." >&2
  python_install_hint >&2
  return 1
}

prompt_install_root() {
  local default_root
  default_root="$(default_install_root)"
  if [[ -n "$INSTALL_ROOT" ]]; then
    return 0
  fi
  if [[ "$ASSUME_YES" == "1" ]] || [[ ! -t 0 ]]; then
    INSTALL_ROOT="$default_root"
    return 0
  fi
  printf "Install FindMyJob into [%s]: " "$default_root" >&2
  local reply
  IFS= read -r reply || reply=""
  if [[ -z "$reply" ]]; then
    INSTALL_ROOT="$default_root"
  else
    INSTALL_ROOT="$reply"
  fi
}

canonical_repo_url() {
  local value="$1"
  value="${value%/}"
  value="${value%.git}"
  if [[ "$value" =~ ^git@github\.com:([^/]+)/([^/]+)$ ]]; then
    value="https://github.com/${BASH_REMATCH[1]}/${BASH_REMATCH[2]}"
  elif [[ "$value" =~ ^ssh://git@github\.com/([^/]+)/([^/]+)$ ]]; then
    value="https://github.com/${BASH_REMATCH[1]}/${BASH_REMATCH[2]}"
  fi
  printf '%s' "$(echo "$value" | tr '[:upper:]' '[:lower:]')"
}

backup_existing_repo() {
  local repo_root="$1"
  local install_root="$2"
  local backup_dir
  backup_dir="$install_root/backups"
  mkdir -p "$backup_dir"
  local stamp
  stamp="$(date +%Y%m%d-%H%M%S)"
  local target="$backup_dir/repo-$stamp"
  mv "$repo_root" "$target"
  echo "Moved existing managed checkout to $target" >&2
}

sync_repo_with_git() {
  local repo_root="$1"
  local install_root="$2"
  if [[ ! -d "$repo_root" ]]; then
    git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$repo_root"
    return
  fi
  if [[ ! -d "$repo_root/.git" ]]; then
    backup_existing_repo "$repo_root" "$install_root"
    git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$repo_root"
    return
  fi

  local existing_remote=""
  if existing_remote="$(git -C "$repo_root" remote get-url origin 2>/dev/null)"; then :; else existing_remote=""; fi

  local dirty=""
  dirty="$(git -C "$repo_root" status --porcelain --untracked-files=no 2>/dev/null || echo dirty)"

  local url_mismatch=0
  if [[ -z "$existing_remote" ]] || [[ "$(canonical_repo_url "$existing_remote")" != "$(canonical_repo_url "$REPO_URL")" ]]; then
    url_mismatch=1
  fi

  if [[ -n "$dirty" || "$url_mismatch" == "1" ]]; then
    backup_existing_repo "$repo_root" "$install_root"
    git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$repo_root"
    return
  fi

  git -C "$repo_root" fetch --depth 1 origin "$BRANCH"
  local current_branch
  current_branch="$(git -C "$repo_root" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")"
  if [[ -z "$current_branch" ]]; then
    git -C "$repo_root" checkout -B "$BRANCH" FETCH_HEAD
  elif [[ "$current_branch" != "$BRANCH" ]]; then
    git -C "$repo_root" checkout "$BRANCH"
  fi
  git -C "$repo_root" merge --ff-only FETCH_HEAD
}

archive_url() {
  local origin="$1"
  local trimmed="${origin%.git}"
  if [[ "$trimmed" =~ ^https://github\.com/([^/]+)/([^/]+)$ ]]; then
    echo "https://codeload.github.com/${BASH_REMATCH[1]}/${BASH_REMATCH[2]}/tar.gz/refs/heads/$BRANCH"
    return 0
  fi
  echo "Archive fallback only supports GitHub HTTPS URLs (got: $origin)" >&2
  return 1
}

sync_repo_from_archive() {
  local repo_root="$1"
  local install_root="$2"
  local url
  url="$(archive_url "$REPO_URL")"
  local tmpdir
  tmpdir="$(mktemp -d)"
  trap 'rm -rf "$tmpdir"' RETURN
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$url" -o "$tmpdir/repo.tar.gz"
  elif command -v wget >/dev/null 2>&1; then
    wget -qO "$tmpdir/repo.tar.gz" "$url"
  else
    echo "Neither curl nor wget is available for archive fallback." >&2
    return 1
  fi
  mkdir -p "$tmpdir/expanded"
  tar -xzf "$tmpdir/repo.tar.gz" -C "$tmpdir/expanded"
  local extracted
  extracted="$(find "$tmpdir/expanded" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
  if [[ -z "$extracted" ]]; then
    echo "Archive did not contain a repository directory." >&2
    return 1
  fi
  if [[ -d "$repo_root" ]]; then
    backup_existing_repo "$repo_root" "$install_root"
  fi
  mkdir -p "$(dirname "$repo_root")"
  mv "$extracted" "$repo_root"
}

write_install_metadata() {
  local install_root="$1"
  local repo_root="$2"
  local mode="$3"
  local metadata="$install_root/install-metadata.json"
  local stamp
  stamp="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
  cat >"$metadata" <<EOF
{
  "install_root": "$install_root",
  "repo_root": "$repo_root",
  "repo_url": "$REPO_URL",
  "branch": "$BRANCH",
  "mode": "$mode",
  "updated_at": "$stamp"
}
EOF
}

write_launcher_files() {
  local install_root="$1"
  local repo_root="$2"
  local bin_root="$install_root/bin"
  mkdir -p "$bin_root"

  cat >"$bin_root/findmyjob" <<EOF
#!/usr/bin/env bash
exec "$repo_root/start.sh" "\$@"
EOF
  chmod +x "$bin_root/findmyjob"

  cat >"$bin_root/findmyjob-update" <<EOF
#!/usr/bin/env bash
exec "$repo_root/install.sh" --install-dir "$install_root" --repo-url "$REPO_URL" --branch "$BRANCH" --no-launch --yes "\$@"
EOF
  chmod +x "$bin_root/findmyjob-update"
}

shell_rc_path() {
  local shell_name
  shell_name="$(basename "${SHELL:-/bin/bash}")"
  case "$shell_name" in
    zsh) echo "$HOME/.zshrc" ;;
    bash)
      if [[ "$OS_KIND" == "Darwin" && -f "$HOME/.bash_profile" ]]; then
        echo "$HOME/.bash_profile"
      else
        echo "$HOME/.bashrc"
      fi
      ;;
    fish) echo "$HOME/.config/fish/config.fish" ;;
    *) echo "$HOME/.profile" ;;
  esac
}

update_user_path() {
  local bin_root="$1"
  local rc_path
  rc_path="$(shell_rc_path)"
  local marker="# Added by FindMyJob installer"
  local line="export PATH=\"$bin_root:\$PATH\""
  case "$rc_path" in
    *config/fish/config.fish) line="set -gx PATH \"$bin_root\" \$PATH" ;;
  esac
  if [[ -f "$rc_path" ]] && grep -Fq "$bin_root" "$rc_path"; then
    return 1
  fi
  mkdir -p "$(dirname "$rc_path")"
  {
    printf '\n%s\n' "$marker"
    printf '%s\n' "$line"
  } >>"$rc_path"
  return 0
}

invoke_findmyjob_launch() {
  local repo_root="$1"
  local args=()
  if [[ "$NO_OPEN" == "1" ]]; then
    export FMJ_OPEN_BROWSER=0
  fi
  if [[ "$SKIP_FRONTEND_BUILD" == "1" ]]; then
    args+=(--skip-frontend-build)
  fi
  if [[ ${#FORWARD_ARGS[@]} -gt 0 ]]; then
    args+=("${FORWARD_ARGS[@]}")
  fi
  exec "$repo_root/start.sh" "${args[@]}"
}

# --- main ---------------------------------------------------------------------

resolve_python312 >/dev/null
prompt_install_root

# Expand ~ if present.
INSTALL_ROOT="${INSTALL_ROOT/#\~/$HOME}"
mkdir -p "$INSTALL_ROOT"
INSTALL_ROOT="$(cd "$INSTALL_ROOT" && pwd)"
REPO_ROOT="$INSTALL_ROOT/repo"

SYNC_MODE="git"
if [[ "$FORCE_ARCHIVE" == "1" ]] || ! command -v git >/dev/null 2>&1; then
  SYNC_MODE="archive"
fi

echo "Installing FindMyJob into $INSTALL_ROOT" >&2
if [[ "$SYNC_MODE" == "git" ]]; then
  sync_repo_with_git "$REPO_ROOT" "$INSTALL_ROOT"
else
  sync_repo_from_archive "$REPO_ROOT" "$INSTALL_ROOT"
fi

write_install_metadata "$INSTALL_ROOT" "$REPO_ROOT" "$SYNC_MODE"
write_launcher_files "$INSTALL_ROOT" "$REPO_ROOT"
chmod +x "$REPO_ROOT/start.sh" "$REPO_ROOT/install.sh" 2>/dev/null || true

PATH_CHANGED=0
if [[ "$NO_PATH_UPDATE" != "1" ]]; then
  if update_user_path "$INSTALL_ROOT/bin"; then
    PATH_CHANGED=1
  fi
fi

cat >&2 <<EOF

Managed repo:    $REPO_ROOT
Launch command:  findmyjob
Update command:  findmyjob-update
EOF

if [[ "$PATH_CHANGED" == "1" ]]; then
  echo "Added $INSTALL_ROOT/bin to your shell rc. Open a new terminal to use 'findmyjob' by name." >&2
elif [[ "$NO_PATH_UPDATE" == "1" ]]; then
  echo "PATH was not modified. Launch manually with $INSTALL_ROOT/bin/findmyjob" >&2
fi

if [[ "$NO_LAUNCH" != "1" ]]; then
  invoke_findmyjob_launch "$REPO_ROOT"
fi
