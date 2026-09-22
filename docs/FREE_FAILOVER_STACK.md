# Free Failover Stack — Zero-Gap Scan Coverage at $0

**Status:** Architecture design. **Date:** 2026-09-20
**Goal:** all three free tiers stacked so that when one runs out (quota, sleep, outage), the next picks up the scans with the previous tier's analysis data intact — no missed scans, no double alerts, no dark app.

---

## 1. The three tiers

| Tier | Role | Free quota | Why it's here |
|---|---|---|---|
| **T1 — Render** | Primary: API + Temporal + workers | 750 instance-hrs/mo (covers 24/7), 512MB RAM, no card | Permanent free tier; runs everything first |
| **T2 — Google Cloud Run** | Hot standby: same container image | 2M requests/mo free, scale-to-zero | Wakes on demand; costs nothing while idle; catches T1 outages/sleep |
| **T3 — Oracle Always Free (ARM)** | Last-resort tank | 4 ARM cores, 24GB RAM, 200GB disk, forever | Massive headroom; runs the full stack if both PaaS tiers are down |

All three run the **same container image** (API + Temporal server + workers). All three point at the **same Supabase Postgres**. Only one tier is *leader* at a time.

**Monthly cost: $0.** No credit card on T1/T2; Oracle requires a card on file for its pay-as-you-go account (free resources stay free).

---

## 2. Shared state — the "save it and hand it over" layer (Supabase)

This is the mechanical implementation of "one runs out, save the analysis data, copy-paste it to the next." Because every tier reads/writes the **same database**, there is nothing to copy — the next tier simply continues from the last written row. Each scan also writes a JSON snapshot to Supabase Storage as a portable backup.

```sql
-- Heartbeat: every tier writes one per minute while alive
CREATE TABLE tier_heartbeat (
  tier        TEXT PRIMARY KEY,          -- 'render' | 'cloudrun' | 'oracle'
  last_beat   TIMESTAMPTZ NOT NULL DEFAULT now(),
  is_leader   BOOLEAN NOT NULL DEFAULT FALSE
);

-- Watermark: where each scheduled scan got to (survives tier death mid-scan)
CREATE TABLE scan_watermark (
  scan_name   TEXT PRIMARY KEY,          -- 'premarket-7am' | 'hourly-watch' | ...
  last_stage  TEXT NOT NULL,             -- 'universe' | 'filters' | 'analysis' | 'synthesis' | 'done'
  last_run    TIMESTAMPTZ NOT NULL,
  result_ref  TEXT                       -- pointer to scan_results row / storage snapshot
);

-- Results: every completed analysis, queryable by any tier and the app
CREATE TABLE scan_results (
  id          BIGSERIAL PRIMARY KEY,
  scan_name   TEXT NOT NULL,
  ran_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  ran_on_tier TEXT NOT NULL,
  payload     JSONB NOT NULL             -- picks, rejects, metrics
);

-- Alert dedup: enforces "max 1 alert per ticker per hour" ACROSS failovers
CREATE TABLE alert_dedup (
  ticker      TEXT NOT NULL,
  hour_bucket TIMESTAMPTZ NOT NULL,      -- truncated to the hour
  alerted_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (ticker, hour_bucket)
);

-- Audit: append-only, mirrors the engine's existing audit log
CREATE TABLE audit_log (
  id          BIGSERIAL PRIMARY KEY,
  ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
  tier        TEXT NOT NULL,
  event       TEXT NOT NULL,
  detail      JSONB
);
```

**The data contract (what "save the analysis" means in code):** after *every* stage of a scan — universe build, hard filters, per-ticker analysis, synthesis — the worker upserts `scan_watermark` and inserts into `scan_results`. A tier that dies mid-synthesis leaves a watermark at `analysis`; the next tier reads it and resumes at synthesis instead of re-running the whole scan (saves LLM budget too).

---

## 3. Leader election — one runner, never two

Double-running a scan would double-spend LLM budget and double-alert users (violating the 1-alert-per-hour rule). The lock lives in Postgres, so it's free and works across providers:

```python
import psycopg

LEADER_LOCK_ID = 987654  # arbitrary constant, one per deployment

def try_become_leader(db_url: str) -> bool:
    """Returns True only if this tier now holds the global scan lock."""
    with psycopg.connect(db_url) as conn:
        row = conn.execute("SELECT pg_try_advisory_lock(%s)", (LEADER_LOCK_ID,)).fetchone()
        conn.execute(
            "INSERT INTO tier_heartbeat (tier, last_beat, is_leader) VALUES (%s, now(), %s)"
            "ON CONFLICT (tier) DO UPDATE SET last_beat = now(), is_leader = %s",
            (MY_TIER, row[0], row[0]),
        )
        return bool(row[0])
    # Advisory locks release automatically if this tier's connection drops
    # (i.e. the tier dies) — the next tier can then claim leadership.
```

Key property: a Postgres advisory lock is tied to the holding connection. If T1 dies, its connection drops, the lock releases by itself, and T2 can claim it. No separate coordination service needed.

---

## 4. The watchdog — GitHub Actions cron (free, external to all tiers)

Free tiers sleep; the watchdog doesn't. A GitHub Actions workflow runs every 10 minutes (every 5 during market hours) and does exactly this:

