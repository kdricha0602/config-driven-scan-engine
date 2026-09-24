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
    "lt_debt": ["LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities",
                "LongTermDebtNoncurrent", "LongTermDebt",
                "ConvertibleDebtNoncurrent",
                "LongTermDebtAndCapitalLeaseObligations"],
    "st_borrow": ["ShortTermBorrowings"],
    "lt_current": ["LongTermDebtCurrent", "ConvertibleDebtCurrent",
                   "LongTermDebtAndCapitalLeaseObligationsCurrent",
                   "DebtCurrent"],
    "st_debt": ["LongTermDebtCurrent", "DebtCurrent", "ShortTermBorrowings"],
    "cash": ["CashAndCashEquivalentsAtCarryingValue"],
    "curr_assets": ["AssetsCurrent"],
    "curr_liab": ["LiabilitiesCurrent"],
    "gross_profit": ["GrossProfit"],
    "equity": ["StockholdersEquity"],
    # assets-liabilities is the robust equity proxy: the StockholdersEquity
    # caption pick can land on a segment cut (FTNT 2025 read -$0.46B vs the
    # true +$1.24B), which explodes ROIC through a near-zero denominator.
    "assets": ["Assets"],
    "liabilities": ["Liabilities"],
    "tax_expense": ["IncomeTaxExpense", "IncomeTaxExpenseBenefit"],
    "pretax_income": ["IncomeLossFromContinuingOperationsBeforeIncomeTaxes",
                      "ProfitLoss"],
    "sbc": ["ShareBasedCompensation"],
    "buybacks": ["PaymentsForRepurchaseOfCommonStock"],
    # Diluted weighted-average only: mixing CommonStockSharesOutstanding
    # (point-in-time) with the diluted average across years distorts the
    # split-adjusted CAGR (LRCX read -15.5% instead of +37.8%).
    "shares_out": ["WeightedAverageNumberOfDilutedSharesOutstanding"],
}


def _year_of(e: dict) -> tuple[int | None, int | None]:
    """(year, quarter|None) for a fact entry.

    The period `end`/`instant` date is ground truth: EDGAR's `fy` is the
    *filing's* fiscal year (a 2026 10-K restating FY2024 comparatives tags
    them fy=2026) and `frame` is unreliable for non-December year-ends
    (LRCX's FY2021 facts carry frame CY2020 or no frame at all). Frame/fy
    survive only as fallbacks for entries without a usable date.
    """
    year: int | None = None
    end = e.get("end") or e.get("instant")
    if end:
        try:
            year = int(str(end)[:4])
        except (TypeError, ValueError):
            year = None
    if year is None:
        frame = str(e.get("frame") or "")
        m = re.match(r"^CY(\d{4})(Q([1-4]))?$", frame)
        if m:
            year = int(m.group(1))
        else:
            try:
                year = int(e["fy"])
            except (KeyError, TypeError, ValueError):
                year = None
    fp = str(e.get("fp") or "")
    q = int(fp[1]) if re.match(r"^Q[1-4]$", fp) else None
    return year, q


def _entries(facts: dict, concept: str) -> list[dict]:
    """Every fact entry for a concept, across all aliases AND all units
    (EPS is reported in USD/shares, not USD — a units-blind lookup misses it).
    """
    return _entries_for_aliases(facts, ALIASES[concept])


def _entries_for_aliases(facts: dict, aliases: list[str]) -> list[dict]:
    gaap = (facts.get("facts", {}) or {}).get("us-gaap", {})
    out = []
    for alias in aliases:
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
                            "val": val, "form": e.get("form"),
                            "alias": alias})
    return out


def _freshest_alias(facts: dict, aliases: list[str]) -> str | None:
    """The alias with the latest 10-K fact year.

    Debt captions go stale when filers relabel (APH's LongTermDebt ends
    FY2017; the live caption is ...IncludingCurrentMaturities). Merging all
    aliases into one pool lets the disambiguation heuristic pick a
    stale-caption value for recent years, so debt groups resolve to the
    freshest label instead of a merged pool.
    """
    best, best_yr = None, -1
    for e in _entries_for_aliases(facts, aliases):
        if e["form"] in _ANNUAL_FORMS and e["year"] > best_yr:
            best, best_yr = e["alias"], e["year"]
    return best


