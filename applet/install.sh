#!/bin/bash
# install.sh — build and install the iCloud Sync COSMIC panel applet for the current user (no sudo).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

BIN_DIR="$HOME/.local/bin"
APP_DIR="$HOME/.local/share/applications"
ICON_DIR="$HOME/.local/share/icons/hicolor/scalable/apps"

echo "==> Building (first build pulls libcosmic; can take several minutes)..."
cargo build --release

echo "==> Installing"
install -Dm755 target/release/cosmic-applet-icloud-sync "$BIN_DIR/cosmic-applet-icloud-sync"
# Absolute Exec path: the panel's environment may not have ~/.local/bin on PATH.
sed "s|^Exec=.*|Exec=$BIN_DIR/cosmic-applet-icloud-sync|" data/io.github.icloudsync.Applet.desktop \
    > "$APP_DIR/io.github.icloudsync.Applet.desktop"
mkdir -p "$ICON_DIR"
install -m644 data/icons/*.svg "$ICON_DIR/"
command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -qtf "$HOME/.local/share/icons/hicolor" || true

# Restart a running copy so the panel picks up the new binary.
pkill -x cosmic-applet-i 2>/dev/null || pkill -f "$BIN_DIR/cosmic-applet-icloud-sync" 2>/dev/null || true

echo ""
echo "Done. Add it to the panel:"
echo "  COSMIC Settings -> Desktop -> Panel -> Configure panel applets -> Add applet -> 'iCloud Sync'"
