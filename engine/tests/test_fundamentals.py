"""Fundamentals + valuation wiring tests.

Covers the 2026-09-23 SEC hardening ported from the prototype:
end-date year parsing (LRCX), subset-sum/max annual disambiguation (FTNT),
stale debt captions + current-maturity double-count guard (APH),
split-adjusted EPS CAGR (LRCX 10:1), Bigdata NTM math, fetch_valuation
mapping, and the new QC hard-filter checks.
"""
import asyncio
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from providers import sec_edgar as se
from providers import bigdata as bd


def _mk_facts(entries_by_alias):
    facts = {"facts": {"us-gaap": {}}}
    for alias, rows in entries_by_alias.items():
        facts["facts"]["us-gaap"][alias] = {"units": {"USD": [
            {"val": v, "end": f"{y}-12-31", "form": "10-K",
             "filed": "2026-02-01", "fy": y, "fp": "FY"}
            for y, v in rows]}}
    return facts


class TestYearOf:
    def test_prefers_end_over_frame_and_fy(self):
        # LRCX: frame CY2020/fy 2026 on a fact ending 2024-06-30
        e = {"end": "2024-06-30", "frame": "CY2020", "fy": 2026, "fp": "FY"}
        assert se._year_of(e) == (2024, None)

    def test_quarter_from_fp(self):
        e = {"end": "2026-06-30", "fp": "Q2"}
        assert se._year_of(e) == (2026, 2)

    def test_instant_for_balance_sheet(self):
        e = {"instant": "2025-12-31", "fy": 2025, "fp": "FY"}
        assert se._year_of(e) == (2025, None)


class TestPickAnnual:
    def test_subset_sum_picks_consolidated(self):
        cands = {(2025, None): [10.39e9, 5.20e9, 5.19e9]}
        assert se._pick_annual(cands) == [(2025, 10.39e9)]

    def test_max_fallback_not_interpolation(self):
        # FTNT 2025 assets: old code interpolated to 7.26B via stale
        # comparatives; the max (10.39B) is the consolidated figure.
        cands = {(2025, None): [7.26e9, 9.76e9, 10.39e9]}
        assert se._pick_annual(cands) == [(2025, 10.39e9)]

    def test_single_candidate_untouched(self):
        cands = {(2024, None): [18.44e9]}
        assert se._pick_annual(cands) == [(2024, 18.44e9)]


class TestDebtByFy:
    def test_skips_current_when_lt_includes_maturities(self):
        # APH: live caption already includes current maturities
        f = _mk_facts({
            "LongTermDebtAndCapitalLeaseObligations"
            "IncludingCurrentMaturities": [(2025, 15.5e9)],
            "LongTermDebtAndCapitalLeaseObligationsCurrent": [(2025, 0.94e9)],
            "ShortTermBorrowings": [(2025, 0.10e9)],
        })
        d = se.debt_by_fy(f)
        assert d[2025] == pytest.approx(15.6e9)

    def test_adds_current_when_separate_caption(self):
        f = _mk_facts({
            "LongTermDebtNoncurrent": [(2025, 0.99e9)],
            "LongTermDebtCurrent": [(2025, 0.50e9)],
        })
        d = se.debt_by_fy(f)
        assert d[2025] == pytest.approx(1.49e9)

    def test_stale_caption_does_not_win(self):
        # stale LongTermDebt (ends 2017) must not override the live caption
        f = _mk_facts({
            "LongTermDebt": [(2017, 3.54e9)],
            "LongTermDebtAndCapitalLeaseObligations"
            "IncludingCurrentMaturities": [(2025, 15.5e9)],
        })
        d = se.debt_by_fy(f)
        assert d[2025] == pytest.approx(15.5e9)


class TestSplitAdjCagr:
    def test_survives_10_to_1_split(self):
        f = _mk_facts({
            "NetIncomeLoss": [(2024, 5.0e9), (2025, 6.0e9), (2026, 7.0e9)],
            "WeightedAverageNumberOfDilutedSharesOutstanding":
                [(2024, 1.0e9), (2025, 1.0e9), (2026, 10.0e9)],
        })
        cagr = se.eps_cagr_split_adj(f)
        # restated on latest shares: 0.5, 0.6, 0.7
        assert cagr == pytest.approx((0.7 / 0.5) ** 0.5 - 1)


