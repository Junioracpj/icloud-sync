# icloud-sync

Two-way sync for **iCloud Drive** on Linux, built on [pyicloud](https://github.com/picklepete/pyicloud) + a FastAPI server, running automatically via systemd. Files land in `~/Documents/icloud-drive` by default.

---

## Fresh install

```bash
git clone https://github.com/Junioracpj/icloud-sync.git ~/Documents/icloud-sync
cd ~/Documents/icloud-sync
./install.sh
```

The installer will:
1. Install `uv` if not already present
2. Install Python dependencies
3. Authenticate iCloud (asks for your Apple ID, password and 2FA code)
4. Enable the systemd timer

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
```

| Setting | Default | Purpose |
|---|---|---|
| `ICLOUD_APPLE_ID` | *(none, asked by `authenticate.py`)* | Apple account |
| `ICLOUD_SYNC_DIR` | `~/Documents/icloud-drive` | Local iCloud folder |
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

### Skipping folders (per machine)

To keep a folder in iCloud but **not** on this machine, list it in `~/.config/icloud-sync/skip.json` (created empty on the first sync; override the path with `ICLOUD_SYNC_SKIP`). Paths start at the iCloud Drive root; globs are allowed:

```json
{
  "skip": [
    "Photos/2019",
    "Backups/*"
  ]
}
```

The file is re-read on every sync, so just edit it — no command needed. Remove a line to sync that folder again (run `sync.py --full` to download it right away). If the file is not valid JSON the sync stops with an error instead of ignoring the list.

Optional helpers that edit the same file:

```bash
uv run python sync.py --skip "Photos/2019"
uv run python sync.py --unskip "Photos/2019"
uv run python sync.py --skipped
```

Each machine has its own list, so the VPS can skip folders your PC keeps, and your folder names stay out of the repo.

A skipped folder is never downloaded, uploaded or deleted on either side, and `--cleanup` never touches it — unlike `.syncignore`, whose matches `--cleanup` **trashes from iCloud**. Its existing local copy is left in place; delete it yourself to free space (the iCloud copy is unaffected).

> Skip first, then delete the local copy. Deleting a synced folder locally *before* skipping it makes the next sync trash it from iCloud.

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

## Project structure

```
~/Documents/icloud-sync/
├── server.py               # FastAPI server — HTTP endpoints, holds iCloud session
├── sync_engine.py          # Core sync logic (scan, download, upload, delete, cleanup)
├── sync.py                 # CLI shim for manual use
├── authenticate.py         # iCloud first-time interactive auth (2FA)
├── install.sh              # One-shot installer
├── pyproject.toml          # Python dependencies (uv)
├── uv.lock
├── .sync_state.json        # iCloud sync state (auto-generated, do not edit)
├── .syncignore             # Patterns to skip (like .gitignore)
├── systemd/                # Unit files (installed to ~/.config/systemd/user/)
│   ├── icloud-sync.service          # Runs uvicorn server (persistent)
│   ├── icloud-sync-trigger.service  # One-shot curl to POST /sync
│   └── icloud-sync-trigger.timer    # Fires trigger every 30 minutes
└── README.md
```

---

## Known issues

- **Apple rate limits** — uploads use only 5 workers to avoid `ZONE_BUSY` / `SERVER_OVERLOADED` errors.
- **iCloud timestamp behavior** — after upload, iCloud sets the remote timestamp to the upload time instead of preserving the local edit time. `sync_engine.py` updates the local file's mtime after each upload to keep them in sync and prevent re-downloads.
- **iCloud-only formats** — Pages, Numbers, and Keynote files are downloaded but not usable on Linux.
- **pyicloud scope** — only Drive files are accessible; Notes, Reminders, and Keychain are not supported.

---

## License

[MIT](LICENSE)
