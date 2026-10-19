#!/usr/bin/env bash
set -e

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SYSTEMD_USER_DIR="$HOME/.config/systemd/user"
CONFIG_FILE="${ICLOUD_SYNC_CONFIG:-$HOME/.config/icloud-sync/config}"

echo "=== Cloud Sync Installer ==="
echo ""

# ── Dependencies ────────────────────────────────────────────────────────────

if ! command -v uv &>/dev/null; then
    echo "Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi

if ! command -v rclone &>/dev/null; then
    echo "Installing rclone..."
    sudo -v && curl -s https://rclone.org/install.sh | sudo bash
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

# ── Google Drive ─────────────────────────────────────────────────────────────

echo ""
echo "--- Google Drive ---"
echo "Starting rclone config for Google Drive."
echo "  - Choose: n (new remote)"
echo "  - Name: gdrive"
echo "  - Storage: drive"
echo "  - Leave client_id/secret blank, scope: 1, auto config: y"
echo ""
read -rp "Press Enter to open rclone config..."
rclone config

[ -f "$CONFIG_FILE" ] && source "$CONFIG_FILE"
ICLOUD_SYNC_DIR="${ICLOUD_SYNC_DIR:-$HOME/Documents/icloud-drive}"
GDRIVE_SYNC_DIR="${GDRIVE_SYNC_DIR:-$HOME/Documents/gdrive}"
grep -q '^GDRIVE_SYNC_DIR=' "$CONFIG_FILE" 2>/dev/null || echo "GDRIVE_SYNC_DIR=$GDRIVE_SYNC_DIR" >> "$CONFIG_FILE"
mkdir -p "$GDRIVE_SYNC_DIR"

echo ""
echo "Running initial Google Drive sync (this may take a while)..."
rclone bisync gdrive: "$GDRIVE_SYNC_DIR" \
    --resync \
    --create-empty-src-dirs \
    --transfers 10 \
    --checkers 20 \
    --log-level INFO \
    --progress

# ── Systemd units ────────────────────────────────────────────────────────────

echo ""
echo "Installing systemd units..."
mkdir -p "$SYSTEMD_USER_DIR"

for unit in icloud-sync.service icloud-sync.timer gdrive-sync.service gdrive-sync.timer; do
    cp "$REPO_DIR/systemd/$unit" "$SYSTEMD_USER_DIR/$unit"
done

systemctl --user daemon-reload
systemctl --user enable --now icloud-sync.timer
systemctl --user enable --now gdrive-sync.timer

# ── Done ─────────────────────────────────────────────────────────────────────

echo ""
echo "============================================"
echo "  All done! Both syncs are running."
echo "============================================"
echo ""
echo "  iCloud local folder:  $ICLOUD_SYNC_DIR"
echo "  Google Drive folder:  $GDRIVE_SYNC_DIR"
echo "  Settings:             $CONFIG_FILE"
echo ""
echo "  Check status:"
echo "    systemctl --user status icloud-sync.timer"
echo "    systemctl --user status gdrive-sync.timer"
echo ""
echo "  Watch logs:"
echo "    journalctl --user -u icloud-sync.service -f"
echo "    journalctl --user -u gdrive-sync.service -f"
