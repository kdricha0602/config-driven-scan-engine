# Config-Driven Scan Engine

One engine, many hunts. A YAML scan config decides what a scan cares about — **Temporal** orchestrates, **Activities** do the work, **LiteLLM** interprets. New hunts mean new YAML, not new code.

This is an independent personal project. Not financial advice.

## Architecture

```
scan-configs.yaml
        │  what does this hunt care about?
        ▼
Temporal workflows  →  per-ticker child workflows (parallel, retried)
        │
        ├── providers/   free public data: Nasdaq screener, Yahoo bars/quotes,
        │                Stooq, SEC EDGAR (XBRL fundamentals + filings),
        │                Google News RSS, true float
        ├── engine/      technicals (RSI/MACD/ATR/RVOL) + MCDX money-flow block,
        │                relative strength, dilution screens
        ├── LiteLLM      catalyst interpretation, thesis contradiction
        │                (steelman the bear case), final synthesis
        └── quality_control  deterministic: every number must exist in a bundle,
                             hard filters recomputed from raw values.
                             Violating picks are dropped, never silently fixed.
```

The core rule, enforced by architecture: **code calculates, the model interprets.** ROIC, RSI, RVOL, FCF margin, and every MCDX signal are computed in Python. The LLM never produces a number that isn't in its input bundle.

### Money flow

`engine/mcdx.py` is a pure-Python port of the LOKEN BULLISH MCDX v2.2 (Banker/Hot Money) transform stack — no dependencies, `python3 mcdx.py` self-tests. `indicators/mcdx-loken-indicator.pine` is the Pine Script reference it was ported from.

### Failover design

Built for a three-tier free failover stack — **Render → Google Cloud Run → Oracle ARM** — with per-stage scan watermarks in Supabase, Postgres advisory-lock leader election, and database-enforced alert deduplication. See `docs/FREE_FAILOVER_STACK.md`, `deploy/` (Dockerfile, `render.yaml`, GitHub Actions watchdog, Supabase migration), and `docs/INTEGRITY_CHARTER.md` for the operating rules.

## Status

⚠️ **In progress.** Engine code, scan configs, money-flow module, tests, docs, and deploy manifests are written. The go-live sequence — Supabase migration → Render → Cloud Run → Oracle ARM → GitHub secrets — **has not been executed yet**, and no end-to-end Temporal run has completed. What's here is real code at a real checkpoint, not a finished system.

## Setup (local dev)

```bash
# Temporal dev server
curl -sSf https://temporal.download/cli.sh | sh
temporal server start-dev        # engine on :7233, UI on :8233

# Python env
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# LLM access for the interpretation activities (data providers need no keys)
export LLM_API_KEY=...
# SEC fair-use rule: identify yourself
export SEC_CONTACT_EMAIL=you@example.com

.venv/bin/python engine/worker.py   # serves task queue "equity-scans"
```

`engine/run_scan.py` offers a direct runner for the Micro Cap Momentum config without a Temporal server.

## Layout

```
engine/            activities, workflows, worker, api, failover,
                   guard_check, security, mcdx + providers/* + tests/*
configs/           scan-configs.yaml — the hunts
deploy/            Dockerfile, render.yaml, github/workflows, supabase/
docs/              FREE_FAILOVER_STACK.md, INTEGRITY_CHARTER.md
indicators/        mcdx-loken-indicator.pine (Pine reference)
```

## License

MIT — see [LICENSE](LICENSE).
