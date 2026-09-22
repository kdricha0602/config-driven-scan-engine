"""Free stock screener via Nasdaq's public API. No key, no signup.

Covers Nasdaq-listed stocks (not NYSE). Fields per row: symbol, name, price
(lastsale), netchange, pctchange, volume, marketCap, sector, industry, country.

Coverage note: this is the best no-key screener available. NYSE-listed names
need a keyed provider (e.g. Finnhub free tier) — the screen() signature is
provider-agnostic so one can be added later without touching the workflow.
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request

from .cache import get as cache_get, put as cache_put
from security import urlopen_guarded

_API = "https://api.nasdaq.com/api/screener/stocks"
_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/120.0 Safari/537.36"),
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
}
_SNAPSHOT_TTL = 6 * 3600
_PAGE_LIMIT = 100


def _fetch_page(offset: int) -> dict:
    params = urllib.parse.urlencode({
        "tableonly": "true",
        "limit": _PAGE_LIMIT,
        "offset": offset,
        "download": "true",
    })
    last: Exception | None = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(f"{_API}?{params}", headers=_HEADERS)
            with urlopen_guarded(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            last = exc
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"Nasdaq screener request failed: {last}")


def _to_float(text: object) -> float | None:
    if text in (None, "", "--", "N/A"):
        return None
    try:
        return float(str(text).replace("$", "").replace(",", "").replace("%", ""))
    except ValueError:
        return None


def _normalize(row: dict) -> dict:
    return {
        "symbol": str(row.get("symbol", "")).strip().upper(),
        "name": str(row.get("name", "")).strip(),
        "price": _to_float(row.get("lastsale")),
        "net_change": _to_float(row.get("netchange")),
        "pct_change": _to_float(row.get("pctchange")),
        "volume": _to_float(row.get("volume")),
        "market_cap": _to_float(row.get("marketCap")),
        "sector": str(row.get("sector", "")).strip(),
        "industry": str(row.get("industry", "")).strip(),
        "country": str(row.get("country", "")).strip(),
    }


def snapshot() -> list[dict]:
    """Full Nasdaq-listed snapshot (cached 6h)."""
    cached = cache_get("nasdaq:snapshot", _SNAPSHOT_TTL)
    if cached is not None:
        return cached  # type: ignore[return-value]
    rows: list[dict] = []
    offset = 0
    total: int | None = None
    while True:
        payload = _fetch_page(offset)
        data = payload.get("data", {}) or {}
        if total is None:
            total = int(data.get("totalrecords") or 0)
        table = data.get("table", {}) or {}
        batch = data.get("rows") or table.get("rows") or []
        if not batch:
            break
        rows.extend(_normalize(r) for r in batch)
        offset += len(batch)
        if total and offset >= total:
            break
        time.sleep(0.4)  # polite paging
        if offset > 20000:  # safety valve
            break
    rows = [r for r in rows if r["symbol"]]
    # download=true returns the full dataset on every page; dedupe by symbol.
    seen: dict[str, dict] = {}
    for r in rows:
        seen.setdefault(r["symbol"], r)
    rows = list(seen.values())
    cache_put("nasdaq:snapshot", rows)
    return rows


def screen(min_price: float | None = None,
           max_price: float | None = None,
           min_volume: float | None = None,
           min_market_cap: float | None = None,
           max_market_cap: float | None = None,
           min_pct_change: float | None = None) -> list[dict]:
    """Filter the Nasdaq snapshot on price/volume/market-cap/momentum."""
    out = []
    for r in snapshot():
        p, v, mc = r["price"], r["volume"], r["market_cap"]
        if p is None or v is None:
            continue
        if min_price is not None and p < min_price:
            continue
        if max_price is not None and p > max_price:
            continue
        if min_volume is not None and v < min_volume:
            continue
        if min_market_cap is not None and (mc is None or mc < min_market_cap):
            continue
        if max_market_cap is not None and (mc is None or mc > max_market_cap):
            continue
        if min_pct_change is not None:
            pc = r["pct_change"]
            if pc is None or pc < min_pct_change:
                continue
        out.append(r)
    out.sort(key=lambda r: (r["volume"] or 0), reverse=True)
    return out
