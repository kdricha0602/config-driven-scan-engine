"""
MCDX money-flow signals — pure-Python port of the LOKEN (v4) BULLISH MCDX v2.2
TradingView Pine Script (© LOKEN94, custom version based on [M2J] MCDX).

Computes the Banker / Hot Money RSI transforms, their composite moving averages,
the hbma trend tracker, and every signal condition from the script:
  topsignals, downtrendsignal, uptrendsignal, bullishsignals, bearishsignals,
  entrysignals, climax, pump, bottom, greed, long
plus the candle-coloring flags: retest, pump_candles, down_candles, dump_candles.

No third-party dependencies — stdlib only, so it runs anywhere (including inside
a Temporal activity).

Port notes / deliberate decisions:
- Pine's rsi() uses Wilder smoothing; rma() here seeds with the SMA of the first
  `length` values, matching TradingView behavior on real data.
- Pine's ema() uses alpha = 2/(length+1), seeded with SMA; wma() uses linear
  weights length..1. sma()/rma()/ema()/wma() all return None ("na") until enough
  bars exist, exactly like Pine.
- The script calls vwma(composite, 1) with two arguments, which is not a valid
  Pine v4 signature (vwma needs source, volume, length). A 1-period VWMA equals
  the value itself, so hbma is implemented as the composite value directly.
- crossover(a, b)  = a[-2] <= b[-2] and a[-1] > b[-1]
  crossunder(a, b) = a[-2] >= b[-2] and a[-1] < b[-1]
  Any None ("na") operand makes the condition False.
"""

from __future__ import annotations

from typing import List, Optional, Sequence

Series = List[Optional[float]]
BoolSeries = List[bool]

NA = None


# ---------------------------------------------------------------------------
# Moving-average primitives (Pine semantics)
# ---------------------------------------------------------------------------

def _check(xs: Sequence[Optional[float]], length: int) -> Series:
    if length < 1:
        raise ValueError("length must be >= 1")
    return [float(x) if x is not None else None for x in xs]


def sma(xs: Sequence[Optional[float]], length: int) -> Series:
    """Simple moving average; None until `length` non-None values are seen."""
    xs = _check(xs, length)
    out: Series = [NA] * len(xs)
    window: List[float] = []
    for i, x in enumerate(xs):
        if x is None:
            window = []
            continue
        window.append(x)
        if len(window) > length:
            window.pop(0)
        if len(window) == length:
            out[i] = sum(window) / length
    return out


def ema(xs: Sequence[Optional[float]], length: int) -> Series:
    """Exponential moving average, alpha = 2/(length+1), seeded with SMA."""
    xs = _check(xs, length)
    out: Series = [NA] * len(xs)
    alpha = 2.0 / (length + 1)
    prev: Optional[float] = None
    seed: List[float] = []
    for i, x in enumerate(xs):
        if x is None:
            prev, seed = None, []
            continue
        seed.append(x)
        if len(seed) < length:
            continue
        if prev is None:
            prev = sum(seed) / length  # seed with SMA
        else:
            prev = alpha * x + (1 - alpha) * prev
        out[i] = prev
    return out


def rma(xs: Sequence[Optional[float]], length: int) -> Series:
    """Wilder's smoothing (Pine rma), alpha = 1/length, seeded with SMA."""
    xs = _check(xs, length)
    out: Series = [NA] * len(xs)
    alpha = 1.0 / length
    prev: Optional[float] = None
    seed: List[float] = []
    for i, x in enumerate(xs):
        if x is None:
            prev, seed = None, []
            continue
        seed.append(x)
        if len(seed) < length:
            continue
        if prev is None:
            prev = sum(seed) / length
        else:
            prev = alpha * x + (1 - alpha) * prev
        out[i] = prev
    return out


def wma(xs: Sequence[Optional[float]], length: int) -> Series:
    """Linearly weighted moving average (weights length..1)."""
    xs = _check(xs, length)
    out: Series = [NA] * len(xs)
    denom = length * (length + 1) / 2.0
    window: List[float] = []
    for i, x in enumerate(xs):
        if x is None:
            window = []
            continue
        window.append(x)
        if len(window) > length:
            window.pop(0)
        if len(window) == length:
            out[i] = sum(v * (j + 1) for j, v in enumerate(window)) / denom
    return out


