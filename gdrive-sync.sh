#!/usr/bin/env bash

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="${ICLOUD_SYNC_CONFIG:-$HOME/.config/icloud-sync/config}"
ENV_DIR="$GDRIVE_SYNC_DIR"
[ -f "$CONFIG_FILE" ] && source "$CONFIG_FILE"
SYNC_DIR="${ENV_DIR:-${GDRIVE_SYNC_DIR:-$HOME/Documents/gdrive}}"
SYNC_DIR="${SYNC_DIR/#\~/$HOME}"
ICON_OK="$REPO_DIR/icons/folder-cloud-ok.svg"
ICON_ERROR="$REPO_DIR/icons/folder-cloud-error.svg"

set_icon() {
    gio set "$SYNC_DIR" metadata::custom-icon "file://$1" 2>/dev/null || true
}

trap 'set_icon "$ICON_ERROR"' ERR

rclone bisync gdrive: "$SYNC_DIR" \
    --resilient \
    --recover \
    --create-empty-src-dirs \
    --compare modtime,size \
    --conflict-resolve newer \
    --conflict-loser num \
    --transfers 10 \
    --checkers 20 \
    --log-level INFO

set_icon "$ICON_OK"
