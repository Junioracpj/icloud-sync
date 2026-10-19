import os
import json
import time
import shutil
import fnmatch
import subprocess
from pathlib import Path
from urllib.parse import quote
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock, Thread
from pyicloud.services.drive import DriveNode, DriveService
from requests_toolbelt import MultipartEncoder

# Per-user settings live outside the repo (KEY=value lines); environment variables override them.
CONFIG_FILE = Path(os.environ.get("ICLOUD_SYNC_CONFIG", Path.home() / ".config" / "icloud-sync" / "config")).expanduser()


def _read_config():
    config = {}
    if CONFIG_FILE.exists():
        for line in CONFIG_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                config[key.strip()] = value.strip().strip("'\"")
    return config


def _setting(key, default=None):
    return os.environ.get(key) or _read_config().get(key) or default


def save_setting(key, value):
    lines = CONFIG_FILE.read_text().splitlines() if CONFIG_FILE.exists() else []
    lines = [l for l in lines if l.split("=", 1)[0].strip() != key] + [f"{key}={value}"]
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text("\n".join(lines) + "\n")
    CONFIG_FILE.chmod(0o600)


APPLE_ID = _setting("ICLOUD_APPLE_ID")
SYNC_DIR = Path(_setting("ICLOUD_SYNC_DIR", Path.home() / "Documents" / "icloud-drive")).expanduser()
# pyicloud defaults to /tmp/pyicloud, which is wiped on reboot and would force a new 2FA login.
COOKIE_DIR = str(Path(_setting("ICLOUD_SYNC_COOKIES", Path.home() / ".config" / "icloud-sync" / "session")).expanduser())
STATE_FILE = Path(_setting("ICLOUD_SYNC_STATE", Path(__file__).parent / ".sync_state.json")).expanduser()

MISSING_APPLE_ID = f"No Apple ID configured. Run `uv run python authenticate.py`, or set ICLOUD_APPLE_ID in {CONFIG_FILE}."
IGNORE_FILE = Path(__file__).parent / ".syncignore"
# Per-machine list of iCloud folders to leave alone (never downloaded, uploaded or deleted).
SKIP_FILE = Path(_setting("ICLOUD_SYNC_SKIP", Path.home() / ".config" / "icloud-sync" / "skip.json")).expanduser()
ICON_OK = Path(__file__).parent / "icons" / "folder-cloud-ok.svg"
ICON_ERROR = Path(__file__).parent / "icons" / "folder-cloud-error.svg"
ICON_SYNCING = Path(__file__).parent / "icons" / "folder-cloud-syncing.svg"
MAX_DOWNLOAD_WORKERS = 100
MAX_UPLOAD_WORKERS = 5
MAX_DELETE_WORKERS = 10
MAX_SCAN_WORKERS = 10
MAX_RETRIES = 5
RETRY_BASE_WAIT = 10
LARGE_FILE_THRESHOLD = 5 * 1024 * 1024
LARGE_UPLOAD_THRESHOLD = 100 * 1024 * 1024
DOWNLOAD_STALL_TIMEOUT = 120

print_lock = Lock()
# Uploads above LARGE_UPLOAD_THRESHOLD run one at a time so several multi-GB
# videos never saturate bandwidth / memory together.
large_upload_lock = Lock()


def _send_file_streaming(self, folder_id, file_object, zone="com.apple.CloudDocs", **kwargs):
    # pyicloud's send_file posts with `files=`, which makes requests read the whole
    # file into memory and then build a second in-memory copy of the multipart body.
    # A 4.7 GB video then costs ~10 GB of RAM. MultipartEncoder streams from disk instead.
    document_id, content_url = self._get_upload_contentws_url(file_object=file_object, zone=zone)

    file_object.seek(0)
    encoder = MultipartEncoder(
        fields={file_object.name: (os.path.basename(file_object.name), file_object)}
    )
    request = self.session.post(
        content_url, data=encoder, headers={"Content-Type": encoder.content_type}
    )
    self._raise_if_error(request)
    content_response = request.json()["singleFile"]
    self._update_contentws(folder_id, content_response, document_id, file_object, zone, **kwargs)


DriveService.send_file = _send_file_streaming


def _upload(node, local_path, file_size):
    with open(local_path, "rb") as f:
        if file_size > LARGE_UPLOAD_THRESHOLD:
            with large_upload_lock:
                node.upload(f)
        else:
            node.upload(f)


def load_ignore_patterns():
    patterns = []
    if IGNORE_FILE.exists():
        with open(IGNORE_FILE) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    patterns.append(line)
    return patterns


def load_skip_folders():
    """Read {"skip": ["Photos/2019", ...]} from SKIP_FILE, creating an empty one on first use.

    A broken file raises instead of returning []: silently syncing a folder the user
    skipped (and maybe deleted locally) would trash it from iCloud.
    """
    if not SKIP_FILE.exists():
        save_skip_folders([])
        return []
    try:
        data = json.loads(SKIP_FILE.read_text() or "{}")
        folders = data.get("skip", [])
        if not isinstance(folders, list) or not all(isinstance(f, str) for f in folders):
            raise ValueError('"skip" must be a list of folder paths')
    except (ValueError, AttributeError) as e:
        raise ValueError(f"Invalid skip list {SKIP_FILE}: {e}. Fix it before syncing.") from None
    return [f.strip().strip("/") for f in folders if f.strip().strip("/")]


