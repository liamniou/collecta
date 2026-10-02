"""Radarr integration: subscribe Radarr to the shared TMDB list via an import list."""
import requests

from .. import config


def _headers() -> dict:
    return {"X-Api-Key": config.RADARR_API_KEY, "Content-Type": "application/json"}


def _get(path: str):
    resp = requests.get(f"{config.RADARR_URL}/api/v3/{path}", headers=_headers(), timeout=30)
    resp.raise_for_status()
    return resp.json()


def _command(name: str, **extra) -> None:
    requests.post(
        f"{config.RADARR_URL}/api/v3/command",
        json={"name": name, **extra}, headers=_headers(), timeout=30,
    )


def _quality_profile_id() -> int:
    profiles = _get("qualityprofile")
    for p in profiles:
        if p.get("name", "").lower() == config.RADARR_QUALITY_PROFILE.lower():
            return p["id"]
    if profiles:
        return profiles[0]["id"]
    raise RuntimeError("No Radarr quality profiles found.")


def library_status(tmdb_ids: list[int]) -> dict[int, str]:
    """Map each TMDB id to its Radarr state:
    downloaded | import_pending | monitored | unmonitored | not_in_radarr."""
    if not config.RADARR_API_KEY:
        raise RuntimeError("RADARR_API_KEY is not configured.")
    lib = {m.get("tmdbId"): m for m in _get("movie")}
    queue_state: dict[int, str] = {}
    try:
        for q in _get("queue?pageSize=500").get("records", []):
            mid = q.get("movieId")
            if mid:
                queue_state[mid] = q.get("trackedDownloadState") or ""
    except Exception:  # noqa: BLE001
        pass

    out: dict[int, str] = {}
    for tid in tmdb_ids:
        m = lib.get(tid)
        if not m:
            out[tid] = "not_in_radarr"
        elif m.get("hasFile"):
            out[tid] = "downloaded"
        elif queue_state.get(m["id"]) in ("importPending", "importBlocked", "importing"):
            out[tid] = "import_pending"
        elif m.get("monitored"):
            out[tid] = "monitored"
        else:
            out[tid] = "unmonitored"
    return out


def _acceptable(rejections: list, force: bool) -> bool:
    """No rejections -> ok. In force mode, tolerate only filename-parse/quality rejections
    (a mapped file with an unreadable name), but never samples/existing/etc."""
    if not rejections:
        return True
    if not force:
        return False
    for r in rejections:
        reason = (r.get("reason") or "").lower()
        if "parse" not in reason and "quality" not in reason:
            return False
    return True


def _import_download(download_id: str, fallback_quality=None, force: bool = False) -> int:
    """Manual-import the ready files for a download id. Returns count imported.

    `fallback_quality` (from the grabbed release) is used when Radarr can't parse the file's
    quality; `force` tolerates parse/quality rejections for files Radarr already mapped.
    """
    try:
        files = _get(f"manualimport?downloadId={download_id}&filterExistingFiles=false")
    except Exception:  # noqa: BLE001
        return 0
    if not isinstance(files, list):
        return 0
    payload = []
    for f in files:
        movie = f.get("movie")
        quality = f.get("quality") or fallback_quality
        if not movie or not quality:
            continue
        if not _acceptable(f.get("rejections") or [], force):
            continue
        payload.append({
            "path": f["path"],
            "movieId": movie["id"],
            "quality": quality,
            "languages": f.get("languages") or [],
            "releaseGroup": f.get("releaseGroup") or "",
            "indexerFlags": f.get("indexerFlags") or 0,
            "downloadId": download_id,
        })
    if payload:
        _command("ManualImport", files=payload, importMode="auto")
    return len(payload)


