"""
Temporal activities for the config-driven equity scan engine.

Each activity does ONE thing against the outside world (API call, calculation,
or LLM call). The workflow in workflows.py orchestrates them; nothing here
knows about scan configs beyond its own inputs.

Data providers are injected, not hardcoded: every market-data/SEC/news activity
takes its source from the activity environment so the engine can swap providers
without touching workflow code.

Install: pip install temporalio litellm
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from temporalio import activity

from mcdx import latest as mcdx_latest

from security import (
    audit_log,
    enforce_prompt_budget,
    redact_secrets,
    scan_output_for_secrets,
    scan_untrusted_text,
    validate_tool_args,
)


# ---------------------------------------------------------------------------
# LLM key resolution
# ---------------------------------------------------------------------------

def _vault_surrogate(connector: str) -> str | None:
    """Fetch a fresh authd surrogate for a connected credential.

    The Secure Vault holds the real key; authd hands us an ``hsurr:*``
    reference and the egress layer swaps it for the real key on approved
    outbound calls to that connector's hosts. The surrogate is never
    logged or persisted. Falls back to None when authd is unreachable
    (then env vars below apply)."""
    try:
        import sys
        helper_dir = "/opt/hatch/skills/skill-creator/bin"
        if helper_dir not in sys.path:
            sys.path.insert(0, helper_dir)
        from dynamic_credentials import dynamic_credential_entry
        return dynamic_credential_entry(connector)["surrogate"]
    except Exception:
        return None


def _openai_surrogate() -> str | None:
    return _vault_surrogate("custom.openai")


def _llm_key_for(model: str) -> str | None:
    """Provider-aware API key for a LiteLLM model string.

    Resolution per model family:
      OpenAI models -> Secure Vault surrogate (authd), then OPENAI_API_KEY, then LLM_API_KEY
      Groq          -> Secure Vault surrogate (authd), then GROQ_API_KEY, then LLM_API_KEY
      Gemini        -> Secure Vault surrogate (authd), then GEMINI_API_KEY/GOOGLE_API_KEY, then LLM_API_KEY
      NVIDIA NIM    -> Secure Vault surrogate (authd), then NVIDIA_API_KEY, then LLM_API_KEY
      Anthropic     -> ANTHROPIC_API_KEY, then LLM_API_KEY
      anything else -> LLM_API_KEY
    Returns None when nothing is set; LiteLLM then raises a clean auth
    error naming the missing key. Raw keys are never written anywhere —
    the Vault path uses a surrogate reference, env fallbacks arrive via
    environment (Secure Vault -> runtime injection).
    """
    m = (model or "").lower()
    if m.startswith(("gpt-", "gpt4", "gpt-4", "o1", "o3", "o4", "openai/")):
        return _openai_surrogate() or os.environ.get("OPENAI_API_KEY") or os.environ.get("LLM_API_KEY")
    if m.startswith("groq/"):
        return _vault_surrogate("custom.groq") or os.environ.get("GROQ_API_KEY") or os.environ.get("LLM_API_KEY")
    if m.startswith("gemini/"):
        return (_vault_surrogate("custom.google-ai")
                or os.environ.get("GEMINI_API_KEY")
                or os.environ.get("GOOGLE_API_KEY")
                or os.environ.get("LLM_API_KEY"))
    if m.startswith("nvidia_nim/"):
        return (_vault_surrogate("custom.nvidia")
                or os.environ.get("NVIDIA_API_KEY")
                or os.environ.get("LLM_API_KEY"))
    if "claude" in m or m.startswith("anthropic/"):
        return os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("LLM_API_KEY")
    return os.environ.get("LLM_API_KEY")


# ---------------------------------------------------------------------------
# Provider fallback chain
#
# OpenAI is primary. When it fails (no credits, rate limit, outage), the
# engine falls through to the free-tier backups in order: Groq, NVIDIA NIM
# (gpt-oss-20b), then Gemini. Fallbacks engage per call — screening,
# analysis, and synthesis each get their own chain matched to the primary
# model's cost tier.
# NOTE 2026-09-20: Groq decommissioned llama-3.1-8b-instant and
# llama-3.3-70b-versatile on 2026-08-16. Groq hops now use openai/gpt-oss-20b
# (screening, ~1000 tok/s) and openai/gpt-oss-120b (analysis).
# ---------------------------------------------------------------------------

_FALLBACK_MODELS: Dict[str, List[str]] = {
    # screening tier (cheap/fast primary)
    # NOTE 2026-09-20: gemini-3.1-flash-lite is tried before gemini-3.7-flash.
    # flash-lite is proven on free tier for forced function calls and is the
    # cheaper hop; 3.7-flash is flaky there (mostly 503s, and in one serving
    # window it rejected the tool schema with a 400 while flash-lite accepted
    # the identical schema).
    "gpt-4o-mini": ["groq/openai/gpt-oss-20b", "nvidia_nim/openai/gpt-oss-20b", "gemini/gemini-3.1-flash-lite", "gemini/gemini-3.7-flash"],
    # analysis/synthesis tier (best primary)
    "gpt-4o": ["groq/openai/gpt-oss-120b", "nvidia_nim/openai/gpt-oss-20b", "gemini/gemini-3.1-flash-lite", "gemini/gemini-3.7-flash"],
}


def _fallbacks_for(model: str) -> List[str]:
    """Fallback models for a primary, [] when the model has no chain."""
    m = (model or "").lower()
    if m.startswith("openai/"):
        m = m[len("openai/"):]
    return list(_FALLBACK_MODELS.get(m, []))


def _is_provider_failure(exc: Exception) -> bool:
    """True when the error looks like the provider failed us (auth, quota,
    billing, rate limit, outage, timeout) rather than our request being
    malformed. Only provider failures trigger a fallback hop.

    Structured signals (exception type, status_code) are checked first;
    message sniffing is a last resort and never matches bare numbers like
    "500" — those appear in unrelated text and used to misclassify real
    bugs as provider outages, silently falling through to weaker models.
    """
    import litellm
    provider_errors = (
        litellm.AuthenticationError,
        litellm.RateLimitError,
        litellm.APIConnectionError,
        litellm.ServiceUnavailableError,
        litellm.Timeout,
        litellm.InternalServerError,
    )
    if isinstance(exc, provider_errors):
        return True
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and status in (
            401, 402, 403, 408, 429, 500, 502, 503, 504):
        return True
    msg = str(exc).lower()
    if any(s in msg for s in (
        "credit", "quota", "billing", "insufficient",
        "rate limit", "rate_limit", "overloaded",
        "temporarily unavailable", "timed out", "timeout",
        "connection error", "connection reset",
        "invalid_api_key", "invalid api key", "unauthorized",
        "unauthenticated",
    )):
        return True
    # Narrow exception: a Gemini model version rejecting the tool schema
    # ("Invalid JSON payload ... function_declarations") while a sibling
    # model accepts the identical schema is model-side strictness, not a
    # malformed request — the chain should continue, not abort.
    return "invalid json payload" in msg and "function_declarations" in msg


async def _acompletion_resilient(model: str, messages: List[Dict[str, str]],
                                 _kwargs_for=None, **kwargs):
    """litellm.acompletion with provider fallback.

    Tries the primary model, then each fallback in order, stopping at the
    first success. Returns (response, model_used). Raises the last error
    when every provider fails. A hop is logged to the worker log (model
    names only — never keys).

    `_kwargs_for`, when given, builds the per-attempt kwargs from the
    model actually being called (e.g. to adjust the tool schema per
    provider); otherwise `kwargs` are used for every attempt.
    """
    import litellm
    last_exc: Exception | None = None
    for attempt in [model] + _fallbacks_for(model):
        try:
            attempt_kwargs = _kwargs_for(attempt) if _kwargs_for else kwargs
            resp = await litellm.acompletion(
                model=attempt,
                messages=messages,
                api_key=_llm_key_for(attempt),
                **attempt_kwargs,
            )
            if attempt != model:
                print(f"[llm] fallback engaged: {model} -> {attempt}")
            return resp, attempt
        except Exception as exc:  # noqa: BLE001 - chain must survive any provider error
            last_exc = exc
            if not _is_provider_failure(exc):
                raise
            print(f"[llm] provider failure on {attempt}: {type(exc).__name__}")
    assert last_exc is not None
    raise last_exc


def _tool_for(model: str, tool: Dict[str, Any]) -> Dict[str, Any]:
    """Provider-adjusted tool definition.

    Strict mode is OpenAI-only; other providers get the same schema
    without it (forced tool_choice still applies)."""
    m = (model or "").lower()
    if m.startswith(("gpt-", "gpt4", "gpt-4", "o1", "o3", "o4", "openai/")):
        return tool
    adjusted = {"type": tool.get("type", "function"),
                "function": dict(tool.get("function", {}))}
    adjusted["function"].pop("strict", None)
    return adjusted


# ---------------------------------------------------------------------------
# Shared shapes
# ---------------------------------------------------------------------------

@dataclass
class Bars:
    ticker: str
    closes: List[float]
    highs: List[float]
    lows: List[float]
    volumes: List[float]
    dates: List[str] = field(default_factory=list)


@dataclass
class Technicals:
    ticker: str
    rsi14: Optional[float] = None
    macd: Optional[float] = None
    macd_signal: Optional[float] = None
    sma20: Optional[float] = None
    sma50: Optional[float] = None
    sma200: Optional[float] = None
    atr14: Optional[float] = None
    rvol: Optional[float] = None
    day_change_pct: Optional[float] = None
    volume: Optional[float] = None
    avg_volume_20: Optional[float] = None
    # MCDX money-flow block (values + fired signals on the latest bar)
    money_flow: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Fundamentals:
    ticker: str
    revenue_ttm: Optional[float] = None
    revenue_cagr_2y: Optional[float] = None
    revenue_yoy_1y: Optional[float] = None
    eps_cagr_2y: Optional[float] = None
    eps_cagr_2y_split_adj: Optional[float] = None
    eps_yoy_1y_split_adj: Optional[float] = None
    # Gate EPS-growth value: forward analyst estimates when available,
    # else TTM YoY (split-adjusted). The source is always recorded —
    # never mix the two silently.
    eps_growth_1y: Optional[float] = None
    eps_growth_source: str = ""
    eps_growth_analysts: int = 0
    current_ratio: Optional[float] = None
    debt_equity: Optional[float] = None
    gross_margin_latest: Optional[float] = None
    gross_margin_expanding: Optional[bool] = None
    eps_ttm: Optional[float] = None
    fcf_ttm: Optional[float] = None
    fcf_margin: Optional[float] = None
    fcf_positive_years_4: Optional[int] = None
    roic: Optional[float] = None
    roic_incremental_2y: Optional[float] = None
    net_debt_ebitda: Optional[float] = None
    ebit_interest: Optional[float] = None
    ebitda_latest: Optional[float] = None
    ebitda_ttm: Optional[float] = None
    net_debt_latest: Optional[float] = None
    debt_latest: Optional[float] = None
    cash_latest: Optional[float] = None
    sbc_annual: Optional[float] = None
    buybacks_annual: Optional[float] = None
    shares_dilution_2y_pct: Optional[float] = None
    fiscal_years: List[int] = field(default_factory=list)
    revenue_history: List[List[float]] = field(default_factory=list)
    eps_history: List[List[float]] = field(default_factory=list)
    fcf_history: List[List[float]] = field(default_factory=list)


@dataclass
class Valuation:
    """Gate 7 (forward, via Bigdata) + gate 8 (trailing multiples)."""
    ticker: str
    fwd_pe: Optional[float] = None
    ntm_eps: Optional[float] = None
    fwd_growth_pct: Optional[float] = None
    peg: Optional[float] = None
    peer_median_fwd_pe: Optional[float] = None
    discount_vs_peers_pct: Optional[float] = None
    peers_used: Optional[int] = None
    gate7_pass: Optional[bool] = None
    ev_ebitda_ttm: Optional[float] = None
    fcf_yield_ttm: Optional[float] = None
    error: str = ""


@dataclass
class NewsItem:
    headline: str
    source: str
    published_at: str
    url: str = ""


@dataclass
class TickerBundle:
    """Everything the workflow gathered for one ticker."""
    ticker: str
    price: Optional[float] = None
    market_cap: Optional[float] = None
    float_shares: Optional[float] = None
    technicals: Optional[Technicals] = None
    fundamentals: Optional[Fundamentals] = None
    news: List[NewsItem] = field(default_factory=list)
    catalyst_verdict: Dict[str, Any] = field(default_factory=dict)
    relative_strength: Dict[str, Any] = field(default_factory=dict)
    valuation: Optional[Valuation] = None
    contradiction: str = ""
    notes: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Provider hooks (swap these for real integrations)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Provider registry (free, public, no API keys)
# ---------------------------------------------------------------------------

def _provider(name: str):
    """Resolve a data-provider module. Imports are lazy so this module stays
    import-light inside Temporal's workflow sandbox (providers only run in
    activity workers).

    Wired:  market_data      -> providers.yahoo (daily bars + quotes)
            screener         -> providers.nasdaq_screener (Nasdaq universe)
            sec_fundamentals -> providers.sec_edgar (facts -> gate ratios)
            sec_filings      -> providers.sec_edgar (filings + going concern)
            news             -> providers.news (RSS headlines + 8-K items)
            benchmarks       -> providers.benchmarks (index RS, sector snap)
            analyst_estimates-> providers.bigdata (gate 7 fwd P/E + PEG)
            literature       -> providers.consensus (evidence for/against
                                the working claim; config-gated per scan)
    Open:   (none — all providers wired; LiteLLM models still unconfigured)
    """
    if name == "market_data":
        from providers import yahoo
        return yahoo
    if name == "screener":
        from providers import nasdaq_screener
        return nasdaq_screener
    if name in ("sec_fundamentals", "sec_filings"):
        from providers import sec_edgar
        return sec_edgar
    if name == "news":
        from providers import news
        return news
    if name == "benchmarks":
        from providers import benchmarks
        return benchmarks
    raise NotImplementedError(
        f"No provider registered for '{name}'. Wire a real client before "
        "running the worker — the engine never invents market data."
    )


def _parse_money(value) -> float | None:
    """'30M' -> 30_000_000, '1B' -> 1_000_000_000, '$5M' -> 5_000_000."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().upper().replace("$", "").replace(",", "")
    mult = 1.0
    if text.endswith("B"):
        mult, text = 1e9, text[:-1]
    elif text.endswith("M"):
        mult, text = 1e6, text[:-1]
    elif text.endswith("K"):
        mult, text = 1e3, text[:-1]
    try:
        return float(text) * mult
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Activities
# ---------------------------------------------------------------------------

