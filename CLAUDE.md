# icloud-sync

Two-way sync between iCloud Drive and a local folder on Linux, using pyicloud.

## Project structure

```
~/Documents/icloud-sync/       # Project root
├── sync.py                    # Main sync script (iCloud)
├── authenticate.py            # Standalone authentication script (iCloud)
├── install.sh                 # One-shot installer
├── pyproject.toml             # Dependencies (managed with uv)
├── uv.lock                    # Lock file
├── .sync_state.json           # Sync state (auto-generated, do not edit)
├── .syncignore                # Patterns for files/folders to skip (like .gitignore)
├── systemd/                   # Systemd unit files (copied to ~/.config/systemd/user/ by install.sh)
│   ├── icloud-sync.service
│   └── icloud-sync.timer
└── CLAUDE.md
```

## How it works

The sync runs in two phases:
1. **Phase 1 (Scan)**: Walks the iCloud Drive tree in parallel (10 workers), collecting all download/upload/delete tasks. In quick mode, skips files/folders unchanged since last sync.
2. **Phase 2 (Execute)**: Runs all tasks through thread pools — downloads (50 workers), uploads (5 workers), deletes (sequential).

### Sync state

`.sync_state.json` tracks:
- `synced_files`: list of all previously synced file paths (used to detect intentional local deletions)
- `last_sync`: UTC timestamp of last sync (used for quick mode filtering)

### Safety: delete logic

- File deleted locally AND was previously synced → trashed from iCloud (recoverable 30 days)
- File exists in iCloud but never synced locally → downloaded (never deleted)
- This prevents accidental mass deletion if the local folder is empty

## Key configuration

Per-user settings (`ICLOUD_APPLE_ID`, `ICLOUD_SYNC_DIR`, `ICLOUD_SYNC_COOKIES`, `ICLOUD_SYNC_STATE`) come from environment variables, then `~/.config/icloud-sync/config` (KEY=value, outside the repo). `authenticate.py` asks for the Apple ID and saves it there. Never hardcode personal values in the repo.

Tuning constants (top of sync_engine.py):

| Constant | Value | Purpose |
|---|---|---|
| `MAX_DOWNLOAD_WORKERS` | 50 | Parallel downloads |
| `MAX_UPLOAD_WORKERS` | 5 | Parallel uploads (low to avoid Apple rate limits) |
| `MAX_DELETE_WORKERS` | 10 | Parallel remote deletions |
| `MAX_SCAN_WORKERS` | 10 | Parallel folder scanning |
| `MAX_RETRIES` | 5 | Retry attempts per file/scan |
| `RETRY_BASE_WAIT` | 10 | Base seconds between retries (multiplied by attempt number) |

## .syncignore

Works like `.gitignore` — patterns for files and folders to skip during sync. Patterns ending with `/` match directories. Uses fnmatch (shell glob) syntax.

Default patterns include: `__pycache__/`, `*.pyc`, `.venv/`, `venv/`, `node_modules/`, `.DS_Store`, `._*`, etc.

Ignored files are:
- Never downloaded from iCloud
- Never uploaded to iCloud
- Skipped silently during scan (visible with `-v`)

## Fresh install (new machine)

```bash
git clone https://github.com/Junioracpj/icloud-sync.git ~/Documents/icloud-sync
cd ~/Documents/icloud-sync
./install.sh
```

The script installs `uv` if missing, authenticates iCloud interactively (asks for the Apple ID), and enables the systemd timer. Files sync to `~/Documents/icloud-drive` unless `ICLOUD_SYNC_DIR` is set.

## Commands

```bash
# Install dependencies
uv sync

# First-time authentication (interactive, requires 2FA)
uv run python authenticate.py

# Quick sync (only changes since last run — default)
uv run python sync.py

# Full scan (re-checks everything)
uv run python sync.py --full

# Verbose mode (shows individual file operations)
uv run python sync.py -v

# Full + verbose
uv run python sync.py --full -v

# Upload-only: push local changes to iCloud, skip downloads and remote deletions
uv run python sync.py --upload-only

# Force-push everything (combine with --full to bypass timestamp filter)
uv run python sync.py --upload-only --full

# Cleanup: remove ignored files from iCloud and local folder
# Scans iCloud, lists all matches, asks for confirmation before trashing
uv run python sync.py --cleanup
```

## Automatic sync (systemd timer)

A systemd user timer runs quick sync every 30 minutes:

```
~/.config/systemd/user/icloud-sync.service
~/.config/systemd/user/icloud-sync.timer
```

### Manage the timer

```bash
# Check timer status
systemctl --user status icloud-sync.timer

# Check last sync result
systemctl --user status icloud-sync.service

# Watch logs live
journalctl --user -u icloud-sync.service -f

# Recent logs
journalctl --user -u icloud-sync.service --since "1 hour ago"

# Run sync manually
systemctl --user start icloud-sync.service

# Stop/start auto-sync
systemctl --user stop icloud-sync.timer
systemctl --user start icloud-sync.timer
```

## Authentication

pyicloud stores session data in `~/.config/icloud-sync/session/`. If the session expires, run `authenticate.py` interactively to re-authenticate with 2FA.

## Known issues

- Apple rate-limits API requests — uploads use only 5 workers to avoid `ZONE_BUSY` / `SERVER_OVERLOADED` errors
- Scan phase retries failed folders automatically (up to 5 times with increasing waits)
- iCloud-only formats (Pages, Numbers, Keynote) are downloaded but not usable on Linux
- pyicloud cannot access Notes, Reminders, or Keychain — only Drive files
- After upload, iCloud sets the remote timestamp to the upload time (not the local file's edit time). To prevent the next sync from seeing the remote as newer and re-downloading, `sync.py` updates the local file's mtime to `time.time()` immediately after each successful upload.