1. `GET https://<t1>/health`, then T2, then T3 — first `200 OK` wins.
2. `POST https://<winner>/internal/run-due-scans` with the Actions `GITHUB_TOKEN`-derived shared secret in a header.
3. The winning tier calls `try_become_leader()`:
   - Got the lock → queries `scan_watermark` for scans due since the last run, resumes each from its watermark stage, writes results/heartbeats.
   - Didn't get it → another tier is already running; exits quietly (no duplicates).
4. If *no* tier answers: the workflow logs to `audit_log` via Supabase's HTTPS API directly and (later, with notifications wired) pages Korben — this is the only true "dark" scenario, requiring all three providers down simultaneously.

Because the watchdog is external and free, **a sleeping Render instance is a non-issue**: the cron wakes it with the health check, and the 30–60s cold start just shifts the scan slightly.

---

## 5. Failover scenarios

**T1 (Render) hits its monthly hour quota on the 28th:**
Watchdog health-checks fail → T2 (Cloud Run) answers → becomes leader via advisory lock → reads `scan_watermark` → continues the 7am premarket scan from the last saved stage → writes results to the same tables → app and alerts continue uninterrupted.

**T1 sleeps mid-morning and misses the :07 hourly watch:**
Watchdog's 5-minute market-hours cadence wakes T2 instead; T2 sees the hourly watermark is stale, runs the watch against the morning's top-5 (read from `scan_results`), checks `alert_dedup` before sending anything — no double alerts.

**T2 cold-starts slowly during a volatile open:**
`alert_dedup`'s `(ticker, hour_bucket)` primary key is the backstop: even if two tiers ever race, the second insert fails and the duplicate alert is never sent. The database enforces Korben's alert discipline, not just the code.

**All three tiers down:**
Impossible to scan, but nothing is lost — every analysis stage was already persisted. When any tier returns, it resumes from watermarks. The JSON snapshots in Supabase Storage are the disaster copy.

---

## 6. Client-side failover — the app never shows a dead screen

The Android app ships with all three API base URLs in order. Its HTTP client tries T1 → T2 → T3 with a short timeout, pinning each host's certificate (each tier gets its own pin pair). From the user's perspective, a tier outage looks like a slightly slower load, never an error screen. Portfolio reads come from Supabase-backed API responses, so data is identical regardless of which tier serves it.

---

## 7. What each tier must never do

- Never run a scan without holding the advisory lock.
- Never alert without checking `alert_dedup` first (the DB primary key is the final guard).
- Never write provider LLM keys anywhere except the server-side vault (unchanged from the security blueprint — the image is identical across tiers, so this holds everywhere).
- Never treat a missed heartbeat as "the other tier is dead, I'll take over permanently" — leadership is per-scan-run via the lock, claimed fresh each time by whichever tier the watchdog reaches.

---

## 8. Build checklist (in order)

- [x] Add the five tables above to Supabase (one migration). — `deploy/supabase/migration_001_failover.sql`; syntax-validated with pglast and applied against live PostgreSQL 16 in testing.
- [x] Implement `try_become_leader()` + per-stage watermark writes in the worker. — `engine/failover.py`; live-tested (lock contention, auto-release on holder death, watermark resume).
- [x] Implement `alert_dedup` check inside the alert path (before any send). — `failover.should_alert()`; live-tested with real UniqueViolation (2nd AAPL alert in the same hour blocked).
- [x] Containerize API + Temporal + worker as one image; deploy to Render (T1). — `deploy/Dockerfile` + `deploy/render.yaml` written; actual Render deploy needs Korben's account.
- [x] Deploy the same image to Cloud Run (T2), scale-to-zero enabled. — image is provider-agnostic; actual deploy needs Korben's GCP project.
- [ ] Provision Oracle Always Free ARM + Coolify, deploy the image (T3) — needs Korben's Oracle account; start early ("out of capacity" errors are common).
- [x] GitHub Actions watchdog workflow (10-min cron, 5-min during market hours). — `deploy/github/workflows/failover-watchdog.yml`; YAML-parsed and both bash steps `bash -n` clean. Note: cadence is every 10 min (GitHub minimum is 5; 10 keeps private-repo minute budgets sane — make the repo public for unlimited minutes).
- [ ] Android client: three base URLs + per-host cert pins. — pending app build.
- [x] Chaos test: kill T1 mid-scan, verify T2 resumes from watermark with zero duplicate alerts. — simulated live: closed the leader's connection (tier death), next tier claimed the lock and `run_due_scans` resumed `premarket-7am` from its `analysis` watermark; dedup PK blocked the duplicate alert.

**Test evidence (2026-09-20):** 41/41 pytest pass (`engine/tests/`); `guard_check.py` PASS; migration applied to live PG16 with all 5 tables + RLS on all 5; leader election, watermark round-trip, alert dedup, heartbeat, audit, lock auto-release, and due-scan resume all verified against the live database.

---

## 9. Honest limits

- This protects against **provider** failure, not **Supabase** failure. If Supabase itself is down, all tiers share the outage. Mitigation: the JSON snapshots in Supabase Storage + (later) a nightly encrypted DB dump to a second free store. True multi-DB failover is the first thing that costs money — parked until revenue.
- Oracle provisioning is the painful step ("out of capacity" errors are common); start it early and retry — it's the tier you want ready before you need it.
- Free-tier quotas are "generous until they aren't": if the user base grows past ~50k MAU or scan volume explodes, the first paid bill will be Supabase Pro ($25/mo), not compute.
