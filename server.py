import sys
import getpass
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from sync_engine import (
    load_state,
    run_sync,
    run_scan,
    run_cleanup,
    APPLE_ID, COOKIE_DIR, MISSING_APPLE_ID,
)


class SyncRequest(BaseModel):
    full: bool = False
    verbose: bool = False


_api = None


def _authenticate():
    global _api
    from pyicloud import PyiCloudService

    if not APPLE_ID:
        sys.exit(MISSING_APPLE_ID)
    print(f"Authenticating with iCloud ({APPLE_ID})...")
    try:
        api = PyiCloudService(APPLE_ID, cookie_directory=COOKIE_DIR)
    except Exception:
        password = getpass.getpass("Enter your Apple ID password: ")
        api = PyiCloudService(APPLE_ID, password, cookie_directory=COOKIE_DIR)

    if api.requires_2fa:
        code = input("Enter the 2FA code sent to your device: ")
        api.validate_2fa_code(code)
        if not api.is_trusted_session:
            api.trust_session()

    _api = api
    print("iCloud session ready.")


@asynccontextmanager
async def lifespan(app: FastAPI):
    _authenticate()
    yield


app = FastAPI(title="icloud-sync", lifespan=lifespan)


def _require_api():
    if _api is None:
        raise HTTPException(status_code=503, detail="iCloud session not initialized")
    return _api


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/status")
def status():
    state = load_state()
    last_sync = state.get("last_sync")
    last_sync_dt = (
        datetime.fromtimestamp(last_sync, tz=timezone.utc).isoformat()
        if last_sync else None
    )
    return {
        "session": "ok" if _api is not None else "unavailable",
        "last_sync": last_sync_dt,
        "synced_files": len(state.get("synced_files", [])),
    }


@app.post("/scan")
def scan(req: SyncRequest = SyncRequest()):
    api = _require_api()
    try:
        counts = run_scan(api, full=req.full, verbose=req.verbose)
        return {"status": "ok", "counts": counts}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/sync")
def sync_all(req: SyncRequest = SyncRequest()):
    api = _require_api()
    try:
        stats = run_sync(api, full=req.full, verbose=req.verbose)
        return {
            "status": "ok" if stats["errors"] == 0 else "error",
            "stats": {k: v for k, v in stats.items() if k != "failed_files"},
            "errors": stats["failed_files"],
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/sync/download")
def sync_download(req: SyncRequest = SyncRequest()):
    from sync_engine import (
        load_ignore_patterns, load_state, save_state,
        collect_tasks, execute_downloads, SYNC_DIR, load_skip_folders,
    )
    from datetime import datetime, timezone

    api = _require_api()
    try:
        ignore_patterns = load_ignore_patterns()
        SYNC_DIR.mkdir(parents=True, exist_ok=True)
        state = load_state()
        synced_files = set(state["synced_files"])
        last_sync = state.get("last_sync")
        last_sync_ts = None if req.full or last_sync is None else last_sync

        download_tasks, _, _, _, new_synced = collect_tasks(
            api.drive.root, SYNC_DIR, synced_files, last_sync_ts, ignore_patterns, verbose=req.verbose,
            skip_folders=load_skip_folders(),
        )

        stats = {"downloaded": 0, "uploaded": 0, "deleted_remote": 0, "deleted_local": 0,
                 "skipped": 0, "errors": 0, "failed_files": []}
        execute_downloads(download_tasks, stats, verbose=req.verbose)

        sync_time = datetime.now(tz=timezone.utc).timestamp()
        state["synced_files"] = sorted(new_synced | synced_files)
        state["last_sync"] = sync_time
        save_state(state)

        return {
            "status": "ok" if stats["errors"] == 0 else "error",
            "stats": {k: v for k, v in stats.items() if k != "failed_files"},
            "errors": stats["failed_files"],
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/sync/upload")
def sync_upload(req: SyncRequest = SyncRequest()):
    api = _require_api()
    try:
        stats = run_sync(api, full=req.full, upload_only=True, verbose=req.verbose)
        return {
            "status": "ok" if stats["errors"] == 0 else "error",
            "stats": {k: v for k, v in stats.items() if k != "failed_files"},
            "errors": stats["failed_files"],
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/sync/delete")
def sync_delete(req: SyncRequest = SyncRequest()):
    from sync_engine import (
        load_ignore_patterns, load_state, save_state,
        collect_tasks, execute_deletes, SYNC_DIR, load_skip_folders,
    )
    from datetime import datetime, timezone

    api = _require_api()
    try:
        ignore_patterns = load_ignore_patterns()
        state = load_state()
        synced_files = set(state["synced_files"])
        last_sync = state.get("last_sync")
        last_sync_ts = None if req.full or last_sync is None else last_sync

        _, _, delete_tasks, _, new_synced = collect_tasks(
            api.drive.root, SYNC_DIR, synced_files, last_sync_ts, ignore_patterns, verbose=req.verbose,
            skip_folders=load_skip_folders(),
        )

        stats = {"downloaded": 0, "uploaded": 0, "deleted_remote": 0, "deleted_local": 0,
                 "skipped": 0, "errors": 0, "failed_files": []}
        execute_deletes(delete_tasks, stats, new_synced, verbose=req.verbose)

        state["synced_files"] = sorted(new_synced)
        save_state(state)

        return {
            "status": "ok" if stats["errors"] == 0 else "error",
            "stats": {k: v for k, v in stats.items() if k != "failed_files"},
            "errors": stats["failed_files"],
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/cleanup")
def cleanup(req: SyncRequest = SyncRequest()):
    api = _require_api()
    try:
        result = run_cleanup(api, verbose=req.verbose)
        if "error" in result:
            raise HTTPException(status_code=400, detail=result["error"])
        return {"status": "ok" if result["errors"] == 0 else "error", **result}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="127.0.0.1", port=8765, reload=False)
