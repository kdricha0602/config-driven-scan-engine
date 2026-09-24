"""
Worker: connects to the Temporal dev server and serves the scan engine.

    temporal server start-dev   # one-time, in another terminal
    pip install temporalio litellm
    export LLM_API_KEY=...      # key for whichever providers LiteLLM routes to
    python worker.py

Then start a scan from any client, e.g.:

    from temporalio.client import Client
    client = await Client.connect("localhost:7233")
    handle = await client.start_workflow(
        "ScanWorkflow",
        {"scan_config": yaml.safe_load(open("scan-configs.yaml"))["scans"][0]},
        id="scan-micro-cap-momentum-2026-09-19",
        task_queue="equity-scans",
    )
    report = await handle.result()

Watch it run at http://localhost:8233.
"""

import asyncio

from temporalio.client import Client
from temporalio.worker import Worker

from activities import (
    check_catalyst,
    compute_relative_strength,
    compute_technicals,
    contradict_thesis,
    enrich_consensus_evidence,
    fetch_float,
    fetch_forward_growth,
    fetch_fundamentals,
    fetch_market_data,
    fetch_news,
    fetch_quote,
    fetch_sec_filings,
    fetch_universe,
    fetch_valuation,
    interpret_catalyst,
    quality_control,
    synthesize,
)
from workflows import ResearchTickerWorkflow, ScanWorkflow, StockLookupWorkflow

TASK_QUEUE = "equity-scans"
TEMPORAL_ADDRESS = "localhost:7233"


async def main() -> None:
    client = await Client.connect(TEMPORAL_ADDRESS)
    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[ScanWorkflow, ResearchTickerWorkflow, StockLookupWorkflow],
        activities=[
            fetch_universe,
            fetch_market_data,
            fetch_quote,
            fetch_float,
            compute_technicals,
            fetch_fundamentals,
            fetch_sec_filings,
            fetch_news,
            compute_relative_strength,
            check_catalyst,
            fetch_valuation,
            fetch_forward_growth,
            enrich_consensus_evidence,
            interpret_catalyst,
            contradict_thesis,
            synthesize,
            quality_control,
        ],
    )
    print(f"Worker serving task queue '{TASK_QUEUE}' on {TEMPORAL_ADDRESS}")
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