def _group_annual(facts: dict, aliases: list[str]) -> list[tuple[int, float]]:
    """annual_series for the freshest alias in a concept group."""
    alias = _freshest_alias(facts, aliases)
    if alias is None:
        return []
    cands = _candidates([e for e in _entries_for_aliases(facts, [alias])
                         if e["form"] in _ANNUAL_FORMS and e["q"] is None])
    return _pick_annual(cands)


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


def _subset_sum_winner(c: list[float]) -> float | None:
    """A candidate that equals the sum of a strict subset of the others.

    Dimensional duplicates in companyfacts are usually {consolidated} ∪
    {segment cuts}; the consolidated figure is the subset sum. (The old
    sum(c) ~= 2*max check was the special case where the subset is ALL
    other candidates.)
    """
    import itertools
    for i, target in enumerate(c):
        if target == 0:
            continue
        rest = c[:i] + c[i + 1:]
        for r in range(1, min(len(rest), 5) + 1):
            for combo in itertools.combinations(rest, r):
                if not any(combo):
                    continue
                if abs(sum(combo) - target) <= 0.02 * abs(target):
                    return target
    return None


def _pick_annual(cands: dict[tuple, list[float]]) -> list[tuple[int, float]]:
    """Resolve ambiguous annual values.

    After latest-filed grouping, residual multiplicity is dimensional
    (consolidated + segment cuts with dimensions dropped). The consolidated
    figure is either a subset sum of the cuts or, failing that, the maximum
    (a segment cut cannot exceed its consolidated total; the old neighbor-
    interpolation fallback locked onto stale comparative values instead —
    FTNT 2025 assets read 7.26B vs the true 10.39B).
    """
    picked: dict[int, float] = {}
    for (y, q), c in cands.items():
        assert q is None
        if len(c) == 1:
            picked[y] = c[0]
            continue
        picked[y] = _subset_sum_winner(c) or max(c)
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


def _lt_includes_current_year(facts: dict, fy: int) -> bool:
    """Per-year version of the guard: true when the IncludingCurrentMaturities
    caption carries a 10-K value for this fiscal year, so the current-portion
    group must be skipped for the year (avoids double counting)."""
    gaap = (facts.get("facts", {}) or {}).get("us-gaap", {})
    node = gaap.get(
        "LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities")
    if not node:
        return False
    for entries in node.get("units", {}).values():
        for e in entries:
            y, _ = _year_of(e)
            if e.get("form") in _ANNUAL_FORMS and y == fy:
                return True
    return False


def debt_by_fy(facts: dict) -> dict[int, float]:
    """Interest-bearing debt per fiscal year.

    Aggregates the LT group + short-term borrowings + current portion of LT
    debt. Debt captions go stale when filers relabel (APH's LongTermDebt ends
    FY2017; its live caption is ...IncludingCurrentMaturities), so each group
    resolves independently and stale groups simply contribute 0 for recent
    years (e.g. TER repaid its converts — correctly ~0 LT debt now).
    """
    lt = dict(_group_annual(facts, ALIASES["lt_debt"]))
    stb = dict(_group_annual(facts, ALIASES["st_borrow"]))
    ltc = dict(_group_annual(facts, ALIASES["lt_current"]))
    out: dict[int, float] = {}
    for fy in set(lt) | set(stb) | set(ltc):
        total = lt.get(fy, 0.0) or 0.0
        if not _lt_includes_current_year(facts, fy):
            total += ltc.get(fy, 0.0) or 0.0
        total += stb.get(fy, 0.0) or 0.0
        out[fy] = total
    return out


def eps_cagr_split_adj(facts: dict, years: int = 2) -> float | None:
    """2-yr EPS CAGR restated for stock splits.

    Reported diluted EPS breaks across splits (LRCX's 10:1 made naive CAGR
    read -55%), so every year's EPS is restated as NI_fy / shares_latest.
    """
    ni = dict(annual_series(facts, "net_income"))
    sh = dict(annual_series(facts, "shares_out"))
    fys = sorted(set(ni) & set(sh))
    if len(fys) < years + 1:
        return None
    s_last = sh[fys[-1]]
    if not s_last:
        return None
    adj = [(fy, ni[fy] / s_last) for fy in fys[-(years + 1):]
           if ni.get(fy)]
    if len(adj) < years + 1 or adj[0][1] <= 0:
        return None
    return (adj[-1][1] / adj[0][1]) ** (1 / years) - 1


