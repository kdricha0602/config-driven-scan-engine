"""Free daily OHLCV bars + quotes from Yahoo Finance chart API. No key, no signup.

Endpoint: https://query1.finance.yahoo.com/v8/finance/chart/{SYM}?interval=1d&range=2y

Notes:
- Covers NYSE/Nasdaq/AMEX (and ADRs) in one call per ticker.
- Prefers the dividend/split-adjusted close series when present, so price
  history has no artificial dividend gaps; falls back to raw close.
- Yahoo tolerates modest request rates with a browser User-Agent; 429s are
  retried with backoff. Bulk scans should still pace themselves.
"""

from __future__ import annotations

import datetime
import json
import time
import urllib.parse
import urllib.request

from .cache import get as cache_get, put as cache_put
from security import clean_ticker, urlopen_guarded

_UA = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/120.0 Safari/537.36")}
_BARS_TTL = 20 * 3600
_QUOTE_TTL = 15 * 60
_RANGE = "2y"  # ~500 trading days: enough for 200DMA + MCDX warmup


def _fetch(url: str, timeout: int = 25, retries: int = 4) -> bytes:
    last: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=_UA)
            with urlopen_guarded(req, timeout=timeout) as resp:
                if resp.status == 429:
                    raise RuntimeError("rate limited (429)")
                return resp.read()
        except Exception as exc:
            last = exc
            time.sleep(2 * (attempt + 1))
    host = urllib.parse.urlparse(url).hostname or "yahoo"
    raise RuntimeError(f"Yahoo request failed after {retries} tries: {host}: {last}")


def _parse(result: dict, ticker: str) -> dict:
    stamps = result.get("timestamp") or []
    quote = (result.get("indicators", {}).get("quote") or [{}])[0]
    adj = (result.get("indicators", {}).get("adjclose") or [{}])[0].get("adjclose")
    closes_raw = quote.get("close") or []
    use_adj = adj and len(adj) == len(stamps)

    dates, opens, highs, lows, closes, volumes = [], [], [], [], [], []
    for i, ts in enumerate(stamps):
        c = None
        if use_adj and adj[i] is not None:
            # rescale OHLC by the same adjustment factor as the close
            raw_c = closes_raw[i] if i < len(closes_raw) else None
            c = adj[i]
            factor = (c / raw_c) if raw_c else 1.0
        else:
            c = closes_raw[i] if i < len(closes_raw) else None
            factor = 1.0
        o = quote.get("open", [None])[i] if i < len(quote.get("open", [])) else None
        h = quote.get("high", [None])[i] if i < len(quote.get("high", [])) else None
        l = quote.get("low", [None])[i] if i < len(quote.get("low", [])) else None
        v = quote.get("volume", [None])[i] if i < len(quote.get("volume", [])) else None
        if c is None or c <= 0:
            continue
        dates.append(datetime.datetime.fromtimestamp(
            ts, tz=datetime.timezone.utc).strftime("%Y-%m-%d"))
        closes.append(float(c))
        opens.append(float(o) * factor if o else float(c))
        highs.append(float(h) * factor if h else float(c))
        lows.append(float(l) * factor if l else float(c))
        volumes.append(int(v) if v else 0)
    if not closes:
        raise RuntimeError(f"Yahoo returned no usable bars for {ticker}")
    return {"ticker": ticker.upper(), "dates": dates, "opens": opens,
            "highs": highs, "lows": lows, "closes": closes, "volumes": volumes}


def daily_bars(ticker: str) -> dict:
    """~2y of daily bars for one ticker (adjusted closes preferred)."""
    ticker = clean_ticker(ticker)
    key = f"yahoo:daily:{ticker}"
    cached = cache_get(key, _BARS_TTL)
    if cached is not None:
        return cached  # type: ignore[return-value]
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
           f"?interval=1d&range={_RANGE}")
    payload = json.loads(_fetch(url).decode("utf-8"))
    results = (payload.get("chart", {}) or {}).get("result") or []
    if not results:
        err = (payload.get("chart", {}) or {}).get("error")
        raise RuntimeError(f"Yahoo has no data for {ticker}: {err}")
    data = _parse(results[0], ticker)
    cache_put(key, data)
    return data


def quote(ticker: str) -> dict:
    """Latest quote from the tail of the daily series (1d range)."""
    ticker = clean_ticker(ticker)
    key = f"yahoo:quote:{ticker}"
    cached = cache_get(key, _QUOTE_TTL)
    if cached is not None:
        return cached  # type: ignore[return-value]
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
           "?interval=1d&range=5d")
    payload = json.loads(_fetch(url).decode("utf-8"))
    results = (payload.get("chart", {}) or {}).get("result") or []
    if not results:
        raise RuntimeError(f"Yahoo has no quote for {ticker}")
    bars = _parse(results[0], ticker)
    i = -1
    data = {"ticker": ticker,
            "price": bars["closes"][i],
            "open": bars["opens"][i],
            "high": bars["highs"][i],
            "low": bars["lows"][i],
            "volume": bars["volumes"][i],
            "date": bars["dates"][i]}
    cache_put(key, data)
    return data