def save_skip_folders(folders):
    SKIP_FILE.parent.mkdir(parents=True, exist_ok=True)
    SKIP_FILE.write_text(json.dumps({"skip": folders}, indent=2, ensure_ascii=False) + "\n")


def is_skipped(rel_path, skip_folders):
    """Skip entries are paths from the iCloud Drive root (globs allowed), e.g. `Photos/2019`."""
    return any(rel_path == f or rel_path.startswith(f + "/") or fnmatch.fnmatch(rel_path, f)
               for f in skip_folders)


def is_ignored(name, rel_path, patterns):
    for pattern in patterns:
        if pattern.endswith("/"):
            dir_pattern = pattern.rstrip("/")
            if fnmatch.fnmatch(name, dir_pattern):
                return True
            for part in Path(rel_path).parts:
                if fnmatch.fnmatch(part, dir_pattern):
                    return True
        else:
            if fnmatch.fnmatch(name, pattern):
                return True
    return False


def set_folder_icon(state):
    if state == "syncing":
        icon = ICON_SYNCING
    elif state == "ok":
        icon = ICON_OK
    else:
        icon = ICON_ERROR
    if not icon.exists():
        return
    try:
        subprocess.run(
            ["gio", "set", str(SYNC_DIR), "metadata::custom-icon", f"file://{icon.resolve()}"],
            capture_output=True, timeout=5
        )
    except Exception:
        pass


def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"synced_files": [], "last_sync": None}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def format_size(size_bytes):
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.1f} GB"


def safe_print(msg, verbose=False, verbose_only=False):
    if verbose_only and not verbose:
        return
    with print_lock:
        print(msg)


def relative_path(path):
    return str(path.relative_to(SYNC_DIR))


def is_shared(node):
    """True for items another account shared with us (read-only for this sync).

    pyicloud can't create, upload or trash inside them (Apple answers 400 or returns
    a half-made folder), so the sync treats shared folders as download-only.
    """
    data = getattr(node, "data", None) or {}
    wsid = str(data.get("drivewsid", ""))
    return data.get("share") == "TO_ME" or wsid.startswith(
        ("SHARED_FOLDER", "FILE_IN_SHARED_FOLDER", "FOLDER_IN_SHARED_FOLDER")
    )


def remote_mtime(node):
    """Cloud modification time as a Unix timestamp, or None.

    pyicloud returns a *naive* datetime that is really UTC; calling .timestamp()
    on it treats it as local time, which shifts every cloud date by the local UTC
    offset (+3h in Brazil) and makes the cloud copy look newer than local edits.
    """
    dt = node.date_modified
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def open_node(node, **kwargs):
    """node.open(), but works for files shared with us by another account.

    pyicloud <= 2.5.0 requests shared files from our own zone and gets a 404.
    The owner's record name must be appended to the download path
    (same fix as upstream timlaing/pyicloud PR #396).
    """
    data = node.data
    owner = (data.get("owner") or {}).get("ownerRecordName")
    is_shared = str(data.get("drivewsid", "")).startswith(("FILE_IN_SHARED_FOLDER", "FOLDER_IN_SHARED_FOLDER"))
    if not (is_shared and isinstance(owner, str) and owner and data.get("size")):
        return node.open(**kwargs)

    drive = node.connection
    url = drive._document_root + f"/ws/{data['zone']}/download/by_id/{quote(owner, safe='')}"
    params = dict(drive.params)
    params["document_id"] = data["docwsid"]
    response = drive.session.get(url, params=params)
    drive._raise_if_error(response)
    tokens = response.json()
    token = tokens.get("data_token") or tokens.get("package_token")
    if not token or not token.get("url"):
        raise KeyError("'data_token' nor 'package_token'")
    return drive.session.get(token["url"], params=drive.params, **kwargs)