def _yoy(series: list[tuple[int, float]]) -> float | None:
    """1-yr growth; None if the base is non-positive (growth off a loss or
    zero base is meaningless)."""
    if len(series) < 2:
        return None
    _, start = series[-2]
    _, end = series[-1]
    if start <= 0 or end is None:
        return None
    return end / start - 1


def revenue_yoy(facts: dict) -> float | None:
    """1-yr revenue growth (Small Cap 4x Growth gate: >30%)."""
    return _yoy(annual_series(facts, "revenue"))


def eps_yoy_split_adj(facts: dict) -> float | None:
    """1-yr EPS growth restated on the latest diluted share count, so a
    stock split between the two years can't fake (or hide) growth."""
    ni = dict(annual_series(facts, "net_income"))
    sh = dict(annual_series(facts, "shares_out"))
    fys = sorted(set(ni) & set(sh))
    if len(fys) < 2 or not sh[fys[-1]]:
        return None
    s_last = sh[fys[-1]]
    base, last = ni[fys[-2]] / s_last, ni[fys[-1]] / s_last
    if not base or base <= 0:
        return None
    return last / base - 1


def current_ratio_latest(facts: dict) -> float | None:
    """Current assets / current liabilities, latest common fiscal year."""
    ca = dict(annual_series(facts, "curr_assets"))
    cl = dict(annual_series(facts, "curr_liab"))
    fys = sorted(set(ca) & set(cl))
    if not fys:
        return None
    liab = cl[fys[-1]]
    if not liab or liab <= 0:
        return None
    return ca[fys[-1]] / liab


def debt_equity_latest(facts: dict) -> float | None:
    """Interest-bearing debt / book equity (assets - liabilities), latest
    common fiscal year. Uses the same stale-caption-safe debt definition as
    the 11-gate leverage screen."""
    debt_s = debt_by_fy(facts)
    assets_d = dict(annual_series(facts, "assets"))
    liab_d = dict(annual_series(facts, "liabilities"))
    fys = sorted(set(debt_s) & set(assets_d) & set(liab_d))
    if not fys:
        return None
    eq = assets_d[fys[-1]] - liab_d[fys[-1]]
    if not eq or eq <= 0:
        return None
    return debt_s[fys[-1]] / eq


