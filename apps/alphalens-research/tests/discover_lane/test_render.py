import unittest

from alphalens_research.discover_lane.models import (
    BriefCandidate,
    ComparisonResult,
    DateBlock,
    DiscoverCandidate,
)
from alphalens_research.discover_lane.render import render_report


class TestRender(unittest.TestCase):
    def test_renders_html_with_tickers_and_sources(self):
        disc = DiscoverCandidate(
            ticker="NVDA",
            company="NVIDIA",
            theme="AI chips",
            rationale="AI demand",
            citation_count=29,
            citation_urls=["https://a.com"],
            source_event_title="AI chip prices double",
            source_event_url="https://a.com",
            mcap=3.0e12,
            resolved=True,
            in_pipeline_universe=False,
        )
        brief = BriefCandidate(
            ticker="SMCI",
            company="Super Micro",
            theme="AI servers",
            source_event_title="server demand",
            mcap=2.0e10,
        )
        cmp = ComparisonResult(
            shared=[],
            perplexity_only=["NVDA"],
            brief_only=["SMCI"],
            discover_median_mcap=3.0e12,
            brief_median_mcap=2.0e10,
        )
        block = DateBlock(date="2026-06-23", discover=[disc], brief=[brief], comparison=cmp)
        html = render_report([block], generated_stamp="2026-06-24T10:00:00Z")
        self.assertIn("<html", html.lower())
        self.assertIn("NVDA", html)
        self.assertIn("SMCI", html)
        self.assertIn("29", html)  # citation count
        self.assertIn("2026-06-23", html)  # date header
        self.assertIn("2026-06-24T10:00:00Z", html)  # generated stamp

    def test_escapes_html_in_text(self):
        disc = DiscoverCandidate(
            ticker="X",
            company="A & B <Co>",
            theme="t",
            rationale="r",
            citation_count=1,
            citation_urls=[],
            source_event_title="t",
            source_event_url="u",
        )
        cmp = ComparisonResult(
            shared=[],
            perplexity_only=["X"],
            brief_only=[],
            discover_median_mcap=None,
            brief_median_mcap=None,
        )
        block = DateBlock(date="2026-06-23", discover=[disc], brief=[], comparison=cmp)
        html = render_report([block], generated_stamp="s")
        self.assertIn("A &amp; B &lt;Co&gt;", html)
