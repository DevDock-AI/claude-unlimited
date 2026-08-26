#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${CLAUDE_UNLIMITED_REPO:-https://github.com/DevDock-AI/claude-unlimited.git}"
REPO_BRANCH="${CLAUDE_UNLIMITED_BRANCH:-main}"
INSTALL_ROOT="$HOME/.local/share/claude-unlimited"
BIN_DIR="$HOME/.local/bin"

# Works two ways: run from a checkout (./install.sh), or piped straight from
# the network (curl … | bash), in which case there is no checkout to copy from
# and the sources are cloned into a temp directory first.
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "$(dirname "${BASH_SOURCE[0]}")/pyproject.toml" ]; then
  SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  CLONED=""
else
  SOURCE_DIR="$(mktemp -d)"
  CLONED="$SOURCE_DIR"
fi
cleanup() { [ -n "$CLONED" ] && rm -rf "$CLONED" || true; }
trap cleanup EXIT

echo "Claude Unlimited installer"
echo "=========================="

missing=""
command -v python3 >/dev/null 2>&1 || missing="$missing python3"
command -v claude  >/dev/null 2>&1 || missing="$missing claude"
[ -n "$CLONED" ] && { command -v git >/dev/null 2>&1 || missing="$missing git"; }
if [ -n "$missing" ]; then
  echo "Missing required command(s):$missing" >&2
  echo "Install them first, then re-run this installer." >&2
  exit 1
fi

if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
  echo "Python 3.10 or newer is required (found $(python3 -V 2>&1))." >&2
  exit 1
fi

if [ -n "$CLONED" ]; then
  echo "Downloading Claude Unlimited…"
  git clone --depth 1 --branch "$REPO_BRANCH" "$REPO_URL" "$SOURCE_DIR" --quiet
fi

mkdir -p "$INSTALL_ROOT" "$BIN_DIR"
rm -rf "$INSTALL_ROOT/app"
cp -R "$SOURCE_DIR" "$INSTALL_ROOT/app"
rm -rf "$INSTALL_ROOT/app/.git"
find "$INSTALL_ROOT/app" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true

echo "Setting up an isolated environment…"
python3 -m venv "$INSTALL_ROOT/venv"
# --no-cache-dir: this venv is built once and never shares wheels with
# anything else, so the cache buys nothing — and a corrupt entry in the user's
# existing pip cache prints alarming warnings during an otherwise clean
# install.
PIP="$INSTALL_ROOT/venv/bin/pip"
"$PIP" install --upgrade pip -q --no-cache-dir --disable-pip-version-check
"$PIP" install "$INSTALL_ROOT/app" -q --no-cache-dir --disable-pip-version-check

# Symlink the venv's own console_script (pyproject's [project.scripts]) so the
# command on PATH always matches what was installed, with its dependency
# resolved, instead of a wrapper hoping the system python3 has cryptography.
ln -sf "$INSTALL_ROOT/venv/bin/claude-unlimited" "$BIN_DIR/claude-unlimited"

echo
echo "Installed: $BIN_DIR/claude-unlimited"
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *)
    echo
    echo "NOTE: $BIN_DIR is not on your PATH. Add this to your shell profile:"
    echo '  export PATH="$HOME/.local/bin:$PATH"'
    ;;
esac

CLI="$BIN_DIR/claude-unlimited"

echo
echo "Checking the install…"
echo
if ! "$CLI" doctor; then
  echo
  echo "Install finished, but the check above found something. Fix it, then run:"
  echo "  claude-unlimited doctor"
  exit 1
fi

# The dashboard is where accounts are actually added, so it is worth being one
# click away rather than a command the reader has to find. Started detached:
# this makes it usable right now, while `claude-unlimited install` is what
# makes it come back on login.
PORT="${CLAUDE_UNLIMITED_PORT:-4317}"
# Digits only. It reaches a URL and a command line, and every expansion here is
# quoted, but validating the shape is cheaper than reasoning about whether
# every future use stays quoted.
case "$PORT" in
  ''|*[!0-9]*) echo "CLAUDE_UNLIMITED_PORT must be a number, got: $PORT" >&2; exit 1 ;;
esac
URL="http://127.0.0.1:${PORT}/"
if ! curl -fsS --max-time 2 "${URL}health" >/dev/null 2>&1; then
  echo
  echo "Starting the daemon…"
  nohup "$CLI" start --port "$PORT" >/dev/null 2>&1 &
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    curl -fsS --max-time 1 "${URL}health" >/dev/null 2>&1 && break
    sleep 0.5
  done
fi

echo
if curl -fsS --max-time 2 "${URL}health" >/dev/null 2>&1; then
  echo "Dashboard: $URL"
  case "$(uname -s)" in
    Darwin) open "$URL" >/dev/null 2>&1 || true ;;
    Linux)  command -v xdg-open >/dev/null 2>&1 && (xdg-open "$URL" >/dev/null 2>&1 || true) ;;
  esac
else
  echo "The daemon did not come up. Start it yourself with:"
  echo "  claude-unlimited start"
fi

echo
echo "Next steps:"
echo "  Add an API key from the dashboard, or add a subscription:"
echo "    claude-unlimited add-account         # a Claude subscription"
echo "    claude-unlimited add-codex-account   # a ChatGPT/Codex subscription"
echo "  claude-unlimited install               # keep it running, starting on login"
echo
echo "To remove everything later:"
echo "  claude-unlimited purge"
