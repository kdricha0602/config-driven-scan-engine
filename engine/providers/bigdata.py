"""Bigdata.com (RavenPack) provider: analyst estimates + industry peers.

Powers gate 7 of the 11-gate screen: forward P/E at least 10% below the
industry peer median AND PEG <= 1.5.

Auth: the connected `custom.bigdata` credential via the authd surrogate
exchange (same pattern as skills/bigdata/bin/bigdata_query.py). The raw key
is never seen, logged, or persisted.

All public functions degrade to {"error": ...} instead of raising, so a
Bigdata outage turns gate 7 into "no data" rather than killing the scan.
"""

from __future__ import annotations

import json
import sys
import urllib.request
import urllib.error
from datetime import date, timedelta

sys.path.insert(0, "/opt/hatch/skills/skill-creator/bin")
from dynamic_credentials import (  # noqa: E402
    add_surrogate_to_request, read_json_response)

_BASE = "https://api.bigdata.com"
_HOSTS = ["api.bigdata.com"]
_CRED = "custom.bigdata"
_PEER_CAP = 10

# Knowledge-graph industry -> company-screener industry enum
INDUSTRY_MAP = {
    "Software": "Software - Infrastructure",
    "Electrical Components and Equipment": "Electrical Equipment & Parts",
    "Semiconductors": "Semiconductors",
}


