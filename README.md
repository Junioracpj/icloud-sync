# cloud-sync

Two-way sync for **iCloud Drive** and **Google Drive** on Linux, running automatically every 30 minutes via systemd.

| Driver | Tool | Local folder |
|---|---|---|
| iCloud Drive | [pyicloud](https://github.com/picklepete/pyicloud) + FastAPI server | `~/Documents/icloud-drive` |
| Google Drive | [rclone bisync](https://rclone.org/bisync/) | `~/Documents/gdrive` |

---

## Fresh install

```bash
git clone <this-repo> ~/Documents/icloud-sync
cd ~/Documents/icloud-sync
./install.sh
```

The installer will:
1. Install `uv` and `rclone` if not already present
2. Install Python dependencies
3. Authenticate iCloud (Apple ID + 2FA)
4. Configure Google Drive via rclone (opens browser for OAuth)
5. Run the initial Google Drive bisync
6. Enable both systemd timers

---

## iCloud Drive

### Architecture

The iCloud sync runs as a **persistent FastAPI server** (`server.py`) that holds the authenticated iCloud session and exposes HTTP endpoints for every sync operation.

```
server.py        ← FastAPI server (port 8765), holds iCloud session
sync_engine.py   ← Core sync logic: scan, download, upload, delete, cleanup
sync.py          ← Thin CLI shim for manual use (calls sync_engine)
authenticate.py  ← Standalone interactive 2FA authentication
```

The systemd service keeps the server running. The timer calls `POST /sync` every 30 minutes via curl.

### How it works

Sync runs in two phases:

1. **Scan** — walks the iCloud Drive tree in parallel (10 workers), collecting download/upload/delete tasks. In quick mode, skips files and folders unchanged since last sync.
2. **Execute** — runs all tasks through thread pools: downloads (100 workers), uploads (5 workers), deletes (10 workers).

### HTTP API

The server runs on `http://localhost:8765`. Interactive docs available at `http://localhost:8765/docs`.

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/status` | Session health + last sync timestamp + synced file count |
| `POST` | `/scan` | Phase 1 only — returns task counts without executing |
| `POST` | `/sync` | Full sync: download + upload + delete |
| `POST` | `/sync/download` | Download remote changes to local only |
| `POST` | `/sync/upload` | Upload local changes to iCloud only |
| `POST` | `/sync/delete` | Trash remote files deleted locally |
| `POST` | `/cleanup` | Trash files in iCloud matching `.syncignore` patterns |

All POST endpoints accept an optional JSON body:

```json
{ "full": true, "verbose": true }
```

- `full` — ignore last sync timestamp, re-check everything (default: `false`)
- `verbose` — print individual file operations to stdout/journal (default: `false`)

All responses follow this shape:

```json
{
  "status": "ok",
  "stats": {
    "downloaded": 5,
    "uploaded": 2,
    "deleted_remote": 1,
    "skipped": 120,
    "errors": 0
  },
  "errors": []
}
```

#### Examples

```bash
# Check session health and last sync time
curl http://localhost:8765/status

# See what would sync without executing
curl -X POST http://localhost:8765/scan

# Full sync
curl -X POST http://localhost:8765/sync

# Full sync with verbose output (visible in journalctl)
curl -X POST http://localhost:8765/sync \
  -H "Content-Type: application/json" \
  -d '{"full": true, "verbose": true}'

# Download only
curl -X POST http://localhost:8765/sync/download

# Upload only
curl -X POST http://localhost:8765/sync/upload

# Delete remote files that were removed locally
curl -X POST http://localhost:8765/sync/delete

# Trash ignored files from iCloud (non-destructive confirmation in logs)
curl -X POST http://localhost:8765/cleanup
```

### CLI (manual use)

`sync.py` is a thin wrapper around `sync_engine` for running sync from the terminal without the server:

```bash
# Install Python dependencies
uv sync

# First-time authentication (interactive, requires 2FA)
uv run python authenticate.py

# Quick sync — only changes since last run (default)
uv run python sync.py

# Full scan — re-checks everything
uv run python sync.py --full

# Verbose — shows individual file operations
uv run python sync.py -v

# Upload only
uv run python sync.py --upload-only

# Cleanup — find and trash files matching .syncignore from iCloud and local
uv run python sync.py --cleanup
```

### Sync state

`.sync_state.json` is auto-generated and tracks:
- `synced_files` — all previously synced paths, used to detect intentional local deletions
- `last_sync` — UTC timestamp of last sync, used for quick mode filtering

### Delete safety

| Situation | Action |
|---|---|
| File deleted locally, was previously synced | Moved to iCloud Trash (recoverable for 30 days) |
| File exists in iCloud but never synced locally | Downloaded — never deleted |

This prevents accidental mass deletion if the local folder is empty or missing.

### Configuration

Per-user settings are read from `~/.config/icloud-sync/config` (`KEY=value` lines, kept outside the repo). Environment variables with the same name take precedence.

```ini
ICLOUD_APPLE_ID=you@icloud.com          # written by authenticate.py
ICLOUD_SYNC_DIR=~/Documents/icloud-drive
GDRIVE_SYNC_DIR=~/Documents/gdrive
```

| Setting | Default | Purpose |
|---|---|---|
| `ICLOUD_APPLE_ID` | *(none, asked by `authenticate.py`)* | Apple account |
| `ICLOUD_SYNC_DIR` | `~/Documents/icloud-drive` | Local iCloud folder |
| `GDRIVE_SYNC_DIR` | `~/Documents/gdrive` | Local Google Drive folder |
| `ICLOUD_SYNC_COOKIES` | `~/.config/icloud-sync/session` | pyicloud session data |
| `ICLOUD_SYNC_STATE` | `<repo>/.sync_state.json` | Sync state file |

> Never point an existing `.sync_state.json` at a different or empty folder: files listed in it but missing locally are treated as deleted and trashed from iCloud.

Tuning constants are at the top of `sync_engine.py`:

| Constant | Default | Purpose |
|---|---|---|
| `MAX_DOWNLOAD_WORKERS` | `100` | Parallel downloads |
| `MAX_UPLOAD_WORKERS` | `5` | Parallel uploads (low to avoid Apple rate limits) |
| `MAX_DELETE_WORKERS` | `10` | Parallel remote deletions |
| `MAX_SCAN_WORKERS` | `10` | Parallel folder scanning |
| `MAX_RETRIES` | `5` | Retry attempts per file/scan |
| `RETRY_BASE_WAIT` | `10` | Base wait in seconds between retries (multiplied by attempt number) |

### .syncignore

Works like `.gitignore` — glob patterns for files and folders to skip. Patterns ending with `/` match directories.

Default patterns: `__pycache__/`, `*.pyc`, `.venv/`, `node_modules/`, `.DS_Store`, `._*`, etc.

Ignored files are never downloaded, never uploaded, and skipped silently during scan (use `-v` or `"verbose": true` to see them).

### Re-authentication

Session data is stored in `~/.config/icloud-sync/session/`. If the session expires, stop the server, re-authenticate, then restart:

```bash
systemctl --user stop icloud-sync.service
uv run python authenticate.py
systemctl --user start icloud-sync.service
```

### Manage the server and timer

```bash
# Server status
systemctl --user status icloud-sync.service

# Live logs
journalctl --user -u icloud-sync.service -f

# Recent logs
journalctl --user -u icloud-sync.service --since "1 hour ago"

# Restart server (e.g. after config change)
systemctl --user restart icloud-sync.service

# Trigger sync now (without waiting for timer)
systemctl --user start icloud-sync-trigger.service
# or via curl:
curl -X POST http://localhost:8765/sync

# Timer status
systemctl --user status icloud-sync-trigger.timer

# Pause / resume auto-sync
systemctl --user stop icloud-sync-trigger.timer
systemctl --user start icloud-sync-trigger.timer
```

---

## Google Drive

Uses `rclone bisync` for two-way sync. Conflicts are resolved by keeping the newer file.

### First sync (manual, required once)

```bash
rclone bisync gdrive: ~/Documents/gdrive \
    --resync --create-empty-src-dirs \
    --transfers 10 --checkers 20 \
    --log-level INFO --progress
```

### Manage the timer

```bash
systemctl --user status gdrive-sync.timer        # timer status
systemctl --user status gdrive-sync.service      # last sync result
journalctl --user -u gdrive-sync.service -f      # live logs
systemctl --user start gdrive-sync.service       # run now
systemctl --user stop gdrive-sync.timer          # pause auto-sync
systemctl --user start gdrive-sync.timer         # resume auto-sync
```

### Re-authentication

rclone stores OAuth tokens in `~/.config/rclone/rclone.conf`. If the token expires:

```bash
rclone config reconnect gdrive:
```

---

## Project structure

```
~/Documents/icloud-sync/
├── server.py               # FastAPI server — HTTP endpoints, holds iCloud session
├── sync_engine.py          # Core sync logic (scan, download, upload, delete, cleanup)
├── sync.py                 # CLI shim for manual use
├── authenticate.py         # iCloud first-time interactive auth (2FA)
├── install.sh              # One-shot installer for both drivers
├── pyproject.toml          # Python dependencies (uv)
├── uv.lock
├── .sync_state.json        # iCloud sync state (auto-generated, do not edit)
├── .syncignore             # Patterns to skip (like .gitignore)
├── systemd/                # Unit files (installed to ~/.config/systemd/user/)
│   ├── icloud-sync.service          # Runs uvicorn server (persistent)
│   ├── icloud-sync-trigger.service  # One-shot curl to POST /sync
│   ├── icloud-sync-trigger.timer    # Fires trigger every 30 minutes
│   ├── gdrive-sync.service
│   └── gdrive-sync.timer
└── README.md
```

---

## Known issues

- **Apple rate limits** — uploads use only 5 workers to avoid `ZONE_BUSY` / `SERVER_OVERLOADED` errors.
- **iCloud timestamp behavior** — after upload, iCloud sets the remote timestamp to the upload time instead of preserving the local edit time. `sync_engine.py` updates the local file's mtime after each upload to keep them in sync and prevent re-downloads.
- **iCloud-only formats** — Pages, Numbers, and Keynote files are downloaded but not usable on Linux.
- **pyicloud scope** — only Drive files are accessible; Notes, Reminders, and Keychain are not supported.
