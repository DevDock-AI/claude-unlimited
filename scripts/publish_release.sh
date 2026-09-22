#!/usr/bin/env bash
# Attach the macOS HUD to the draft release CI created for a tag, prove the
# attached file is the one the tagged tree's HUD.sha256 authorises, then
# publish. Run on the maintainer's Mac after `./build.sh --release` and after
# the Release workflow for the tag has finished. See docs/RELEASING.md.
#
# Order matters because published releases here are IMMUTABLE: once public,
# nothing can be attached. Every check runs while the release is still a draft.
set -euo pipefail
cd "$(dirname "$0")/.."

TAG="${1:?usage: scripts/publish_release.sh vX.Y.Z [release-notes.md]}"
NOTES="${2:-}"
[ -z "$NOTES" ] || [ -f "$NOTES" ] || { echo "no such notes file: $NOTES" >&2; exit 1; }
VERSION="${TAG#v}"
ASSET="HUD-$VERSION-macos.zip"
ZIP="macos-widget/dist/$ASSET"

[ -f "$ZIP" ] || { echo "missing $ZIP — run: (cd macos-widget && ./build.sh --release)" >&2; exit 1; }

draft="$(gh release view "$TAG" --json isDraft --jq .isDraft 2>/dev/null || true)"
[ "$draft" = "true" ] || { echo "$TAG is not a draft release (got: '${draft:-none}'). Did the Release workflow finish? Never re-publish a tag." >&2; exit 1; }

# The digest the INSTALLED tree will check against: read from the tag itself,
# not from the working copy, which may have moved on.
wanted="$(git show "$TAG:macos-widget/HUD.sha256" | awk -v a="$ASSET" '$2==a || $2=="*"a {print $1}')"
[ -n "$wanted" ] || { echo "HUD.sha256 at $TAG has no line for $ASSET — build.sh --release before tagging" >&2; exit 1; }
local_digest="$(shasum -a 256 "$ZIP" | awk '{print $1}')"
[ "$local_digest" = "$wanted" ] || { echo "$ZIP does not match HUD.sha256 at $TAG ($local_digest != $wanted)" >&2; exit 1; }

tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
unzip -p "$ZIP" "HUD - Heads-Up Display.app/Contents/MacOS/HUD" > "$tmp/HUD"
archs="$(lipo -archs "$tmp/HUD" 2>/dev/null || true)"
case "$archs" in *arm64*x86_64*|*x86_64*arm64*) ;; *) echo "HUD binary is not universal ($archs)" >&2; exit 1 ;; esac

gh release upload "$TAG" "$ZIP" --clobber

# Round trip: what a user will download, not what we meant to upload.
gh release download "$TAG" --pattern "$ASSET" --dir "$tmp"
remote_digest="$(shasum -a 256 "$tmp/$ASSET" | awk '{print $1}')"
[ "$remote_digest" = "$wanted" ] || { echo "uploaded asset digest $remote_digest != $wanted — NOT publishing" >&2; exit 1; }

# Optional hand-written notes replace the generated commit list — set while
# still a draft, since a published release can no longer be edited.
[ -z "$NOTES" ] || gh release edit "$TAG" --notes-file "$NOTES"
gh release edit "$TAG" --draft=false --latest
echo "published $TAG with $ASSET ($wanted)"
