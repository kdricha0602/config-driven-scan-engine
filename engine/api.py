"""Tier API: health/readiness probes for the watchdog + the secured trigger
endpoint that runs due scans. One image runs on all three tiers; TIER_NAME
distinguishes them.

Endpoints:
  GET  /health                  liveness (no DB touch)
  GET  /ready                   readiness (DB reachable)
  POST /internal/run-due-scans  watchdog trigger; X-Watchdog-Secret required
  GET  /internal/watermark/{scan}  debug: last watermark for a scan
"""

from __future__ import annotations

import hashlib
import hmac
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

import failover

TIER_NAME = os.environ.get("TIER_NAME", "local")
WATCHDOG_SECRET = os.environ.get("WATCHDOG_SECRET", "")
TEMPORAL_ADDRESS = os.environ.get("TEMPORAL_ADDRESS", "localhost:7233")

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

    async def _run() -> dict:
        client = await Client.connect(TEMPORAL_ADDRESS)
        handle = await client.start_workflow(
            "ScanWorkflow",
            {"scan_name": scan_name},
            id=f"scan-{scan_name}-{TIER_NAME}-{int(datetime.now().timestamp())}",
            task_queue="equity-scans",
        )
        return await handle.result()

    return asyncio.run(_run())


@app.post("/internal/run-due-scans")
def run_due_scans(body: TriggerBody,
                  x_watchdog_secret: str | None = Header(default=None)):
    _check_secret(x_watchdog_secret)
    try:
        summary = failover.run_due_scans(
            tier=TIER_NAME, runner=_start_temporal_scan)
    except RuntimeError as exc:
        # e.g. DATABASE_URL missing -> fail closed, watchdog sees 500
        raise HTTPException(status_code=500, detail=str(exc))
    failover.audit(TIER_NAME, "watchdog_trigger",
                   {"by": body.triggered_by, "run_id": body.run_id,
                    "summary": summary})
    return summary
