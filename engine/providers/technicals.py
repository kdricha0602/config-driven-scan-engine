"""Classic technical indicators. Dependency-free, deterministic.

SMA / EMA / RSI(14) / MACD(12,26,9) / ATR(14) / RVOL(20) / daily change /
position vs moving averages / 63-day high-low. Complements the MCDX
money-flow module; the LLM never calculates.
"""

from __future__ import annotations


def sma(vals: list[float], n: int) -> float | None:
    if len(vals) < n or n <= 0:
        return None
    return sum(vals[-n:]) / n


def ema_series(vals: list[float], n: int) -> list[float]:
    if len(vals) < n:
        return []
    k = 2 / (n + 1)
    out = [sum(vals[:n]) / n]
    for v in vals[n:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def rsi(closes: list[float], n: int = 14) -> float | None:
    if len(closes) < n + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    ag = sum(gains[:n]) / n
    al = sum(losses[:n]) / n
    for g, l in zip(gains[n:], losses[n:]):
        ag = (ag * (n - 1) + g) / n
        al = (al * (n - 1) + l) / n
    if al == 0:
        return 100.0 if ag > 0 else 50.0
    return 100 - 100 / (1 + ag / al)


def macd(closes: list[float]) -> dict:
    """MACD line, signal, histogram on the latest bar."""
    if len(closes) < 35:
        return {"line": None, "signal": None, "hist": None}
    e12, e26 = ema_series(closes, 12), ema_series(closes, 26)
    m = len(e26)
    line = [a - b for a, b in zip(e12[-m:], e26)]
    sig = ema_series(line, 9)
    if not sig:
        return {"line": line[-1], "signal": None, "hist": None}
    return {"line": line[-1], "signal": sig[-1], "hist": line[-1] - sig[-1]}


def atr(highs: list[float], lows: list[float], closes: list[float],
        n: int = 14) -> float | None:
    if len(closes) < n + 1:
        return None
    trs = []
    for i in range(1, len(closes)):
        trs.append(max(highs[i] - lows[i],
                       abs(highs[i] - closes[i - 1]),
                       abs(lows[i] - closes[i - 1])))
    return sum(trs[-n:]) / n


def summarize(bars: dict) -> dict:
    """Latest-bar classic technicals for one ticker's daily bars."""
    closes, highs = bars["closes"], bars["highs"]
    lows, vols = bars["lows"], bars["volumes"]
    price = closes[-1]
    avg20 = (sum(vols[-20:]) / 20) if len(vols) >= 20 else None
    s20, s50, s200 = sma(closes, 20), sma(closes, 50), sma(closes, 200)
    hi63 = max(highs[-63:]) if len(highs) >= 63 else max(highs)
    lo63 = min(lows[-63:]) if len(lows) >= 63 else min(lows)
    m = macd(closes)
    a = atr(highs, lows, closes)
    return {
        "price": price,
        "day_change_pct": (closes[-1] / closes[-2] - 1) if len(closes) >= 2 else None,
        "volume": vols[-1] if vols else None,
        "avg_volume_20": avg20,
        "rvol": (vols[-1] / avg20) if vols and avg20 else None,
        "rsi14": rsi(closes),
        "macd_line": m["line"], "macd_signal": m["signal"], "macd_hist": m["hist"],
        "sma20": s20, "sma50": s50, "sma200": s200,
        "atr14": a,
        "atr_pct": (a / price) if a else None,
        "above_sma20": (price > s20) if s20 else None,
        "above_sma50": (price > s50) if s50 else None,
        "above_sma200": (price > s200) if s200 else None,
        "sma200_rising": (s200 > sma(closes[:-20], 200)) if s200 and len(closes) >= 220 else None,
        "high_63d": hi63, "low_63d": lo63,
        "off_high_63d_pct": (price / hi63 - 1) if hi63 else None,
        "bars": len(closes),
        "last_date": bars["dates"][-1] if bars.get("dates") else None,
    }
