"""Failover tests: leadership, scheduling, dedup, and the tier API.

Pure-logic tests use fakes (no Postgres). Live Postgres tests run against
pgserver (embedded) when available and are skipped otherwise.
"""
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import failover

CT = ZoneInfo("America/Chicago")


def dt(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=CT)


# ---------------------------------------------------------------------------
# Scheduling (pure function — no DB)
# ---------------------------------------------------------------------------

class TestIsDue:
    def test_premarket_due_after_7am_weekday(self):
        assert failover.is_due("premarket-7am", dt(2026, 9, 21, 7, 5),
                               dt(2026, 9, 18, 7, 1)) is True

    def test_premarket_not_due_before_7am(self):
        assert failover.is_due("premarket-7am", dt(2026, 9, 21, 6, 55),
                               dt(2026, 9, 18, 7, 1)) is False

    def test_premarket_not_due_twice_same_day(self):
        assert failover.is_due("premarket-7am", dt(2026, 9, 21, 9, 0),
                               dt(2026, 9, 21, 7, 2)) is False

    def test_premarket_never_on_saturday(self):
        # Saturday 2026-09-26
        assert failover.is_due("premarket-7am", dt(2026, 9, 26, 9, 0),
                               dt(2026, 9, 25, 7, 1)) is False

    def test_hourly_watch_due_at_907(self):
        assert failover.is_due("hourly-watch", dt(2026, 9, 21, 9, 7),
                               dt(2026, 9, 21, 8, 7)) is True

    def test_hourly_watch_not_due_at_905(self):
        assert failover.is_due("hourly-watch", dt(2026, 9, 21, 9, 5),
                               dt(2026, 9, 21, 8, 7)) is False

    def test_hourly_watch_not_due_twice_same_hour(self):
        assert failover.is_due("hourly-watch", dt(2026, 9, 21, 9, 30),
                               dt(2026, 9, 21, 9, 8)) is False

    def test_hourly_watch_outside_window(self):
        assert failover.is_due("hourly-watch", dt(2026, 9, 21, 16, 7),
                               dt(2026, 9, 21, 15, 7)) is False

    def test_midcap_monthly(self):
        # Oct 3 2026 is a Saturday -> rolls to Monday Oct 5
        assert failover.is_due("midcap-pipeline", dt(2026, 10, 5, 9, 30),
                               dt(2026, 9, 3, 9, 1)) is True
        assert failover.is_due("midcap-pipeline", dt(2026, 10, 5, 9, 30),
                               dt(2026, 10, 5, 9, 1)) is False
        assert failover.is_due("midcap-pipeline", dt(2026, 10, 4, 9, 30),
                               dt(2026, 9, 3, 9, 1)) is False
        # Nov 3 2026 is a Tuesday -> fires on the 3rd itself
        assert failover.is_due("midcap-pipeline", dt(2026, 11, 3, 9, 30),
                               dt(2026, 10, 5, 9, 1)) is True
        assert failover.is_due("midcap-pipeline", dt(2026, 11, 4, 9, 30),
                               dt(2026, 10, 5, 9, 1)) is False


# ---------------------------------------------------------------------------
# Leadership + dedup with a fake connection (no Postgres needed)
# ---------------------------------------------------------------------------

class FakeConn:
    def __init__(self, lock_result=True, unique_violation=False):
        self.lock_result = lock_result
        self.unique_violation = unique_violation
        self.executed = []
        self.closed = False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

        class R:
            pass
        r = R()
        if "pg_try_advisory_lock" in sql:
            r.fetchone = lambda: (self.lock_result,)
            return r
        if "alert_dedup" in sql and "INSERT" in sql:
            if self.unique_violation:
                import psycopg
                raise psycopg.errors.UniqueViolation("duplicate")
            r.rowcount = 1
            return r
        if "RETURNING id" in sql:
            r.fetchone = lambda: (42,)
            return r
        r.fetchone = lambda: None
        return r

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def fake_db(monkeypatch):
    conns = []

    def fake_get_conn():
        c = FakeConn(lock_result=fake_db.lock_result,
                     unique_violation=fake_db.unique_violation)
        conns.append(c)
        return c

    fake_db.lock_result = True
    fake_db.unique_violation = False
    monkeypatch.setattr(failover, "get_conn", fake_get_conn)
    monkeypatch.setattr(failover, "DATABASE_URL", "fake://db")
    return fake_db


