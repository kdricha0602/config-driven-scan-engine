"""Consensus (consensus.app) provider: research-paper evidence search.

Powers the literature-evidence layer of the engine: for scan finalists, the
published literature backing a catalyst claim (catalyst_quality — e.g. for
a Disruptive Innovator, published evidence the technology works/scales;
biotech, clinical evidence) and counter-evidence phrased against the working
thesis (thesis_contradiction).

Auth: the connected `custom.consensus` credential via the authd surrogate
exchange (same pattern as providers/bigdata.py). The connector must be
stored with `{"custom_header": "x-api-key"}` placement so the surrogate is
injected as the API's x-api-key header. The raw key is never seen, logged,
or persisted.

All public functions degrade to {"error": ...} instead of raising, so a
Consensus outage turns literature evidence into "no data" rather than
killing the scan.
"""

from __future__ import annotations

import re
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, "/opt/hatch/skills/skill-creator/bin")
from dynamic_credentials import (  # noqa: E402
    add_surrogate_to_request, read_json_response)

from .cache import get as cache_get, put as cache_put

_BASE = "https://api.consensus.app"
_HOSTS = ["api.consensus.app"]
_CRED = "custom.consensus"
_TTL = 24 * 3600  # literature changes slowly; 24h like the SEC fact cache
_TOP_N = 3


def _normalize(query: str) -> str:
    return re.sub(r"\s+", " ", query or "").strip().lower()


def _key(query: str) -> str:
    return f"consensus:{_normalize(query)}"


def search(query: str, include_full_text_chunks: bool = False,
           timeout: int = 30) -> dict:
    """Search the literature for `query`.

    Full-text chunks default OFF: the vendor 403s include_full_text_chunks=true
    on standard plans (verified live 2026-09-23). Titles, takeaways, abstracts
    and citation metadata still come back and are plenty for evidence.

    Never raises: failures return {"query", "error"} so callers fail closed.
    Successful responses are disk-cached 24h keyed by normalized query.
    """
    norm = _normalize(query)
    if not norm:
        return {"query": query, "error": "empty query"}
    cached = cache_get(_key(norm), _TTL)
    if cached is not None:
        return cached
    try:
        params = urllib.parse.urlencode(
            {"query": norm,
             "include_full_text_chunks":
             "true" if include_full_text_chunks else "false"})
        req = urllib.request.Request(
            f"{_BASE}/v1/search?{params}",
            headers={"Accept": "application/json",
                     "User-Agent": "quantkernal-scan-engine/1.0"})
        add_surrogate_to_request(req, _CRED, allowed_hosts=_HOSTS)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = read_json_response(resp)
        if not isinstance(payload, dict) or "results" not in payload:
            raise RuntimeError("unexpected response shape")
    except Exception as exc:  # noqa: BLE001 — graceful degradation
        return {"query": norm, "error": f"{type(exc).__name__}: {exc}"[:200]}
    out = {"query": norm, "results": payload.get("results") or []}
    cache_put(_key(norm), out)
    return out


def parse_papers(result: dict, top_n: int = _TOP_N) -> list[dict]:
    """Top `top_n` papers -> evidence items (title, journal, year, DOI, URL,
    takeaway, citation count, SJR quartile). Papers without a title are
    skipped; an {"error": ...} result yields []."""
    items = []
    for p in (result or {}).get("results") or []:
        title = (p.get("title") or "").strip()
        if not title:
            continue
        items.append({
            "title": title,
            "journal": p.get("journal_name") or "",
            "year": p.get("publish_year"),
            "doi": p.get("doi") or "",
            "url": p.get("url") or "",
            "takeaway": (p.get("takeaway") or "").strip(),
            "citation_count": p.get("citation_count"),
            "quartile": p.get("sjr_best_quartile"),
        })
        if len(items) >= top_n:
            break
    return items
