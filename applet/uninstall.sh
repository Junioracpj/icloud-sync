#!/bin/bash
# uninstall.sh — remove the iCloud Sync COSMIC panel applet (the sync service is left alone).
set -euo pipefail
rm -f "$HOME/.local/bin/cosmic-applet-icloud-sync"
rm -f "$HOME/.local/share/applications/io.github.icloudsync.Applet.desktop"
rm -f "$HOME"/.local/share/icons/hicolor/scalable/apps/io.github.icloudsync.Applet-*-symbolic.svg
pkill -f "$HOME/.local/bin/cosmic-applet-icloud-sync" 2>/dev/null || true
echo "Removed. (Remove it from the panel in COSMIC Settings if it is still listed.)"