class TestBigdataMath:
    def test_ntm_eps_time_weights(self):
        asof = date(2026, 9, 23)
        ests = [(date(2026, 12, 31), 4.0, 10),
                (date(2027, 12, 31), 5.0, 10)]
        # 99 days of FY26 + 266 days of FY27 in the window
        assert bd.ntm_eps(ests, asof) == pytest.approx(
            (99 * 4.0 + 266 * 5.0) / 365, abs=0.02)

    def test_ntm_eps_none_when_no_coverage(self):
        assert bd.ntm_eps([(date(2030, 12, 31), 4.0, 10)],
                          date(2026, 9, 23)) is None

    def test_fwd_growth(self):
        ests = [(date(2026, 12, 31), 4.0, 10),
                (date(2027, 12, 31), 5.0, 10)]
        assert bd.fwd_growth(ests, date(2026, 9, 23)) == pytest.approx(0.25)


class TestCriteriaChecks:
    def _bundle(self):
        from activities import TickerBundle, Fundamentals, Valuation
        b = TickerBundle(ticker="TST")
        b.market_cap, b.price = 10e9, 100.0
        b.fundamentals = Fundamentals(
            ticker="TST", roic=0.20, revenue_cagr_2y=0.15,
            eps_cagr_2y_split_adj=0.12, fcf_positive_years_4=4,
            fcf_margin=0.20, net_debt_ebitda=0.5, ebit_interest=10.0)
        b.valuation = Valuation(
            ticker="TST", ev_ebitda_ttm=12.0, fcf_yield_ttm=0.05,
            gate7_pass=True)
        return b

    def _scan(self):
        return {"hard_filters": {
                    "roic": {"min": "14%"},
                    "revenue_cagr": {"min": "9%"},
                    "eps_cagr": {"min": "9%"},
                    "fcf": {"positive_years": 3, "margin_min": "6%"},
                    "net_debt_ebitda": {"max": 2.75},
                    "ebit_interest": {"min": 3.5},
                    "valuation": {"ev_ebitda_max": 15,
                                  "fcf_yield_min": "4%"},
                    "gate7": {"require_pass": True}},
                "universe": {}}

    def test_all_fundamental_gates_pass(self):
        from activities import _criteria_checks
        checks = dict(_criteria_checks(self._bundle(), self._scan()))
        for name in ("roic_min", "revenue_cagr_min", "eps_cagr_min",
                     "fcf_history_margin", "leverage_max", "coverage_min",
                     "valuation_gate8", "gate7_forward_value"):
            assert checks[name] is True, name

    def test_gate8_or_logic(self):
        from activities import _criteria_checks
        b = self._bundle()
        b.valuation.ev_ebitda_ttm = 30.0  # too expensive...
        assert dict(_criteria_checks(b, self._scan()))[
            "valuation_gate8"] is True  # ...but 5% FCF yield saves it
        b.valuation.fcf_yield_ttm = 0.01
        assert dict(_criteria_checks(b, self._scan()))[
            "valuation_gate8"] is False

    def test_gate7_fail_closed(self):
        from activities import _criteria_checks
        b = self._bundle()
        b.valuation.gate7_pass = False
        assert dict(_criteria_checks(b, self._scan()))[
            "gate7_forward_value"] is False
        b.valuation.gate7_pass = None  # Bigdata outage: no pass either
        assert dict(_criteria_checks(b, self._scan()))[
            "gate7_forward_value"] is False

    def test_unconfigured_filters_not_checked(self):
        from activities import _criteria_checks
        b = self._bundle()
        b.fundamentals.roic = 0.01
        checks = dict(_criteria_checks(b, {"hard_filters": {},
                                           "universe": {}}))
        assert "roic_min" not in checks


