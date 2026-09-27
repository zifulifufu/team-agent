#!/bin/sh
# Run Team Agent from source as an app that *is* Team Agent.
#
# Two reasons this exists rather than a line in the README saying `npx electron .`:
#
#   * **Name and icon.** `npx electron .` runs Electron's own bundle, so the menu bar, the About
#     panel and the Dock say "Electron" next to a window titled Team Agent. `app.setName()` and
#     `app.dock.setIcon()` in `electron/main.cjs` cover the first two and the icon, but a process is
#     attributed to the bundle its *executable* lives in, and only a bundle with our own name in it
#     changes what the Dock calls it. So this makes one: a copy of Electron.app, renamed.
#   * **A bundle to launch.** On macOS, a GUI app started from a sandboxed or unusual shell inherits
#     that shell's `ELECTRON_RUN_AS_NODE` (Electron then runs as plain node: `ipcMain` is undefined)
#     and its seatbelt profile (Chromium's own sandbox cannot initialise). LaunchServices hands a
#     launched app a normal session environment, so the app is built and then *opened*.
#
# Everything it writes lives under ~/Library/Caches/team-agent/dev-app, outside the repo.
#
#   sh scripts/dev-app.sh            build it if needed, print the command to run it
#   sh scripts/dev-app.sh --force    rebuild the app bundle from node_modules
#   sh scripts/dev-app.sh --rebuild-icon   redraw desktop/build/icon.icns first (see make-icon.py)
#
# ⚠️ The app bundle is a **snapshot of the Electron version in node_modules**. Upgrading Electron
#    leaves this copy behind, and the launcher would keep starting the old one — so the version is
#    stamped and a mismatch rebuilds it on the next run.

set -e

ROOT=$(cd "$(dirname "$0")/.." && pwd)
DEV_DIR="$HOME/Library/Caches/team-agent/dev-app"
ELECTRON_APP="$ROOT/desktop/node_modules/electron/dist/Electron.app"
APP="$DEV_DIR/Team Agent.app"
LAUNCHER="$DEV_DIR/TeamAgent.app"
ICON="$ROOT/desktop/build/icon.icns"
STAMP="$DEV_DIR/.electron-version"

FORCE=""
for arg in "$@"; do
  case "$arg" in
    --force) FORCE=1 ;;
    --rebuild-icon) "$ROOT/.venv/bin/python" "$ROOT/scripts/make-icon.py" ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

[ -d "$ELECTRON_APP" ] || { echo "no Electron at $ELECTRON_APP — run npm install in desktop/" >&2; exit 1; }
[ -f "$ICON" ] || { echo "no icon at $ICON — run .venv/bin/python scripts/make-icon.py" >&2; exit 1; }

# The version of the Electron actually installed, not the one in package.json's range.
VERSION=$("$ROOT/.venv/bin/python" -c "
import json,sys
print(json.load(open('$ROOT/desktop/node_modules/electron/package.json'))['version'])")
mkdir -p "$DEV_DIR"

if [ -n "$FORCE" ] || [ ! -d "$APP" ] || [ "$(cat "$STAMP" 2>/dev/null)" != "$VERSION" ]; then
  echo "building $APP (Electron $VERSION)"
  rm -rf "$APP"
  cp -c -R "$ELECTRON_APP" "$APP"                 # clonefile: a 350 MB copy that costs nothing
  "$ROOT/.venv/bin/python" - "$APP/Contents/Info.plist" <<'PY'
import plistlib, sys
path = sys.argv[1]
info = plistlib.loads(open(path, "rb").read())
info["CFBundleName"] = "Team Agent"          # what the Dock and the menu call it
info["CFBundleDisplayName"] = "Team Agent"
info["CFBundleIdentifier"] = "local.team-agent.app"
info["CFBundleIconFile"] = "AppIcon"
version = info.get("CFBundleShortVersionString", "1.0")
info["CFBundleShortVersionString"] = version
info["CFBundleVersion"] = version
info["NSHumanReadableCopyright"] = "Apache-2.0"
open(path, "wb").write(plistlib.dumps(info))
PY
  cp "$ICON" "$APP/Contents/Resources/AppIcon.icns"
  # A modified bundle invalidates its signature, and the copy has no quarantine attribute, so macOS
  # runs it ad-hoc (measured). Re-stamping it keeps the Dock from caching a stale identity.
  /usr/bin/codesign --force --deep --sign - "$APP" >/dev/null 2>&1 || true
  echo "$VERSION" > "$STAMP"
  touch "$APP"
fi

# The launcher: a tiny .app whose only job is to drop the environment this shell would otherwise
# hand over, and then exec the branded binary with the project directory as its argument.
mkdir -p "$LAUNCHER/Contents/MacOS" "$LAUNCHER/Contents/Resources"
cp "$ICON" "$LAUNCHER/Contents/Resources/AppIcon.icns"
cat > "$LAUNCHER/Contents/MacOS/TeamAgent" <<EOF
#!/bin/sh
# Written by scripts/dev-app.sh — see that file for why any of this is necessary.
exec >>"$DEV_DIR/app.log" 2>&1
echo "--- launcher \$(date '+%F %T') ---"
for v in \$(env | grep '^CODEBUDDY_' | cut -d= -f1); do unset "\$v"; done
unset ELECTRON_RUN_AS_NODE ELECTRON_NO_ATTACH_CONSOLE NODE_OPTIONS PYTHONPATH
cd "$ROOT/desktop" || exit 1
exec env -u ELECTRON_RUN_AS_NODE -u NODE_OPTIONS -u PYTHONPATH \\
  "$APP/Contents/MacOS/Electron" . --team-agent-launcher
EOF
chmod +x "$LAUNCHER/Contents/MacOS/TeamAgent"
cat > "$LAUNCHER/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleExecutable</key><string>TeamAgent</string>
  <key>CFBundleIdentifier</key><string>local.team-agent.launcher</string>
  <key>CFBundleName</key><string>Team Agent</string>
  <key>CFBundleDisplayName</key><string>Team Agent</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST
touch "$LAUNCHER"

echo "app:     $APP"
echo "launcher: $LAUNCHER"
echo
echo "run it:  open -n \"$LAUNCHER\""
echo "logs:    $DEV_DIR/app.log"
echo "stop it: for p in \$(pgrep -f team-agent-launcher); do kill \"\$p\"; done"
