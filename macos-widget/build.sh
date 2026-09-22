#!/bin/bash
# Builds "HUD - Heads-Up Display.app" and installs it to ~/Applications.
# A menu-bar app has to be a real bundle: LSUIElement is what keeps it out of
# the Dock and the app switcher, and a bare `swift build` binary has no plist.
set -euo pipefail
cd "$(dirname "$0")"

# The version this build is stamped with, read from the one place that
# defines it. The release asset's name and the checksum the installer verifies
# are both built from it, so a mismatch here would be an unfindable bug.
VERSION="$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' ../claude_unlimited/__init__.py)"
[ -n "$VERSION" ] || { echo "could not read __version__" >&2; exit 2; }

# --release: build a distributable zip and the checksum that authorises it,
# instead of installing into ~/Applications. Run on the maintainer's Mac —
# CI cannot do this, because the shaders are not in this repository.
RELEASE=""
if [ "${1:-}" = "--release" ]; then
  RELEASE="1"
  shift
fi

if [ -n "$RELEASE" ]; then
  APP="$(pwd)/dist/HUD - Heads-Up Display.app"
  NO_LAUNCH=1
else
  APP="${1:-$HOME/Applications/HUD - Heads-Up Display.app}"
fi
# The name this app shipped under before it was renamed. Removed below so an
# upgrade does not leave two copies — and two floating docks — installed.
LEGACY_APP="$HOME/Applications/CapacityWidget.app"

# This script rm -rf's $APP. Refuse anything that is not an .app bundle path,
# so a mistyped or empty argument cannot delete a directory that matters.
case "$APP" in
  *.app) ;;
  *) echo "refusing to build into '$APP': expected a path ending in .app" >&2; exit 2 ;;
esac

# The licensed shader effects, when this machine has them.
#
# They are licensed to compile and ship inside the app, not to publish, so both
# the shaders and the Swift that drives them live OUTSIDE this repository. The
# build copies them in, compiles, and deletes them again; only the compiled
# library and the built binary are ever distributed.
#
# A checkout without them builds a HUD in which every effect falls back to the
# plain SwiftUI it replaced — a supported build, which is why this is a skip and
# not an error. `HUD_EFFECTS` is what the committed code tests for.
SHADER_DIR="${HUD_SHADER_DIR:-$HOME/.config/claude-unlimited/hud-shaders}"
EFFECTS_DIR="$SHADER_DIR/hud-effects"
GENERATED="$(pwd)/Sources/HUD/Generated"
SWIFT_FLAGS=()

# Always, on every exit path: a leftover Generated/ is private source sitting in
# a git working tree, which is the one thing this arrangement exists to prevent.
cleanup_generated() { rm -rf "$GENERATED"; }
trap cleanup_generated EXIT

