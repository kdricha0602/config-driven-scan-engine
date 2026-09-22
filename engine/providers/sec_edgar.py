"""SEC EDGAR fundamentals + filings. Free, no key; SEC requires identification.

Endpoints:
  Ticker->CIK: https://www.sec.gov/files/company_tickers.json
  Facts:       https://data.sec.gov/api/xbrl/companyfacts/CIK{cik10}.json
  Submissions: https://data.sec.gov/submissions/CIK{cik10}.json
  Filing docs: https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}

Set SEC_CONTACT_EMAIL to a real address (SEC's fair-use rule). Defaults to a
placeholder and says so in the User-Agent.

Rate limit: SEC asks <=10 req/s; we sleep 0.25s between calls.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.request
from datetime import date, timedelta

from .cache import get as cache_get, put as cache_put
from security import urlopen_guarded

_CONTACT = os.environ.get("SEC_CONTACT_EMAIL", "quantkernal-scan-engine@example.com")
_UA = {"User-Agent": f"QuantKernal scan-engine/1.0 (contact: {_CONTACT})",
       "Accept": "application/json"}
_TICKERS_TTL = 7 * 24 * 3600
_FACTS_TTL = 24 * 3600
_SUB_TTL = 12 * 3600
_LAST_CALL = [0.0]


def _fetch(url: str, timeout: int = 40, retries: int = 3,
           binary: bool = False) -> bytes | dict:
    last: Exception | None = None
    for attempt in range(retries):
        try:
            gap = time.time() - _LAST_CALL[0]
            if gap < 0.25:
                time.sleep(0.25 - gap)
            req = urllib.request.Request(url, headers=_UA)
            with urlopen_guarded(req, timeout=timeout) as resp:
                raw = resp.read()
            _LAST_CALL[0] = time.time()
            if binary:
                return raw
            return json.loads(raw.decode("utf-8", errors="replace"))
        except Exception as exc:
            last = exc
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"SEC request failed: {url}: {last}")


# ---------------------------------------------------------------------------
# CIK resolution + raw payloads
# ---------------------------------------------------------------------------

def ticker_to_cik(ticker: str) -> str:
    """'FTNT' -> '013646...' zero-padded 10-digit CIK string."""
    ticker = ticker.strip().upper()
    key = "sec:tickers"
    mapping = cache_get(key, _TICKERS_TTL)
    if mapping is None:
        payload = _fetch("https://www.sec.gov/files/company_tickers.json")
        mapping = {v["ticker"].upper(): str(v["cik_str"]).zfill(10)
                   for v in payload.values()}
        cache_put(key, mapping)
    cik = mapping.get(ticker)  # type: ignore[union-attr]
    if not cik:
        raise RuntimeError(f"No SEC CIK found for ticker {ticker}")
    return cik


def company_facts(cik: str) -> dict:
    key = f"sec:facts:{cik}"
    cached = cache_get(key, _FACTS_TTL)
    if cached is not None:
        return cached  # type: ignore[return-value]
    data = _fetch(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json")
    cache_put(key, data)
    return data


def submissions(cik: str) -> dict:
    key = f"sec:sub:{cik}"
    cached = cache_get(key, _SUB_TTL)
    if cached is not None:
        return cached  # type: ignore[return-value]
    data = _fetch(f"https://data.sec.gov/submissions/CIK{cik}.json")
    cache_put(key, data)
    return data


# ---------------------------------------------------------------------------
# XBRL concept extraction
# ---------------------------------------------------------------------------

# concept -> aliases to try, in order
ALIASES = {
    "revenue": ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
                "SalesRevenueNet"],
    "net_income": ["NetIncomeLoss"],
    "eps_diluted": ["EarningsPerShareDiluted"],
    "ocf": ["NetCashProvidedByUsedInOperatingActivities"],
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment",
              "PaymentsToAcquireProductiveAssets"],
    "op_income": ["OperatingIncomeLoss"],
    "interest_expense": ["InterestExpense"],
    "depreciation": ["DepreciationDepletionAndAmortization",
                     "DepreciationAndAmortization"],
    "lt_debt": ["LongTermDebtNoncurrent", "LongTermDebt"],
    "st_debt": ["LongTermDebtCurrent", "DebtCurrent", "ShortTermBorrowings"],
    "cash": ["CashAndCashEquivalentsAtCarryingValue"],
    "equity": ["StockholdersEquity"],
    "tax_expense": ["IncomeTaxExpenseBenefit"],
    "pretax_income": ["IncomeLossFromContinuingOperationsBeforeIncomeTaxes",
                      "ProfitLoss"],
    "sbc": ["ShareBasedCompensation"],
    "buybacks": ["PaymentsForRepurchaseOfCommonStock"],
    "shares_out": ["CommonStockSharesOutstanding",
                   "WeightedAverageNumberOfDilutedSharesOutstanding"],
}


def _year_of(e: dict) -> tuple[int | None, int | None]:
    """(year, quarter|None) for a fact entry, preferring EDGAR's `frame`.

    Observed in the wild: FTNT's revenue facts carry `fy` up to 2 years off
    the true period, while `frame` (CY####) is correct. Frame wins; `fy`/`fp`
    are the fallback for entries without a frame.
    """
    frame = str(e.get("frame") or "")
    m = re.match(r"^CY(\d{4})(Q([1-4]))?$", frame)
    if m:
        return int(m.group(1)), int(m.group(3)) if m.group(3) else None
    try:
        fy = int(e["fy"])
    except (KeyError, TypeError, ValueError):
        return None, None
    fp = str(e.get("fp") or "")
    q = int(fp[1]) if re.match(r"^Q[1-4]$", fp) else None
    return fy, q


def _entries(facts: dict, concept: str) -> list[dict]:
    """Every fact entry for a concept, across all aliases AND all units
    (EPS is reported in USD/shares, not USD — a units-blind lookup misses it).
    """
    gaap = (facts.get("facts", {}) or {}).get("us-gaap", {})
    out = []
    for alias in ALIASES[concept]:
        node = gaap.get(alias)
        if not node:
            continue
        for entries in node.get("units", {}).values():
            for e in entries:
                year, q = _year_of(e)
                if year is None:
                    continue
                try:
                    val = float(e["val"])
                except (TypeError, ValueError):
                    continue
                out.append({"year": year, "q": q,
                            "filed": str(e.get("filed", "")),
                            "val": val, "form": e.get("form")})
    return out


_ANNUAL_FORMS = ("10-K", "10-K/A")
_QTR_FORMS = ("10-Q", "10-Q/A")


def _candidates(entries: list[dict]) -> dict[tuple, list[float]]:
    """{(year, quarter): distinct values sharing the latest filed date}.

    companyfacts drops segment dimensions, so one filing can carry several
    values for the same concept/period (consolidated + segment cuts,
    continuing vs total, as-reported vs restated). Callers disambiguate.
    """
    bykey: dict[tuple, list[dict]] = {}
    for e in entries:
        bykey.setdefault((e["year"], e["q"]), []).append(e)
    out: dict[tuple, list[float]] = {}
    for key, lst in bykey.items():
        maxfiled = max(e["filed"] for e in lst)
        out[key] = sorted({e["val"] for e in lst if e["filed"] == maxfiled})
    return out


def _pick_annual(cands: dict[tuple, list[float]]) -> list[tuple[int, float]]:
    """Resolve ambiguous annual values.

    1. Segment-breakdown signature (one candidate ~= sum of the rest):
       the max is the consolidated figure.
    2. Otherwise the candidate closest to neighbor interpolation — a
       genuine spike year still shows in its neighbors' trend; a
       dimensional duplicate does not.
    """
    years = sorted(y for (y, q) in cands)
    picked: dict[int, float] = {}
    for y in years:
        c = cands[(y, None)]
        if len(c) == 1:
            picked[y] = c[0]
    for _ in range(3):  # relaxation passes for adjacent ambiguous years
        for y in years:
            if y in picked:
                continue
            c = cands[(y, None)]
            mx = max(c)
            if abs(sum(c) - 2 * mx) <= 0.02 * abs(mx or 1):
                picked[y] = mx
                continue
            prev, nxt = picked.get(y - 1), picked.get(y + 1)
            target = ((prev + nxt) / 2 if prev is not None and nxt is not None
                      else prev if prev is not None else nxt)
            if target is None:
                target = sorted(c)[len(c) // 2]
            picked[y] = min(c, key=lambda v: abs(v - target))
    return sorted(picked.items())


def _pick_quarterly(cands: dict[tuple, list[float]],
                    annual: dict[int, float]) -> list[tuple[int, int, float]]:
    """Resolve ambiguous quarterly values, anchored to the annual figure.

    Full year present: the combo whose sum best matches the annual.
    Partial year: assume missing quarters run at the chosen median rate and
    minimize the gap to the annual. No annual: continuity with the
    prior-year same quarter.
    """
    import itertools
    by_year: dict[int, dict[int, list[float]]] = {}
    for (y, q), c in cands.items():
        by_year.setdefault(y, {})[q] = c
    picked: dict[tuple[int, int], float] = {}
    for y in sorted(by_year):
        qs = by_year[y]
        order = sorted(qs)
        A = annual.get(y)
        if A is not None:
            combos = list(itertools.product(*(qs[q] for q in order)))
            if len(order) == 4:
                combo = min(combos, key=lambda cb: abs(sum(cb) - A))
            else:
                def score(cb: tuple) -> float:
                    med = sorted(cb)[len(cb) // 2]
                    return abs(A - sum(cb) - med * (4 - len(cb)))
                combo = min(combos, key=score)
            for q, v in zip(order, combo):
                picked[(y, q)] = v
        else:
            for q in order:
                c = qs[q]
                if len(c) == 1:
                    picked[(y, q)] = c[0]
                    continue
                ref = picked.get((y - 1, q))
                if ref is None:
                    refs = [v for (yy, qq), v in picked.items()
                            if yy == y and qq != q]
                    ref = (sorted(refs)[len(refs) // 2] if refs
                           else sorted(c)[len(c) // 2])
                picked[(y, q)] = min(c, key=lambda v: abs(v - ref))
    return [(y, q, picked[(y, q)]) for (y, q) in sorted(picked)]


def annual_series(facts: dict, concept: str) -> list[tuple[int, float]]:
    """[(year, value)] from 10-K/10-K/A annual facts; disambiguated."""
    cands = _candidates([e for e in _entries(facts, concept)
                         if e["form"] in _ANNUAL_FORMS and e["q"] is None])
    return _pick_annual(cands)


def quarterly_series(facts: dict, concept: str,
                     annual: dict[int, float] | None = None
                     ) -> list[tuple[int, int, float]]:
    """[(year, quarter, value)] from 10-Q/10-Q/A facts; disambiguated."""
    cands = _candidates([e for e in _entries(facts, concept)
                         if e["form"] in _QTR_FORMS and e["q"] is not None])
    return _pick_quarterly(cands, annual or {})


def _last(s: list) -> float | None:
    return s[-1][1] if s else None


def ttm_ytd(facts: dict, concept: str) -> float | None:
    """TTM for YTD-reported flow concepts: FY_last + YTD_now - YTD_prior.

    Falls back to the latest FY value when no newer quarterly data exists.
    """
    ann = annual_series(facts, concept)
    if not ann:
        return None
    yr_last, fy_val = ann[-1]
    qs = quarterly_series(facts, concept, dict(ann))
    newer = [(y, q, v) for y, q, v in qs if (y, q) > (yr_last, 4)]
    if not newer:
        return fy_val
    y, q, ytd_now = max(newer)
    prior = next((v for yy, qq, v in qs if yy == y - 1 and qq == q), None)
    if prior is None:
        return fy_val
    return fy_val + ytd_now - prior


def eps_ttm(facts: dict) -> float | None:
    """Sum of the most recent 4 quarterly diluted-EPS values."""
    ann = annual_series(facts, "eps_diluted")
    qs = quarterly_series(facts, "eps_diluted", dict(ann))
    if len(qs) >= 4:
        return sum(v for _, _, v in qs[-4:])
    return _last(ann)


def _cagr(series: list[tuple[int, float]], years: int) -> float | None:
    """CAGR over `years` using the last years+1 annual points; None if the
    base is non-positive (growth off a loss is meaningless)."""
    if len(series) < years + 1:
        return None
    _, start = series[-(years + 1)]
    _, end = series[-1]
    if start <= 0 or end is None:
        return None
    return (end / start) ** (1 / years) - 1


# ---------------------------------------------------------------------------
# Fundamentals (maps to the 11-gate screen)
# ---------------------------------------------------------------------------

def fundamentals(ticker: str) -> dict:
    """Compute gate-ready fundamentals for one ticker.

    Returns annual histories plus TTM figures; None where EDGAR lacks data.
    Ratios follow the screen's definitions (see module docstring decisions).
    """
    cik = ticker_to_cik(ticker)
    facts = company_facts(cik)

    rev = annual_series(facts, "revenue")
    eps = annual_series(facts, "eps_diluted")
    ocf_s = annual_series(facts, "ocf")
    capex_s = annual_series(facts, "capex")
    opinc_s = annual_series(facts, "op_income")
    int_s = annual_series(facts, "interest_expense")
    dep_s = annual_series(facts, "depreciation")
    ltd_s = annual_series(facts, "lt_debt")
    std_s = annual_series(facts, "st_debt")
    cash_s = annual_series(facts, "cash")
    eq_s = annual_series(facts, "equity")
    tax_s = annual_series(facts, "tax_expense")
    pretax_s = annual_series(facts, "pretax_income")
    sbc_s = annual_series(facts, "sbc")
    bb_s = annual_series(facts, "buybacks")
    sh_s = annual_series(facts, "shares_out")

    # FCF history (gate 2 needs 4-of-5 positive years + margin > 10%)
    ocf_by_fy = dict(ocf_s)
    fcf_hist = [(fy, ocf_by_fy[fy] - dict(capex_s).get(fy, 0.0))
                for fy in sorted(set(ocf_by_fy) & set(dict(capex_s)))]
    if not fcf_hist and ocf_s:  # capex missing -> OCF proxy, flagged
        fcf_hist = [(fy, v) for fy, v in ocf_s]

    rev_ttm = ttm_ytd(facts, "revenue")
    fcf_ttm = ttm_ytd(facts, "ocf")
    if fcf_ttm is not None:
        capex_tTM = ttm_ytd(facts, "capex")
        if capex_tTM is not None:
            fcf_ttm -= capex_tTM

    opinc, interest = _last(opinc_s), _last(int_s)
    debt = (_last(ltd_s) or 0.0) + (_last(std_s) or 0.0)
    cash, equity = _last(cash_s), _last(eq_s)

    # ROIC = NOPAT / invested capital (end-of-year; documented simplification)
    roic = None
    tax_exp, pretax = _last(tax_s), _last(pretax_s)
    if opinc is not None and equity is not None:
        tax_rate = 0.0
        if tax_exp is not None and pretax:
            tax_rate = min(max(tax_exp / pretax, 0.0), 0.35)
        nopat = opinc * (1 - tax_rate)
        invested = equity + debt - (cash or 0.0)
        if invested and invested > 0:
            roic = nopat / invested

    ebitda = None
    if opinc is not None:
        ebitda = opinc + (_last(dep_s) or 0.0)

    net_debt_ebitda = None
    if ebitda:
        net_debt_ebitda = (debt - (cash or 0.0)) / ebitda

    ebit_interest = None
    if opinc is not None and interest:
        ebit_interest = opinc / interest

    dilution_2y = None
    if len(sh_s) >= 3:
        base, now = sh_s[-3][1], sh_s[-1][1]
        if base:
            dilution_2y = (now - base) / base

    return {
        "ticker": ticker.upper(),
        "cik": cik,
        "fiscal_years": [fy for fy, _ in rev],
        "revenue_history": rev,
        "eps_history": eps,
        "fcf_history": fcf_hist,
        "revenue_ttm": rev_ttm,
        "revenue_cagr_2y": _cagr(rev, 2),
        "eps_cagr_2y": _cagr(eps, 2),
        "eps_ttm": eps_ttm(facts),
        "fcf_ttm": fcf_ttm,
        "fcf_margin": (fcf_ttm / rev_ttm) if fcf_ttm is not None and rev_ttm else None,
        "roic": roic,
        "net_debt_ebitda": net_debt_ebitda,
        "ebit_interest": ebit_interest,
        "ebitda_latest": ebitda,
        "net_debt_latest": debt - (cash or 0.0),
        "sbc_annual": _last(sbc_s),
        "buybacks_annual": _last(bb_s),
        "shares_dilution_2y_pct": dilution_2y,
    }


# ---------------------------------------------------------------------------
# Filings summary
# ---------------------------------------------------------------------------

_DILUTION_FORMS = {"S-1", "S-3", "S-3ASR", "F-1", "F-3", "424B2", "424B5",
                   "424B3", "424B4", "424B7"}
_COMP_PLAN_FORMS = {"S-8"}


def filings_summary(ticker: str) -> dict:
    """Recent 10-K/10-Q/8-K, shelf & takedown filings, going-concern screen."""
    cik = ticker_to_cik(ticker)
    sub = submissions(cik)
    recent = sub.get("filings", {}).get("recent", {}) or {}
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accs = recent.get("accessionNumber", [])
    docs = recent.get("primaryDocument", [])

    filings = []
    for i, form in enumerate(forms):
        filings.append({
            "form": form,
            "filing_date": dates[i] if i < len(dates) else "",
            "accession": accs[i].replace("-", "") if i < len(accs) else "",
            "primary_doc": docs[i] if i < len(docs) else "",
        })

    cutoff_90 = (date.today() - timedelta(days=90)).isoformat()
    cutoff_365 = (date.today() - timedelta(days=365)).isoformat()
    eight_k_90d = sum(1 for f in filings
                      if f["form"] == "8-K" and f["filing_date"] >= cutoff_90)
    shelf = [f for f in filings
             if f["form"] in _DILUTION_FORMS and f["filing_date"] >= cutoff_365]
    comp_plans = [f for f in filings
                  if f["form"] in _COMP_PLAN_FORMS and f["filing_date"] >= cutoff_365]
    latest_10k = next((f for f in filings if f["form"] == "10-K"), None)

    going_concern: dict = {"checked": False, "flag": None, "snippet": ""}
    if latest_10k and latest_10k["accession"] and latest_10k["primary_doc"]:
        try:
            url = (f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
                   f"{latest_10k['accession']}/{latest_10k['primary_doc']}")
            raw = _fetch(url, timeout=60, binary=True)
            text = raw.decode("utf-8", errors="replace")
            text_l = text.lower()
            idx = text_l.find("substantial doubt")
            flag = False
            snippet = ""
            if idx != -1:
                window = text_l[max(0, idx - 200):idx + 200]
                if "going concern" in window:
                    flag = True
                    import html as _html
                    snippet = _html.unescape(re.sub(
                        r"\s+", " ", text[max(0, idx - 200):idx + 200])).strip()
            going_concern = {"checked": True, "flag": flag, "snippet": snippet,
                             "filing_date": latest_10k["filing_date"]}
        except Exception as exc:
            going_concern = {"checked": False, "flag": None,
                             "snippet": f"check failed: {exc}"}

    return {
        "ticker": ticker.upper(),
        "cik": cik,
        "latest_10k_date": latest_10k["filing_date"] if latest_10k else None,
        "eight_k_last_90d": eight_k_90d,
        "shelf_or_takedown_last_365d": [
            {"form": f["form"], "filing_date": f["filing_date"]} for f in shelf],
        "comp_plan_s8_last_365d": [
            {"form": f["form"], "filing_date": f["filing_date"]}
            for f in comp_plans],
        "going_concern": going_concern,
        "recent_filings": filings[:15],
    }