def download_file(node, local_path, verbose=False):
    local_path.parent.mkdir(parents=True, exist_ok=True)

    remote_modified = remote_mtime(node)
    if local_path.exists() and remote_modified:
        local_modified = local_path.stat().st_mtime
        if remote_modified <= local_modified:
            return "skipped"

    file_name = local_path.relative_to(SYNC_DIR)
    total_size = node.size or 0
    size_str = f"  ({format_size(total_size)})" if total_size > 0 else ""
    large_file = total_size >= LARGE_FILE_THRESHOLD

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            if attempt > 1:
                wait = RETRY_BASE_WAIT * attempt
                safe_print(f"  ↻ Retry {attempt}/{MAX_RETRIES} (waiting {wait}s): {file_name}", verbose=verbose)
                time.sleep(wait)

            if large_file:
                safe_print(f"  ↓ Downloading: {file_name}{size_str}", verbose=verbose)
            else:
                safe_print(f"  ↓ Downloading: {file_name}{size_str}", verbose=verbose, verbose_only=True)

            downloaded = 0
            last_pct = -1
            last_report_time = time.time()
            start_time = time.time()
            last_chunk_time = [time.time()]
            stall_error = [None]

            with open(local_path, "wb") as f:
                response = open_node(node, stream=True)

                def watchdog():
                    while stall_error[0] is None:
                        time.sleep(10)
                        if stall_error[0] is not None:
                            break
                        if time.time() - last_chunk_time[0] > DOWNLOAD_STALL_TIMEOUT:
                            stall_error[0] = TimeoutError(
                                f"No data for {DOWNLOAD_STALL_TIMEOUT}s"
                            )
                            try:
                                response.close()
                            except Exception:
                                pass
                            break

                wd = Thread(target=watchdog, daemon=True)
                wd.start()

                for chunk in response.iter_content(chunk_size=65536):
                    if stall_error[0]:
                        raise stall_error[0]
                    if chunk:
                        f.write(chunk)
                        last_chunk_time[0] = time.time()
                        if large_file and total_size > 0:
                            downloaded += len(chunk)
                            now = time.time()
                            pct = int(downloaded / total_size * 100)
                            if pct >= last_pct + 10 or (now - last_report_time) >= 5:
                                elapsed = now - start_time
                                speed = downloaded / elapsed if elapsed > 0 else 0
                                safe_print(
                                    f"      {file_name}  {pct}%"
                                    f"  {format_size(downloaded)}/{format_size(total_size)}"
                                    f"  {format_size(speed)}/s",
                                    verbose=verbose,
                                )
                                last_pct = pct
                                last_report_time = now

                stall_error[0] = False

            if remote_modified:
                ts = remote_modified
                os.utime(local_path, (ts, ts))

            if large_file:
                elapsed = time.time() - start_time
                speed = total_size / elapsed if elapsed > 0 else 0
                safe_print(
                    f"  ✓ Done: {file_name}  ({format_size(total_size)} in {elapsed:.1f}s, avg {format_size(speed)}/s)",
                    verbose=verbose,
                )
            else:
                safe_print(f"  ✓ Done: {file_name}", verbose=verbose, verbose_only=True)
            return "downloaded"
        except Exception as e:
            if attempt == MAX_RETRIES:
                if local_path.exists():
                    local_path.unlink()
                raise e

    return "downloaded"


def upload_file_new(node, local_path, verbose=False):
    file_name = local_path.relative_to(SYNC_DIR)
    file_size = local_path.stat().st_size

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            if attempt > 1:
                wait = RETRY_BASE_WAIT * attempt
                safe_print(f"  ↻ Retry {attempt}/{MAX_RETRIES} (waiting {wait}s): {file_name}", verbose=verbose)
                time.sleep(wait)

            safe_print(f"  ↑ Uploading (new): {file_name}  {format_size(file_size)}", verbose=verbose, verbose_only=True)
            _upload(node, local_path, file_size)
            upload_ts = time.time()
            os.utime(local_path, (upload_ts, upload_ts))
            safe_print(f"  ✓ Done: {file_name}", verbose=verbose, verbose_only=True)
            return "uploaded"
        except Exception as e:
            if attempt == MAX_RETRIES:
                raise e

    return "uploaded"


def upload_file_existing(node, local_path, verbose=False):
    # BUG 3 FIX: only catch KeyError/IndexError; other exceptions propagate to retry loop
    remote_modified = None
    try:
        existing = node[local_path.name]
        remote_modified = remote_mtime(existing)
    except (KeyError, IndexError):
        pass

    if remote_modified:
        local_modified = local_path.stat().st_mtime
        # Allow 60s of clock skew: iCloud records its own receive timestamp which can be
        # slightly earlier than the local time.time() we set after the upload completes.
        if local_modified <= remote_modified + 60:
            return "skipped"

    file_name = local_path.relative_to(SYNC_DIR)
    file_size = local_path.stat().st_size

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            if attempt > 1:
                wait = RETRY_BASE_WAIT * attempt
                safe_print(f"  ↻ Retry {attempt}/{MAX_RETRIES} (waiting {wait}s): {file_name}", verbose=verbose)
                time.sleep(wait)

            safe_print(f"  ↑ Uploading: {file_name}  {format_size(file_size)}", verbose=verbose, verbose_only=True)
            _upload(node, local_path, file_size)
            upload_ts = time.time()
            os.utime(local_path, (upload_ts, upload_ts))
            safe_print(f"  ✓ Done: {file_name}", verbose=verbose, verbose_only=True)
            return "uploaded"
        except Exception as e:
            if attempt == MAX_RETRIES:
                raise e

    return "uploaded"