class TestFetchValuation:
    def test_maps_gate7_and_trailing_multiples(self, monkeypatch):
        import activities
        monkeypatch.setattr(
            "providers.bigdata.gate7",
            lambda t, p: {"fwd_pe": 26.7, "ntm_eps": 3.08,
                          "fwd_growth_pct": 22.6, "peg": 1.18,
                          "peer_median_fwd_pe": 27.4,
                          "discount_vs_peers_pct": 2.6,
                          "peers_used": 10, "pass": False})
        monkeypatch.setattr(
            "providers.sec_edgar.fundamentals",
            lambda t: {"debt_latest": 15.5e9, "cash_latest": 3.3e9,
                       "ebitda_ttm": 9.0e9, "fcf_ttm": 4.97e9})
        v = asyncio.run(activities.fetch_valuation("APH", 82.2, 200e9))
        assert v.fwd_pe == 26.7
        assert v.peg == 1.18
        assert v.gate7_pass is False
        assert v.ev_ebitda_ttm == pytest.approx(
            (200e9 + 15.5e9 - 3.3e9) / 9.0e9)
        assert v.fcf_yield_ttm == pytest.approx(4.97e9 / 200e9)

    def test_bigdata_outage_degrades(self, monkeypatch):
        import activities
        monkeypatch.setattr(
            "providers.bigdata.gate7",
            lambda t, p: {"ticker": t, "error": "boom"})
        monkeypatch.setattr(
            "providers.sec_edgar.fundamentals",
            lambda t: {"debt_latest": 1e9, "cash_latest": 2e9,
                       "ebitda_ttm": 2e9, "fcf_ttm": 1e9})
        v = asyncio.run(activities.fetch_valuation("TST", 50.0, 10e9))
        assert v.error == "boom"
        assert v.gate7_pass is None
        assert v.ev_ebitda_ttm == pytest.approx((10e9 + 1e9 - 2e9) / 2e9)


class TestSmallCap4xGates:
    def test_revenue_yoy(self):
        f = _mk_facts({"Revenues": [(2024, 100.0e6), (2025, 140.0e6)]})
        assert se.revenue_yoy(f) == pytest.approx(0.40)

    def test_revenue_yoy_none_on_loss_base(self):
        f = _mk_facts({"Revenues": [(2024, -10.0e6), (2025, 140.0e6)]})
        assert se.revenue_yoy(f) is None

    def test_eps_yoy_split_adj(self):
        # 2:1 split in 2025: share count doubles, NI restated on latest
        f = _mk_facts({
            "NetIncomeLoss": [(2024, 10.0e6), (2025, 30.0e6)],
            "WeightedAverageNumberOfDilutedSharesOutstanding":
                [(2024, 10.0e6), (2025, 20.0e6)],
        })
        # adj EPS: 0.5 -> 1.5 = +200%
        assert se.eps_yoy_split_adj(f) == pytest.approx(2.0)

    def test_current_ratio(self):
        f = _mk_facts({
            "AssetsCurrent": [(2025, 300.0e6)],
            "LiabilitiesCurrent": [(2025, 150.0e6)],
        })
        assert se.current_ratio_latest(f) == pytest.approx(2.0)

    def test_current_ratio_missing_leg(self):
        f = _mk_facts({"AssetsCurrent": [(2025, 300.0e6)]})
        assert se.current_ratio_latest(f) is None

    def test_debt_equity_stale_caption(self):
        # stale LongTermDebt (2020) + live ...IncludingCurrentMaturities
        f = _mk_facts({
            "LongTermDebt": [(2020, 500.0e6)],
            "LongTermDebtAndCapitalLeaseObligations"
            "IncludingCurrentMaturities": [(2025, 100.0e6)],
            "Assets": [(2025, 1.0e9)],
            "Liabilities": [(2025, 400.0e6)],
        })
        # debt 100M / equity (1000-400)M = 1/6
        assert se.debt_equity_latest(f) == pytest.approx(1 / 6)

    def test_debt_equity_none_on_negative_equity(self):
        f = _mk_facts({
            "LongTermDebtNoncurrent": [(2025, 100.0e6)],
            "Assets": [(2025, 300.0e6)],
            "Liabilities": [(2025, 400.0e6)],
        })
        assert se.debt_equity_latest(f) is None

    def test_gross_margin_trend(self):
        f = _mk_facts({
            "GrossProfit": [(2024, 40.0e6), (2025, 70.0e6)],
            "Revenues": [(2024, 100.0e6), (2025, 140.0e6)],
        })
        m, expanding = se.gross_margin_trend(f)
        assert m == pytest.approx(0.50)
        assert expanding is True

    def test_gross_margin_contracting(self):
        f = _mk_facts({
            "GrossProfit": [(2024, 60.0e6), (2025, 56.0e6)],
            "Revenues": [(2024, 100.0e6), (2025, 140.0e6)],
        })
        m, expanding = se.gross_margin_trend(f)
        assert m == pytest.approx(0.40)
        assert expanding is False