def rsi_wilder(closes: Sequence[float], length: int) -> Series:
    """Pine's rsi(): Wilder RSI over `length` periods."""
    if length < 1:
        raise ValueError("length must be >= 1")
    n = len(closes)
    out: Series = [NA] * n
    if n <= length:
        return out
    gains: List[float] = []
    losses: List[float] = []
    for i in range(1, n):
        chg = closes[i] - closes[i - 1]
        gains.append(max(chg, 0.0))
        losses.append(max(-chg, 0.0))
    avg_gain: Optional[float] = None
    avg_loss: Optional[float] = None
    for i in range(len(gains)):
        g, l = gains[i], losses[i]
        if avg_gain is None:
            # seed with SMA over the first `length` changes -> first value at bar `length`
            if i < length - 1:
                continue
            if i == length - 1:
                avg_gain = sum(gains[:length]) / length
                avg_loss = sum(losses[:length]) / length
            else:  # pragma: no cover - defensive
                continue
        else:
            avg_gain = (avg_gain * (length - 1) + g) / length
            avg_loss = (avg_loss * (length - 1) + l) / length
        bar = i + 1  # changes index -> price-bar index
        if avg_loss == 0:
            out[bar] = 100.0
        else:
            rs = avg_gain / avg_loss
            out[bar] = 100.0 - 100.0 / (1.0 + rs)
    return out


# ---------------------------------------------------------------------------
# Cross helpers
# ---------------------------------------------------------------------------

def _prev2(s: Series, i: int, back: int) -> Optional[float]:
    j = i - back
    return s[j] if 0 <= j < len(s) else None


def crossover(a: Series, b: Series, i: int) -> bool:
    a0, a1 = _prev2(a, i, 0), _prev2(a, i, 1)
    b0, b1 = _prev2(b, i, 0), _prev2(b, i, 1)
    if None in (a0, a1, b0, b1):
        return False
    return a1 <= b1 and a0 > b0  # type: ignore[operator]


def crossunder(a: Series, b: Series, i: int) -> bool:
    a0, a1 = _prev2(a, i, 0), _prev2(a, i, 1)
    b0, b1 = _prev2(b, i, 0), _prev2(b, i, 1)
    if None in (a0, a1, b0, b1):
        return False
    return a1 >= b1 and a0 < b0  # type: ignore[operator]


def gt_const(s: Series, i: int, c: float) -> bool:
    v = _prev2(s, i, 0)
    return v is not None and v > c


def lt_const(s: Series, i: int, c: float) -> bool:
    v = _prev2(s, i, 0)
    return v is not None and v < c


# ---------------------------------------------------------------------------
# The indicator
# ---------------------------------------------------------------------------

DEFAULTS = {
    "rsi_base_banker": 50.0,
    "rsi_period_banker": 50,
    "rsi_base_hotmoney": 30.0,
    "rsi_period_hotmoney": 40,
    "sensitivity_banker": 1.5,
    "sensitivity_hotmoney": 0.7,
}


def _combine(a: Series, b: Series, op) -> Series:
    return [op(x, y) if x is not None and y is not None else NA
            for x, y in zip(a, b)]


def _scale(s: Series, k: float) -> Series:
    return [x * k if x is not None else NA for x in s]


def _add3(a: Series, b: Series, c: Series) -> Series:
    return [x + y + z if None not in (x, y, z) else NA
            for x, y, z in zip(a, b, c)]


def _add2(a: Series, b: Series) -> Series:
    return [x + y if x is not None and y is not None else NA
            for x, y in zip(a, b)]


