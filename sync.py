import sys
import fcntl
import getpass
import argparse
from pyicloud import PyiCloudService
from sync_engine import (
    APPLE_ID, SYNC_DIR, COOKIE_DIR, STATE_FILE, MISSING_APPLE_ID,
    load_ignore_patterns, load_state, save_state,
    set_folder_icon, run_sync, run_cleanup,
    SKIP_FILE, load_skip_folders, save_skip_folders,
)


def _install_default_timeout(session, connect=30, read=60):
    # pyicloud's session.get/post calls don't set timeouts, so a flaky Apple
    # CDN connection can block download_file's node.open() forever — before
    # the chunk-level watchdog gets a chance to run. Inject a default timeout
    # on every request so stuck connections raise and the retry loop kicks in.
    orig_request = session.request

    def request_with_timeout(method, url, **kwargs):
        kwargs.setdefault("timeout", (connect, read))
        return orig_request(method, url, **kwargs)

    session.request = request_with_timeout


def authenticate():
    if not APPLE_ID:
        sys.exit(MISSING_APPLE_ID)
    try:
        api = PyiCloudService(APPLE_ID, cookie_directory=COOKIE_DIR)
    except Exception:
        if not sys.stdin.isatty():
            sys.exit("iCloud session expired. Run `uv run python authenticate.py` in a terminal.")
        password = getpass.getpass("Enter your Apple ID password: ")
        api = PyiCloudService(APPLE_ID, password, cookie_directory=COOKIE_DIR)

    if api.requires_2fa:
        if not sys.stdin.isatty():
            sys.exit("iCloud needs a new 2FA code. Run `uv run python authenticate.py` in a terminal.")
        code = input("Enter the 2FA code sent to your device: ")
        api.validate_2fa_code(code)
        if not api.is_trusted_session:
            api.trust_session()

    _install_default_timeout(api.session)
    return api


def manage_skip(args):
    try:
        folders = load_skip_folders()
    except ValueError as e:
        sys.exit(str(e))
    if args.skip:
        folder = args.skip.strip().strip("/")
        if folder not in folders:
            save_skip_folders(folders + [folder])
        print(f"Skipping '{folder}' from now on (saved in {SKIP_FILE}).")
        local = SYNC_DIR / folder
        if local.exists():
            print(f"Its local copy was left in place: {local}")
            print("You can delete it to free space; the iCloud copy is not affected.")
    elif args.unskip:
        folder = args.unskip.strip().strip("/")
        if folder not in folders:
            sys.exit(f"'{folder}' is not in the skip list. See `sync.py --skipped`.")
        save_skip_folders([f for f in folders if f != folder])
        print(f"'{folder}' will sync again. Run `sync.py --full` to download it now.")
    else:
        if not folders:
            print("No skipped folders.")
        for f in folders:
            print(f)


def main():
    parser = argparse.ArgumentParser(description="Sync iCloud Drive to local folder")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--cleanup", action="store_true")
    parser.add_argument("--upload-only", action="store_true")
    parser.add_argument("--skip", metavar="FOLDER", help="stop syncing an iCloud folder (path from the iCloud Drive root)")
    parser.add_argument("--unskip", metavar="FOLDER", help="sync a skipped folder again")
    parser.add_argument("--skipped", action="store_true", help="list skipped folders")
    args = parser.parse_args()

    if args.skip or args.unskip or args.skipped:
        manage_skip(args)
        return

    # One sync at a time (the 5-min timer and a manual run would corrupt the state file).
    lock = open(STATE_FILE.with_suffix(".lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("Another sync is already running. Skipping.")
        return

    ignore_patterns = load_ignore_patterns()
    try:
        skip_folders = load_skip_folders()
    except ValueError as e:
        sys.exit(str(e))

    print("Authenticating with iCloud...")
    api = authenticate()

    if args.cleanup:
        if not ignore_patterns:
            print("No .syncignore file found. Nothing to clean up.")
            return
        result = run_cleanup(api, verbose=args.verbose)
        if "error" in result:
            print(result["error"])
            return
        print(f"\n{'=' * 45}")
        print(f"  Cleanup complete!")
        print(f"{'=' * 45}")
        print(f"  Trashed from iCloud: {result['trashed']}")
        print(f"  Deleted locally:     {result['local_deleted']}")
        print(f"  Errors:              {result['errors']}")
        if result["failed"]:
            print(f"  Failed:")
            for f in result["failed"]:
                print(f"    {f}")
        print(f"{'=' * 45}")
        return

    print(f"\nSyncing iCloud Drive <-> {SYNC_DIR}")
    if ignore_patterns:
        print(f"  Ignoring {len(ignore_patterns)} patterns from .syncignore")
    if skip_folders:
        print(f"  Skipping {len(skip_folders)} folder(s) from {SKIP_FILE}")

    state = load_state()
    last_sync = state.get("last_sync")

    from datetime import datetime, timezone
    if args.full or args.upload_only or last_sync is None:
        mode = "full"
    else:
        last_sync_dt = datetime.fromtimestamp(last_sync, tz=timezone.utc)
        mode = f"quick (changes since {last_sync_dt.strftime('%Y-%m-%d %H:%M:%S')})"

    if args.upload_only:
        mode += " [upload-only]"
    print(f"  Mode: {mode}")

    stats = run_sync(api, full=args.full, upload_only=args.upload_only, verbose=args.verbose)

    print(f"\n{'=' * 45}")
    if stats["errors"] == 0:
        print(f"  Sync complete! All files synced.")
    else:
        print(f"  Sync finished with {stats['errors']} error(s).")
    print(f"{'=' * 45}")
    print(f"  Downloaded:          {stats['downloaded']}")
    print(f"  Uploaded:            {stats['uploaded']}")
    print(f"  Deleted from iCloud: {stats['deleted_remote']}")
    print(f"  Deleted locally:     {stats['deleted_local']}")
    print(f"  Already up to date:  {stats['skipped']}")
    print(f"  Errors:              {stats['errors']}")
    if stats["failed_files"]:
        print(f"{'=' * 45}")
        print(f"  Failed files:")
        for f in stats["failed_files"]:
            print(f"    {f}")
    print(f"{'=' * 45}")


if __name__ == "__main__":
    main()
