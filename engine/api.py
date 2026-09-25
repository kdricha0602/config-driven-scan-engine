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


import os
import hmac
import hashlib
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import yaml
from fastapi import FastAPI, Header, HTTPException, BackgroundTasks
from pydantic import BaseModel
from temporalio.client import Client

# --- Mock imports / placeholders assumed from your environment ---
# import failover
# TIER_NAME = "prod-scan-tier"
# WATCHDOG_SECRET = os.environ.get("WATCHDOG_SECRET")
# TEMPORAL_ADDRESS = os.environ.get("TEMPORAL_ADDRESS", "localhost:7233")
# -----------------------------------------------------------------

SCHEDULE_TO_SCAN = {
    "premarket-7am": "Micro Cap Momentum",
    "weekday-equity-scan": "Large Cap Compounders",
    "hourly-watch": "Micro Cap Momentum",
    "eod-scan": "Large Cap Compounders",
    "midcap-pipeline": "Small Cap 4x Growth",
}

# Pre-calculate the secret hash once to protect against timing attacks efficiently
SECRET_HASH = (
    hashlib.sha256(WATCHDOG_SECRET.encode()).hexdigest() 
    if WATCHDOG_SECRET else None
)


def _configs_path() -> Path:
    """Locate scan-configs.yaml using env overrides or deterministic file anchors."""
    env = os.environ.get("SCAN_CONFIGS_PATH")
    if env:
        return Path(env)
    
    # Anchor to the directory containing this source file explicitly
    here = Path(__file__).resolve().parent 
    candidates = [
        here.parent / "scan-configs.yaml",
        here / "scan-configs.yaml",
        Path.cwd() / "scan-configs.yaml"
    ]
    for cand in candidates:
        if cand.exists():
            return cand
    raise RuntimeError("scan-configs.yaml configuration file not found")


def _load_scan_config(scan_name: str) -> dict:
    """Resolve a schedule name to its full scan config dict mapping."""
    want = SCHEDULE_TO_SCAN.get(scan_name)
    if not want:
        raise RuntimeError(f"No scan configured for schedule '{scan_name}'")
        
    with open(_configs_path(), "r", encoding="utf-8") as fh:
        doc = yaml.safe_load(fh) or {}
        
    for entry in doc.get("scans", []):
        scan = entry.get("scan", {})
        if scan.get("name") == want:
            return {"engine_rules": doc.get("engine_rules", {}), "scan": scan}
            
    raise RuntimeError(f"Scan target '{want}' not found in scan-configs.yaml")


class TriggerBody(BaseModel):
    triggered_by: str = "unknown"
    run_id: str = "n/a"


def _check_secret(provided: str | None) -> None:
    """Enforce a fail-closed verification loop via a secure timing-safe comparison."""
    if not SECRET_HASH:
        raise HTTPException(status_code=503, detail="Watchdog auth not configured globally")
    if not provided:
        raise HTTPException(status_code=403, detail="Forbidden: Missing authorization header")
        
    provided_hash = hashlib.sha256(provided.encode()).hexdigest()
    if not hmac.compare_digest(provided_hash, SECRET_HASH):
        raise HTTPException(status_code=403, detail="Forbidden: Invalid credentials")


# --- Lifespan Management for Persistent Connections ---
app = FastAPI(title="scan-tier", version="1.0.0")
temporal_client: Client | None = None

@app.on_event("startup")
async def startup_event():
    """Establish stateful, shared client connections when the API server boots up."""
    global temporal_client
    try:
        temporal_client = await Client.connect(TEMPORAL_ADDRESS)
    except Exception as e:
        print(f"CRITICAL: Failed to connect to Temporal Server at {TEMPORAL_ADDRESS}: {e}")


async def _start_temporal_scan(scan_name: str) -> dict:
    """Dispatches workflow tracking via the shared persistent client loop."""
    if not temporal_client:
        raise RuntimeError("Temporal client has not been initialized.")
        
    cfg = _load_scan_config(scan_name)
    run_timestamp = int(datetime.now().timestamp())
    
    handle = await temporal_client.start_workflow(
        "ScanWorkflow",
        {"scan_config": cfg},
        id=f"scan-{scan_name}-{TIER_NAME}-{run_timestamp}",
        task_queue="equity-scans",
    )
    return await handle.result()


@app.get("/health")
def health():
    return {
        "status": "ok", 
        "tier": TIER_NAME,
        "ts": datetime.now(ZoneInfo("America/Chicago")).isoformat()
    }


@app.get("/ready")
def ready():
    try:
        with failover.get_conn() as conn:
            conn.execute("SELECT 1")
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Database unreachable: {exc}")
    return {"status": "ready", "tier": TIER_NAME}


@app.get("/internal/watermark/{scan_name}")
def watermark(scan_name: str, x_watchdog_secret: str | None = Header(default=None)):
    _check_secret(x_watchdog_secret)
    wm = failover.get_watermark(scan_name)
    if wm is None:
        raise HTTPException(status_code=404, detail="No watermark generated yet")
    return wm


@app.post("/internal/run-due-scans")
def run_due_scans(
    body: TriggerBody, 
    background_tasks: BackgroundTasks, 
    x_watchdog_secret: str | None = Header(default=None)
):
    _check_secret(x_watchdog_secret)
    if not failover.DATABASE_URL:
        raise HTTPException(
            status_code=500,
            detail="DATABASE_URL not set — refusing execution without transactional safety keys."
        )
        
    # Standardize on FastAPI's elegant BackgroundTasks rather than raw daemon threads
    background_tasks.add_task(_run_due_scans_bg, body)
    
    return {
        "accepted": True, 
        "tier": TIER_NAME,
        "triggered_by": body.triggered_by, 
        "run_id": body.run_id
    }


def _run_due_scans_bg(body: TriggerBody) -> None:
    """Background worker execution layer. 
    
    Claims advisory leadership via Postgres locks, runs all due configurations,
    and cleanly streams logs straight into analytics database schemas.
    """
    print(f"BACKGROUND WORKER DISPATCHED: {body.run_id}", flush=True)

    try:
        # Pass sync-adapted wrapper if failover requires blocking calls,
        # or execute your async scanner natively.
        summary = failover.run_due_scans(tier=TIER_NAME, runner=_start_temporal_scan)
        
        failover.audit(TIER_NAME, "watchdog_trigger", {
            "by": body.triggered_by, 
            "run_id": body.run_id,
            "summary": summary
        })
    except Exception as exc:
        failover.audit(TIER_NAME, "watchdog_trigger_failed", {
            "by": body.triggered_by, 
            "run_id": body.run_id,
            "error": str(exc)[:500]
        })
