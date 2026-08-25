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
"$INSTALL_ROOT/venv/bin/pip" install --upgrade pip -q
"$INSTALL_ROOT/venv/bin/pip" install "$INSTALL_ROOT/app" -q

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

echo
echo "Next steps:"
echo "  claude-unlimited doctor         # check the install"
echo "  claude-unlimited add-account    # add a Claude subscription"
echo "  claude-unlimited install        # run in the background on login"
