"""Failover coordination for the three free tiers.

One shared Supabase Postgres holds all coordination state:
  - leader election via a Postgres advisory lock (auto-releases when the
    holder's connection drops, i.e. when a tier dies — no coordinator service)
  - per-stage scan watermarks so the next tier resumes instead of restarting
  - alert dedup with a DB primary key so a racing tier can never double-alert
  - heartbeats + append-only audit

Fail-closed: without DATABASE_URL nothing runs; without the advisory lock
nothing scans. A tier that cannot prove leadership stays quiet.
"""

from __future__ import annotations

import os
from datetime import datetime, time as dtime, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

CT = ZoneInfo("America/Chicago")

TIER_NAME = os.environ.get("TIER_NAME", "local")
DATABASE_URL = os.environ.get("DATABASE_URL", "")

# One constant per deployment — every tier contends for the same lock.
LEADER_LOCK_ID = 987654

# stage order for resume logic
STAGES = ["universe", "filters", "analysis", "synthesis", "done"]

# Scan schedules, mirroring Korben's standing commitments. The watchdog is
# dumb (probe + trigger); the tier decides what is due. Times are
# America/Chicago; weekdays only (US market holidays are not modeled —
# a holiday just yields an empty scan, which is safe).
SCAN_SCHEDULES = {
    # name: (kind, params)
    "premarket-7am": ("daily", {"at": dtime(7, 0)}),
    "weekday-equity-scan": ("daily", {"at": dtime(8, 42)}),
    "hourly-watch": ("hourly", {"minute": 7, "from": dtime(8, 30),
                                "to": dtime(15, 30)}),
    "eod-scan": ("daily", {"at": dtime(15, 45)}),
    "midcap-pipeline": ("monthly", {"day": 3, "at": dtime(9, 0)}),
}


def get_conn():
    """Connect to the shared Postgres. Raises RuntimeError if unconfigured."""
    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL is not set — refusing to run without shared state "
            "(fail closed).")
    import psycopg
    return psycopg.connect(DATABASE_URL, connect_timeout=10)


def try_become_leader(tier: str = TIER_NAME):
    """Try to claim the global scan lock.

    Returns an open connection holding the lock (caller must close it to
    release), or None if another tier is already leader.
    """
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT pg_try_advisory_lock(%s)", (LEADER_LOCK_ID,)).fetchone()
        leader = bool(row[0])
        conn.execute(
            """INSERT INTO tier_heartbeat (tier, last_beat, is_leader)
               VALUES (%s, now(), %s)
               ON CONFLICT (tier)
               DO UPDATE SET last_beat = now(), is_leader = %s""",
            (tier, leader, leader))
        conn.commit()
        if not leader:
            conn.close()
            return None
        return conn
    except Exception:
        conn.close()
        raise


def write_heartbeat(tier: str = TIER_NAME, is_leader: bool = False) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO tier_heartbeat (tier, last_beat, is_leader)
               VALUES (%s, now(), %s)
               ON CONFLICT (tier)
               DO UPDATE SET last_beat = now(), is_leader = %s""",
            (tier, is_leader, is_leader))


def record_stage(scan_name: str, stage: str,
                 result_ref: str | None = None) -> None:
    """Persist how far a scan got — the next tier resumes from here."""
    assert stage in STAGES, f"unknown stage {stage!r}"
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO scan_watermark (scan_name, last_stage, last_run, result_ref)
               VALUES (%s, %s, now(), %s)
               ON CONFLICT (scan_name)
               DO UPDATE SET last_stage = EXCLUDED.last_stage,
                             last_run = EXCLUDED.last_run,
                             result_ref = EXCLUDED.result_ref""",
            (scan_name, stage, result_ref))


def get_watermark(scan_name: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT scan_name, last_stage, last_run, result_ref "
            "FROM scan_watermark WHERE scan_name = %s",
            (scan_name,)).fetchone()
    if not row:
        return None
    return {"scan_name": row[0], "last_stage": row[1],
            "last_run": row[2], "result_ref": row[3]}


def save_result(scan_name: str, tier: str, payload: dict) -> int:
    """Persist a completed analysis. Returns the new row id."""
    import json
    with get_conn() as conn:
        row = conn.execute(
            "INSERT INTO scan_results (scan_name, ran_on_tier, payload) "
            "VALUES (%s, %s, %s) RETURNING id",
            (scan_name, tier, json.dumps(payload, default=str))).fetchone()
        return int(row[0])


