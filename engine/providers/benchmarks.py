"""Benchmarks: index bars + relative strength vs market and sector.

Free via the Yahoo provider (SPY/QQQ/IWM). Sector context comes from the
Nasdaq screener snapshot (sector/industry per listing); peer RS for a scan
uses the screener's pct_change as a cheap same-day proxy, while finalists
get a real 63-day RS vs SPY from bars.
"""

from __future__ import annotations

from . import yahoo
from .cache import get as cache_get, put as cache_put

_BENCH_TTL = 20 * 3600


def index_bars(symbol: str = "SPY") -> dict:
    key = f"bench:daily:{symbol.upper()}"
    cached = cache_get(key, _BENCH_TTL)
    if cached is not None:
        return cached  # type: ignore[return-value]
    data = yahoo.daily_bars(symbol)
    cache_put(key, data)
    return data


def trailing_return(closes: list[float], days: int) -> float | None:
    if len(closes) < days + 1:
        return None
    base = closes[-(days + 1)]
    if not base:
        return None
    return closes[-1] / base - 1


def relative_strength(ticker_closes: list[float], bench_closes: list[float],
                      days: int = 63) -> dict:
    """3-month (63 trading days) return vs the benchmark, excess return."""
    t = trailing_return(ticker_closes, days)
    # align on the tail: benchmarks have longer histories
    b = trailing_return(bench_closes[-len(ticker_closes):], days)
    if t is None or b is None:
        return {"rs_pct": None, "bench_pct": None, "excess_pct": None,
                "days": days}
    return {"rs_pct": t, "bench_pct": b, "excess_pct": t - b, "days": days}


def market_rs(ticker: str, bars: dict, days: int = 63) -> dict:
    """Ticker's trailing return vs SPY / QQQ / IWM."""
    out = {"ticker": ticker.upper(), "days": days, "vs": {}}
    for bench in ("SPY", "QQQ", "IWM"):
        try:
            bb = index_bars(bench)
            out["vs"][bench] = relative_strength(bars["closes"],
                                                 bb["closes"], days)
        except Exception as exc:
            out["vs"][bench] = {"error": str(exc)}
    return out


def sector_snapshot(rows: list[dict], sector: str) -> dict:
    """Same-day median move for a sector, from the screener snapshot.

    Cheap breadth proxy for the scan funnel; not a substitute for real
    trailing RS on finalists.
    """
    moves = [r["pct_change"] for r in rows
             if r.get("sector") == sector
             and isinstance(r.get("pct_change"), (int, float))]
    if not moves:
        return {"sector": sector, "names": 0}
    moves.sort()
    mid = len(moves) // 2
    median = (moves[mid] if len(moves) % 2
              else (moves[mid - 1] + moves[mid]) / 2)
    return {"sector": sector, "names": len(moves),
            "median_day_pct": median,
            "advancers": sum(1 for m in moves if m > 0),
            "decliners": sum(1 for m in moves if m < 0)}
