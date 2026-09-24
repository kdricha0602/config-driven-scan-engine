"""Tiny TTL file cache for provider responses (stdlib only).

Bars are cached ~20h (refreshed by the daily scan); screener snapshots ~6h.
The cache lives under ~/.cache (NOT /tmp: /tmp is a 512M tmpfs and a full
1200-name scan run's SEC fact cache fills it, degrading every provider to
refetch). A miss just refetches. Override with SCAN_ENGINE_CACHE_DIR.
"""

from __future__ import annotations

import hashlib
import json
import os
import time

_CACHE_DIR = os.environ.get(
    "SCAN_ENGINE_CACHE_DIR",
    os.path.join(os.path.expanduser("~"), ".cache", "scan-engine"),
)


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
    path = _path(key)
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"ts": time.time(), "data": data}, fh)
    except OSError:
        # Caching is best-effort; remove the partial file (e.g. ENOSPC
        # leaves a 0-byte husk) so a later get() retries cleanly instead
        # of tripping over a corrupt entry.
        try:
            os.remove(path)
        except OSError:
            pass
