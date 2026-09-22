#!/usr/bin/env python3
"""Direct runner for the Micro Cap Momentum scan (no Temporal server needed).

Exercises the real engine activities end to end:
  fetch_universe -> fetch_market_data -> compute_technicals -> check_catalyst
  -> compute_relative_strength -> quality_control

Scan-as-of: Friday 2026-09-18 17:00 America/Chicago (EOD). Bars, screener,
and filings all reflect the Friday close; news/8-K recency is anchored to
the same timestamp so nothing dated after the close can qualify.

Funnel (all deterministic until the optional LLM synthesis, which is
skipped here — ranking is rvol * (1 + day move)):
  1. Nasdaq universe: price $2-20, market cap $30M-$1B
  2. Fresh bars (last bar = 2026-09-18), RVOL >= 3, day move >= +10%,
     session volume >= 500k
  3. Float proxy (market_cap / price) < 10M
  4. Material catalyst within 7 days (news classifier + 8-K item footprint)
  5. Deterministic QC (hard-filter recheck + mega-cap engine rule)

Usage: .venv/bin/python run_scan.py [--max-universe N]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent))

import yaml

import activities
from activities import TickerBundle

HERE = Path(__file__).parent
AS_OF = datetime(2026, 9, 18, 17, 0, tzinfo=ZoneInfo("America/Chicago"))
AS_OF_ISO = AS_OF.isoformat()
LAST_BAR = "2026-09-18"


def load_scan():
    cfg = yaml.safe_load(open(HERE.parent / "scan-configs.yaml"))
    scan = next(s["scan"] for s in cfg["scans"]
                if s["scan"]["name"] == "Micro Cap Momentum")
    return scan, cfg.get("engine_rules", {})


async def fetch_bars(symbol: str):
    # activities are async; the providers inside are blocking, so bars
    # fetch serially (~0.3s each, cached on re-runs).
    try:
        return await activities.fetch_market_data(symbol)
    except Exception as exc:
        return exc


async def main() -> dict:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-universe", type=int, default=0)
    args = ap.parse_args()

    t0 = time.time()
    scan, engine_rules = load_scan()
    hf = scan["hard_filters"]
    uni = scan["universe"]

    # ---- 1. universe ------------------------------------------------------
    screener = activities._provider("screener")
    rows = screener.screen(
        min_price=hf["price"]["min"], max_price=hf["price"]["max"],
        min_market_cap=activities._parse_money(uni["market_cap"]["min"]),
        max_market_cap=activities._parse_money(uni["market_cap"]["max"]))
    by_sym = {r["symbol"]: r for r in rows}
    if args.max_universe:
        rows = rows[:args.max_universe]
    funnel = {"universe": len(rows)}
    print(f"universe: {len(rows)} (price 2-20, mcap 30M-1B)", flush=True)

    # ---- 2. bars + technicals --------------------------------------------
    bars_list = await asyncio.gather(
        *(fetch_bars(r["symbol"]) for r in rows))
    ok_bars = [b for b in bars_list if not isinstance(b, Exception)]
    funnel["bars_ok"] = len(ok_bars)
    funnel["bars_failed"] = len(bars_list) - len(ok_bars)

    cands: list[TickerBundle] = []
    for b in ok_bars:
        t = await activities.compute_technicals(b)
        if not b.dates or b.dates[-1] != LAST_BAR:
            continue  # stale/halted — not Friday's tape
        if t.rvol is None or t.rvol < hf["relative_volume"]["min"]:
            continue
        if (t.day_change_pct is None
                or t.day_change_pct < activities._parse_pct(hf["daily_move"]["min"])):
            continue
        if t.volume is None or t.volume < hf["volume"]["min"]:
            continue
        row = by_sym[b.ticker]
        price = b.closes[-1]
        mcap = row.get("market_cap")
        bundle = TickerBundle(ticker=b.ticker, price=price, market_cap=mcap,
                              technicals=t)
        bundle.float_shares = (mcap / price) if mcap and price else None
        bundle.notes.append(row.get("name", ""))
        cands.append(bundle)
    funnel["technical_filters"] = len(cands)
    print(f"after RVOL/move/volume/stale filters: {len(cands)}", flush=True)

    # ---- 3. float: true float first, proxy fallback -------------------------
    # True float comes free from stockanalysis.com statistics pages
    # (providers/float.py); the market_cap/price proxy is only a fallback.
    float_max = activities._parse_money(hf["float"]["max"])
    tech_passers = cands  # all 12 stay visible on the below-the-bar board
    from providers import float as float_provider

    async def resolve_float(c: TickerBundle):
        try:
            return await asyncio.to_thread(float_provider.float_stats,
                                           c.ticker)
        except Exception:
            return {"ticker": c.ticker, "float_shares": None}

    fstats = await asyncio.gather(*(resolve_float(c) for c in tech_passers))
    float_info: dict[str, dict] = {}
    float_ok = set()
    for c, fs in zip(tech_passers, fstats):
        true_f = fs.get("float_shares")
        src = fs.get("source", "stockanalysis.com") if true_f else "proxy"
        float_info[c.ticker] = {
            "true": int(true_f) if true_f else None,
            "source": src,
            "short_pct_float": fs.get("short_pct_float"),
            "short_ratio": fs.get("short_ratio"),
            "inst_own_pct": fs.get("inst_own_pct"),
        }
        eff = true_f if true_f else c.float_shares
        if eff is not None and eff <= float_max:
            float_ok.add(c.ticker)
    funnel["float_filter"] = len(float_ok)
    print(f"after float<10M (true float first): {len(float_ok)}", flush=True)

    # ---- 4. catalyst (checked for every technical passer) -------------------
    async def check(c: TickerBundle):
        try:
            return await activities.check_catalyst(
                c.ticker, c.notes[0] if c.notes else "",
                hf["catalyst_age"]["max_days"], AS_OF_ISO)
        except Exception as exc:
            return {"error": str(exc), "passes": False,
                    "news_verdict": {"verdict": "error"}}

    verdicts = await asyncio.gather(*(check(c) for c in tech_passers))
    passed: list[TickerBundle] = []
    for c, v in zip(tech_passers, verdicts):
        c.catalyst_verdict = v
        if v.get("passes") and c.ticker in float_ok:
            passed.append(c)
    funnel["catalyst_filter"] = len(passed)
    print(f"after catalyst filter: {len(passed)}", flush=True)

    # ---- 5. rank + bundles -------------------------------------------------
    def score(c: TickerBundle) -> float:
        t = c.technicals
        return (t.rvol or 0) * (1 + (t.day_change_pct or 0))

    def stat_block(c: TickerBundle) -> dict:
        t = c.technicals
        nv = (c.catalyst_verdict.get("news_verdict", {}) or {})
        fi = float_info.get(c.ticker, {})
        return {
            "ticker": c.ticker,
            "name": c.notes[0] if c.notes else "",
            "price": round(c.price, 2),
            "market_cap": c.market_cap,
            "float_proxy": int(c.float_shares) if c.float_shares else None,
            "float_true": fi.get("true"),
            "float_source": fi.get("source"),
            "short_pct_float": fi.get("short_pct_float"),
            "short_ratio_days": fi.get("short_ratio"),
            "inst_own_pct": fi.get("inst_own_pct"),
            "day_change_pct": round(t.day_change_pct, 4),
            "rvol": round(t.rvol, 2),
            "volume": t.volume,
            "rsi14": round(t.rsi14, 1) if t.rsi14 else None,
            "catalyst_verdict": nv.get("verdict"),
            "catalyst": nv.get("headline", ""),
            "catalyst_source": nv.get("source", ""),
            "catalyst_age_days": (round(nv["age_days"], 1)
                                  if nv.get("age_days") is not None else None),
        }

    passed.sort(key=score, reverse=True)
    # below-the-bar board: every technical passer, with its verdict, so a
    # thin tape is legible instead of looking like an error
    board = [stat_block(c) for c in
             sorted(tech_passers, key=score, reverse=True)]
    finalists = passed[:scan["output"]["top_candidates"]]

    for c in finalists:  # real 63d RS vs SPY/QQQ/IWM for finalists only
        bars = next(b for b in ok_bars if b.ticker == c.ticker)
        try:
            c.relative_strength = await activities.compute_relative_strength(bars)
        except Exception as exc:
            c.relative_strength = {"error": str(exc)}

    picks = []
    for c in finalists:
        blk = stat_block(c)
        rs = ((c.relative_strength.get("vs", {}).get("SPY", {})) or {})
        blk["rs_vs_spy_63d"] = rs.get("excess_pct")
        blk["score"] = round(score(c), 2)
        picks.append(blk)

    report = {"scan": scan["name"], "as_of": AS_OF_ISO,
              "universe": "Nasdaq screener (full tape available to engine)",
              "funnel": funnel, "picks": picks,
              "technical_passers": board,
              "notes": ["float gate uses true float from stockanalysis.com "
                        "(free); market_cap/price proxy only as fallback",
                        "catalyst window per engine config: 7 days; "
                        "6-K EX-99.1 exhibits now classified for foreign filers",
                        "technical_passers cleared price/RVOL/move/volume "
                        "but not the float/catalyst gates"]}

    # ---- 6. deterministic QC ------------------------------------------------
    report = await activities.quality_control(
        report, finalists, {"scan": scan, "engine_rules": engine_rules})
    funnel["qc_dropped"] = len(report["qc"]["dropped"])
    report["elapsed_s"] = round(time.time() - t0, 1)
    return report


if __name__ == "__main__":
    rep = asyncio.run(main())
    outdir = HERE.parent.parent / "hidden_files"
    outdir.mkdir(exist_ok=True)
    out = outdir / "microcap-scan-2026-09-18.json"
    json.dump(rep, open(out, "w"), indent=2, default=str)
    print(f"\nsaved {out}")
    print(json.dumps({k: rep[k] for k in ("funnel", "qc")}, indent=2,
                     default=str))
    for p in rep["picks"]:
        rs = p["rs_vs_spy_63d"]
        rs_s = f"RS/SPY {rs * 100:+.0f}%" if rs is not None else "RS/SPY n/a"
        fl = p["float_true"] or p["float_proxy"]
        fl_s = f"{fl / 1e6:.1f}M" if fl else "n/a"
        print(f"{p['ticker']:6s} ${p['price']:<7} {p['day_change_pct'] * 100:+.1f}%  "
              f"RVOL {p['rvol']:<6} float {fl_s}  {rs_s}"
              f"  | {p['catalyst'][:90]}")
