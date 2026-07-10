#!/usr/bin/env bash
# Installer for the request-reviewer menu bar app (macOS only).
#
# Builds the Swift package, installs the binary to ~/.claude/menubar/, and
# registers a LaunchAgent so it starts at login and restarts if it crashes —
# the same "just works, don't think about it" behavior as the reviewer hook.
#
# Usage:
#   ./install.sh                # build + install + start now
#   ./install.sh --uninstall    # stop, unregister, remove
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="$HOME/.claude/menubar"
BINARY_PATH="$INSTALL_DIR/RequestReviewerBar"
PLIST_LABEL="com.request-reviewer.bar"
PLIST_PATH="$HOME/Library/LaunchAgents/$PLIST_LABEL.plist"

if [[ "$(uname)" != "Darwin" ]]; then
  echo "The menu bar app is macOS-only. The reviewer hook itself works fine without it."
  exit 1
fi

if [[ "${1:-}" == "--uninstall" ]]; then
  launchctl unload -w "$PLIST_PATH" 2>/dev/null || true
  rm -f "$PLIST_PATH"
  pkill -f "$BINARY_PATH" 2>/dev/null || true
  rm -rf "$INSTALL_DIR"
  echo "Removed $PLIST_PATH and $INSTALL_DIR."
  exit 0
fi

echo "Building release binary..."
(cd "$SCRIPT_DIR" && swift build -c release)

mkdir -p "$INSTALL_DIR"
cp "$SCRIPT_DIR/.build/release/RequestReviewerBar" "$BINARY_PATH"
chmod +x "$BINARY_PATH"

cat > "$PLIST_PATH" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$PLIST_LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$BINARY_PATH</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>StandardOutPath</key>
    <string>$INSTALL_DIR/menubar.out.log</string>
    <key>StandardErrorPath</key>
    <string>$INSTALL_DIR/menubar.err.log</string>
</dict>
</plist>
PLIST

launchctl unload -w "$PLIST_PATH" 2>/dev/null || true
launchctl load -w "$PLIST_PATH"

echo
echo "✓ Installed and started. It will now:"
echo "  - launch automatically at login (RunAtLoad)"
echo "  - restart itself if it ever crashes, but stay quit if you Quit it from the menu (KeepAlive: SuccessfulExit=false)"
echo "  - keep running across Claude Code / terminal restarts — it's an independent LaunchAgent"
echo
echo "Look for the shield icon in your menu bar now."
echo "To remove: ./install.sh --uninstall"
