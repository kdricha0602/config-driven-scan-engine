"""Consensus provider + evidence-enrichment tests (all mocked — no live API).

Covers: response parsing, graceful degradation (401/429/timeout/
unexpected shape), 24h cache behavior + query normalization, the post-QC
enrichment activity (query budget, support/contradict wiring, error paths,
no-claim path), and per-scan config gating.
"""
import asyncio
import os
import sys
import urllib.error
import urllib.request
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from providers import consensus as co


def _paper(title, **kw):
    d = {"title": title, "authors": ["A. Author"],
         "journal_name": "EFSA Journal", "publisher_name": "Wiley",
         "publish_year": 2024, "sjr_best_quartile": 1, "citation_count": 5,
         "doi": "10.2903/j.efsa.2024.9100",
         "url": "https://consensus.app/papers/x/1/", "takeaway": "No effect.",
         "full_text_chunks": []}
    d.update(kw)
    return d


SAMPLE = {"results": [
    _paper("Creatine and improvement in cognitive function",
           takeaway="Creatine supplementation has not been established as "
                    "a cause-and-effect relationship."),
    _paper(""),  # untitled — must be skipped
    _paper("Protein and muscle protein synthesis", citation_count=120,
           sjr_best_quartile=2, publish_year=2023),
    _paper("Caffeine and reaction time"),
]}

_N = [0]
_PID = os.getpid()  # per-process token: the /tmp cache persists across runs


def _uniq():
    _N[0] += 1
    return f"test consensus query {_PID} uniqueness {_N[0]}"


class TestParsePapers:
    def test_maps_fields(self):
        items = co.parse_papers(SAMPLE)
        first = items[0]
        assert first["title"].startswith("Creatine and improvement")
        assert first["journal"] == "EFSA Journal"
        assert first["year"] == 2024
        assert first["doi"] == "10.2903/j.efsa.2024.9100"
        assert first["url"].startswith("https://consensus.app/papers/")
        assert "cause-and-effect" in first["takeaway"]
        assert first["citation_count"] == 5
        assert first["quartile"] == 1

    def test_skips_untitled_and_respects_top_n(self):
        assert all(i["title"] for i in co.parse_papers(SAMPLE))
        assert len(co.parse_papers(SAMPLE)) == 3  # default top_n
        assert len(co.parse_papers(SAMPLE, top_n=1)) == 1

    def test_error_result_yields_empty(self):
        assert co.parse_papers({"query": "x", "error": "boom"}) == []


class TestSearch:
    def _mocked(self, payload=SAMPLE):
        cm = mock.MagicMock()
        cm.__enter__.return_value = object()
        return (mock.patch("urllib.request.urlopen", return_value=cm),
                mock.patch.object(co, "add_surrogate_to_request"),
                mock.patch.object(co, "read_json_response",
                                  return_value=payload))

    def test_request_shape_and_surrogate(self):
        q = _uniq()
        p_urlopen, p_surr, p_read = self._mocked()
        with p_urlopen as m_open, p_surr as m_surr, p_read:
            out = co.search(q)
        req = m_open.call_args[0][0]
        assert req.full_url.startswith(
            "https://api.consensus.app/v1/search?query=")
        # Default: full-text chunks OFF — the vendor 403s them on standard
        # plans (verified live 2026-09-23).
        assert "include_full_text_chunks=false" in req.full_url
        m_surr.assert_called_once_with(req, "custom.consensus",
                                       allowed_hosts=["api.consensus.app"])
        assert out["query"] == q.lower()
        assert len(out["results"]) == 4

    def test_include_full_text_chunks_flag(self):
        q = _uniq()
        p_urlopen, p_surr, p_read = self._mocked()
        with p_urlopen as m_open, p_surr, p_read:
            co.search(q, include_full_text_chunks=True)
        req = m_open.call_args[0][0]
        assert "include_full_text_chunks=true" in req.full_url

    def test_401_degrades(self):
        q = _uniq()
        err = urllib.error.HTTPError("http://x", 401, "Unauthorized", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=err), \
                mock.patch.object(co, "add_surrogate_to_request"):
            out = co.search(q)
        assert "error" in out and "401" in out["error"]

    def test_429_and_timeout_degrade(self):
        for exc in (urllib.error.HTTPError("http://x", 429, "Slow", {}, None),
                    urllib.error.URLError("timed out")):
            q = _uniq()
            with mock.patch("urllib.request.urlopen", side_effect=exc), \
                    mock.patch.object(co, "add_surrogate_to_request"):
                out = co.search(q)
            assert "error" in out, exc

    def test_unexpected_shape_degrades(self):
        q = _uniq()
        p_urlopen, p_surr, p_read = self._mocked(payload={"foo": 1})
        with p_urlopen, p_surr, p_read:
            out = co.search(q)
        assert "error" in out

    def test_empty_query_never_hits_network(self):
        with mock.patch("urllib.request.urlopen") as m_open, \
                mock.patch.object(co, "add_surrogate_to_request"):
            out = co.search("   ")
        m_open.assert_not_called()
        assert "error" in out

    def test_cache_and_normalization(self):
        # Hermetic: dict-backed cache fakes, so this never depends on /tmp.
        store: dict = {}

        def fake_get(key, max_age_s):
            return store.get(key)

        def fake_put(key, data):
            store[key] = data

        p_urlopen, p_surr, p_read = self._mocked()
        a_q = f"  What   DOSE  Uniq-Cache-Test-{_PID}  "
        b_q = f"what dose uniq-cache-test-{_PID}"
        with (p_urlopen as m_open, p_surr, p_read,
              mock.patch.object(co, "cache_get", side_effect=fake_get),
              mock.patch.object(co, "cache_put", side_effect=fake_put)):
            a = co.search(a_q)
            b = co.search(b_q)
        assert m_open.call_count == 1
        assert a["results"] == b["results"]
        assert len(store) == 1  # both spellings map to one normalized key