def should_alert(ticker: str) -> bool:
    """True once per ticker per hour — enforced by the DB primary key.

    A racing tier's duplicate INSERT raises UniqueViolation and returns
    False: the second alert is never sent, no matter the code path.
    """
    import psycopg
    with get_conn() as conn:
        try:
            conn.execute(
                """INSERT INTO alert_dedup (ticker, hour_bucket)
                   VALUES (%s, date_trunc('hour', now()))""",
                (ticker.upper(),))
            return True
        except psycopg.errors.UniqueViolation:
            conn.rollback()
            return False


def audit(tier: str, event: str, detail: dict | None = None) -> None:
    import json
    try:
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO audit_log (tier, event, detail) "
                "VALUES (%s, %s, %s)",
                (tier, event, json.dumps(detail or {}, default=str)))
    except Exception:
        pass  # audit must never break the scan


# ---------------------------------------------------------------------------
# Due-scan scheduling
# ---------------------------------------------------------------------------

def _is_weekday(now: datetime) -> bool:
    return now.weekday() < 5


def is_due(scan_name: str, now: datetime,
           last_run: datetime | None) -> bool:
    """Has this scan's schedule fired since last_run? Pure function, tested."""
    kind, p = SCAN_SCHEDULES[scan_name]
    now = now.astimezone(CT)
    if last_run is not None:
        last_run = last_run.astimezone(CT)

    if kind == "daily":
        if not _is_weekday(now):
            return False
        fired_today = now.time() >= p["at"]
        ran_today = (last_run is not None and last_run.date() == now.date())
        return fired_today and not ran_today

    if kind == "hourly":
        if not _is_weekday(now):
            return False
        if not (p["from"] <= now.time() <= p["to"]):
            return False
        if now.minute < p["minute"]:
            return False  # this hour's slot hasn't fired yet
        if last_run is None:
            return True
        # due if last run was before this hour's slot
        slot = now.replace(minute=p["minute"], second=0, microsecond=0)
        return last_run < slot

    if kind == "monthly":
        # Effective fire day: the 3rd, rolled forward past weekends so a
        # month never silently skips when the 3rd falls on Sat/Sun.
        eff = now.replace(day=p["day"])
        while eff.weekday() >= 5:
            eff += timedelta(days=1)
        if now.date() != eff.date():
            return False
        fired = now.time() >= p["at"]
        ran_this_month = (last_run is not None
                          and (last_run.year, last_run.month)
                          == (now.year, now.month))
        return fired and not ran_this_month

    return False


def get_due_scans(now: datetime | None = None) -> list[str]:
    """Scan names whose schedule has fired since their watermark."""
    now = (now or datetime.now(CT)).astimezone(CT)
    due = []
    for name in SCAN_SCHEDULES:
        wm = get_watermark(name)
        last = wm["last_run"] if wm else None
        # a crashed mid-scan run (watermark not 'done') is always re-due
        if wm and wm["last_stage"] != "done":
            due.append(name)
        elif is_due(name, now, last):
            due.append(name)
    return due


def run_due_scans(tier: str = TIER_NAME,
                  runner: Callable[[str], dict] | None = None,
                  now: datetime | None = None) -> dict:
    """Claim leadership, run every due scan, release. The failover entrypoint.

    `runner(scan_name)` performs one scan and returns its report payload.
    Returns a summary dict; raises RuntimeError if leadership can't be had.
    """
    conn = try_become_leader(tier)
    if conn is None:
        audit(tier, "leader_contended", {})
        return {"leader": False, "tier": tier}
    try:
        write_heartbeat(tier, True)
        due = get_due_scans(now)
        results = []
        for name in due:
            try:
                record_stage(name, "universe")
                payload = runner(name) if runner else {"scan": name,
                                                      "note": "no runner wired"}
                rid = save_result(name, tier, payload)
                record_stage(name, "done", result_ref=str(rid))
                results.append({"scan": name, "result_id": rid})
                audit(tier, "scan_completed",
                      {"scan": name, "result_id": rid})
            except Exception as exc:  # one scan's failure never blocks others
                audit(tier, "scan_failed",
                      {"scan": name, "error": str(exc)[:500]})
                results.append({"scan": name, "error": str(exc)[:200]})
        return {"leader": True, "tier": tier, "ran": results}
    finally:
        conn.close()  # releasing the advisory lock with the connection
