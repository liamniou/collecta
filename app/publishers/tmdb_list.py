"""TMDB list integration (v3): create/update a list that both Radarr and Kometa read.

Uses the existing v3 API key + a one-time user session (stored in KeyValue), so no
separate v4 token is needed. Radarr consumes it via a "TMDb List" import; Kometa via
the `tmdb_list` builder.
"""
import requests

from .. import config
from ..models import get_session, kv_get, kv_set

_BASE = "https://api.themoviedb.org/3"
_SESSION_KEY = "tmdb_session"


def configured() -> bool:
    return bool(config.TMDB_API_KEY)


def is_authed() -> bool:
    with get_session() as session:
        return bool(kv_get(session, _SESSION_KEY))


def _params(session_id: str | None = None) -> dict:
    p = {"api_key": config.TMDB_API_KEY}
    if session_id:
        p["session_id"] = session_id
    return p


def start_auth() -> dict:
    resp = requests.get(f"{_BASE}/authentication/token/new", params=_params(), timeout=20)
    resp.raise_for_status()
    token = resp.json()["request_token"]
    return {"request_token": token, "approve_url": f"https://www.themoviedb.org/authenticate/{token}"}


def finish_auth(request_token: str) -> bool:
    resp = requests.post(
        f"{_BASE}/authentication/session/new",
        params=_params(),
        json={"request_token": request_token},
        timeout=20,
    )
    if resp.status_code == 200 and resp.json().get("success"):
        with get_session() as session:
            kv_set(session, _SESSION_KEY, resp.json()["session_id"])
        return True
    return False


def _session_id() -> str | None:
    with get_session() as session:
        return kv_get(session, _SESSION_KEY)


def create_or_update_list(name: str, movies: list[dict], existing_list_id: str | None = None) -> dict:
    """Create a new TMDB list (or re-sync an existing one) with the given movies."""
    sid = _session_id()
    if not sid:
        raise RuntimeError("TMDB is not connected. Connect your TMDB account first.")

    list_id = existing_list_id
    if list_id:
        # Re-sync: clear then re-add so the list mirrors the current approvals.
        requests.post(
            f"{_BASE}/list/{list_id}/clear",
            params={**_params(sid), "confirm": "true"},
            timeout=20,
        )
    else:
        resp = requests.post(
            f"{_BASE}/list",
            params=_params(sid),
            json={"name": name[:250], "description": "Created by Collecta", "language": "en"},
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("success"):
            raise RuntimeError(f"TMDB list creation failed: {data}")
        list_id = str(data["list_id"])

    added = 0
    for m in movies:
        if not m.get("tmdb_id"):
            continue
        r = requests.post(
            f"{_BASE}/list/{list_id}/add_item",
            params=_params(sid),
            json={"media_id": m["tmdb_id"]},
            timeout=20,
        )
        if r.status_code in (200, 201) and r.json().get("success"):
            added += 1

    return {"id": str(list_id), "url": f"https://www.themoviedb.org/list/{list_id}", "added": added}