class TestEnrichment:
    def _bundle(self, ticker="NAVN", catalyst="Corporate travel AI booking"):
        import activities
        b = activities.TickerBundle(ticker=ticker)
        b.catalyst_verdict = {"llm": {"catalyst": catalyst},
                              "news_verdict": {"headline": ""}}
        return b

    def _report(self, tickers=("NAVN",)):
        return {"picks": [
            {"ticker": t, "thesis": "Travel platform growth", "key_numbers": "",
             "entry": "", "stop": "", "contradiction": "Valuation high"}
            for t in tickers]}

    def _fake_search(self, calls, error_for=None):
        def _s(q):
            calls.append(q)
            if error_for == "__all__" or (error_for and error_for in q):
                return {"query": q, "error": "HTTPError: HTTP 401"}
            return {"query": q, "results": SAMPLE["results"]}
        return _s

    def test_support_and_contradict_wiring(self):
        import activities
        calls = []
        report = self._report()
        with mock.patch("providers.consensus.search",
                        side_effect=self._fake_search(calls)):
            out = asyncio.run(activities.enrich_consensus_evidence(
                report, [self._bundle()], {}))
        ev = out["picks"][0]["consensus_evidence"]
        assert ev["queries"]["support"] == "Corporate travel AI booking"
        assert ev["queries"]["contradict"] == \
            "evidence against Corporate travel AI booking"
        assert calls == ["Corporate travel AI booking",
                         "evidence against Corporate travel AI booking"]
        assert ev["support"][0]["journal"] == "EFSA Journal"
        assert ev["contradict"][0]["citation_count"] == 5
        assert ev["error"] == ""

    def test_budget_cap_five_queries(self):
        import activities
        calls = []
        report = self._report(("A", "B", "C", "D"))
        bundles = [self._bundle(t, f"catalyst claim {t}")
                   for t in ("A", "B", "C", "D")]
        with mock.patch("providers.consensus.search",
                        side_effect=self._fake_search(calls)):
            out = asyncio.run(activities.enrich_consensus_evidence(
                report, bundles, {}))
        assert len(calls) == 5  # 2+2+1, then the budget is exhausted
        assert out["picks"][2]["consensus_evidence"]["queries"] == \
            {"support": "catalyst claim C"}
        assert out["picks"][3]["consensus_evidence"]["queries"] == {}
        assert out["picks"][2]["consensus_evidence"]["error"] == \
            "query budget exhausted"

    def test_provider_error_fails_closed(self):
        import activities
        calls = []
        report = self._report()
        with mock.patch("providers.consensus.search",
                        side_effect=self._fake_search(calls,
                                                     error_for="__all__")):
            out = asyncio.run(activities.enrich_consensus_evidence(
                report, [self._bundle()], {}))
        ev = out["picks"][0]["consensus_evidence"]
        assert ev["support"] == [] and ev["contradict"] == []
        assert "401" in ev["error"]
        assert calls == ["Corporate travel AI booking",
                         "evidence against Corporate travel AI booking"]

    def test_no_claim_skips_search(self):
        import activities
        calls = []
        b = self._bundle(catalyst="none")
        b.catalyst_verdict = {"llm": {"catalyst": "none"},
                              "news_verdict": {"headline": ""}}
        report = {"picks": [{"ticker": "NAVN", "thesis": "", "key_numbers": "",
                             "entry": "", "stop": "", "contradiction": ""}]}
        with mock.patch("providers.consensus.search",
                        side_effect=self._fake_search(calls)):
            out = asyncio.run(activities.enrich_consensus_evidence(
                report, [b], {}))
        assert calls == []
        assert out["picks"][0]["consensus_evidence"]["error"] == \
            "no claim to search"

    def test_headline_fallback_claim(self):
        import activities
        calls = []
        b = self._bundle(catalyst="none")
        b.catalyst_verdict = {"llm": {"catalyst": "none"},
                              "news_verdict": {"headline": "FDA approval wins"}}
        report = self._report()
        with mock.patch("providers.consensus.search",
                        side_effect=self._fake_search(calls)):
            out = asyncio.run(activities.enrich_consensus_evidence(
                report, [b], {}))
        ev = out["picks"][0]["consensus_evidence"]
        assert ev["queries"]["support"] == "FDA approval wins"


class TestCachePutCleanup:
    def test_failed_write_leaves_no_husk(self):
        from providers import cache as pcache
        key = f"consensus:test put cleanup {_PID}"
        path = pcache._path(key)
        if os.path.exists(path):
            os.remove(path)
        with mock.patch("json.dump", side_effect=OSError(28, "No space")):
            pcache.put(key, {"x": 1})  # must not raise
        assert not os.path.exists(path)
        assert pcache.get(key, 86400) is None


class TestConfigGating:
    def test_only_4x_growth_enables_consensus(self):
        import yaml
        cfg = yaml.safe_load(open(os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "..", "scan-configs.yaml")))
        enabled = [s["scan"]["name"] for s in cfg["scans"]
                   if s["scan"].get("analytics", {})
                   .get("consensus_evidence", False)]
        assert enabled == ["Small Cap 4x Growth"]
