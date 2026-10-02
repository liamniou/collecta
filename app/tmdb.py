"""Stage 3: resolve candidate titles into TMDB movies (poster + id + year)."""
import requests

from . import config

_BASE = "https://api.themoviedb.org/3"


def search(query: str, year: int | None = None, limit: int = 4, include_adult: bool = False) -> list[dict]:
    if not config.TMDB_API_KEY:
        return []
    params = {
        "api_key": config.TMDB_API_KEY,
        "query": query,
        "include_adult": "true" if include_adult else "false",
    }
    if year:
        params["year"] = year
    try:
        resp = requests.get(f"{_BASE}/search/movie", params=params, timeout=20)
        resp.raise_for_status()
        results = resp.json().get("results", [])
    except Exception as exc:  # noqa: BLE001
        print(f"[tmdb] search failed for {query!r}: {exc}")
        return []

    out = []
    for r in results[:limit]:
        release = (r.get("release_date") or "")[:4]
        out.append({
            "tmdb_id": r.get("id"),
            "title": r.get("title") or r.get("original_title"),
            "year": int(release) if release.isdigit() else None,
            "poster_path": r.get("poster_path") or "",
            "overview": r.get("overview") or "",
        })
    return out


def match(candidate: dict) -> dict | None:
    """Return {best: {...}, alternates: [...]}. None if no TMDB hit."""
    results = search(candidate["title"], candidate.get("year"))
    if not results and candidate.get("year"):
        results = search(candidate["title"])  # retry without year
    if not results:
        return None
    return {"best": results[0], "alternates": results[1:]}


def poster_url(poster_path: str, size: str = "w342") -> str:
    if not poster_path:
        return ""
    return f"{config.TMDB_IMAGE_BASE}/{size}{poster_path}"