def collect_tasks(node, local_dir, synced_files, last_sync_ts=None, ignore_patterns=None, skip_downloads=False, verbose=False, shared=False, skip_folders=()):
    """Walk one folder level; classify each entry into one of six scenarios.

    For every name in (local_children ∪ remote_children):
      1. Both exist, local newer     -> upload (overwrite cloud)
      2. Both exist, cloud newer     -> download (overwrite local)
      3. Local-only, in manifest     -> delete local      (cloud-side deletion)
      4. Cloud-only, in manifest     -> delete cloud      (local-side deletion)
      5. Local-only, not in manifest -> upload as new
      6. Cloud-only, not in manifest -> download as new

    Manifest membership (`synced_files`) is what distinguishes 3/4 from 5/6.
    Inside folders shared with us (`shared`), the cloud always wins: local deletions
    are re-downloaded and new or edited local items are left alone, never pushed.
    Returns (download_tasks, upload_tasks, delete_remote, delete_local, manifest_entries).
    """
    local_dir.mkdir(parents=True, exist_ok=True)

    download_tasks = []
    upload_tasks = []
    delete_remote_tasks = []
    delete_local_tasks = []
    all_synced = set()

    if ignore_patterns is None:
        ignore_patterns = []

    # Snapshot both sides into name -> handle dicts. remote_listed tracks names
    # that iCloud returned even if we then failed to fetch their DriveNode, so a
    # transient fetch failure isn't misread as a cloud-side deletion.
    remote_listed = set()
    remote_children = {}
    try:
        for name in node.dir():
            remote_listed.add(name)
            try:
                remote_children[name] = node[name]
            except (KeyError, IndexError, Exception) as e:
                rel = relative_path(local_dir / name)
                safe_print(f"  !! Skipping vanished remote item: {rel} ({e})", verbose=verbose, verbose_only=True)
    except Exception:
        raise

    local_children = {}
    if local_dir.exists():
        for item in local_dir.iterdir():
            local_children[item.name] = item

    folder_name = local_dir.relative_to(SYNC_DIR) if local_dir != SYNC_DIR else Path(".")
    safe_print(f"  Scanning: {folder_name}/", verbose=verbose, verbose_only=True)

    subfolder_tasks = []  # (remote_node, local_path, skip_downloads_for_child, name)

    for name in remote_listed | set(local_children):
        local_path = local_dir / name
        rel = relative_path(local_path)
        remote = remote_children.get(name)
        local = local_children.get(name)
        was_synced = rel in synced_files

        if is_ignored(name, rel, ignore_patterns):
            safe_print(f"  ~ Ignoring: {rel}", verbose=verbose, verbose_only=True)
            continue

        # Skipped folders are left untouched on both sides and dropped from the
        # manifest, so un-skipping later downloads them again instead of deleting.
        if is_skipped(rel, skip_folders):
            safe_print(f"  ~ Skipping: {rel}", verbose=verbose, verbose_only=True)
            continue

        # iCloud listed it but the DriveNode fetch failed — defer to next sync.
        if name in remote_listed and remote is None:
            if was_synced:
                all_synced.add(rel)
            continue

        is_folder = (
            (remote is not None and remote.type in ("folder", "app_library")) or
            (local is not None and local.is_dir())
        )

        if is_folder:
            remote_shared = remote is not None and is_shared(remote)
            # Shared, local-only folder: we can't create it in someone else's folder.
            if remote is None and shared and not was_synced:
                safe_print(f"  !! Not uploaded (shared folder is download-only): {rel}", verbose=verbose)
                continue
            # Shared folder deleted/moved locally -> restore it instead of trashing the owner's copy
            if local is None and was_synced and remote_shared:
                safe_print(f"  ↓ Restoring shared folder removed locally: {rel}", verbose=verbose, verbose_only=True)
                was_synced = False
            # Scenario 3 (folder deleted from cloud) -> trash local
            if remote is None and was_synced:
                delete_local_tasks.append((local_path, rel))
                all_synced.add(rel)  # discarded by execute_local_deletes on success
                continue
            # Scenario 4 (folder deleted locally) -> trash on cloud
            if local is None and was_synced:
                delete_remote_tasks.append((remote, rel))
                all_synced.add(rel)  # discarded by execute_deletes on success
                continue
            # Scenario 5 (new local folder) -> create on cloud, then recurse
            if remote is None:
                remote = _create_remote_folder(node, name, rel, verbose)
                if remote is None:
                    continue
            # Scenario 6 (new cloud folder): recursion will mkdir local_path.

            all_synced.add(rel)
            # Quick-mode skip: if the remote folder hasn't been touched since
            # last sync, don't enqueue downloads inside it (uploads/deletes still run).
            child_skip_dl = bool(
                last_sync_ts and remote_mtime(remote)
                and remote_mtime(remote) <= last_sync_ts
            )
            if remote_shared:
                child_skip_dl = False
            subfolder_tasks.append((remote, local_path, child_skip_dl, name, shared or remote_shared))
            continue

        # ---- File scenarios ----
        if remote is None and shared and not was_synced:
            safe_print(f"  !! Not uploaded (shared folder is download-only): {rel}", verbose=verbose)
            continue
        if local is None and was_synced and is_shared(remote):
            # Shared file deleted/moved locally -> restore it instead of trashing the owner's copy
            download_tasks.append((remote, local_path, rel))
            all_synced.add(rel)
            continue

        if remote is None:
            # Scenario 3 (local file, was synced, cloud-side deletion)
            if was_synced:
                delete_local_tasks.append((local_path, rel))
                all_synced.add(rel)
            # Scenario 5 (local file, never synced)
            else:
                upload_tasks.append(("new", node, local_path, rel))
                all_synced.add(rel)
            continue

        if local is None:
            # Scenario 4 (cloud file, was synced, local-side deletion)
            if was_synced:
                delete_remote_tasks.append((remote, rel))
                all_synced.add(rel)
            # Scenario 6 (cloud file, never synced)
            elif not skip_downloads:
                download_tasks.append((remote, local_path, rel))
                all_synced.add(rel)
            else:
                all_synced.add(rel)
            continue

        # Both exist — pick winner by mtime.
        local_mtime = local_path.stat().st_mtime
        cloud_mtime = remote_mtime(remote) or 0
        # iCloud overwrites the remote mtime with its own receive time on upload;
        # allow 60s of clock skew so a just-uploaded file isn't re-downloaded.
        if abs(local_mtime - cloud_mtime) <= 60:
            all_synced.add(rel)
        elif local_mtime > cloud_mtime and is_shared(remote):
            safe_print(f"  !! Local edit not uploaded (shared folder is download-only): {rel}", verbose=verbose)
            all_synced.add(rel)
        elif local_mtime > cloud_mtime:
            # Scenario 1: local newer -> upload
            upload_tasks.append(("existing", node, local_path, rel))
            all_synced.add(rel)
        elif not skip_downloads:
            # Scenario 2: cloud newer -> download
            download_tasks.append((remote, local_path, rel))
            all_synced.add(rel)
        else:
            all_synced.add(rel)

    if subfolder_tasks:
        with ThreadPoolExecutor(max_workers=MAX_SCAN_WORKERS) as scan_executor:
            pending = list(subfolder_tasks)
            attempt = 0

            while pending and attempt < MAX_RETRIES:
                attempt += 1
                if attempt > 1:
                    wait = RETRY_BASE_WAIT * attempt
                    safe_print(f"  ↻ Scan retry {attempt}/{MAX_RETRIES} for {len(pending)} folders (waiting {wait}s)...", verbose=verbose)
                    time.sleep(wait)

                scan_futures = {}
                for child_node, child_path, child_skip, child_name, child_shared in pending:
                    future = scan_executor.submit(
                        collect_tasks, child_node, child_path, synced_files,
                        last_sync_ts, ignore_patterns, child_skip, verbose, child_shared,
                        skip_folders,
                    )
                    scan_futures[future] = (child_node, child_path, child_skip, child_name, child_shared)

                failed = []
                parent_refreshed = False
                for future in as_completed(scan_futures):
                    child_node, child_path, child_skip, child_name, child_shared = scan_futures[future]
                    try:
                        c_dl, c_ul, c_drm, c_dlc, c_synced = future.result()
                        download_tasks.extend(c_dl)
                        upload_tasks.extend(c_ul)
                        delete_remote_tasks.extend(c_drm)
                        delete_local_tasks.extend(c_dlc)
                        all_synced.update(c_synced)
                    except KeyError as e:
                        # pyicloud quirk: parent's listing returned a child item without
                        # 'drivewsid', so get_children() on the child crashes. Refresh the
                        # parent once per retry round and re-grab the child by name.
                        if "drivewsid" in str(e):
                            try:
                                if not parent_refreshed:
                                    node.get_children(force=True)
                                    parent_refreshed = True
                                fresh = node[child_name]
                                safe_print(f"  ↻ Refreshing child node (missing drivewsid): {child_path}", verbose=verbose, verbose_only=True)
                                failed.append((fresh, child_path, child_skip, child_name, child_shared))
                                continue
                            except Exception:
                                pass
                        if attempt == MAX_RETRIES:
                            safe_print(f"  !! Error scanning {child_path}: {e}", verbose=verbose)
                        else:
                            safe_print(f"  !! Scan failed (will retry): {child_path}", verbose=verbose, verbose_only=True)
                        failed.append((child_node, child_path, child_skip, child_name, child_shared))
                    except Exception as e:
                        if attempt == MAX_RETRIES:
                            safe_print(f"  !! Error scanning {child_path}: {e}", verbose=verbose)
                        else:
                            safe_print(f"  !! Scan failed (will retry): {child_path}", verbose=verbose, verbose_only=True)
                        failed.append((child_node, child_path, child_skip, child_name, child_shared))

                pending = failed

            # Folders we never managed to scan: keep their old manifest entries (so nothing
            # inside looks deleted next time) and don't mark a never-synced folder as synced
            # (otherwise the next run would think it vanished from iCloud and delete it locally).
            for _, child_path, _, _, _ in pending:
                child_rel = relative_path(child_path)
                if child_rel not in synced_files:
                    all_synced.discard(child_rel)
                prefix = child_rel + "/"
                all_synced.update(k for k in synced_files if k == child_rel or k.startswith(prefix))

    return download_tasks, upload_tasks, delete_remote_tasks, delete_local_tasks, all_synced


