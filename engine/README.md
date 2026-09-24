# Config-driven scan engine

One engine, many hunts. A YAML scan config decides what a scan cares about;
Temporal orchestrates, Activities do the work, LiteLLM interprets.

## Layout

```
engine/
  mcdx.py          Pure-Python port of LOKEN (v4) BULLISH MCDX v2.2 (Banker/Hot Money).
                   No dependencies. Self-test: python3 mcdx.py
  activities.py    11 activities: universe, market data, technicals (+MCDX),
                   fundamentals, SEC filings, news, relative strength,
                   catalyst interpretation (LiteLLM), thesis contradiction
                   (LiteLLM), synthesis (LiteLLM), quality control.
  workflows.py     ScanWorkflow (full scan), ResearchTickerWorkflow (one ticker),
                   StockLookupWorkflow (ad-hoc lookup).
  worker.py        Serves task queue "equity-scans".
```

## How a scan flows

```
ScanWorkflow(scan_config)
│
├── fetch_universe          cheap hard filters -> tickers
│
├── per ticker (child workflow, parallel, independently retried)
│   ├── fetch_market_data ─┬─▶ compute_technicals ──▶ MCDX money-flow block
│   ├── fetch_fundamentals ┘
│   ├── fetch_sec_filings ──▶ dilution / supply-overhang notes
│   ├── fetch_news ──▶ interpret_catalyst (LiteLLM, cheap model)
│   ├── compute_relative_strength (vs sector / peers / SPY / own history)
│   └── contradict_thesis (LiteLLM: steelman the bear case)
│
├── synthesize (LiteLLM, best model) ──▶ top-N, NEVER quota-filled
└── quality_control (deterministic: every number must exist in a bundle)
```

Key rule, enforced by architecture: **code calculates, the model interprets.**
ROIC, RSI, RVOL, FCF margin, and every MCDX signal are computed in Python.
LiteLLM never produces a number that isn't in its input bundle — QC drops
picks that violate this.

## Scan config schema

```yaml
scan:
  name: "Micro Cap Momentum"
  universe:        { market_cap: {min: 30M, max: 1B} }
  hard_filters:   { price: {min: 2, max: 20}, float: {max: 10M},
                    relative_volume: {min: 3}, daily_move: {min: 10%},
                    volume: {min: 500000}, catalyst_age: {max_days: 7} }
  analytics:      { technicals: true, catalyst_quality: true, dilution: true,
                    relative_strength: true, fundamentals: true,
                    valuation: true, thesis_contradiction: true,
                    data_confidence: true }
  models:         { screening: "gpt-4o-mini", analysis: "claude-sonnet",
                    synthesis: "claude-opus" }
  output:         { top_candidates: 5, watchlist: 5 }
```

Two starter configs live in `../scan-configs.yaml`. New hunts = new YAML, not new code.

## Money-flow module (MCDX)

`mcdx.compute(closes)` returns the full Banker/Hot Money transform stack and
every signal from the Pine Script:

- transforms: `rsi_banker`, `rsi_hotmoney` (clamped 0–20, like the script)
- composites: `hotma`, `bankma`, `hotsignal`, `banksignal`, `hbma` (trend
  tracker), `major`, `lowampsignal`, `lowmsignal`
- signals: `bullish`, `bearish`, `entry`, `climax`, `pump`, `bottom`,
  `greed`, `long`, `topsignals`, `uptrend`, `downtrend`
- candle flags: `retest`, `pump_candles`, `down_candles`, `dump_candles`

`mcdx.latest(closes)` gives the most recent bar: values + which signals fired.
`compute_technicals` calls it on every ticker's close series, so money-flow
state ships inside every `TickerBundle` the synthesizer sees.

Port note: the script's `vwma(composite, 1)` isn't valid Pine (vwma needs
source, volume, length); a 1-period VWMA equals the value itself, so `hbma`
is the composite directly. Documented in `mcdx.py`.

## Setup

```bash
curl -sSf https://temporal.download/cli.sh | sh
temporal server start-dev        # engine on :7233, UI on :8233
python3 -m venv .venv && .venv/bin/pip install temporalio litellm pyyaml
export LLM_API_KEY=...
.venv/bin/python files/engine/worker.py   # run from the goal workspace
```

## Wired providers (free, public, no API keys)

- **Market data** — `providers/yahoo.py`: ~2y daily bars (adjusted closes) +
  quotes via Yahoo's chart API. Verified live: 501 bars for AAPL, MCDX
  computed on real closes.