@activity.defn
async def fetch_universe(scan_config: Dict[str, Any]) -> List[str]:
    """Screen the ticker universe down to names passing hard filters that can
    be evaluated cheaply (price, volume, market cap). RVOL / daily-move need
    bars, so those filter at the per-ticker stage."""
    screener = _provider("screener")
    scan = scan_config.get("scan", {})
    uni = scan.get("universe", {})
    hf = scan.get("hard_filters", {})
    price = hf.get("price", {})
    mcap = uni.get("market_cap", {})
    rows = screener.screen(
        min_price=price.get("min"),
        max_price=price.get("max"),
        min_volume=hf.get("volume", {}).get("min"),
        min_market_cap=_parse_money(mcap.get("min")),
        max_market_cap=_parse_money(mcap.get("max")),
    )
    tickers = [r["symbol"] for r in rows if r.get("symbol")]
    # max_candidates: trim to the top-N by the screener's own ordering
    # (Nasdaq screener sorts by volume — a liquidity ranking, not a
    # market-cap filter). Used by wide universes like Small Cap 4x Growth.
    max_candidates = uni.get("max_candidates")
    if max_candidates:
        try:
            tickers = tickers[:int(max_candidates)]
        except (ValueError, TypeError):
            pass
    # test_limit: bound the universe for e2e tests (e.g. scan.test_limit: 2)
    test_limit = scan.get("test_limit")
    if test_limit:
        try:
            tickers = tickers[:int(test_limit)]
        except (ValueError, TypeError):
            pass
    return tickers


