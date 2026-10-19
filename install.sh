#!/usr/bin/env bash
set -e

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SYSTEMD_USER_DIR="$HOME/.config/systemd/user"
CONFIG_FILE="${ICLOUD_SYNC_CONFIG:-$HOME/.config/icloud-sync/config}"

echo "=== iCloud Sync Installer ==="
echo ""

# ── Dependencies ────────────────────────────────────────────────────────────

if ! command -v uv &>/dev/null; then
    echo "Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi

# ── iCloud ──────────────────────────────────────────────────────────────────

echo ""
echo "--- iCloud Drive ---"

echo "Installing Python dependencies..."
cd "$REPO_DIR"
uv sync

echo ""
echo "Starting iCloud authentication (requires Apple ID + 2FA)..."
uv run python authenticate.py

# ── Systemd units ────────────────────────────────────────────────────────────

echo ""
echo "Installing systemd units..."
mkdir -p "$SYSTEMD_USER_DIR"

for unit in icloud-sync.service icloud-sync.timer; do
    cp "$REPO_DIR/systemd/$unit" "$SYSTEMD_USER_DIR/$unit"
done

systemctl --user daemon-reload
systemctl --user enable --now icloud-sync.timer

# ── Done ─────────────────────────────────────────────────────────────────────

echo ""
echo "============================================"
echo "  All done! iCloud sync is running."
echo "============================================"
echo ""
[ -f "$CONFIG_FILE" ] && source "$CONFIG_FILE"
ICLOUD_SYNC_DIR="${ICLOUD_SYNC_DIR:-$HOME/Documents/icloud-drive}"
echo "  iCloud local folder:  $ICLOUD_SYNC_DIR"
echo "  Settings:             $CONFIG_FILE"
echo ""
echo "  Check status:"
echo "    systemctl --user status icloud-sync.timer"
echo ""
echo "  Watch logs:"
echo "    journalctl --user -u icloud-sync.service -f"
