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

rm -f "$HOME/.local/bin/claude-unlimited"
rm -rf "$HOME/.local/share/claude-unlimited" "$HOME/.claude-unlimited"
echo "Removed. ~/.claude was left untouched."
