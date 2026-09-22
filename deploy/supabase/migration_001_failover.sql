-- ============================================================================
-- Failover stack migration 001 — shared state for the three free tiers
-- Apply in the Supabase SQL editor (or `supabase db push`). Idempotent.
-- ============================================================================

-- 1. Heartbeat: every tier writes one per minute while alive -----------------
CREATE TABLE IF NOT EXISTS tier_heartbeat (
  tier        TEXT PRIMARY KEY,          -- 'render' | 'cloudrun' | 'oracle'
  last_beat   TIMESTAMPTZ NOT NULL DEFAULT now(),
  is_leader   BOOLEAN NOT NULL DEFAULT FALSE
);

-- 2. Watermark: where each scheduled scan got to (survives tier death mid-scan)
CREATE TABLE IF NOT EXISTS scan_watermark (
  scan_name   TEXT PRIMARY KEY,          -- 'premarket-7am' | 'hourly-watch' | ...
  last_stage  TEXT NOT NULL,             -- 'universe'|'filters'|'analysis'|'synthesis'|'done'
  last_run    TIMESTAMPTZ NOT NULL,
  result_ref  TEXT                       -- scan_results.id or storage snapshot path
);

-- 3. Results: every completed analysis, queryable by any tier and the app ----
CREATE TABLE IF NOT EXISTS scan_results (
  id          BIGSERIAL PRIMARY KEY,
  scan_name   TEXT NOT NULL,
  ran_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  ran_on_tier TEXT NOT NULL,
  payload     JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS scan_results_scan_ran_idx
  ON scan_results (scan_name, ran_at DESC);

-- 4. Alert dedup: enforces "max 1 alert per ticker per hour" ACROSS failovers.
--    The PRIMARY KEY is the enforcement — a racing tier's duplicate INSERT
--    physically fails instead of sending a second alert.
CREATE TABLE IF NOT EXISTS alert_dedup (
  ticker      TEXT NOT NULL,
  hour_bucket TIMESTAMPTZ NOT NULL,      -- date_trunc('hour', now())
  alerted_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (ticker, hour_bucket)
);

-- 5. Audit: append-only event log (mirrors the engine's local audit trail) ---
CREATE TABLE IF NOT EXISTS audit_log (
  id          BIGSERIAL PRIMARY KEY,
  ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
  tier        TEXT NOT NULL,
  event       TEXT NOT NULL,
  detail      JSONB
);
CREATE INDEX IF NOT EXISTS audit_log_ts_idx ON audit_log (ts DESC);

-- ---------------------------------------------------------------------------
-- Row Level Security: deny-by-default. The API tiers connect with the
-- SERVICE ROLE key (bypasses RLS); the anon key gets nothing. The Android app
-- never talks to Supabase directly — it goes through the tier API.
-- ---------------------------------------------------------------------------
ALTER TABLE tier_heartbeat  ENABLE ROW LEVEL SECURITY;
ALTER TABLE scan_watermark  ENABLE ROW LEVEL SECURITY;
ALTER TABLE scan_results    ENABLE ROW LEVEL SECURITY;
ALTER TABLE alert_dedup     ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_log       ENABLE ROW LEVEL SECURITY;

-- No permissive policies are created on purpose: with RLS enabled and zero
-- policies, anon/authenticated keys can read/write nothing. Service-role
-- connections (our tiers) bypass RLS entirely. If a future direct-read need
-- arises, add a narrow policy then — never preemptively.
