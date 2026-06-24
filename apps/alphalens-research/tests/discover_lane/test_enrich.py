import unittest

from alphalens_research.discover_lane.enrich import enrich_candidates
from alphalens_research.discover_lane.models import DiscoverCandidate


def _cand(ticker):
    return DiscoverCandidate(
        ticker=ticker,
        company=ticker,
        theme="t",
        rationale="r",
        citation_count=1,
        citation_urls=["u"],
        source_event_title="t",
        source_event_url="u",
    )


class _FakeYf:
    def __init__(self, mcaps):
        self._mcaps = mcaps

    def market_cap(self, ticker):
        return self._mcaps.get(ticker)


class TestEnrich(unittest.TestCase):
    def test_flags_and_mcap(self):
        yf = _FakeYf({"NVDA": 3.0e12, "ZZZZ": None})
        out = enrich_candidates([_cand("NVDA"), _cand("ZZZZ")], yf_client=yf, universe={"NVDA"})
        by = {c.ticker: c for c in out}
        self.assertEqual(by["NVDA"].mcap, 3.0e12)
        self.assertTrue(by["NVDA"].resolved)
        self.assertTrue(by["NVDA"].in_pipeline_universe)
        self.assertIsNone(by["ZZZZ"].mcap)
        self.assertFalse(by["ZZZZ"].resolved)
        self.assertFalse(by["ZZZZ"].in_pipeline_universe)

    def test_dedups_by_ticker(self):
        yf = _FakeYf({"NVDA": 1.0})
        out = enrich_candidates([_cand("NVDA"), _cand("NVDA")], yf_client=yf, universe=set())
        self.assertEqual(len(out), 1)
