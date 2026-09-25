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

import asyncio
import hashlib
import hmac
import os
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException
from pydantic import BaseModel
from temporalio.client import Client

import failover


# ---------------------------------------------------------------------------
# Environment / deployment configuration
# ---------------------------------------------------------------------------

TIER_NAME = os.environ.get("TIER_NAME", "local")
WATCHDOG_SECRET = os.environ.get("WATCHDOG_SECRET")
TEMPORAL_ADDRESS = os.environ.get("TEMPORAL_ADDRESS", "localhost:7233")


# ---------------------------------------------------------------------------
# Schedule → scan mapping
# ---------------------------------------------------------------------------

SCHEDULE_TO_SCAN = {
    "premarket-7am": "Micro Cap Momentum",
    "weekday-equity-scan": "Large Cap Compounders",
    "hourly-watch": "Micro Cap Momentum",
    "eod-scan": "Large Cap Compounders",
    "midcap-pipeline": "Small Cap 4x Growth",
}


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

SECRET_HASH = (
    hashlib.sha256(WATCHDOG_SECRET.encode()).hexdigest()
    if WATCHDOG_SECRET
    else None
)


def _check_secret(provided: str | None) -> None:
    """Fail-closed timing-safe watchdog authentication."""

    if not SECRET_HASH:
        raise HTTPException(
            status_code=503,
            detail="Watchdog auth not configured globally",
        )

    if not provided:
        raise HTTPException(
            status_code=403,
            detail="Forbidden: Missing authorization header",
        )

    provided_hash = hashlib.sha256(provided.encode()).hexdigest()

    if not hmac.compare_digest(provided_hash, SECRET_HASH):
        raise HTTPException(
            status_code=403,
            detail="Forbidden: Invalid credentials",
        )


# ---------------------------------------------------------------------------
# Scan configuration
# ---------------------------------------------------------------------------

def _configs_path() -> Path:
    """Locate scan-configs.yaml using environment override or known anchors."""

    env = os.environ.get("SCAN_CONFIGS_PATH")

    if env:
        return Path(env)

    here = Path(__file__).resolve().parent

    candidates = [
        here.parent / "scan-configs.yaml",
        here / "scan-configs.yaml",
        Path.cwd() / "scan-configs.yaml",
    ]

    for candidate in candidates:
        if candidate.exists():
            return candidate

    raise RuntimeError(
        "scan-configs.yaml configuration file not found"
    )


def _load_scan_config(scan_name: str) -> dict:
    """Resolve a schedule name to its complete scan configuration."""

    target_scan = SCHEDULE_TO_SCAN.get(scan_name)

    if not target_scan:
        raise RuntimeError(
            f"No scan configured for schedule '{scan_name}'"
        )

    config_path = _configs_path()

    with open(config_path, "r", encoding="utf-8") as fh:
        document = yaml.safe_load(fh) or {}

    for entry in document.get("scans", []):
        scan = entry.get("scan", {})

        if scan.get("name") == target_scan:
            return {
                "engine_rules": document.get("engine_rules", {}),
                "scan": scan,
            }

    raise RuntimeError(
        f"Scan target '{target_scan}' not found in {config_path}"
    )


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class TriggerBody(BaseModel):
    triggered_by: str = "unknown"
    run_id: str = "n/a"


# ---------------------------------------------------------------------------
# FastAPI application state
# ---------------------------------------------------------------------------

app = FastAPI(
    title="scan-tier",
    version="1.0.0",
)

temporal_client: Client | None = None
main_event_loop: asyncio.AbstractEventLoop | None = None


# ---------------------------------------------------------------------------
# Startup / shutdown
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def startup_event():
    """
    Establish the shared Temporal client and retain the FastAPI event loop.

    The event loop reference allows synchronous failover workers to safely
    submit Temporal coroutines back onto this loop.
    """

    global temporal_client
    global main_event_loop

    main_event_loop = asyncio.get_running_loop()

    try:
        temporal_client = await Client.connect(TEMPORAL_ADDRESS)

        print(
            f"CONNECTED TO TEMPORAL: {TEMPORAL_ADDRESS}",
            flush=True,
        )

    except Exception as exc:
        temporal_client = None

        print(
            f"CRITICAL: Failed to connect to Temporal Server "
            f"at {TEMPORAL_ADDRESS}: {exc}",
            flush=True,
        )


# ---------------------------------------------------------------------------
# Temporal execution
# ---------------------------------------------------------------------------