def gross_margin_trend(facts: dict) -> tuple[float | None, bool | None]:
    """(latest gross margin, expanding-vs-prior-year bool). Used for the
    Disruptive Innovator bucket (>50% and expanding)."""
    gp = dict(annual_series(facts, "gross_profit"))
    rv = dict(annual_series(facts, "revenue"))
    fys = sorted(set(gp) & set(rv))

    def _m(fy: int) -> float | None:
        return gp[fy] / rv[fy] if rv[fy] else None

    if not fys:
        return None, None
    latest = _m(fys[-1])
    expanding = None
    if len(fys) >= 2:
        prior = _m(fys[-2])
        if latest is not None and prior is not None:
            expanding = latest > prior
    return latest, expanding


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
    cash_s = annual_series(facts, "cash")
    assets_s = annual_series(facts, "assets")
    liab_s = annual_series(facts, "liabilities")
    # Book equity as assets - liabilities (robust to StockholdersEquity
    # caption mis-picks; see ALIASES note).
    assets_d, liab_d = dict(assets_s), dict(liab_s)
    eq_s = sorted((fy, assets_d[fy] - liab_d[fy])
                  for fy in set(assets_d) & set(liab_d))
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
    debt_s = debt_by_fy(facts)  # per-FY, stale-caption-safe
    cash, equity = _last(cash_s), _last(eq_s)
    debt_latest = debt_s[max(debt_s)] if debt_s else None

    # ROIC = NOPAT / average invested capital (financing side:
    # book equity + interest-bearing debt). Effective tax rate clamped
    # 0-50% with a 21% fallback when no usable rate can be derived.
    opinc_d, eq_d, tax_d, pretax_d = (dict(opinc_s), dict(eq_s),
                                     dict(tax_s), dict(pretax_s))

    def _nopat_ic(fy):
        op, eq = opinc_d.get(fy), eq_d.get(fy)
        if op is None or eq is None:
            return None, None
        tx, pt = tax_d.get(fy), pretax_d.get(fy)
        if tx is not None and pt:
            tax_rate = min(max(tx / pt, 0.0), 0.50)
        else:
            tax_rate = 0.21
        return op * (1 - tax_rate), eq + (debt_s.get(fy) or 0.0)

    roic, roic_inc = None, None
    roic_fys = sorted(set(opinc_d) & set(eq_d) & set(debt_s))
    if len(roic_fys) >= 2:
        nopat1, ic1 = _nopat_ic(roic_fys[-1])
        _, ic0 = _nopat_ic(roic_fys[-2])
        avg_ic = ((ic1 or 0.0) + (ic0 or 0.0)) / 2
        if nopat1 is not None and avg_ic > 0:
            roic = nopat1 / avg_ic
    if len(roic_fys) >= 3:
        # base = the available FY at or just before latest-2 (a missing FY
        # must not shift the window: TER lacks FY2024, so [-3] would grab
        # FY2022 and flip the sign).
        base_cands = [fy for fy in roic_fys[:-1] if fy <= roic_fys[-1] - 2]
        if base_cands:
            base_fy = max(base_cands)
            nopat2, ic2 = _nopat_ic(roic_fys[-1])
            nopat0, ic_0 = _nopat_ic(base_fy)
            if (nopat2 is not None and nopat0 is not None and ic2 is not None
                    and ic_0 is not None and ic2 != ic_0):
                roic_inc = (nopat2 - nopat0) / (ic2 - ic_0)

    ebitda = None
    if opinc is not None:
        ebitda = opinc + (_last(dep_s) or 0.0)
    ebitda_ttm = None
    opinc_ttm = ttm_ytd(facts, "op_income")
    if opinc_ttm is not None:
        ebitda_ttm = opinc_ttm + (ttm_ytd(facts, "depreciation") or 0.0)

    net_debt = ((debt_latest or 0.0) - (cash or 0.0))
    net_debt_ebitda = None
    if (ebitda_ttm or ebitda):
        net_debt_ebitda = net_debt / (ebitda_ttm or ebitda)

    ebit_interest = None
    if opinc is not None and interest:
        ebit_interest = opinc / interest

    fcf_pos_years = sum(1 for _, v in fcf_hist[-4:] if v and v > 0)

    dilution_2y = None
    if len(sh_s) >= 3:
        base, now = sh_s[-3][1], sh_s[-1][1]
        if base:
            dilution_2y = (now - base) / base

    gm_latest, gm_expanding = gross_margin_trend(facts)

    return {
        "ticker": ticker.upper(),
        "cik": cik,
        "fiscal_years": [fy for fy, _ in rev],
        "revenue_history": rev,
        "eps_history": eps,
        "fcf_history": fcf_hist,
        "revenue_ttm": rev_ttm,
        "revenue_cagr_2y": _cagr(rev, 2),
        "revenue_yoy_1y": _yoy(rev),
        "eps_cagr_2y": _cagr(eps, 2),
        "eps_yoy_1y_split_adj": eps_yoy_split_adj(facts),
        "eps_ttm": eps_ttm(facts),
        "current_ratio_latest": current_ratio_latest(facts),
        "debt_equity_latest": debt_equity_latest(facts),
        "gross_margin_latest": gm_latest,
        "gross_margin_expanding": gm_expanding,
        "fcf_ttm": fcf_ttm,
        "fcf_margin": (fcf_ttm / rev_ttm) if fcf_ttm is not None and rev_ttm else None,
        "fcf_positive_years_4": fcf_pos_years,
        "roic": roic,
        "roic_incremental_2y": roic_inc,
        "eps_cagr_2y_split_adj": eps_cagr_split_adj(facts),
        "net_debt_ebitda": net_debt_ebitda,
        "ebit_interest": ebit_interest,
        "ebitda_latest": ebitda,
        "ebitda_ttm": ebitda_ttm,
        "net_debt_latest": net_debt,
        "debt_latest": debt_latest,
        "cash_latest": cash,
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
