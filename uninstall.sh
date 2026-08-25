#!/usr/bin/env bash
set -euo pipefail

# If it was registered to auto-start on login (launchd), deregister it first —
# otherwise deleting the app directory below leaves a LaunchAgent plist
# pointing at a now-missing script, which fails silently on every future login.
if command -v claude-unlimited >/dev/null 2>&1; then
  claude-unlimited uninstall || true
fi

rm -f "$HOME/.local/bin/claude-unlimited"
rm -rf "$HOME/.local/share/claude-unlimited" "$HOME/.claude-unlimited"
echo "Claude Unlimited removed. ~/.claude was left untouched."
echo "Profile credentials in macOS Keychain (service prefix claude-unlimited.oauth.*) were NOT removed automatically — remove them from Keychain Access if you want them gone too."