def _create_remote_folder(parent_node, name, rel, verbose=False):
    """Create a folder under parent_node and return its DriveNode (or None on failure).

    Handles three pyicloud quirks: stale parent listings, mkdir returning raw
    JSON instead of a DriveNode, and propagation delay before the folder appears.
    """
    # Stale parent cache: get_children(force=True) is required because passing
    # node._children = None alone leaves get_children() rebuilding from cached data.
    try:
        parent_node.get_children(force=True)
        if name in set(parent_node.dir()):
            safe_print(f"  ~ Folder already exists in iCloud (was stale): {rel}", verbose=verbose, verbose_only=True)
            return parent_node[name]
    except Exception:
        pass

    safe_print(f"  ↑ Creating folder: {rel}", verbose=verbose, verbose_only=True)
    try:
        mkdir_result = parent_node.mkdir(name)
        if mkdir_result:
            folder_data = mkdir_result[0] if isinstance(mkdir_result, list) else mkdir_result
            if isinstance(folder_data, dict):
                return DriveNode(parent_node.connection, folder_data)
            if hasattr(folder_data, 'dir'):
                return folder_data
    except Exception as e:
        safe_print(f"  !! mkdir error for {rel}: {e}", verbose=verbose)

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            parent_node.get_children(force=True)
            if name in set(parent_node.dir()):
                return parent_node[name]
        except Exception:
            pass
        wait = RETRY_BASE_WAIT * attempt
        safe_print(f"  ↻ Waiting for folder to appear ({attempt}/{MAX_RETRIES}, {wait}s): {rel}", verbose=verbose)
        time.sleep(wait)

    safe_print(f"  !! Could not create folder after retries — will retry on next sync: {rel}", verbose=verbose)
    return None


