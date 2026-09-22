"""Tiny TTL file cache for provider responses (stdlib only).

Bars are cached ~20h (refreshed by the daily scan); screener snapshots ~6h.
Cache lives in /tmp so it never pollutes the workspace; a miss just refetches.
"""

from __future__ import annotations

import hashlib
import json
import os
import time

_CACHE_DIR = os.path.join("/tmp", "scan-engine-cache")


def _path(key: str) -> str:
    digest = hashlib.sha256(key.encode()).hexdigest()[:32]
    return os.path.join(_CACHE_DIR, digest + ".json")


def get(key: str, max_age_s: int) -> object | None:
    try:
        with open(_path(key), "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError):
        return None
    if time.time() - payload.get("ts", 0) > max_age_s:
        return None
    return payload.get("data")


def put(key: str, data: object) -> None:
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        with open(_path(key), "w", encoding="utf-8") as fh:
            json.dump({"ts": time.time(), "data": data}, fh)
    except OSError:
        pass  # caching is best-effort; the fetch already succeeded
