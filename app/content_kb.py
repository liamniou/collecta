"""Content-type knowledge base: what kinds of links exist and how to parse each.

Loaded from content_types.yaml so parsing rules can be extended without code changes.
"""
import os

import yaml

_KB_PATH = os.path.join(os.path.dirname(__file__), "content_types.yaml")

with open(_KB_PATH, encoding="utf-8") as _fh:
    _KB = yaml.safe_load(_fh) or {}

TYPES = _KB.get("types", [])
_BY_ID = {t["id"]: t for t in TYPES}

# Implicit option: let the ingester decide (tries media, then text).
AUTO = {
    "id": "auto",
    "label": "Auto-detect",
    "description": "Let Collecta pick the best parser for this link.",
    "strategy": "auto",
}


def list_types() -> list[dict]:
    """UI-facing list, auto first."""
    out = [AUTO]
    out += [
        {"id": t["id"], "label": t["label"], "description": t.get("description", ""),
         "strategy": t["strategy"]}
        for t in TYPES
    ]
    return out


def strategy_for(type_id: str) -> str:
    t = _BY_ID.get(type_id)
    return t["strategy"] if t else "auto"


def detect(url: str) -> str:
    """Best-matching type id for a URL, or 'auto' if nothing matches."""
    u = (url or "").lower()
    for t in TYPES:
        match = t.get("match") or {}
        for pat in (match.get("domains") or []) + (match.get("keywords") or []):
            if pat and pat.lower() in u:
                return t["id"]
    return "auto"