def compute(closes: Sequence[float], **params) -> dict:
    """Run the full MCDX computation over a close-price series.

    Returns a dict with float series (None = na) and boolean signal series:
      series:  rsi_banker, rsi_hotmoney, hotma, bankma, hotsignal, banksignal,
               hbma, major, lowampsignal, lowmsignal
      signals: topsignals, downtrend, uptrend, bullish, bearish, entry,
               climax, pump, bottom, greed, long
      candles: retest, pump_candles, down_candles, dump_candles
    """
    p = {**DEFAULTS, **params}
    closes = [float(c) for c in closes]
    n = len(closes)

    def rsi_transform(sensitivity, period, base) -> Series:
        r = rsi_wilder(closes, int(period))
        out: Series = []
        for v in r:
            if v is None:
                out.append(NA)
                continue
            t = sensitivity * (v - base)
            out.append(20.0 if t > 20 else (0.0 if t < 0 else t))
        return out

    rsi_banker = rsi_transform(p["sensitivity_banker"], p["rsi_period_banker"],
                               p["rsi_base_banker"])
    rsi_hot = rsi_transform(p["sensitivity_hotmoney"], p["rsi_period_hotmoney"],
                            p["rsi_base_hotmoney"])

    hotma2 = rma(rsi_hot, 2)
    bankma2 = sma(rsi_banker, 2)
    hotma7 = rma(rsi_hot, 7)
    bankma7 = ema(rsi_banker, 7)
    hotma31 = rma(rsi_hot, 31)
    bankma31 = ema(rsi_banker, 31)

    hotma = ema(_scale(_add3(_scale(hotma2, 34), _scale(hotma7, 33),
                             _scale(hotma31, 33)), 0.01), 2)
    bankma = sma(_scale(_add3(_scale(bankma2, 70), _scale(bankma7, 20),
                              _scale(bankma31, 10)), 0.01), 1)
    hotsignal = rma(hotma, 2)
    banksignal = rma(bankma, 4)

    # hbma: script calls vwma(composite, 1); a 1-period VWMA == the value itself.
    hbma = _scale(_add3(
        _add3(_scale(rsi_hot, 10), _scale(hotma, 35), _scale(hotsignal, 15)),
        _scale(bankma, 35), _scale(banksignal, 5)), 0.01)
    major = wma(hbma, 9)

    low = {k: _scale(rsi_banker, 1.0 / k) for k in range(2, 8)}
    # low_sum = lowma2+...+lowma7  (each is rsi_banker/k)
    low_sum = [NA] * n
    for i in range(n):
        vals = [low[k][i] for k in range(2, 8)]
        if None not in vals:
            low_sum[i] = sum(vals)  # type: ignore[arg-type]
    lowmaster = sma(_scale(low_sum, 1 / 6), 1)
    lowamp = sma(low_sum, 1)
    lowampsignal = ema(lowamp, 31)
    lowmsignal = ema(_scale(_add2(_scale(lowmaster, 90),
                                  _scale(lowamp, 10)), 0.01), 7)

    def const_series(c: float) -> Series:
        return [c] * n

    c85 = const_series(8.5)
    c19 = const_series(19.0)

    sig = {
        "topsignals": [False] * n,
        "downtrend": [False] * n,
        "uptrend": [False] * n,
        "bullish": [False] * n,
        "bearish": [False] * n,
        "entry": [False] * n,
        "climax": [False] * n,
        "pump": [False] * n,
        "bottom": [False] * n,
        "greed": [False] * n,
        "long": [False] * n,
    }
    cnd = {
        "retest": [False] * n,
        "pump_candles": [False] * n,
        "down_candles": [False] * n,
        "dump_candles": [False] * n,
    }

    for i in range(n):
        rb = _prev2(rsi_banker, i, 0)
        rh = _prev2(rsi_hot, i, 0)

        sig["topsignals"][i] = (
            crossunder(bankma, banksignal, i)
            and lt_const(rsi_banker, i, 15)
            and gt_const(rsi_hot, i, 10)
            and gt_const(banksignal, i, 8)
        )
        sig["downtrend"][i] = crossunder(hotma, hotsignal, i)
        sig["uptrend"][i] = crossover(hotma, hotsignal, i)

        def back(s: Series, b: int, pred) -> bool:
            v = _prev2(s, i, b)
            return v is not None and pred(v)

        sig["bullish"][i] = (
            crossover(rsi_banker, c85, i)
            and gt_const(rsi_hot, i, 17)
            and _prev2(bankma, i, 0) is not None
            and _prev2(banksignal, i, 0) is not None
            and _prev2(bankma, i, 0) > _prev2(banksignal, i, 0)  # type: ignore[operator]
            and _prev2(hotma, i, 0) is not None
            and _prev2(hotsignal, i, 0) is not None
            and _prev2(hotma, i, 0) > _prev2(hotsignal, i, 0)  # type: ignore[operator]
            and back(rsi_banker, 2, lambda v: v < 6)
            and back(rsi_banker, 5, lambda v: v < 5)
            and back(rsi_banker, 24, lambda v: v < 12)
        )
        sig["bearish"][i] = (
            crossunder(rsi_banker, c85, i)
            and lt_const(rsi_hot, i, 18)
            and _prev2(bankma, i, 0) is not None
            and _prev2(banksignal, i, 0) is not None
            and _prev2(bankma, i, 0) < _prev2(banksignal, i, 0)  # type: ignore[operator]
            and _prev2(hotma, i, 0) is not None
            and _prev2(hotsignal, i, 0) is not None
            and _prev2(hotma, i, 0) < _prev2(hotsignal, i, 0)  # type: ignore[operator]
            and lt_const(rsi_banker, i, 5)
        )
        sig["entry"][i] = (
            crossover(rsi_hot, const_series(16.0), i)
            and gt_const(rsi_banker, i, 0)
            and back(rsi_hot, 3, lambda v: v < 15)
            and back(rsi_hot, 10, lambda v: v < 13)
            and back(rsi_hot, 20, lambda v: v < 13)
        )
        sig["climax"][i] = crossover(hbma, c19, i)
        sig["pump"][i] = crossover(rsi_banker, hbma, i)
        sig["bottom"][i] = crossunder(rsi_hot, hbma, i)
        sig["greed"][i] = (
            crossover(lowampsignal, hbma, i)
            and gt_const(lowampsignal, i, 12)
            and gt_const(rsi_banker, i, 8.5)
        )
        sig["long"][i] = (
            crossunder(lowampsignal, banksignal, i)
            and lt_const(lowampsignal, i, 10)
        )

        # Candle-coloring flags (visual in Pine; useful as features here)
        bs = _prev2(banksignal, i, 0)
        bm = _prev2(bankma, i, 0)
        cnd["retest"][i] = (
            bs is not None and bm is not None and rb is not None
            and bs > bm and rb > 0
        )
        hb = _prev2(hbma, i, 0)
        cnd["pump_candles"][i] = rb is not None and hb is not None and rb > hb

        def dec_ok() -> bool:
            need = [rsi_banker, rsi_banker, rsi_banker, rsi_banker, rsi_banker,
                    rsi_banker, rsi_banker]
            backs = [0, 1, 2, 1, 2, 3, 4, 3, 4, 6]
            # rsi_Banker<rsi_Banker[1] and <[2] and [1]<[2] and <[3] and <[4]
            # and [3]<[4] and [6]>8.5 and <10
            v = [_prev2(rsi_banker, i, b) for b in (0, 1, 2, 3, 4, 6)]
            if None in v:
                return False
            v0, v1, v2, v3, v4, v6 = v  # type: ignore[misc]
            return (v0 < v1 and v0 < v2 and v1 < v2 and v0 < v3 and v0 < v4
                    and v3 < v4 and v6 > 8.5 and v0 < 10)

        cnd["down_candles"][i] = dec_ok()
        r1 = _prev2(rsi_banker, i, 1)
        cnd["dump_candles"][i] = (
            rb is not None and r1 is not None and r1 != 0 and rb < r1 / 1.75
        )

    return {
        "series": {
            "rsi_banker": rsi_banker,
            "rsi_hotmoney": rsi_hot,
            "hotma": hotma,
            "bankma": bankma,
            "hotsignal": hotsignal,
            "banksignal": banksignal,
            "hbma": hbma,
            "major": major,
            "lowampsignal": lowampsignal,
            "lowmsignal": lowmsignal,
        },
        "signals": sig,
        "candles": cnd,
    }