def execute_downloads(tasks, stats, new_synced, verbose=False):
    if not tasks:
        return

    total = len(tasks)
    print(f"\n  Starting {total} downloads ({MAX_DOWNLOAD_WORKERS} parallel)...")

    with ThreadPoolExecutor(max_workers=MAX_DOWNLOAD_WORKERS) as executor:
        futures = {}
        for node, local_path, rel in tasks:
            future = executor.submit(download_file, node, local_path, verbose)
            futures[future] = rel

        done = 0
        for future in as_completed(futures):
            rel = futures[future]
            done += 1
            try:
                result = future.result()
                if result == "downloaded":
                    stats["downloaded"] += 1
                else:
                    stats["skipped"] += 1
            except Exception as e:
                safe_print(f"  !! Error downloading {rel}: {e}", verbose=verbose)
                stats["errors"] += 1
                stats["failed_files"].append(f"↓ {rel}")
                new_synced.discard(rel)  # don't record a failed download as synced

            if verbose:
                if done % 20 == 0 or done == total:
                    safe_print(f"  [{done}/{total}] downloads processed", verbose=verbose)
            else:
                remaining = total - done
                with print_lock:
                    import sys
                    sys.stdout.write(f"\r  ↓ Downloads: {done}/{total} done ({remaining} remaining)")
                    if done == total:
                        sys.stdout.write("\n")
                    sys.stdout.flush()


def execute_uploads(tasks, stats, new_synced, verbose=False):
    if not tasks:
        return

    total = len(tasks)
    print(f"\n  Starting {total} uploads ({MAX_UPLOAD_WORKERS} parallel)...")

    with ThreadPoolExecutor(max_workers=MAX_UPLOAD_WORKERS) as executor:
        futures = {}
        for task in tasks:
            _, node, local_path, rel = task
            if task[0] == "new":
                future = executor.submit(upload_file_new, node, local_path, verbose)
            else:
                future = executor.submit(upload_file_existing, node, local_path, verbose)
            futures[future] = rel

        done = 0
        for future in as_completed(futures):
            rel = futures[future]
            done += 1
            try:
                result = future.result()
                if result == "uploaded":
                    stats["uploaded"] += 1
                    new_synced.add(rel)
                else:
                    stats["skipped"] += 1
            except Exception as e:
                safe_print(f"  !! Error uploading {rel}: {e}", verbose=verbose)
                stats["errors"] += 1
                stats["failed_files"].append(f"↑ {rel}")

            if verbose:
                if done % 10 == 0 or done == total:
                    safe_print(f"  [{done}/{total}] uploads processed", verbose=verbose)
            else:
                remaining = total - done
                with print_lock:
                    import sys
                    sys.stdout.write(f"\r  ↑ Uploads: {done}/{total} done ({remaining} remaining)")
                    if done == total:
                        sys.stdout.write("\n")
                    sys.stdout.flush()


