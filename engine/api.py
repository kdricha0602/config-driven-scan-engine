"""Tier API: health/readiness probes for the watchdog + the secured trigger
endpoint that runs due scans. One image runs on all three tiers; TIER_NAME
distinguishes them.

Endpoints:
  GET  /health                  liveness (no DB touch)
  GET  /ready                   readiness (DB reachable)
  POST /internal/run-due-scans  watchdog trigger; X-Watchdog-Secret required
                                (returns immediately; scans run in background)
  GET  /internal/watermark/{scan}  debug: last watermark for a scan
"""

from __future__ import annotations

import hashlib
import hmac
import os
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

import failover
import yaml

TIER_NAME = os.environ.get("TIER_NAME", "local")
WATCHDOG_SECRET = os.environ.get("WATCHDOG_SECRET", "")
TEMPORAL_ADDRESS = os.environ.get("TEMPORAL_ADDRESS", "localhost:7233")

# Schedule name (failover.SCAN_SCHEDULES) -> scan "name" in scan-configs.yaml.
# The watchdog only knows schedule names; the workflow needs the full config.
SCHEDULE_TO_SCAN = {
    "premarket-7am": "Micro Cap Momentum",
    "weekday-equity-scan": "Large Cap Compounders",
    "hourly-watch": "Micro Cap Momentum",
    "eod-scan": "Large Cap Compounders",
    "midcap-pipeline": "Small Cap 4x Growth",
}


def _configs_path() -> Path:
    """Locate scan-configs.yaml: env override, then next to the engine dir."""
    env = os.environ.get("SCAN_CONFIGS_PATH")
    if env:
        return Path(env)
    here = Path(__file__).resolve().parent  # .../engine
    for cand in (here.parent / "scan-configs.yaml",  # deployed: /app/scan-configs.yaml
                 Path.cwd() / "scan-configs.yaml"):
        if cand.exists():
            return cand
    raise RuntimeError("scan-configs.yaml not found")


def _load_scan_config(scan_name: str) -> dict:
    """Resolve a schedule name to its full scan config dict."""
    want = SCHEDULE_TO_SCAN.get(scan_name)
    if not want:
        raise RuntimeError(f"no scan configured for schedule '{scan_name}'")
    with open(_configs_path()) as fh:
        doc = yaml.safe_load(fh)
    for entry in doc.get("scans", []):
        scan = entry.get("scan", {})
        if scan.get("name") == want:
            cfg = {"engine_rules": doc.get("engine_rules", {}), "scan": scan}
            return cfg
    raise RuntimeError(f"scan '{want}' not found in scan-configs.yaml")

app = FastAPI(title="scan-tier", version="1.0.0")


class TriggerBody(BaseModel):
    triggered_by: str = "unknown"
    run_id: str = "n/a"


def _check_secret(provided: str | None) -> None:
    if not WATCHDOG_SECRET:
        # Fail closed: no secret configured -> trigger endpoint is dead.
        raise HTTPException(status_code=503,
                            detail="watchdog auth not configured")
    if not provided or not hmac.compare_digest(
            hashlib.sha256(provided.encode()).hexdigest(),
            hashlib.sha256(WATCHDOG_SECRET.encode()).hexdigest()):
        raise HTTPException(status_code=403, detail="forbidden")


@app.get("/health")
def health():
    return {"status": "ok", "tier": TIER_NAME,
            "ts": datetime.now(ZoneInfo("America/Chicago")).isoformat()}


@app.get("/ready")
def ready():
    try:
        with failover.get_conn() as conn:
            conn.execute("SELECT 1")
    except Exception as exc:
        raise HTTPException(status_code=503,
                            detail=f"database unreachable: {exc}")
    return {"status": "ready", "tier": TIER_NAME}


@app.get("/internal/watermark/{scan_name}")
def watermark(scan_name: str,
              x_watchdog_secret: str | None = Header(default=None)):
    _check_secret(x_watchdog_secret)
    wm = failover.get_watermark(scan_name)
    if wm is None:
        raise HTTPException(status_code=404, detail="no watermark yet")
    return wm


def _start_temporal_scan(scan_name: str) -> dict:
    """Trigger one scan through Temporal. Imported lazily so the API
    imports cleanly without a Temporal server present."""
    from temporalio.client import Client

    import asyncio

    cfg = _load_scan_config(scan_name)

    async def _run() -> dict:
        client = await Client.connect(TEMPORAL_ADDRESS)
        handle = await client.start_workflow(
            "ScanWorkflow",
            {"scan_config": cfg},
            id=f"scan-{scan_name}-{TIER_NAME}-{int(datetime.now().timestamp())}",
            task_queue="equity-scans",
        )
        return await handle.result()

    return asyncio.run(_run())


@app.post("/internal/run-due-scans")
def run_due_scans(body: TriggerBody,
                  x_watchdog_secret: str | None = Header(default=None)):
    _check_secret(x_watchdog_secret)
    if not failover.DATABASE_URL:
        # Fail closed, synchronously: without shared state nothing runs.
        raise HTTPException(
            status_code=500,
            detail="DATABASE_URL is not set — refusing to run without "
                   "shared state (fail closed).")
    thread = threading.Thread(target=_run_due_scans_bg, args=(body,),
                              name=f"due-scans-{body.run_id}", daemon=True)
    thread.start()
    return {"accepted": True, "tier": TIER_NAME,
            "triggered_by": body.triggered_by, "run_id": body.run_id}


def _run_due_scans_bg(body: TriggerBody) -> None:
    """Background worker for POST /internal/run-due-scans.
    ...
    """
    print(f"BACKGROUND THREAD STARTED: {body.run_id}", flush=True)

    try:
        summary = failover.run_due_scans(
            tier=TIER_NAME, runner=_start_temporal_scan)
    """Background worker for POST /internal/run-due-scans.

    Claims leadership, runs every due scan, persists watermarks/results.
    Runs in a thread so the HTTP trigger returns immediately: the
    watchdog's curl gives up after 120s, but a full scan takes many
    minutes, so a synchronous endpoint could never succeed. Duplicate
    triggers are harmless — the Postgres advisory lock lets exactly one
    thread scan; the rest audit 'leader_contended' and exit.
    """
    try:
        summary = failover.run_due_scans(
            tier=TIER_NAME, runner=_start_temporal_scan)
    except RuntimeError as exc:
        # e.g. DATABASE_URL missing -> fail closed, recorded for forensics
        failover.audit(TIER_NAME, "watchdog_trigger_failed",
                       {"by": body.triggered_by, "run_id": body.run_id,
                        "error": str(exc)[:500]})
        return
    failover.audit(TIER_NAME, "watchdog_trigger",
                   {"by": body.triggered_by, "run_id": body.run_id,
                    "summary": summary})