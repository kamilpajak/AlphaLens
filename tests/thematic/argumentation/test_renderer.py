import unittest

import pandas as pd

from alphalens.thematic.argumentation import renderer

_BRIEF = {
    "tldr": "QUBT pure-play benefits from NVIDIA Ising tooling.",
    "supply_chain_reasoning": "Ising lowers deployment friction; QUBT photonic processors get pulled in. ETF inclusion in QTUM signals institutional flow.",
    "bear_summary": "Pre-revenue; zero insider buying in 90d; valuation at 1st percentile.",
    "catalyst_failure_exit": "Exit if Q1 cash burn widens or NVIDIA drops quantum roadmap.",
    "entry_price_note": "prefer 5-10 bps below current.",
    "model_used": "gemini-2.5-flash",
}

_ROW = {
    "ticker": "QUBT",
    "company_name": "Quantum Computing Inc",
    "theme": "quantum_computing",
    "industry_name": "Computer Hardware",
    "sector_name": "Technology",
    "layer4_weighted_score": 2,
    "insider_score_usd": 0.0,
    "insider_score_sector_percentile": 50.0,
    "fcff_yield_pct": None,
    "fcff_yield_sector_percentile": None,
    "valuation_composite_sector_percentile": 1.0,
    "technicals_summary_str": "RSI 60 / MA50 +4.1% / ATR 6.6% / volZ 3.8",
    "gates_passed_str": "tenk,press",
}


class TestRenderMarkdown(unittest.TestCase):
    def test_includes_header_with_ticker_and_score(self):
        md = renderer.render_markdown(_ROW, _BRIEF)
        self.assertIn("QUBT", md)
        self.assertIn("Quantum Computing Inc", md)
        self.assertIn("conf 2/5", md)
        self.assertIn("quantum_computing", md)
        self.assertIn("Computer Hardware", md)

    def test_includes_all_brief_sections(self):
        md = renderer.render_markdown(_ROW, _BRIEF)
        self.assertIn("Thesis", md)
        self.assertIn(_BRIEF["tldr"], md)
        self.assertIn("Supply chain", md)
        self.assertIn(_BRIEF["supply_chain_reasoning"], md)
        self.assertIn("Bear case", md)
        self.assertIn(_BRIEF["bear_summary"], md)
        self.assertIn("Catalyst-failure exit", md)
        self.assertIn(_BRIEF["catalyst_failure_exit"], md)

    def test_includes_setup_specs(self):
        md = renderer.render_markdown(_ROW, _BRIEF)
        self.assertIn("Setup", md)
        self.assertIn(_BRIEF["entry_price_note"], md)
        self.assertIn("stop -25%", md)
        self.assertIn("8w", md)

    def test_includes_signal_panel(self):
        md = renderer.render_markdown(_ROW, _BRIEF)
        self.assertIn("insider", md.lower())
        self.assertIn("FCFF", md)
        self.assertIn("RSI 60", md)
        self.assertIn("tenk,press", md)

    def test_target_length_500_to_1500_chars(self):
        md = renderer.render_markdown(_ROW, _BRIEF)
        self.assertGreaterEqual(len(md), 500)
        self.assertLessEqual(len(md), 1500)

    def test_handles_none_numerical_values(self):
        row = dict(_ROW)
        row["insider_score_usd"] = None
        row["fcff_yield_pct"] = None
        row["valuation_composite_sector_percentile"] = None
        md = renderer.render_markdown(row, _BRIEF)
        self.assertIn("n/a", md)
        # Should not raise on missing numericals.
        self.assertIn("QUBT", md)


class TestRenderMarkdownGracefulDegradation(unittest.TestCase):
    """When the LLM brief is missing / partial, deterministic facts must still render."""

    def test_brief_none_still_renders_ticker_header_and_signals(self):
        md = renderer.render_markdown(_ROW, None)
        # Deterministic sections present.
        self.assertIn("QUBT", md)
        self.assertIn("Quantum Computing Inc", md)
        self.assertIn("conf 2/5", md)
        self.assertIn("quantum_computing", md)
        self.assertIn("RSI 60", md)
        self.assertIn("tenk,press", md)
        # Prose sections render placeholders, not silent omission.
        self.assertIn("Thesis", md)
        self.assertIn("Supply chain", md)
        self.assertIn("Bear case", md)
        # Operator-visible note explaining the degraded state.
        self.assertIn("LLM brief unavailable", md)

    def test_brief_with_catalyst_in_row_renders_catalyst_line_when_brief_none(self):
        row = {
            **_ROW,
            "source_event_url": "https://example.com/news",
            "source_event_title": "Headline",
            "source_event_published_at": "2026-04-14",
        }
        md = renderer.render_markdown(row, None)
        self.assertIn("Catalyst", md)
        self.assertIn("https://example.com/news", md)

    def test_partial_brief_renders_present_fields_and_placeholders_for_missing(self):
        partial = {"tldr": "Thesis present.", "model_used": "gemini-2.5-flash"}
        md = renderer.render_markdown(_ROW, partial)
        self.assertIn("Thesis present.", md)
        self.assertIn("Bear case", md)  # heading still rendered
        # Missing fields → placeholder, not silent drop.
        self.assertIn("_unavailable_", md)

    def test_brief_none_does_not_render_global_unavailable_block(self):
        md = renderer.render_markdown(_ROW, None)
        # The legacy "(brief unavailable)" sentinel must not appear — the
        # whole-block replacement is what this refactor eliminates.
        self.assertNotIn("(brief unavailable)", md)


class TestRenderDayBundle(unittest.TestCase):
    def test_concatenates_briefs_with_separator(self):
        briefs_df = pd.DataFrame(
            [
                {**_ROW, "brief_full_md": "## QUBT brief..."},
                {**_ROW, "ticker": "RGTI", "brief_full_md": "## RGTI brief..."},
            ]
        )
        bundle = renderer.render_day_bundle(briefs_df, asof_str="2026-04-14")
        self.assertIn("2026-04-14", bundle)
        self.assertIn("## QUBT brief...", bundle)
        self.assertIn("## RGTI brief...", bundle)
        self.assertIn("---", bundle)  # markdown horizontal rule between briefs

    def test_empty_df_returns_short_header(self):
        bundle = renderer.render_day_bundle(pd.DataFrame(), asof_str="2026-04-14")
        self.assertIn("2026-04-14", bundle)
        self.assertIn("no", bundle.lower())  # "no briefs" style message


if __name__ == "__main__":
    unittest.main()