def execute_deletes(tasks, stats, new_synced, verbose=False):
    if not tasks:
        return

    total = len(tasks)
    print(f"\n  Processing {total} deletions ({MAX_DELETE_WORKERS} parallel)...")

    with ThreadPoolExecutor(max_workers=MAX_DELETE_WORKERS) as executor:
        futures = {}
        for child, rel in tasks:
            if is_shared(child):
                safe_print(f"  !! Refusing to trash shared item: {rel}", verbose=verbose)
                continue
            future = executor.submit(child.move_to_trash)
            futures[future] = (child, rel)

        done = 0
        for future in as_completed(futures):
            _, rel = futures[future]
            done += 1
            try:
                future.result()
                safe_print(f"  x Trashed: {rel}", verbose=verbose, verbose_only=True)
                stats["deleted_remote"] += 1
                new_synced.discard(rel)
            except Exception as e:
                safe_print(f"  !! Error deleting {rel}: {e}", verbose=verbose)
                stats["errors"] += 1
                stats["failed_files"].append(f"x {rel}")

            if not verbose:
                remaining = total - done
                with print_lock:
                    import sys
                    sys.stdout.write(f"\r  x Deletes: {done}/{total} done ({remaining} remaining)")
                    if done == total:
                        sys.stdout.write("\n")
                    sys.stdout.flush()


def execute_local_deletes(tasks, stats, new_synced, verbose=False):
    if not tasks:
        return

    total = len(tasks)
    print(f"\n  Processing {total} local deletions (removed from iCloud)...")
    done = 0

    for local_path, rel in tasks:
        try:
            # Move to the desktop Trash (recoverable) instead of deleting for good.
            subprocess.run(["gio", "trash", "--", str(local_path)], check=True, capture_output=True)
            for key in list(new_synced):
                if key == rel or key.startswith(rel + "/"):
                    new_synced.discard(key)
            safe_print(f"  x Moved to Trash: {rel}", verbose=verbose, verbose_only=True)
            stats["deleted_local"] += 1
        except Exception as e:
            safe_print(f"  !! Error deleting local {rel}: {e}", verbose=verbose)
            stats["errors"] += 1
            stats["failed_files"].append(f"x local {rel}")
        done += 1
        if not verbose:
            remaining = total - done
            with print_lock:
                import sys
                sys.stdout.write(f"\r  x Local deletes: {done}/{total} done ({remaining} remaining)")
                if done == total:
                    sys.stdout.write("\n")
                sys.stdout.flush()


def collect_ignored_remote(node, path_prefix, ignore_patterns, verbose=False):
    found = []
    subfolders = []

    try:
        names = node.dir()
    except Exception as e:
        safe_print(f"  !! Error scanning {path_prefix}: {e}", verbose=verbose)
        return found

    folder_name = path_prefix or "."
    safe_print(f"  Scanning: {folder_name}/", verbose=verbose, verbose_only=True)

    for name in names:
        child = node[name]
        rel = f"{path_prefix}/{name}" if path_prefix else name

        if is_ignored(name, rel, ignore_patterns):
            found.append((child, rel))
            continue

        if child.type in ("folder", "app_library"):
            subfolders.append((child, rel, name))

    if subfolders:
        with ThreadPoolExecutor(max_workers=MAX_SCAN_WORKERS) as executor:
            pending = list(subfolders)
            attempt = 0

            while pending and attempt < MAX_RETRIES:
                attempt += 1
                if attempt > 1:
                    wait = RETRY_BASE_WAIT * attempt
                    safe_print(f"  ↻ Scan retry {attempt}/{MAX_RETRIES} for {len(pending)} folders (waiting {wait}s)...", verbose=verbose)
                    time.sleep(wait)

                futures = {}
                for child_node, child_rel, child_name in pending:
                    future = executor.submit(
                        collect_ignored_remote, child_node, child_rel, ignore_patterns, verbose
                    )
                    futures[future] = (child_node, child_rel, child_name)

                failed = []
                parent_refreshed = False
                for future in as_completed(futures):
                    child_node, child_rel, child_name = futures[future]
                    try:
                        found.extend(future.result())
                    except KeyError as e:
                        if "drivewsid" in str(e):
                            try:
                                if not parent_refreshed:
                                    node.get_children(force=True)
                                    parent_refreshed = True
                                fresh = node[child_name]
                                safe_print(f"  ↻ Refreshing child node (missing drivewsid): {child_rel}", verbose=verbose, verbose_only=True)
                                failed.append((fresh, child_rel, child_name))
                                continue
                            except Exception:
                                pass
                        if attempt == MAX_RETRIES:
                            safe_print(f"  !! Error scanning {child_rel}: {e}", verbose=verbose)
                        else:
                            safe_print(f"  !! Scan failed (will retry): {child_rel}", verbose=verbose, verbose_only=True)
                        failed.append((child_node, child_rel, child_name))
                    except Exception as e:
                        if attempt == MAX_RETRIES:
                            safe_print(f"  !! Error scanning {child_rel}: {e}", verbose=verbose)
                        else:
                            safe_print(f"  !! Scan failed (will retry): {child_rel}", verbose=verbose, verbose_only=True)
                        failed.append((child_node, child_rel, child_name))

                pending = failed

    return found