rm -rf "$GENERATED"
if [ -d "$EFFECTS_DIR" ] && ls "$EFFECTS_DIR"/*.swift >/dev/null 2>&1; then
  mkdir -p "$GENERATED"
  cp "$EFFECTS_DIR"/*.swift "$GENERATED/"
  SWIFT_FLAGS=(-Xswiftc -DHUD_EFFECTS)
  echo "effects: $(ls "$EFFECTS_DIR"/*.swift | wc -l | tr -d ' ') source file(s) from $EFFECTS_DIR"
else
  echo "no effects at $EFFECTS_DIR — building with the plain SwiftUI fallbacks"
fi

# A release build is universal (Apple Silicon + Intel): it is what every Mac
# that installs Claude Unlimited downloads, and an arm64-only binary installs
# fine on an Intel Mac and then never launches. A local build stays native.
if [ -n "$RELEASE" ]; then
  swift build -c release --arch arm64 --arch x86_64 "${SWIFT_FLAGS[@]}"
  BINARY=".build/apple/Products/Release/HUD"
else
  swift build -c release "${SWIFT_FLAGS[@]}"
  BINARY=".build/release/HUD"
fi

# Stop the running copy BEFORE replacing the bundle underneath it: otherwise
# the old process keeps running against a deleted bundle and you end up
# looking at a menu-bar item that is not the build you just made.
pkill -f "$APP/Contents/MacOS/HUD" 2>/dev/null || true
pkill -f "CapacityWidget.app/Contents/MacOS/CapacityWidget" 2>/dev/null || true

# Assembled in a staging directory and swapped in at the end, never built in
# place: this script used to `rm -rf "$APP"` first, so anything that failed
# afterwards (a shader that would not compile, an interrupted run) left the
# machine with NO widget installed and the Dashboard's button hidden.
STAGE_ROOT="$(mktemp -d)"
STAGE="$STAGE_ROOT/$(basename "$APP")"
# ONE exit trap for everything: a second `trap … EXIT` REPLACES the first, and
# that is how the licensed effect sources were being left behind in
# Sources/HUD/Generated after every build.
AIR_DIR=""
cleanup() { rm -rf "$GENERATED" "$STAGE_ROOT" ${AIR_DIR:+"$AIR_DIR"}; }
trap cleanup EXIT
mkdir -p "$STAGE/Contents/MacOS" "$STAGE/Contents/Resources"
cp "$BINARY" "$STAGE/Contents/MacOS/HUD"
if [ -n "$RELEASE" ]; then
  ARCHS="$(lipo -archs "$STAGE/Contents/MacOS/HUD")"
  case "$ARCHS" in
    *arm64*x86_64*|*x86_64*arm64*) ;;
    *) echo "release binary is not universal (got: $ARCHS) — refusing to ship it" >&2; exit 3 ;;
  esac
fi

# The shaders themselves, compiled into the bundle beside the binary.
#
# The library has to be called `default.metallib`: `ShaderLibrary.someShader`
# resolves against Bundle.main's DEFAULT library, and a library under any other
# name would need every call site to name the file instead.
if [ -d "$EFFECTS_DIR" ] && ls "$EFFECTS_DIR"/*.metal >/dev/null 2>&1; then
  AIR_DIR="$(mktemp -d)"
  for src in "$EFFECTS_DIR"/*.metal; do
    xcrun -sdk macosx metal -c "$src" -o "$AIR_DIR/$(basename "${src%.metal}").air"
  done
  xcrun -sdk macosx metallib "$AIR_DIR"/*.air -o "$STAGE/Contents/Resources/default.metallib"
  echo "compiled $(ls "$EFFECTS_DIR"/*.metal | wc -l | tr -d ' ') shader(s) from $EFFECTS_DIR"
fi

cat > "$STAGE/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>HUD</string>
  <key>CFBundleDisplayName</key><string>HUD - Heads-Up Display</string>
  <key>CFBundleIdentifier</key><string>ai.devdock.claude-unlimited.hud</string>
  <key>CFBundleExecutable</key><string>HUD</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>$VERSION</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <!-- menu-bar only: no Dock icon, no app-switcher entry -->
  <key>LSUIElement</key><true/>
  <key>NSHighResolutionCapable</key><true/>
  <!-- talks to 127.0.0.1 only; ATS would otherwise block plain HTTP -->
  <key>NSAppTransportSecurity</key>
  <dict>
    <key>NSAllowsLocalNetworking</key><true/>
  </dict>
</dict>
</plist>
PLIST

# Ad-hoc signature: unsigned bundles get killed on arm64, and this keeps the
# POC runnable without a developer certificate.
codesign --force --sign - "$STAGE" >/dev/null 2>&1 || true

# Everything built: now, and only now, replace the installed bundle.
rm -rf "$APP"
mkdir -p "$(dirname "$APP")"
mv "$STAGE" "$APP"
if [ -d "$LEGACY_APP" ] && [ -z "${1:-}" ]; then
  rm -rf "$LEGACY_APP"
  echo "removed the pre-rename bundle: $LEGACY_APP"
fi
echo "installed: $APP"

if [ -n "$RELEASE" ]; then
  ASSET="HUD-$VERSION-macos.zip"
  rm -f "dist/$ASSET"
  # ditto, not zip: it is the only archiver that preserves the bundle's
  # symlinks and extended attributes intact, and it is what the installer
  # unpacks with on the other side.
  ditto -c -k --keepParent "$APP" "dist/$ASSET"
  DIGEST="$(shasum -a 256 "dist/$ASSET" | awk '{print $1}')"
  printf '%s  %s\n' "$DIGEST" "$ASSET" > HUD.sha256
  echo
  echo "release asset: $(pwd)/dist/$ASSET"
  echo "checksum written to $(pwd)/HUD.sha256 — commit it BEFORE tagging $VERSION:"
  cat HUD.sha256
  exit 0
fi

# Relaunch unless asked not to, so `./build.sh` leaves you with the new build
# actually running rather than an installed bundle you still have to open.
if [ "${NO_LAUNCH:-}" != "1" ]; then
  open "$APP"
  echo "launched: $APP"
fi