def import_downloads() -> str:
    """Nudge Radarr to import completed downloads: process the queue + manual-import ready files."""
    if not config.RADARR_API_KEY:
        raise RuntimeError("RADARR_API_KEY is not configured.")
    _command("ProcessMonitoredDownloads")

    records = _get("queue?pageSize=500").get("records", [])
    seen_dl: set[str] = set()
    imported = 0
    for q in records:
        if q.get("trackedDownloadState") != "importPending":
            continue
        dl = q.get("downloadId")
        if not dl or dl in seen_dl:
            continue
        seen_dl.add(dl)
        imported += _import_download(dl)

    pending = sum(1 for q in records if q.get("trackedDownloadState") == "importPending")
    blocked = sum(1 for q in records if q.get("trackedDownloadState") == "importBlocked")
    return (
        f"Triggered Radarr import: sent {imported} ready file(s); "
        f"{pending} pending, {blocked} blocked (need manual review in Radarr)."
    )


def rescue_searching(tmdb_ids: list[int]) -> str:
    """Import files for 'searching' movies whose grabbed download left the queue (orphaned).

    For each still-searching movie, find its most recent grabbed download from history and
    manual-import its files — the fix for downloads that finished but never got imported.
    """
    if not config.RADARR_API_KEY:
        raise RuntimeError("RADARR_API_KEY is not configured.")
    lib = {m.get("tmdbId"): m for m in _get("movie")}

    download_ids: dict[str, dict] = {}  # downloadId -> {"title", "quality"}
    checked = 0
    for tid in tmdb_ids:
        m = lib.get(tid)
        if not m or m.get("hasFile") or not m.get("monitored"):
            continue
        checked += 1
        try:
            history = _get(f"history/movie?movieId={m['id']}")
        except Exception:  # noqa: BLE001
            continue
        grabbed = [h for h in history if h.get("eventType") == "grabbed" and h.get("downloadId")]
        if not grabbed:
            continue
        grabbed.sort(key=lambda h: h.get("date", ""), reverse=True)
        g = grabbed[0]
        download_ids[g["downloadId"]] = {"title": m.get("title") or str(tid), "quality": g.get("quality")}

    imported = sum(
        _import_download(dl, fallback_quality=info["quality"], force=True)
        for dl, info in download_ids.items()
    )
    return (
        f"Rescue import: checked {checked} searching item(s), found {len(download_ids)} "
        f"grabbed download(s), imported {imported} file(s)."
    )


def add_tmdb_list(list_id: str, list_name: str) -> str:
    """Create (or update) a Radarr 'TMDb List' import list pointing at the TMDB list."""
    if not config.RADARR_API_KEY:
        raise RuntimeError("RADARR_API_KEY is not configured.")

    profile_id = _quality_profile_id()
    name = f"Collecta: {list_name}"[:60]
    payload = {
        "enabled": True,
        "enableAuto": True,
        "monitor": "movieOnly",
        "minimumAvailability": "released",
        "rootFolderPath": config.RADARR_ROOT_FOLDER,
        "qualityProfileId": profile_id,
        "searchOnAdd": True,
        "name": name,
        "implementation": "TMDbListImport",
        "configContract": "TMDbListSettings",
        "listType": "tmdb",
        "fields": [{"name": "listId", "value": str(list_id)}],
    }

    existing = next((l for l in _get("importlist") if l.get("name") == name), None)
    if existing:
        payload["id"] = existing["id"]
        resp = requests.put(
            f"{config.RADARR_URL}/api/v3/importlist/{existing['id']}",
            json=payload, headers=_headers(), timeout=30,
        )
    else:
        resp = requests.post(
            f"{config.RADARR_URL}/api/v3/importlist",
            json=payload, headers=_headers(), timeout=30,
        )
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"Radarr import list failed: {resp.status_code} {resp.text[:200]}")

    # Pull the list now so Radarr adds/searches the movies immediately.
    try:
        requests.post(
            f"{config.RADARR_URL}/api/v3/command",
            json={"name": "ImportListSync"}, headers=_headers(), timeout=30,
        )
    except Exception:  # noqa: BLE001
        pass

    action = "updated" if existing else "added"
    return f"Radarr import list '{name}' {action} (TMDb list {list_id}); triggered sync."

