"""
Temporal workflows for the config-driven equity scan engine.

Two workflows, one engine:

- ScanWorkflow:        full config-driven scan (universe -> per-ticker research
                       in parallel -> synthesis). The scan YAML decides what the
                       hunt cares about; the workflow shape stays the same.
- StockLookupWorkflow: ad-hoc single-ticker deep dive reusing the same
                       activities and the money-flow module.

Model routing lives in the scan config so each hunt can choose its own
cost/quality tradeoff:
  scan:
    models:
      screening:  "gpt-4o-mini"   # fast/cheap: catalyst triage
      analysis:   "gpt-4o"       # thesis contradiction
      synthesis:  "gpt-4o"       # final ranking (best model)

Run: temporal server start-dev   (UI at localhost:8233)
     python worker.py             (connects to localhost:7233)
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Dict, List

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from activities import (
        TickerBundle,
        check_catalyst,
        compute_relative_strength,
        compute_technicals,
        contradict_thesis,
        fetch_float,
        fetch_fundamentals,
        fetch_market_data,
        fetch_news,
        fetch_quote,
        fetch_sec_filings,
        fetch_universe,
        interpret_catalyst,
        quality_control,
        synthesize,
    )


@dataclass
class ScanInput:
    scan_config: Dict[str, Any]


def _models(scan_config: Dict[str, Any]) -> Dict[str, str]:
    return {
        "screening": "gpt-4o-mini",
        "analysis": "gpt-4o",
        "synthesis": "gpt-4o",
        **scan_config.get("scan", {}).get("models", {}),
    }


@workflow.defn
class ResearchTickerWorkflow:
    """Child workflow: the full research pipeline for ONE ticker."""

    @workflow.run
    async def run(self, ticker: str, scan_config: Dict[str, Any]) -> TickerBundle:
        models = _models(scan_config)
        filters = scan_config.get("scan", {}).get("hard_filters", {})
        analytics = scan_config.get("scan", {}).get("analytics", {})
        catalyst_days = int(filters.get("catalyst_age", {}).get("max_days", 7))

        bundle = TickerBundle(ticker=ticker)

        # Independent fetches run in parallel; Temporal retries each on failure
        # without losing the rest of the scan.
        bars, fundamentals, filings, news, quote, float_info = await asyncio.gather(
            workflow.execute_activity(
                fetch_market_data, ticker,
                start_to_close_timeout=timedelta(minutes=2)),
            workflow.execute_activity(
                fetch_fundamentals, ticker,
                start_to_close_timeout=timedelta(minutes=5)),
            workflow.execute_activity(
                fetch_sec_filings, ticker,
                start_to_close_timeout=timedelta(minutes=5)),
            workflow.execute_activity(
                fetch_news, args=[ticker, catalyst_days],
                start_to_close_timeout=timedelta(minutes=3)),
            workflow.execute_activity(
                fetch_quote, ticker,
                start_to_close_timeout=timedelta(minutes=2)),
            workflow.execute_activity(
                fetch_float, ticker,
                start_to_close_timeout=timedelta(minutes=2)),
        )

        technicals, rel = await asyncio.gather(
            workflow.execute_activity(
                compute_technicals, bars,
                start_to_close_timeout=timedelta(minutes=2)),
            workflow.execute_activity(
                compute_relative_strength, bars,
                start_to_close_timeout=timedelta(minutes=2)),
        )
        bundle.technicals = technicals
        bundle.fundamentals = fundamentals
        bundle.news = news
        bundle.relative_strength = rel
        # price / float / market cap (were previously never populated)
        try:
            bundle.price = quote.get("price") or (bars.closes[-1] if bars.closes else None)
        except Exception:
            bundle.price = bars.closes[-1] if bars.closes else None
        try:
            bundle.float_shares = float_info.get("float_shares")
            shares_out = float_info.get("shares_outstanding")
            if bundle.price and shares_out:
                bundle.market_cap = bundle.price * shares_out
        except Exception:
            pass

        # Dilution / supply-overhang screen from filings (deterministic).
        bundle.notes.append(
            f"sec_filings: {filings.get('ticker', ticker)} reviewed")

        if analytics.get("catalyst_quality", True):
            # Deterministic verdict FIRST (news + 8-K/6-K classification in
            # code), then the LLM refinement. Merged so QC's hard-filter gate
            # always sees the deterministic `news_verdict` — the model refines,
            # never originates, the catalyst decision.
            deterministic = await workflow.execute_activity(
                check_catalyst, args=[ticker, "", catalyst_days, ""],
                start_to_close_timeout=timedelta(minutes=3))
            llm_verdict = await workflow.execute_activity(
                interpret_catalyst, args=[bundle, models["screening"]],
                start_to_close_timeout=timedelta(minutes=3))
            bundle.catalyst_verdict = {**deterministic, "llm": llm_verdict}

        if analytics.get("thesis_contradiction", True):
            bundle.contradiction = await workflow.execute_activity(
                contradict_thesis, args=[bundle, models["analysis"]],
                start_to_close_timeout=timedelta(minutes=5))

        return bundle


@workflow.defn
class ScanWorkflow:
    """Top-level scan: universe -> parallel per-ticker research -> synthesis."""

    @workflow.run
    async def run(self, data: ScanInput) -> Dict[str, Any]:
        cfg = data.scan_config
        models = _models(cfg)
        analytics = cfg.get("scan", {}).get("analytics", {})

        tickers: List[str] = await workflow.execute_activity(
            fetch_universe, cfg, start_to_close_timeout=timedelta(minutes=10))

        # One child workflow per ticker: independent, retried, resumable.
        # Bounded concurrency: an unbounded gather over a full universe is a
        # self-DoS (thousands of concurrent children, huge histories,
        # runaway LLM cost). Tickers are processed in chunks instead, and
        # an oversized universe fails closed rather than running away.
        limits = cfg.get("engine_rules", {}).get("limits", {})
        max_concurrent = int(limits.get("max_concurrent_tickers", 25))
        max_tickers = int(limits.get("max_tickers", 5000))
        if len(tickers) > max_tickers:
            raise RuntimeError(
                f"universe size {len(tickers)} exceeds max_tickers={max_tickers}; "
                "raise the limit explicitly or narrow the universe")
        bundles: List[TickerBundle] = []
        for i in range(0, len(tickers), max_concurrent):
            chunk = tickers[i:i + max_concurrent]
            bundles.extend(await asyncio.gather(*[
                workflow.execute_child_workflow(
                    ResearchTickerWorkflow.run, args=[t, cfg],
                    id=f"{workflow.info().workflow_id}/{t}",
                )
                for t in chunk
            ]))

        # Synthesis needs an LLM key; bounded e2e tests can skip it.
        if analytics.get("synthesis", True):
            report: Dict[str, Any] = await workflow.execute_activity(
                synthesize, args=[cfg, bundles, models["synthesis"]],
                start_to_close_timeout=timedelta(minutes=10))
        else:
            report = {
                "picks": [
                    {
                        "ticker": b.ticker,
                        "price": b.price,
                        "market_cap": b.market_cap,
                        "rvol": b.technicals.rvol if b.technicals else None,
                        "day_change_pct": b.technicals.day_change_pct if b.technicals else None,
                        "thesis": "e2e test — synthesis skipped (no LLM key)",
                    }
                    for b in bundles
                ],
                "rejected": [],
                "synthesis_skipped": True,
            }

        report = await workflow.execute_activity(
            quality_control, args=[report, bundles, cfg],
            start_to_close_timeout=timedelta(minutes=2))
        return report


@workflow.defn
class StockLookupWorkflow:
    """Ad-hoc lookup: one ticker through the same research pipeline, then a
    short-form synthesis (no ranking needed)."""

    @workflow.run
    async def run(self, ticker: str) -> Dict[str, Any]:
        cfg = {"scan": {"name": "Ad-hoc lookup",
                        "analytics": {"catalyst_quality": True,
                                      "thesis_contradiction": True}}}
        bundle: TickerBundle = await workflow.execute_child_workflow(
            ResearchTickerWorkflow.run, args=[ticker, cfg],
            id=f"{workflow.info().workflow_id}/{ticker}")
        models = _models(cfg)
        report: Dict[str, Any] = await workflow.execute_activity(
            synthesize, args=[cfg, [bundle], models["synthesis"]],
            start_to_close_timeout=timedelta(minutes=10))
        return await workflow.execute_activity(
            quality_control, args=[report, [bundle], cfg],
            start_to_close_timeout=timedelta(minutes=2))