def _post(path: str, body: dict, timeout: int = 60) -> dict:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"{_BASE}/{path.lstrip('/')}", data=data,
        headers={"Content-Type": "application/json",
                 "Accept": "application/json"},
        method="POST")
    add_surrogate_to_request(req, _CRED, allowed_hosts=_HOSTS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return read_json_response(resp)
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            detail = ""
        raise RuntimeError(f"Bigdata {path} HTTP {e.code}: {detail}")


def resolve(ticker: str) -> dict:
    """Ticker -> knowledge-graph entity (id, name, industry, ...)."""
    d = _post("v1/knowledge-graph/companies",
              {"query": ticker.upper(), "types": ["PUBLIC"],
               "countries": ["US"]})
    items = d.get("results") or d.get("data") or []
    exact = [i for i in items if i.get("ticker") == ticker.upper()]
    if not exact and items:
        exact = items[:1]
    if not exact:
        raise RuntimeError(f"no Bigdata entity for {ticker}")
    return exact[0]


def industry_peers(industry: str, exclude_id: str,
                   cap: int = _PEER_CAP) -> list[dict]:
    """Top `cap` US actively-trading peers in the same screener industry,
    by market cap, excluding the ticker itself."""
    screener_industry = INDUSTRY_MAP.get(industry, industry)
    try:
        d = _post("v1/company-screener/query", {
            "filters": {"industry": screener_industry, "country": "US",
                        "is_actively_trading": True,
                        "market_cap_more_than": 750_000_000},
            "limit": 40})
    except RuntimeError:
        if screener_industry == industry:
            raise
        d = _post("v1/company-screener/query", {
            "filters": {"industry": industry, "country": "US",
                        "is_actively_trading": True,
                        "market_cap_more_than": 750_000_000},
            "limit": 40})
    items = d.get("results") or []
    if isinstance(items, dict):
        items = items.get("data") or []
    out = [p for p in items
           if p.get("rp_entity_id") != exclude_id and p.get("symbol")]
    out.sort(key=lambda p: p.get("market_cap") or 0, reverse=True)
    return out[:cap]


def annual_estimates(rp_id: str, limit: int = 5
                     ) -> list[tuple[date, float, int]]:
    """[(fiscal-year-end, consensus EPS, analyst count)], oldest first."""
    d = _post("v1/analyst-estimates/query", {
        "identifier": {"type": "rp_entity_id", "value": rp_id},
        "period": "annual", "limit": limit})
    res = d.get("results", {})
    fields, rows = res.get("fields", []), res.get("values", [])
    ie, ip = fields.index("FISCAL_PERIOD_END_DATE"), fields.index("EPS_AVG")
    ine = (fields.index("NUM_ANALYSTS_EPS") if "NUM_ANALYSTS_EPS" in fields
           else None)
    out = []
    for r in rows:
        if r[ip] is None:
            continue
        out.append((date.fromisoformat(r[ie]), float(r[ip]),
                    int(r[ine] or 0) if ine is not None else 0))
    out.sort()
    return out


def ntm_eps(ests: list[tuple[date, float, int]],
            asof: date | None = None) -> float | None:
    """Next-12-months EPS: FY estimates time-weighted across the window."""
    asof = asof or date.today()
    start, end = asof, asof + timedelta(days=365)
    total, wsum = 0.0, 0.0
    for ed, eps, _ in ests:
        fy_start = ed - timedelta(days=365)
        w = max(0, (min(end, ed) - max(start, fy_start)).days) / 365
        if w > 0:
            total += w * eps
            wsum += w
    return total / wsum if wsum > 0.5 else None


def fwd_growth(ests: list[tuple[date, float, int]],
               asof: date | None = None) -> float | None:
    """1-yr forward EPS growth: current FY -> next FY."""
    asof = asof or date.today()
    cur = nxt = None
    for ed, eps, _ in ests:
        if ed >= asof and cur is None:
            cur = (ed, eps)
        elif cur is not None:
            nxt = (ed, eps)
            break
    if cur and nxt and cur[1] and cur[1] > 0:
        return nxt[1] / cur[1] - 1
    return None


def forward_eps_growth(ticker: str,
                       asof: date | None = None) -> dict:
    """1-yr forward EPS growth (current FY -> next FY consensus) plus the
    max analyst count behind it. Raises on any failure — callers fall back
    to TTM YoY EPS growth and record the source."""
    ent = resolve(ticker)
    ests = annual_estimates(ent["id"])
    g = fwd_growth(ests, asof)
    if g is None:
        raise RuntimeError(f"no forward EPS growth for {ticker}")
    analysts = max((n for _, _, n in ests), default=0)
    return {"growth": g, "analysts": analysts}


def gate7(ticker: str, price: float | None,
          asof: date | None = None) -> dict:
    """Gate 7: forward P/E >=10% below peer median AND PEG <= 1.5.

    Never raises: failures return {"ticker", "error"} so the scan degrades
    gracefully.
    """
    asof = asof or date.today()
    out: dict = {"ticker": ticker.upper(), "asof": asof.isoformat()}
    try:
        if not price:
            raise RuntimeError("no price for forward P/E")
        ent = resolve(ticker)
        industry = ent.get("industry")
        out["industry"] = industry
        est = annual_estimates(ent["id"])
        ntm = ntm_eps(est, asof)
        g = fwd_growth(est, asof)
        fwd_pe = price / ntm if ntm and ntm > 0 else None
        out.update(price=round(price, 2),
                   ntm_eps=round(ntm, 2) if ntm else None,
                   fwd_pe=round(fwd_pe, 1) if fwd_pe else None,
                   fwd_growth_pct=(round(g * 100, 1) if g is not None
                                   else None))
        peer_pes = []
        for p in industry_peers(industry, ent["id"]):
            try:
                pe_ntm = ntm_eps(annual_estimates(p["rp_entity_id"]), asof)
            except Exception:  # noqa: BLE001 — one bad peer skips, not aborts
                continue
            if pe_ntm and pe_ntm > 0 and p.get("price"):
                peer_pes.append(p["price"] / pe_ntm)
        peer_pes.sort()
        med = peer_pes[len(peer_pes) // 2] if peer_pes else None
        out["peer_median_fwd_pe"] = round(med, 1) if med else None
        out["peers_used"] = len(peer_pes)
        peg = (fwd_pe / (g * 100) if fwd_pe and g and g > 0 else None)
        out["peg"] = round(peg, 2) if peg else None
        out["discount_vs_peers_pct"] = (
            round((1 - fwd_pe / med) * 100, 1)
            if fwd_pe and med else None)
        out["pass_discount"] = bool(fwd_pe and med and fwd_pe <= 0.9 * med)
        out["pass_peg"] = bool(peg and peg <= 1.5)
        out["pass"] = out["pass_discount"] and out["pass_peg"]
    except Exception as exc:  # noqa: BLE001 — graceful degradation
        out["error"] = f"{type(exc).__name__}: {exc}"[:200]
    return out