async def _start_temporal_scan(scan_name: str) -> dict:
    """Start one ScanWorkflow through the shared Temporal client."""

    if temporal_client is None:
        raise RuntimeError(
            "Temporal client has not been initialized."
        )

    config = _load_scan_config(scan_name)

    # Use microseconds to substantially reduce workflow-ID collisions.
    run_id = datetime.now(ZoneInfo("America/Chicago")).strftime(
        "%Y%m%d-%H%M%S-%f"
    )

    workflow_id = (
        f"scan-{scan_name}-{TIER_NAME}-{run_id}"
    )

    handle = await temporal_client.start_workflow(
        "ScanWorkflow",
        {
            "scan_config": config,
        },
        id=workflow_id,
        task_queue="equity-scans",
    )

    result = await handle.result()

    return result


def _run_temporal_scan_from_sync(scan_name: str) -> dict:
    """
    Synchronous adapter used by failover.run_due_scans().

    failover.py is intentionally synchronous. This function safely submits
    the async Temporal operation back to FastAPI's existing event loop.
    """

    if main_event_loop is None:
        raise RuntimeError(
            "FastAPI event loop has not been initialized."
        )

    if temporal_client is None:
        raise RuntimeError(
            "Temporal client is not connected."
        )

    future = asyncio.run_coroutine_threadsafe(
        _start_temporal_scan(scan_name),
        main_event_loop,
    )

    return future.result()


# ---------------------------------------------------------------------------
# Health endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {
        "status": "ok",
        "tier": TIER_NAME,
        "temporal_connected": temporal_client is not None,
        "ts": datetime.now(
            ZoneInfo("America/Chicago")
        ).isoformat(),
    }


@app.get("/ready")
def ready():
    # Database readiness
    try:
        with failover.get_conn() as conn:
            conn.execute("SELECT 1")
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Database unreachable: {exc}",
        )

    # Temporal readiness
    if temporal_client is None:
        raise HTTPException(
            status_code=503,
            detail="Temporal client is not connected",
        )

    return {
        "status": "ready",
        "tier": TIER_NAME,
        "temporal": "connected",
        "database": "connected",
    }


# ---------------------------------------------------------------------------
# Watermark endpoint
# ---------------------------------------------------------------------------

@app.get("/internal/watermark/{scan_name}")
def watermark(
    scan_name: str,
    x_watchdog_secret: str | None = Header(default=None),
):
    _check_secret(x_watchdog_secret)

    wm = failover.get_watermark(scan_name)

    if wm is None:
        raise HTTPException(
            status_code=404,
            detail="No watermark generated yet",
        )

    return wm


# ---------------------------------------------------------------------------
# Due-scan execution
# ---------------------------------------------------------------------------

def _run_due_scans_bg(body: TriggerBody) -> None:
    """
    Execute failover's synchronous scheduling/leadership layer in a worker
    thread while safely bridging Temporal back to FastAPI's event loop.
    """

    print(
        f"BACKGROUND WORKER DISPATCHED: {body.run_id}",
        flush=True,
    )

    try:
        summary = failover.run_due_scans(
            tier=TIER_NAME,
            runner=_run_temporal_scan_from_sync,
        )

        failover.audit(
            TIER_NAME,
            "watchdog_trigger",
            {
                "by": body.triggered_by,
                "run_id": body.run_id,
                "summary": summary,
            },
        )

        print(
            f"BACKGROUND WORKER COMPLETED: {body.run_id}",
            flush=True,
        )

    except Exception as exc:

        print(
            f"BACKGROUND WORKER FAILED: {body.run_id}: {exc}",
            flush=True,
        )

        failover.audit(
            TIER_NAME,
            "watchdog_trigger_failed",
            {
                "by": body.triggered_by,
                "run_id": body.run_id,
                "error": str(exc)[:500],
            },
        )


@app.post("/internal/run-due-scans")
def run_due_scans(
    body: TriggerBody,
    background_tasks: BackgroundTasks,
    x_watchdog_secret: str | None = Header(default=None),
):
    _check_secret(x_watchdog_secret)

    if not failover.DATABASE_URL:
        raise HTTPException(
            status_code=500,
            detail=(
                "DATABASE_URL not set — refusing execution "
                "without transactional safety keys."
            ),
        )

    if temporal_client is None:
        raise HTTPException(
            status_code=503,
            detail="Temporal client is not connected",
        )

    background_tasks.add_task(
        _run_due_scans_bg,
        body,
    )

    return {
        "accepted": True,
        "tier": TIER_NAME,
        "triggered_by": body.triggered_by,
        "run_id": body.run_id,
    }