@activity.defn
async def fetch_quote(ticker: str) -> Dict[str, Any]:
    """Latest price/quote for one ticker (Yahoo)."""
    md = _provider("market_data")
    return md.quote(ticker)


@activity.defn
async def fetch_float(ticker: str) -> Dict[str, Any]:
    """True float / shares outstanding for one ticker."""
    from providers import float as _float
    return _float.float_stats(ticker)


@activity.defn
async def fetch_market_data(ticker: str) -> Bars:
    """Daily OHLCV bars (~2y, adjusted closes) for one ticker."""
    md = _provider("market_data")
    d = md.daily_bars(ticker)
    return Bars(ticker=d["ticker"], closes=d["closes"], highs=d["highs"],
                lows=d["lows"], volumes=d["volumes"],
                dates=d.get("dates", []))


@activity.defn
async def compute_technicals(bars: Bars) -> Technicals:
    """Deterministic technicals. All math in code — the LLM never calculates.

    Includes the MCDX money-flow block: ported LOKEN v2.2 signals computed on
    the close series, latest-bar values plus which signals fired. Classic
    indicators (RSI/MACD/SMA/ATR/RVOL) come from providers.technicals.
    """
    from providers import technicals as _tech
    t = Technicals(ticker=bars.ticker)
    bars_d = {"ticker": bars.ticker, "closes": bars.closes, "highs": bars.highs,
              "lows": bars.lows, "volumes": bars.volumes, "dates": bars.dates}
    if len(bars.closes) >= 60:
        t.money_flow = mcdx_latest(bars.closes)
    if len(bars.closes) >= 20:
        s = _tech.summarize(bars_d)
        t.rsi14 = s["rsi14"]
        t.macd = s["macd_line"]
        t.macd_signal = s["macd_signal"]
        t.sma20 = s["sma20"]
        t.sma50 = s["sma50"]
        t.sma200 = s["sma200"]
        t.atr14 = s["atr14"]
        t.rvol = s["rvol"]
        t.day_change_pct = s["day_change_pct"]
        t.volume = s["volume"]
        t.avg_volume_20 = s["avg_volume_20"]
    return t


