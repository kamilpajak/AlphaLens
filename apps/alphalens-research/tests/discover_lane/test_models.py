import unittest

from alphalens_research.discover_lane import __status__
from alphalens_research.discover_lane.models import (
    ComparisonResult,
    DateBlock,
    DiscoverCandidate,
)


class TestModels(unittest.TestCase):
    def test_status_is_research_only(self):
        self.assertEqual(__status__, "RESEARCH_ONLY")

    def test_discover_candidate_defaults(self):
        c = DiscoverCandidate(
            ticker="NVDA",
            company="NVIDIA",
            theme="AI chips",
            rationale="benefits from AI demand",
            citation_count=29,
            citation_urls=["https://example.com/a"],
            source_event_title="AI chip prices double",
            source_event_url="https://example.com/a",
        )
        self.assertIsNone(c.mcap)
        self.assertFalse(c.resolved)
        self.assertFalse(c.in_pipeline_universe)

    def test_comparison_and_dateblock_construct(self):
        cmp = ComparisonResult(
            shared=["NVDA"],
            perplexity_only=[],
            brief_only=["ABC"],
            discover_median_mcap=1.0,
            brief_median_mcap=2.0,
        )
        block = DateBlock(date="2026-06-23", discover=[], brief=[], comparison=cmp)
        self.assertEqual(block.date, "2026-06-23")
        self.assertEqual(cmp.shared, ["NVDA"])