class TestLeadership:
    def test_claims_lock_writes_heartbeat(self, fake_db, monkeypatch):
        monkeypatch.setattr(failover, "audit", lambda *a, **k: None)
        conn = failover.try_become_leader("render")
        assert conn is not None
        assert any("tier_heartbeat" in sql for sql, _ in conn.executed)

    def test_loses_gracefully_when_locked(self, fake_db):
        fake_db.lock_result = False
        conn = failover.try_become_leader("cloudrun")
        assert conn is None

    def test_fail_closed_without_database_url(self, monkeypatch):
        monkeypatch.setattr(failover, "DATABASE_URL", "")
        with pytest.raises(RuntimeError):
            failover.get_conn()


class TestAlertDedup:
    def test_first_alert_allowed(self, fake_db):
        assert failover.should_alert("AAPL") is True

    def test_duplicate_within_hour_blocked(self, fake_db):
        fake_db.unique_violation = True
        assert failover.should_alert("AAPL") is False


class TestWatermark:
    def test_record_stage_upserts(self, fake_db):
        # valid stage: upsert executes without error
        failover.record_stage("premarket-7am", "analysis")

    def test_record_stage_rejects_unknown(self, fake_db):
        with pytest.raises(AssertionError):
            failover.record_stage("premarket-7am", "bogus-stage")


# ---------------------------------------------------------------------------
# Tier API (TestClient, Temporal call mocked out)
# ---------------------------------------------------------------------------

def _wait_for(pred, timeout=5.0):
    """Poll a predicate until true (background-thread assertions)."""
    import time
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return bool(pred())


class TestTierAPI:
    @pytest.fixture
    def client(self, monkeypatch):
        monkeypatch.setenv("WATCHDOG_SECRET", "test-secret")
        monkeypatch.setenv("TIER_NAME", "test-tier")
        import api as api_mod
        import importlib
        importlib.reload(api_mod)
        from fastapi.testclient import TestClient
        return TestClient(api_mod.app), api_mod

    def test_health(self, client):
        c, _ = client
        r = c.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    def test_trigger_rejects_bad_secret(self, client):
        c, _ = client
        r = c.post("/internal/run-due-scans",
                   headers={"X-Watchdog-Secret": "wrong"},
                   json={"triggered_by": "t", "run_id": "1"})
        assert r.status_code == 403

    def test_trigger_accepts_and_runs_due_scans_in_background(
            self, client, monkeypatch, fake_db):
        c, api_mod = client
        audits = []
        monkeypatch.setattr(
            failover, "audit",
            lambda tier, event, detail=None: audits.append(event))
        # freeze "now" to a Monday 7:05am CT -> premarket due
        monkeypatch.setattr(failover, "get_due_scans",
                            lambda now=None: ["premarket-7am"])
        ran = []
        monkeypatch.setattr(api_mod, "_start_temporal_scan",
                            lambda name: ran.append(name) or {"ok": True})
        r = c.post("/internal/run-due-scans",
                   headers={"X-Watchdog-Secret": "test-secret"},
                   json={"triggered_by": "t", "run_id": "1"})
        assert r.status_code == 200
        body = r.json()
        assert body["accepted"] is True
        assert body["tier"] == "test-tier"
        # the scan itself happens in the background thread
        assert _wait_for(lambda: ran == ["premarket-7am"])
        assert _wait_for(lambda: "watchdog_trigger" in audits)

    def test_trigger_returns_before_slow_scan_finishes(
            self, client, monkeypatch, fake_db):
        import time
        c, api_mod = client
        monkeypatch.setattr(failover, "audit", lambda *a, **k: None)
        monkeypatch.setattr(failover, "get_due_scans",
                            lambda now=None: ["premarket-7am"])
        started = []

        def slow_runner(name):
            started.append(name)
            time.sleep(2)  # longer than any watchdog curl timeout
            return {"ok": True}

        monkeypatch.setattr(api_mod, "_start_temporal_scan", slow_runner)
        t0 = time.time()
        r = c.post("/internal/run-due-scans",
                   headers={"X-Watchdog-Secret": "test-secret"},
                   json={"triggered_by": "t", "run_id": "1"})
        elapsed = time.time() - t0
        assert r.status_code == 200
        assert r.json()["accepted"] is True
        # the old synchronous endpoint would have blocked the full 2s
        assert elapsed < 1.5
        assert _wait_for(lambda: started == ["premarket-7am"], timeout=5)

    def test_trigger_contended_leader_still_accepts(
            self, client, monkeypatch, fake_db):
        c, _ = client
        fake_db.lock_result = False
        audits = []
        monkeypatch.setattr(
            failover, "audit",
            lambda tier, event, detail=None: audits.append(event))
        r = c.post("/internal/run-due-scans",
                   headers={"X-Watchdog-Secret": "test-secret"},
                   json={"triggered_by": "t", "run_id": "1"})
        assert r.status_code == 200
        assert r.json()["accepted"] is True
        assert _wait_for(lambda: "leader_contended" in audits)