@activity.defn
async def fetch_fundamentals(ticker: str) -> Fundamentals:
    """TTM + 2y-growth fundamentals from SEC EDGAR companyfacts (anchored to
    10-K fiscal years; ratios winsorized per AGENTS.md lessons)."""
    sec = _provider("sec_fundamentals")
    d = sec.fundamentals(ticker)
    return Fundamentals(
        ticker=d["ticker"],
        revenue_ttm=d["revenue_ttm"],
        revenue_cagr_2y=d["revenue_cagr_2y"],
        revenue_yoy_1y=d.get("revenue_yoy_1y"),
        eps_cagr_2y=d["eps_cagr_2y"],
        eps_cagr_2y_split_adj=d.get("eps_cagr_2y_split_adj"),
        eps_yoy_1y_split_adj=d.get("eps_yoy_1y_split_adj"),
        eps_growth_1y=d.get("eps_yoy_1y_split_adj"),
        eps_growth_source=("ttm_yoy"
                           if d.get("eps_yoy_1y_split_adj") is not None
                           else ""),
        current_ratio=d.get("current_ratio_latest"),
        debt_equity=d.get("debt_equity_latest"),
        gross_margin_latest=d.get("gross_margin_latest"),
        gross_margin_expanding=d.get("gross_margin_expanding"),
        eps_ttm=d["eps_ttm"],
        fcf_ttm=d["fcf_ttm"],
        fcf_margin=d["fcf_margin"],
        fcf_positive_years_4=d.get("fcf_positive_years_4"),
        roic=d["roic"],
        roic_incremental_2y=d.get("roic_incremental_2y"),
        net_debt_ebitda=d["net_debt_ebitda"],
        ebit_interest=d["ebit_interest"],
        ebitda_latest=d["ebitda_latest"],
        ebitda_ttm=d.get("ebitda_ttm"),
        net_debt_latest=d["net_debt_latest"],
        debt_latest=d.get("debt_latest"),
        cash_latest=d.get("cash_latest"),
        sbc_annual=d["sbc_annual"],
        buybacks_annual=d["buybacks_annual"],
        shares_dilution_2y_pct=d["shares_dilution_2y_pct"],
        fiscal_years=d["fiscal_years"],
        revenue_history=[list(p) for p in d["revenue_history"]],
        eps_history=[list(p) for p in d["eps_history"]],
        fcf_history=[list(p) for p in d["fcf_history"]],
    )


@activity.defn
async def fetch_valuation(ticker: str, price: Optional[float],
                         market_cap: Optional[float]) -> Valuation:
    """Gate 7 (forward P/E vs industry peers + PEG, via Bigdata analyst
    estimates) and gate 8 (EV/EBITDA, FCF yield).

    Never raises: a Bigdata outage degrades to a Valuation carrying error
    text with the trailing multiples still computed, so QC treats gate 7
    as failed-closed while gate 8 still decides.
    """
    from providers import bigdata, sec_edgar
    v = Valuation(ticker=ticker.upper())
    try:
        g7 = bigdata.gate7(ticker, price)
    except Exception as exc:  # noqa: BLE001 — shouldn't happen (gate7
        g7 = {"error": f"{type(exc).__name__}: {exc}"[:200]}       # degrades)
    if g7.get("error"):
        v.error = g7["error"]
    else:
        v.fwd_pe = g7.get("fwd_pe")
        v.ntm_eps = g7.get("ntm_eps")
        v.fwd_growth_pct = g7.get("fwd_growth_pct")
        v.peg = g7.get("peg")
        v.peer_median_fwd_pe = g7.get("peer_median_fwd_pe")
        v.discount_vs_peers_pct = g7.get("discount_vs_peers_pct")
        v.peers_used = g7.get("peers_used")
        v.gate7_pass = g7.get("pass")
    try:
        d = sec_edgar.fundamentals(ticker)  # SEC facts cached 24h
        debt = d.get("debt_latest") or 0.0
        cash = d.get("cash_latest") or 0.0
        ebitda = d.get("ebitda_ttm") or d.get("ebitda_latest")
        if market_cap and ebitda:
            v.ev_ebitda_ttm = (market_cap + debt - cash) / ebitda
        fcf = d.get("fcf_ttm")
        if market_cap and fcf:
            v.fcf_yield_ttm = fcf / market_cap
    except Exception as exc:  # noqa: BLE001 — trailing legs optional
        v.error = (v.error + f" | sec: {exc}"[:160]).strip(" |")
    return v


@activity.defn
async def fetch_forward_growth(ticker: str) -> Dict[str, Any]:
    """Best-effort 1-yr forward EPS growth via Bigdata analyst estimates.

    Never raises: on any failure the caller keeps the TTM YoY value already
    on the bundle and the source stays "ttm_yoy" — the two are never mixed
    silently.
    """
    from providers import bigdata
    try:
        r = bigdata.forward_eps_growth(ticker)
        return {"growth": r["growth"], "analysts": r["analysts"],
                "error": ""}
    except Exception as exc:  # noqa: BLE001 — graceful degradation
        return {"growth": None, "analysts": 0,
                "error": f"{type(exc).__name__}: {exc}"[:160]}