- **Screener** — `providers/nasdaq_screener.py`: Nasdaq's public screener API
  (price/volume/market-cap filters). Verified live: 7,137 listings, 584 pass
  the Micro Cap Momentum hard filters. **Nasdaq-listed only** — NYSE names
  need a keyed provider later (Finnhub free tier); the `screen()` signature
  is provider-agnostic.
- **SEC fundamentals** — `providers/sec_edgar.py`: XBRL companyfacts →
  gate-ready ratios (revenue/eps CAGR, FCF history + margin, ROIC, leverage,
  interest coverage, dilution, SBC, buybacks). Verified live on FTNT: FY
  2009-2025 history, 13.2% 2y revenue CAGR, 33.1% FCF margin, net-cash
  balance sheet.
  Hard-won rules: key periods by EDGAR `frame` (filers mis-tag `fy`),
  scan all units (EPS lives in USD/shares), disambiguate dimensional
  duplicates via annual-anchor (quarterly) and neighbor interpolation
  (annual). Set `SEC_CONTACT_EMAIL` to a real address (SEC fair-use rule).
- **SEC filings** — same module: 10-K recency, 8-K cadence, shelf/takedown
  filings (S-1/S-3/424B*) split from routine S-8 comp plans, plus a
  going-concern screen of the latest 10-K. Verified live: CYPH flagged with
  going-concern language + a Nov-2025 424B5 takedown.
- `providers/cache.py`: TTL file cache (bars 20h, screener 6h, SEC facts 24h,
  submissions 12h, news 1h) so repeated scans don't hammer free endpoints.
- **News + catalysts** — `providers/news.py`: Google News RSS per ticker
  (headlines/source/date, recency-anchored for scan-as-of runs) plus 8-K
  item codes from EDGAR filing indexes (1.01/2.01/7.01/8.01 = the regulatory
  footprint of a catalyst). 6-K EX-99.1 press-release exhibits are fetched
  and classified too — foreign filers report material news via 6-K, not 8-K
  (caught SLMT's scan-day crypto-promo PR; correctly rejected per the
  desk's blockchain/conference rejection list). A rejected verdict from the
  company's own filing outranks any headline. `classify_catalyst` is
  deterministic — built from the desk's own valid/reject catalyst lists.
  `check_catalyst` activity wraps all three into one verdict dict;
  `interpret_catalyst` (LLM) may refine it later, never originate it.
- **True float** — `providers/float.py`: stockanalysis.com statistics pages
  (free, no key) expose the real float share count plus short-%-of-float,
  short ratio, and institutional ownership. The float gate uses true float
  first and only falls back to the market_cap/price proxy when unavailable.
  Verified live: SLMT true float 4.46M (proxy said 11.0M — screener mcap was
  stale post-reverse-split); TAOX 7.32M.
- **Benchmarks** — `providers/benchmarks.py`: SPY/QQQ/IWM bars via the Yahoo
  provider, 63-day trailing return vs each, sector snapshot (median day-move,
  advancers/decliners) from the screener tape.
- **Deterministic QC** — `quality_control(report, bundles, scan)` activity:
  every pick must trace to a bundle (reported price/RVOL/move match within
  2%), pass ALL hard filters recomputed from raw values, and obey the
  engine-wide `engine_rules`. Output shape enforced (no duplicates, at most
  `top_candidates`). Violating picks are dropped, never silently fixed.

## Engine rules (scan-configs.yaml, enforced by QC — the LLM can't override)

- **mega_cap**: a mega-cap (market cap ≥ `threshold`, default $200B) priced
  OVER `price_cap` ($35) is never emitted in any scan result. A mega-cap at
  $35 or under may appear only if it satisfies ≥ `min_criteria_fraction`
  (0.8) of the scan's hard criteria. This is a guardrail against bad
  market-cap data leaking large caps into small-cap scans.

## Suggested build order

1. ~~Providers: market data + screener~~ — wired (Yahoo + Nasdaq). Next: NYSE
   coverage via a keyed provider if you want the full tape.
2. ~~`compute_technicals` classic indicators (RSI/MACD/SMA/ATR/RVOL)~~ —
   wired in `providers/technicals.py`; MCDX money-flow block included.
3. ~~SEC EDGAR fundamentals + filings~~ — wired. Next: winsorize ratios.
4. ~~News + benchmarks + deterministic QC~~ — wired (see above).
   `interpret_catalyst` still needs a LiteLLM model key to refine verdicts.
5. `contradict_thesis` + `synthesize` with stronger models.
6. First live run: Micro Cap Momentum config — see `run_scan.py` (direct
   runner, no Temporal server needed) and
   `../hidden_files/microcap-scan-2026-09-18.json`.
7. Temporal server + full `ScanWorkflow` end-to-end.