class TestCriteriaChecks4x:
    def _scan(self):
        return {"hard_filters": {
                    "price": {"min": 2, "max": 25},
                    "above_200dma": {"require": True},
                    "revenue_yoy": {"min": "30%"},
                    "eps_growth": {"min": "25%"},
                    "current_ratio": {"min": 1.5},
                    "debt_equity": {"max": 0.5}},
                "universe": {"max_candidates": 1200}}

    def _bundle(self):
        from activities import TickerBundle, Fundamentals, Technicals
        b = TickerBundle(ticker="TST")
        b.price = 10.0
        t = Technicals(ticker="TST")
        t.sma200 = 8.0
        b.technicals = t
        b.fundamentals = Fundamentals(
            ticker="TST", revenue_yoy_1y=0.45, eps_growth_1y=0.60,
            eps_growth_source="forward_estimates", eps_growth_analysts=5,
            current_ratio=2.2, debt_equity=0.30)
        return b

    def test_all_4x_gates_pass(self):
        from activities import _criteria_checks
        checks = dict(_criteria_checks(self._bundle(), self._scan()))
        for name in ("price_range", "above_200dma", "revenue_yoy_min",
                     "eps_growth_min", "current_ratio_min",
                     "debt_equity_max"):
            assert checks[name] is True, name

    def test_each_4x_gate_fails_closed(self):
        from activities import _criteria_checks
        cases = [
            ("revenue_yoy_1y", 0.10, "revenue_yoy_min"),
            ("eps_growth_1y", 0.10, "eps_growth_min"),
            ("current_ratio", 1.0, "current_ratio_min"),
            ("debt_equity", 0.9, "debt_equity_max"),
        ]
        for field, bad, check in cases:
            b = self._bundle()
            setattr(b.fundamentals, field, bad)
            assert dict(_criteria_checks(b, self._scan()))[check] is False, check
        b = self._bundle()
        b.fundamentals.eps_growth_1y = None  # no data -> fail closed
        assert dict(_criteria_checks(b, self._scan()))["eps_growth_min"] is False
        b = self._bundle()
        b.technicals.sma200 = 12.0  # price below 200DMA
        assert dict(_criteria_checks(b, self._scan()))["above_200dma"] is False

    def test_microcap_gates_not_required_when_unconfigured(self):
        from activities import _criteria_checks
        checks = dict(_criteria_checks(self._bundle(), self._scan()))
        for name in ("float", "rvol", "daily_move", "volume", "catalyst",
                     "market_cap_range"):
            assert name not in checks, name


class TestFetchForwardGrowth:
    def test_maps_success(self, monkeypatch):
        import activities
        monkeypatch.setattr(
            "providers.bigdata.forward_eps_growth",
            lambda t: {"growth": 0.35, "analysts": 4})
        r = asyncio.run(activities.fetch_forward_growth("TST"))
        assert r == {"growth": 0.35, "analysts": 4, "error": ""}

    def test_degrades_gracefully(self, monkeypatch):
        import activities

        def _boom(t):
            raise RuntimeError("no Bigdata entity for TST")

        monkeypatch.setattr("providers.bigdata.forward_eps_growth", _boom)
        r = asyncio.run(activities.fetch_forward_growth("TST"))
        assert r["growth"] is None
        assert "RuntimeError" in r["error"]