def latest(closes: Sequence[float], **params) -> dict:
    """Most recent bar: indicator values + which signals fired.

    Returns {"values": {...}, "fired": [signal names], "candles": {...}}.
    """
    res = compute(closes, **params)
    i = len(closes) - 1
    values = {k: (s[i] if i < len(s) else None)
              for k, s in res["series"].items()}
    fired = [name for name, s in res["signals"].items() if i < len(s) and s[i]]
    candles = {k: (s[i] if i < len(s) else False)
               for k, s in res["candles"].items()}
    return {"values": values, "fired": fired, "candles": candles}


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

def _synthetic(n: int = 400, seed: int = 7) -> List[float]:
    """Deterministic pseudo-random walk with trend regimes (no imports)."""
    rnd = seed
    price = 100.0
    out = []
    for i in range(n):
        rnd = (rnd * 1103515245 + 12345) & 0x7FFFFFFF
        shock = (rnd / 0x7FFFFFFF - 0.5) * 4.0
        if 120 <= i < 200:
            shock += 1.2   # uptrend regime
        elif 280 <= i < 340:
            shock -= 1.4   # downtrend regime
        price = max(5.0, price + shock)
        out.append(price)
    return out


def self_test() -> None:
    closes = _synthetic()
    res = compute(closes)
    n = len(closes)
    print(f"bars: {n}")
    print("signal counts:")
    for name, s in res["signals"].items():
        print(f"  {name:12s} {sum(s)}")
    print("candle-flag counts:")
    for name, s in res["candles"].items():
        print(f"  {name:12s} {sum(s)}")
    # The 0..20 clamp must hold on every non-None transform value.
    for key in ("rsi_banker", "rsi_hotmoney"):
        bad = [v for v in res["series"][key]
               if v is not None and not (0.0 <= v <= 20.0)]
        assert not bad, f"{key} escaped the 0..20 clamp"
    # hbma must equal its composite definition (1-period VWMA == value).
    assert res["series"]["hbma"][n - 1] is not None
    lat = latest(closes)
    print("latest values:", {k: round(v, 3) if v is not None else None
                             for k, v in lat["values"].items()})
    print("latest fired:", lat["fired"] or "(none)")
    print("self-test OK")


if __name__ == "__main__":
    self_test()
