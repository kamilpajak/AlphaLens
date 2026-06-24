import unittest

from alphalens_research.discover_lane.compare import compare_candidates
from alphalens_research.discover_lane.models import BriefCandidate, DiscoverCandidate


def _disc(ticker, mcap):
    return DiscoverCandidate(
        ticker=ticker,
        company=ticker,
        theme="t",
        rationale="r",
        citation_count=1,
        citation_urls=["u"],
        source_event_title="t",
        source_event_url="u",
        mcap=mcap,
        resolved=mcap is not None,
    )


def _brief(ticker, mcap):
    return BriefCandidate(
        ticker=ticker, company=ticker, theme="t", source_event_title="t", mcap=mcap
    )


class TestCompare(unittest.TestCase):
    def test_overlap_and_medians(self):
        discover = [_disc("NVDA", 3.0e12), _disc("AMD", 2.0e11)]
        brief = [_brief("AMD", 2.0e11), _brief("SMCI", 2.0e10)]
        res = compare_candidates(discover, brief)
        self.assertEqual(res.shared, ["AMD"])
        self.assertEqual(res.perplexity_only, ["NVDA"])
        self.assertEqual(res.brief_only, ["SMCI"])
        self.assertEqual(res.discover_median_mcap, 1.6e12)  # median(3e12, 2e11)
        self.assertEqual(res.brief_median_mcap, 1.1e11)  # median(2e11, 2e10)

    def test_handles_missing_mcap(self):
        res = compare_candidates([_disc("X", None)], [])
        self.assertIsNone(res.discover_median_mcap)
        self.assertIsNone(res.brief_median_mcap)
        self.assertEqual(res.perplexity_only, ["X"])