def run_sync(api, full=False, upload_only=False, verbose=False):
    ignore_patterns = load_ignore_patterns()
    skip_folders = load_skip_folders()
    SYNC_DIR.mkdir(parents=True, exist_ok=True)
    set_folder_icon("syncing")

    state = load_state()
    synced_files = set(state["synced_files"])
    last_sync = state.get("last_sync")

    if full or last_sync is None:
        last_sync_ts = None
    else:
        last_sync_ts = last_sync

    print("\nPhase 1: Scanning iCloud Drive...")
    download_tasks, upload_tasks, delete_tasks, local_delete_tasks, new_synced = collect_tasks(
        api.drive.root, SYNC_DIR, synced_files, last_sync_ts, ignore_patterns, verbose=verbose,
        skip_folders=skip_folders,
    )

    print(f"\n  Found: {len(download_tasks)} to check/download, "
          f"{len(upload_tasks)} to check/upload, "
          f"{len(delete_tasks)} to delete from iCloud, "
          f"{len(local_delete_tasks)} to delete locally")

    stats = {"downloaded": 0, "uploaded": 0, "deleted_remote": 0, "deleted_local": 0,
             "skipped": 0, "errors": 0, "failed_files": []}

    print("\nPhase 2: Syncing files...")
    if upload_only:
        # Start from existing synced state so unexecuted downloads aren't recorded as synced,
        # and preserve last_sync so the next normal run still picks up pending remote changes.
        new_synced = set(synced_files)
        execute_uploads(upload_tasks, stats, new_synced, verbose=verbose)
    else:
        execute_downloads(download_tasks, stats, new_synced, verbose=verbose)
        execute_uploads(upload_tasks, stats, new_synced, verbose=verbose)
        execute_deletes(delete_tasks, stats, new_synced, verbose=verbose)
        execute_local_deletes(local_delete_tasks, stats, new_synced, verbose=verbose)

    if upload_only:
        save_state({"synced_files": sorted(new_synced), "last_sync": last_sync})
    else:
        sync_time = datetime.now(tz=timezone.utc).timestamp()
        save_state({"synced_files": sorted(new_synced), "last_sync": sync_time})
    set_folder_icon("ok" if stats["errors"] == 0 else "error")

    return stats


def run_scan(api, full=False, verbose=False):
    ignore_patterns = load_ignore_patterns()
    skip_folders = load_skip_folders()
    state = load_state()
    synced_files = set(state["synced_files"])
    last_sync = state.get("last_sync")
    last_sync_ts = None if full or last_sync is None else last_sync

    print("\nScanning iCloud Drive...")
    download_tasks, upload_tasks, delete_tasks, local_delete_tasks, _ = collect_tasks(
        api.drive.root, SYNC_DIR, synced_files, last_sync_ts, ignore_patterns, verbose=verbose,
        skip_folders=skip_folders,
    )

    return {
        "downloads": len(download_tasks),
        "uploads": len(upload_tasks),
        "deletes": len(delete_tasks),
        "local_deletes": len(local_delete_tasks),
    }


def run_cleanup(api, verbose=False):
    ignore_patterns = load_ignore_patterns()
    if not ignore_patterns:
        return {"error": "No .syncignore file found. Nothing to clean up."}

    print("\nScanning iCloud for files matching .syncignore patterns...")
    found = collect_ignored_remote(api.drive.root, "", ignore_patterns, verbose=verbose)

    if not found:
        return {"trashed": 0, "local_deleted": 0, "errors": 0, "items": []}

    items = [rel for _, rel in found]

    trashed = 0
    errors = 0
    failed_items = []

    def trash_item(child, rel):
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                if attempt > 1:
                    wait = RETRY_BASE_WAIT * attempt
                    safe_print(f"  ↻ Retry {attempt}/{MAX_RETRIES} (waiting {wait}s): {rel}", verbose=verbose)
                    time.sleep(wait)
                child.move_to_trash()
                return "trashed"
            except Exception as e:
                if attempt == MAX_RETRIES:
                    raise e
        return "trashed"

    with ThreadPoolExecutor(max_workers=MAX_UPLOAD_WORKERS) as executor:
        futures = {executor.submit(trash_item, child, rel): rel for child, rel in found}
        for future in as_completed(futures):
            rel = futures[future]
            try:
                future.result()
                trashed += 1
            except Exception as e:
                safe_print(f"  !! Error trashing {rel}: {e}", verbose=verbose)
                errors += 1
                failed_items.append(rel)

    local_deleted = 0
    for _, rel in found:
        local_path = SYNC_DIR / rel
        if local_path.exists():
            try:
                if local_path.is_dir():
                    shutil.rmtree(local_path)
                else:
                    local_path.unlink()
                local_deleted += 1
            except Exception as e:
                safe_print(f"  !! Error deleting local {rel}: {e}", verbose=verbose)

    state = load_state()
    synced = set(state["synced_files"])
    for _, rel in found:
        matching = [s for s in synced if s == rel or s.startswith(rel + "/")]
        for m in matching:
            synced.discard(m)
    state["synced_files"] = sorted(synced)
    save_state(state)

    return {
        "trashed": trashed,
        "local_deleted": local_deleted,
        "errors": errors,
        "items": items,
        "failed": failed_items,
    }