@activity.defn
async def enrich_consensus_evidence(report: Dict[str, Any],
                                    bundles: List[TickerBundle],
                                    scan_cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Post-QC literature evidence for surviving picks (Consensus).

    Runs ONLY on picks that survived QC — never on the whole universe. Per
    pick it spends at most 2 queries (support + counter-evidence) and the
    whole run is capped at 5 queries total. All queries are 24h-cached at
    the provider layer, so re-runs of the same scan cost nothing.

    - support (catalyst_quality): the literature backing the working claim —
      the LLM's catalyst one-liner, else the deciding news headline, else
      the pick's thesis. For a Disruptive Innovator: published evidence the
      technology works/scales; biotech: clinical evidence.
    - contradict (thesis_contradiction): counter-evidence phrased against
      the working thesis ("evidence against <claim>") — the strongest
      contradicting takeaways, alongside the LLM's own contradiction.

    Never raises: a provider outage attaches {"error": ...} to the pick and
    the scan proceeds without literature evidence.
    """
    from providers import consensus

    by_ticker = {b.ticker.upper(): b for b in bundles}
    picks = report.get("picks", [])
    by_pick = {str(p.get("ticker", "")).upper(): p for p in picks}
    budget = 5  # queries per scan run, hard cap

    def claim_for(tic: str) -> str:
        b = by_ticker.get(tic)
        pick = by_pick.get(tic, {})
        claim = ""
        if b and b.catalyst_verdict:
            claim = str((b.catalyst_verdict.get("llm", {}) or {})
                        .get("catalyst", "") or "").strip()
        if not claim or claim.lower() == "none":
            claim = str((b.catalyst_verdict.get("news_verdict", {}) or {})
                        .get("headline", "") or "").strip() if b else ""
        if not claim:
            claim = str(pick.get("thesis", "")).strip()
        return claim[:300]

    for pick in picks:
        tic = str(pick.get("ticker", "")).upper()
        claim = claim_for(tic)
        evidence: Dict[str, Any] = {"queries": {}, "support": [],
                                    "contradict": [], "error": ""}
        if not claim:
            evidence["error"] = "no claim to search"
        else:
            queries = (("support", claim),
                       ("contradict", f"evidence against {claim}"))
            for kind, q in queries:
                if budget <= 0:
                    break
                budget -= 1
                evidence["queries"][kind] = q
                try:
                    r = consensus.search(q)
                    if r.get("error"):
                        evidence["error"] += f"{kind}: {r['error']}; "
                    else:
                        evidence[kind] = consensus.parse_papers(r)
                except Exception as exc:  # noqa: BLE001 — fail closed
                    evidence["error"] += f"{kind}: {type(exc).__name__}; "
            evidence["error"] = evidence["error"].strip()
            if budget <= 0 and not evidence["error"]:
                evidence["error"] = "query budget exhausted"
        pick["consensus_evidence"] = evidence
    return report


@activity.defn
async def fetch_sec_filings(ticker: str) -> Dict[str, Any]:
    """10-K recency, 8-K cadence, shelf/takedown filings (dilution vehicles on
    file), and a going-concern screen of the latest 10-K."""
    sec = _provider("sec_filings")
    return sec.filings_summary(ticker)


@activity.defn
async def fetch_news(ticker: str, max_age_days: int,
                     company_name: str = "",
                     as_of_iso: str = "") -> List[NewsItem]:
    """Headlines mentioning the ticker/company within the window, newest first.

    `as_of_iso` anchors recency for scan-as-of runs (items dated after it
    are dropped).
    """
    from datetime import datetime, timezone
    news = _provider("news")
    as_of = (datetime.fromisoformat(as_of_iso) if as_of_iso
             else datetime.now(timezone.utc))
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)
    items = news.ticker_news(ticker, company_name, days=max_age_days,
                             as_of=as_of)
    return [NewsItem(headline=i["title"], source=i["source"],
                     published_at=i["published"], url=i.get("url", ""))
            for i in items]


@activity.defn
async def check_catalyst(ticker: str, company_name: str, max_age_days: int,
                         as_of_iso: str = "") -> Dict[str, Any]:
    """Deterministic catalyst verdict: news headlines + 8-K item footprint,
    classified against the desk's material/reject catalyst lists in code.
    `interpret_catalyst` (LLM) may refine, never originate, the verdict."""
    from datetime import datetime, timezone
    news = _provider("news")
    as_of = (datetime.fromisoformat(as_of_iso) if as_of_iso
             else datetime.now(timezone.utc))
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)
    return news.catalyst_report(ticker, company_name, days=max_age_days,
                                as_of=as_of)


@activity.defn
async def compute_relative_strength(bars: Bars) -> Dict[str, Any]:
    """Trailing 63-day return vs SPY/QQQ/IWM (benchmarks provider)."""
    bench = _provider("benchmarks")
    return bench.market_rs(bars.ticker,
                           {"closes": bars.closes}, days=63)


# ---------------------------------------------------------------------------
# Structured LLM outputs (Tier 1 function calling)
#
# The two LLM steps that produce JSON (catalyst verdict, final ranking) use
# ONE forced function call with strict mode instead of asking for JSON in
# prose. Single round trip, no agentic loop, no extra API calls: this is
# validated structured output, not tool use. The model still only
# interprets numbers computed by activities; it calculates nothing.
# contradict_thesis stays plain text — it returns a string with no JSON
# parsing, so a schema would add tokens for zero reliability gain.
# ---------------------------------------------------------------------------

def _submit_tool(name: str, description: str,
                 parameters: Dict[str, Any]) -> Dict[str, Any]:
    """One strict-mode function tool definition, canonical OpenAI shape.

    Nested ``{"type": "function", "function": {...}}`` — NVIDIA's server
    500s on the flat shape while Gemini/OpenAI tolerate it, so canonical
    it is.
    """
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "strict": True,
            "parameters": parameters,
        },
    }


_CATALYST_TOOL = _submit_tool(
    "submit_catalyst_verdict",
    "Submit the catalyst analysis verdict for the ticker.",
    {
        "type": "object",
        "properties": {
            "qualifying": {
                "type": "boolean",
                "description": "True only if a specific, material catalyst exists.",
            },
            "quality": {
                "type": "integer",
                "description": "Catalyst quality 0-10.",
            },
            "catalyst": {
                "type": "string",
                "description": "One-line description of the catalyst, or 'none'.",
            },
            "source": {
                "type": "string",
                "description": "The single best primary source, or 'none'.",
            },
            "published_at": {
                "type": "string",
                "description": "Publication date of the source; empty string if unknown.",
            },
            "reason": {
                "type": "string",
                "description": "Why it qualifies or fails to qualify.",
            },
        },
        "required": ["qualifying", "quality", "catalyst", "source",
                     "published_at", "reason"],
        "additionalProperties": False,
    },
)

_SYNTHESIS_TOOL = _submit_tool(
    "submit_ranking",
    "Submit the final ranked picks. May return fewer than the max — or none.",
    {
        "type": "object",
        "properties": {
            "picks": {
                "type": "array",
                "description": "Ranked picks, best first. Empty if nothing earns it.",
                "items": {
                    "type": "object",
                    "properties": {
                        "ticker": {"type": "string"},
                        "thesis": {"type": "string",
                                  "description": "One-line thesis."},
                        "key_numbers": {"type": "string",
                                       "description": "Key numbers, only from the bundles."},
                        "entry": {"type": "string",
                                  "description": "Entry level; empty string if none."},
                        "stop": {"type": "string",
                                 "description": "Stop level; empty string if none."},
                        "contradiction": {"type": "string",
                                          "description": "Strongest contradiction."},
                    },
                    "required": ["ticker", "thesis", "key_numbers",
                                 "entry", "stop", "contradiction"],
                    "additionalProperties": False,
                },
            },
            "rejected": {
                "type": "array",
                "description": "Tickers considered but not picked.",
                "items": {
                    "type": "object",
                    "properties": {
                        "ticker": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": ["ticker", "reason"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["picks", "rejected"],
        "additionalProperties": False,
    },
)


async def _structured_call(model: str, messages: List[Dict[str, str]],
                           tool: Dict[str, Any]) -> Dict[str, Any]:
    """Forced single function call -> parsed arguments dict.

    tool_choice pins the model to `tool`; strict mode guarantees the
    schema on OpenAI. Provider fallback applies: if the primary model
    fails (no credits, rate limit, outage), the call retries on Groq,
    then Gemini, with the same schema minus strict mode. One API call
    per provider attempt, no agentic loop. If a provider ever ignores
    tool_choice and returns plain text, falls back to parsing the
    content as before.
    """
    import json
    # The tool schema is built per attempt: strict mode is OpenAI-only,
    # fallbacks get the same schema without it (forced tool_choice still
    # applies on every provider).
    resp, _model_used = await _acompletion_resilient(
        model,
        messages,
        _kwargs_for=lambda attempt: {
            "tools": [_tool_for(attempt, tool)],
            "tool_choice": {"type": "function",
                            "function": {"name": tool["function"]["name"]}},
        },
    )
    msg = resp.choices[0].message
    tool_calls = getattr(msg, "tool_calls", None) or []
    if tool_calls:
        raw_args = tool_calls[0].function.arguments or ""
    else:
        raw_args = (msg.content or "").strip()
    parsed = json.loads(raw_args)
    # Guard: secrets must never ride home in model output.
    leaks = scan_output_for_secrets(raw_args)
    if leaks:
        audit_log("secret_in_llm_output",
                  {"tool": tool["function"]["name"], "indicators": leaks})
        parsed = json.loads(redact_secrets(raw_args))
    # Guard: well-formed JSON is not enough — enforce the schema's types.
    validate_tool_args(
        tool["function"]["name"], parsed,
        tool["function"].get("parameters", {}).get("properties", {}))
    return parsed


@activity.defn
async def interpret_catalyst(bundle: TickerBundle, model: str) -> Dict[str, Any]:
    """LiteLLM: is there a qualifying, material catalyst? Returns verdict +
    quality score + the single best source. Cheap/fast model is fine here."""
    import litellm
    # Guard: headlines are untrusted third-party text. Scan for
    # prompt-injection indicators BEFORE they enter the prompt; drop
    # flagged items and audit-log them. The verdict is computed from the
    # remaining items only — hostile text is data, never instructions.
    clean_news = []
    for n in bundle.news:
        hits = scan_untrusted_text(f"{n.headline} {n.source}")
        if hits:
            audit_log("injection_indicator_dropped",
                      {"ticker": bundle.ticker, "indicators": hits})
            continue
        clean_news.append(n)
    prompt = (
        "You are a catalyst analyst. Given the news items below for "
        f"{bundle.ticker}, decide: (1) is there a specific, material catalyst "
        "(FDA/regulatory, trial results, contract/partnership, earnings beat "
        "with raised guidance, M&A, patent/court ruling, major operational "
        "milestone)? Reject vague AI/blockchain PR, conference attendance, "
        "unverified social headlines, reverse splits/compliance notices, and "
        "dilution vehicles. (2) Rate catalyst quality 0-10. (3) Cite the one "
        "best primary source. Submit your verdict with the "
        "submit_catalyst_verdict function. News: "
        + "\n".join(f"- {n.published_at} [{n.source}] {n.headline}" for n in clean_news)
    )
    prompt = enforce_prompt_budget(prompt, f"interpret_catalyst:{bundle.ticker}")
    return await _structured_call(
        model,
        [{"role": "user", "content": prompt}],
        _CATALYST_TOOL,
    )


@activity.defn
async def contradict_thesis(bundle: TickerBundle, model: str) -> str:
    """LiteLLM: steelman the bear case from the VERIFIED numbers in the bundle.
    The model interprets; it must not invent figures not present in the input."""
    prompt = (
        f"Steelman the bear case for {bundle.ticker} using ONLY the verified "
        "figures below. Name the 2-3 strongest reasons NOT to buy, including "
        "dilution/supply overhang, valuation vs peers/history, and what would "
        "invalidate the bullish thesis. Do not invent numbers.\n"
        f"Technicals: {bundle.technicals}\n"
        f"Fundamentals: {bundle.fundamentals}\n"
        f"Catalyst: {bundle.catalyst_verdict}\n"
        f"Relative strength: {bundle.relative_strength}"
    )
    prompt = enforce_prompt_budget(prompt, f"contradict_thesis:{bundle.ticker}")
    resp, _model_used = await _acompletion_resilient(
        model,
        [{"role": "user", "content": prompt}],
    )
    text = resp.choices[0].message.content or ""
    # Guard: this output flows into the synthesis prompt (second-order
    # injection path). If the model's own text carries instruction-override
    # phrases, fail closed — withhold it and audit-log.
    hits = scan_untrusted_text(text)
    if hits:
        audit_log("injection_indicator_in_output",
                  {"ticker": bundle.ticker, "stage": "contradict_thesis",
                   "indicators": hits})
        return "[withheld: failed integrity scan]"
    leaks = scan_output_for_secrets(text)
    if leaks:
        audit_log("secret_in_llm_output",
                  {"stage": "contradict_thesis", "ticker": bundle.ticker,
                   "indicators": leaks})
        text = redact_secrets(text)
    return text


@activity.defn
async def synthesize(scan_config: Dict[str, Any], bundles: List[TickerBundle],
                     model: str) -> Dict[str, Any]:
    """LiteLLM (best model): final ranking. May return FEWER than the
    configured top_candidates — never force a name in to fill the quota."""
    import litellm
    top_n = scan_config.get("output", {}).get("top_candidates", 5)
    prompt = (
        f"Scan: {scan_config.get('scan', {}).get('name')}. "
        f"Rank the candidates below into a top list of AT MOST {top_n}. "
        "You may return fewer — or none — if nothing earns it. For each pick: "
        "ticker, one-line thesis, key numbers (only from the bundles), "
        "entry/stop levels, and the strongest contradiction. "
        "Submit the ranking with the submit_ranking function.\n"
        + "\n\n".join(
            f"### {b.ticker}\nprice={b.price} mktcap={b.market_cap} "
            f"float={b.float_shares}\ntechnicals={b.technicals}\n"
            f"fundamentals={b.fundamentals}\n"
            f"catalyst={b.catalyst_verdict}\ncontradiction={b.contradiction}"
            for b in bundles
        )
    )
    prompt = enforce_prompt_budget(prompt, "synthesize")
    return await _structured_call(
        model,
        [{"role": "user", "content": prompt}],
        _SYNTHESIS_TOOL,
    )


def _parse_pct(value) -> float | None:
    """'10%' -> 0.10, 10 -> 0.10, 0.10 -> 0.10."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        return v / 100 if v > 1 else v
    text = str(value).strip().replace("%", "")
    try:
        v = float(text)
    except ValueError:
        return None
    return v / 100 if v > 1 else v


def _criteria_checks(bundle: TickerBundle, scan: dict) -> list[tuple[str, bool]]:
    """(criterion, passed) for every hard filter + universe gate.

    Every check applies only when its filter is configured: a scan that
    doesn't ask for a float cap, RVOL, or a catalyst verdict must not fail
    tickers for lacking them (previously every Large Cap pick failed QC on
    the micro-cap gates).
    """
    hf = scan.get("hard_filters", {})
    uni = scan.get("universe", {})
    t = bundle.technicals
    checks: list[tuple[str, bool]] = []
    mc = uni.get("market_cap", {})
    if mc.get("min") is not None or mc.get("max") is not None:
        checks.append(("market_cap_range",
                       (bundle.market_cap or 0) >= (_parse_money(mc.get("min")) or 0)
                       and (bundle.market_cap or 0) <= (_parse_money(mc.get("max")) or float("inf"))))
    pr = hf.get("price", {})
    if pr.get("min") is not None or pr.get("max") is not None:
        checks.append(("price_range",
                       (bundle.price or 0) >= (pr.get("min") or 0)
                       and (bundle.price or 0) <= (pr.get("max") or float("inf"))))
    fl = hf.get("float", {})
    if fl.get("max") is not None:
        checks.append(("float",
                       bundle.float_shares is not None
                       and bundle.float_shares <= (_parse_money(fl.get("max")) or float("inf"))))
    rv = hf.get("relative_volume", {})
    if rv.get("min") is not None:
        checks.append(("rvol",
                       t is not None and t.rvol is not None
                       and t.rvol >= (rv.get("min") or 0)))
    dm = hf.get("daily_move", {})
    if dm.get("min") is not None:
        checks.append(("daily_move",
                       t is not None and t.day_change_pct is not None
                       and t.day_change_pct >= (_parse_pct(dm.get("min")) or 0)))
    vo = hf.get("volume", {})
    if vo.get("min") is not None:
        # hard filter is the session's volume (RVOL covers the average leg)
        vol = t.volume if t else None
        checks.append(("volume", vol is not None
                       and vol >= (vo.get("min") or 0)))
    ca = hf.get("catalyst_age", {})
    if ca.get("max_days") is not None:
        nv = (bundle.catalyst_verdict or {}).get("news_verdict", {})
        age = nv.get("age_days")
        checks.append(("catalyst",
                       nv.get("verdict") == "material"
                       and age is not None
                       and age <= (ca.get("max_days") or float("inf"))))
    ad = hf.get("above_200dma", {})
    if ad.get("require"):
        checks.append(("above_200dma",
                       t is not None and t.sma200 is not None
                       and (bundle.price or 0) > t.sma200))

    # --- fundamentals hard filters (11-gate screen; each applies only when
    # configured). Missing/uncomputable values fail closed.
    f = bundle.fundamentals
    val = bundle.valuation
    ro = hf.get("roic", {})
    if ro.get("min") is not None:
        thr = _parse_pct(ro.get("min"))
        checks.append(("roic_min",
                       f is not None and f.roic is not None
                       and thr is not None and f.roic >= thr))
    rc = hf.get("revenue_cagr", {})
    if rc.get("min") is not None:
        thr = _parse_pct(rc.get("min"))
        checks.append(("revenue_cagr_min",
                       f is not None and f.revenue_cagr_2y is not None
                       and thr is not None and f.revenue_cagr_2y >= thr))
    ec = hf.get("eps_cagr", {})
    if ec.get("min") is not None:
        thr = _parse_pct(ec.get("min"))
        checks.append(("eps_cagr_min",
                       f is not None
                       and f.eps_cagr_2y_split_adj is not None
                       and thr is not None
                       and f.eps_cagr_2y_split_adj >= thr))
    ff = hf.get("fcf", {})
    if ff.get("positive_years") is not None or ff.get("margin_min") is not None:
        ok_years = (f is not None and f.fcf_positive_years_4 is not None
                    and f.fcf_positive_years_4 >= int(ff.get("positive_years", 0)))
        mthr = _parse_pct(ff.get("margin_min"))
        ok_margin = (f is not None and f.fcf_margin is not None
                     and mthr is not None and f.fcf_margin >= mthr) \
            if ff.get("margin_min") is not None else True
        checks.append(("fcf_history_margin", ok_years and ok_margin))
    nd = hf.get("net_debt_ebitda", {})
    if nd.get("max") is not None:
        checks.append(("leverage_max",
                       f is not None and f.net_debt_ebitda is not None
                       and f.net_debt_ebitda <= float(nd.get("max"))))
    ei = hf.get("ebit_interest", {})
    if ei.get("min") is not None:
        checks.append(("coverage_min",
                       f is not None and f.ebit_interest is not None
                       and f.ebit_interest >= float(ei.get("min"))))
    va = hf.get("valuation", {})
    if va.get("ev_ebitda_max") is not None or va.get("fcf_yield_min") is not None:
        ev_ok = (val is not None and val.ev_ebitda_ttm is not None
                 and va.get("ev_ebitda_max") is not None
                 and val.ev_ebitda_ttm <= float(va["ev_ebitda_max"]))
        ythr = _parse_pct(va.get("fcf_yield_min"))
        fy_ok = (val is not None and val.fcf_yield_ttm is not None
                 and ythr is not None and val.fcf_yield_ttm >= ythr)
        checks.append(("valuation_gate8", ev_ok or fy_ok))
    g7 = hf.get("gate7", {})
    if g7.get("require_pass"):
        checks.append(("gate7_forward_value",
                       val is not None and val.gate7_pass is True))

    # --- Small Cap 4x Growth gates (each applies only when configured).
    # Missing/uncomputable values fail closed.
    ry = hf.get("revenue_yoy", {})
    if ry.get("min") is not None:
        thr = _parse_pct(ry.get("min"))
        checks.append(("revenue_yoy_min",
                       f is not None and f.revenue_yoy_1y is not None
                       and thr is not None and f.revenue_yoy_1y >= thr))
    eg = hf.get("eps_growth", {})
    if eg.get("min") is not None:
        thr = _parse_pct(eg.get("min"))
        checks.append(("eps_growth_min",
                       f is not None and f.eps_growth_1y is not None
                       and thr is not None and f.eps_growth_1y >= thr))
    cr = hf.get("current_ratio", {})
    if cr.get("min") is not None:
        checks.append(("current_ratio_min",
                       f is not None and f.current_ratio is not None
                       and f.current_ratio >= float(cr.get("min"))))
    de = hf.get("debt_equity", {})
    if de.get("max") is not None:
        checks.append(("debt_equity_max",
                       f is not None and f.debt_equity is not None
                       and f.debt_equity <= float(de.get("max"))))
    return checks


@activity.defn
async def quality_control(report: Dict[str, Any],
                          bundles: List[TickerBundle],
                          scan: Dict[str, Any]) -> Dict[str, Any]:
    """Deterministic QC over the synthesized report. Drops violating picks —
    never silently fixes them.

    - Every pick must trace to a bundle; reported price/rvol/move must match
      the bundle within tolerance.
    - Every pick must pass ALL hard filters (recomputed from raw values).
    - Mega-cap rule (engine_rules.mega_cap): a mega-cap (market cap at or
      above the threshold, default $200B) priced over $35 is NEVER emitted;
      a mega-cap at $35 or less must satisfy >=80% of the scan's criteria.
    - Output shape: no duplicates, at most top_candidates picks.
    """
    scan_cfg = scan.get("scan", scan)
    rules = scan.get("engine_rules", {}) or {}
    mega = rules.get("mega_cap", {}) or {}
    mega_threshold = _parse_money(mega.get("threshold")) or 200e9
    mega_price_cap = mega.get("price_cap", 35)
    mega_min_frac = mega.get("min_criteria_fraction", 0.8)
    top_n = scan_cfg.get("output", {}).get("top_candidates", 5)

    by_ticker = {b.ticker.upper(): b for b in bundles}
    violations: list[str] = []
    dropped: list[dict] = []
    clean: list[dict] = []
    seen: set[str] = set()

    for pick in report.get("picks", []):
        tic = str(pick.get("ticker", "")).upper()
        if tic in seen:
            violations.append(f"{tic}: duplicate pick")
            dropped.append({"ticker": tic, "reason": "duplicate"})
            continue
        seen.add(tic)
        b = by_ticker.get(tic)
        if b is None:
            violations.append(f"{tic}: no research bundle")
            dropped.append({"ticker": tic, "reason": "no bundle"})
            continue

        # number traceability: reported figures must match the bundle
        for field, bval in (("price", b.price),
                            ("rvol", b.technicals.rvol if b.technicals else None),
                            ("day_change_pct", b.technicals.day_change_pct if b.technicals else None)):
            rval = pick.get(field)
            if rval is not None and bval is not None and bval != 0:
                if abs(rval - bval) / abs(bval) > 0.02:
                    violations.append(
                        f"{tic}: reported {field}={rval} != bundle {bval:.4g}")

        # hard filters, recomputed
        checks = _criteria_checks(b, scan_cfg)
        failed = [name for name, ok in checks if not ok]
        total, passed = len(checks), sum(1 for _, ok in checks if ok)
        frac = passed / total if total else 1.0

        # mega-cap rule
        is_mega = (b.market_cap or 0) >= mega_threshold
        if is_mega and (b.price or 0) > mega_price_cap:
            violations.append(
                f"{tic}: mega-cap (${(b.market_cap or 0)/1e9:.0f}B) over "
                f"${mega_price_cap} — excluded by engine rule")
            dropped.append({"ticker": tic, "reason": "mega-cap over price cap"})
            continue
        if is_mega and frac < mega_min_frac:
            violations.append(
                f"{tic}: mega-cap at ${b.price:.2f} meets {passed}/{total} "
                f"criteria (< {mega_min_frac:.0%}) — excluded by engine rule")
            dropped.append({"ticker": tic, "reason": "mega-cap below 80% criteria"})
            continue

        if failed:
            violations.append(f"{tic}: fails hard filters: {', '.join(failed)}")
            dropped.append({"ticker": tic, "reason": f"hard filters: {', '.join(failed)}"})
            continue
        clean.append(pick)

    if len(clean) > top_n:
        violations.append(f"report has {len(clean)} picks > top {top_n}; truncated")
        clean = clean[:top_n]

    report["picks"] = clean
    report["qc"] = {"passed": not violations, "violations": violations,
                    "dropped": dropped,
                    "mega_cap_rule": {
                        "threshold": mega_threshold, "price_cap": mega_price_cap,
                        "min_criteria_fraction": mega_min_frac}}
    return report
