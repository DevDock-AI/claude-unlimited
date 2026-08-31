#!/usr/bin/env bash
set -euo pipefail

# `claude-unlimited purge` is the thorough path: it also removes each Profile's
# credential from the OS keystore, which needs the config that names those
# Profiles to still exist. This script only handles the case where the CLI is
# already gone or broken.
if command -v claude-unlimited >/dev/null 2>&1; then
  exec claude-unlimited purge "$@"
fi

echo "The claude-unlimited command isn't available — removing files directly."
echo "NOTE: stored credentials cannot be removed this way, because the config"
echo "that names them is about to go. Remove entries with the service prefix"
echo "'claude-unlimited.oauth.' from your OS keystore by hand if you want them gone."
echo

# Tear down the background service first, so its files aren't orphaned. Without
# this the Linux systemd --user unit stays enabled and pointed at a python that
# no longer exists — producing exec failures on every login — and the macOS
# LaunchAgent stays registered too.
case "$(uname -s)" in
  Linux)
    if command -v systemctl >/dev/null 2>&1; then
      systemctl --user disable --now claude-unlimited.service 2>/dev/null || true
      rm -f "$HOME/.config/systemd/user/claude-unlimited.service"
      systemctl --user daemon-reload 2>/dev/null || true
    fi
    command -v loginctl >/dev/null 2>&1 && loginctl disable-linger "$(id -un)" 2>/dev/null || true
    ;;
  Darwin)
    PLIST="$HOME/Library/LaunchAgents/com.claude-unlimited.daemon.plist"
    if [ -f "$PLIST" ]; then
      launchctl bootout "gui/$(id -u)/com.claude-unlimited.daemon" 2>/dev/null || true
      rm -f "$PLIST"
    fi
    ;;
esac

rm -f "$HOME/.local/bin/claude-unlimited"
rm -rf "$HOME/.local/share/claude-unlimited" "$HOME/.claude-unlimited"
echo "Removed. ~/.claude was left untouched."
