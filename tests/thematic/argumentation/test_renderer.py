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

    def test_handles_pandas_null_types_in_prose_fields(self):
        # Defense against parquet round-trip producing pd.NaT / pd.NA in
        # prose fields (zen review 2026-05-17 M1 finding). With the old
        # float-only _is_nan helper these would render as literal "NaT"/
        # "<NA>" text; pd.isna handles them uniformly.
        brief_with_nulls = {
            **_BRIEF,
            "tldr": pd.NaT,
            "entry_price_note": pd.NA,
        }
        md = renderer.render_markdown(_ROW, brief_with_nulls)
        self.assertIn("_unavailable_", md)
        self.assertNotIn("NaT", md)
        self.assertNotIn("<NA>", md)


class TestRenderMarkdownLayout(unittest.TestCase):
    """Visual layout: blank lines between Theme / Catalyst / Pattern so the
    operator's eye lands cleanly on each scan-bucket cue, and Signals +
    Verified gates rendered as a markdown table instead of one wide bar."""

    def test_blank_lines_between_all_sections(self):
        # Every bold-prefixed section is followed by a blank line before
        # the next one, so the operator's eye lands on each cue cleanly.
        # Covers: Theme → Catalyst → Pattern → Thesis → Supply chain →
        # Bear case → Setup → Catalyst-failure exit → signals table.
        row = {
            **_ROW,
            "source_event_url": "https://example.com/news",
            "source_event_title": "Headline",
            "source_event_published_at": "2026-04-14",
            "technical_pct_off_52w_high": -67.0,
            "technical_ma200_distance_pct": -39.0,
            "technical_ma200_slope_pct_per_day": -0.31,
        }
        md = renderer.render_markdown(row, _BRIEF)
        section_markers = [
            "**Theme**",
            "**Catalyst**",
            "**Pattern**",
            "**Thesis**",
            "**Supply chain**",
            "**Bear case**",
            "**Setup**",
            "**Catalyst-failure exit**",
            "| Signal | Value |",
        ]
        # All markers present in order.
        positions = [md.index(m) for m in section_markers]
        self.assertEqual(positions, sorted(positions))
        # Between each consecutive pair there must be a blank line (\n\n).
        for prev, nxt in zip(section_markers, section_markers[1:]):
            chunk = md[md.index(prev) : md.index(nxt)]
            self.assertIn("\n\n", chunk, f"missing blank line between {prev} and {nxt}")

    def test_signals_rendered_as_markdown_table(self):
        md = renderer.render_markdown(_ROW, _BRIEF)
        # Table header + separator row.
        self.assertIn("| Signal | Value |", md)
        self.assertIn("|---|---|", md)
        # Each row keyed by a recognizable label.
        self.assertIn("| Insider 90d opportunistic |", md)
        self.assertIn("| FCFF yield |", md)
        self.assertIn("| Valuation composite |", md)
        self.assertIn("| Technicals |", md)
        self.assertIn("| Verified gates |", md)

    def test_signals_table_omits_legacy_inline_signals_line(self):
        # The old "**Signals**: insider $0k ... | ..." bar should be gone —
        # the table replaces it. Guard against a regression that prints both.
        md = renderer.render_markdown(_ROW, _BRIEF)
        self.assertNotIn("**Signals**:", md)

    def test_signals_table_includes_earnings_row_when_present(self):
        brief_with_earnings = {**_BRIEF, "next_earnings_date": "2026-05-08"}
        md = renderer.render_markdown(_ROW, brief_with_earnings)
        self.assertIn("| Next earnings |", md)
        self.assertIn("2026-05-08", md)

    def test_signals_table_skips_earnings_row_when_absent(self):
        md = renderer.render_markdown(_ROW, _BRIEF)
        self.assertNotIn("| Next earnings |", md)


class TestRenderInsiderValueDisplay(unittest.TestCase):
    """Insider row display dispatches on score: ``None`` → n/a,
    ``0`` → "no opportunistic buys" (descriptor, not pctile — high pctile
    of tied zeros is mathematically true but reads as positive in UI),
    positive → "(pctile X)". Bug 6 from 2026-05-18 audit."""

    def test_insider_zero_score_shows_no_activity_not_pctile(self):
        row = {**_ROW, "insider_score_usd": 0.0, "insider_score_sector_percentile": 96.0}
        md = renderer.render_markdown(row, _BRIEF)
        self.assertIn("$0k (no opportunistic buys)", md)
        # Pctile token must be absent from the insider row.
        self.assertNotIn("pctile 96", md)

    def test_insider_positive_score_keeps_pctile_display(self):
        row = {
            **_ROW,
            "insider_score_usd": 250_000.0,
            "insider_score_sector_percentile": 80.0,
        }
        md = renderer.render_markdown(row, _BRIEF)
        self.assertIn("$250k (pctile 80)", md)

    def test_insider_none_score_renders_na(self):
        row = {
            **_ROW,
            "insider_score_usd": None,
            "insider_score_sector_percentile": None,
        }
        md = renderer.render_markdown(row, _BRIEF)
        # n/a value with n/a pctile — no Form-4 data at all.
        self.assertIn("n/a (pctile n/a)", md)


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


class TestClassifySetupPattern(unittest.TestCase):
    """Pure helper exposing the technical-setup pattern label so the
    renderer + operator both have a one-word handle on the cherry-pick
    candidate type (per PR δ doctrine: 52w / MA200 are first-class UI
    citizens, not ornaments).
    """

    def test_deep_drawdown_when_far_below_52w_high(self):
        # QUBT-like: -67% off 52w high. Worth a closer look.
        row = {"technical_pct_off_52w_high": -67.0, "technical_ma200_distance_pct": -39.0}
        self.assertEqual(renderer.classify_setup_pattern(row), "deep_drawdown")

    def test_extended_when_at_or_above_52w_high_and_parabolic_ma200(self):
        # FORM-like: 0% off 52w high + MA200 +120%. Already rallied.
        row = {"technical_pct_off_52w_high": 0.0, "technical_ma200_distance_pct": 120.0}
        self.assertEqual(renderer.classify_setup_pattern(row), "extended")

    def test_neutral_when_neither_extreme(self):
        row = {"technical_pct_off_52w_high": -15.0, "technical_ma200_distance_pct": -3.0}
        self.assertEqual(renderer.classify_setup_pattern(row), "neutral")

    def test_unknown_when_inputs_missing(self):
        row = {"technical_pct_off_52w_high": None, "technical_ma200_distance_pct": None}
        self.assertEqual(renderer.classify_setup_pattern(row), "unknown")

    def test_unknown_when_pandas_null_inputs(self):
        row = {"technical_pct_off_52w_high": pd.NA, "technical_ma200_distance_pct": float("nan")}
        self.assertEqual(renderer.classify_setup_pattern(row), "unknown")


class TestRenderMarkdownPatternLine(unittest.TestCase):
    """render_markdown surfaces the pattern label + supporting numbers
    as a dedicated **Pattern** line so the operator can scan-classify
    candidates without parsing the full signal panel.
    """

    def test_pattern_line_present_for_deep_drawdown(self):
        row = {
            **_ROW,
            "technical_pct_off_52w_high": -67.0,
            "technical_ma200_distance_pct": -39.0,
            "technical_ma200_slope_pct_per_day": -0.31,
        }
        md = renderer.render_markdown(row, _BRIEF)
        self.assertIn("**Pattern**", md)
        self.assertIn("deep drawdown", md)
        self.assertIn("-67", md)  # the supporting number
        # Pattern line must appear between Catalyst and Thesis so the
        # operator reads it as scan-bucket context, not deep in the body.
        self.assertLess(md.index("**Pattern**"), md.index("**Thesis**"))

    def test_pattern_line_present_for_extended(self):
        row = {
            **_ROW,
            "technical_pct_off_52w_high": 0.0,
            "technical_ma200_distance_pct": 120.0,
        }
        md = renderer.render_markdown(row, _BRIEF)
        self.assertIn("**Pattern**: extended", md)

    def test_pattern_line_present_for_neutral(self):
        row = {
            **_ROW,
            "technical_pct_off_52w_high": -15.0,
            "technical_ma200_distance_pct": -3.0,
        }
        md = renderer.render_markdown(row, _BRIEF)
        self.assertIn("**Pattern**: neutral", md)

    def test_pattern_line_omitted_when_inputs_missing(self):
        # Don't render a useless "Pattern: unknown" line — saves visual
        # noise for tickers with insufficient OHLCV history.
        row = {
            **_ROW,
            "technical_pct_off_52w_high": None,
            "technical_ma200_distance_pct": None,
        }
        md = renderer.render_markdown(row, _BRIEF)
        self.assertNotIn("**Pattern**", md)


class TestRenderDayBundleCherryPickSort(unittest.TestCase):
    """Default day-bundle order sorts by cherry-pick score so the
    deepest-drawdown candidates appear first in the operator's scroll.
    Per the 2026-05-17 NVDA→QUBT post-mortem: deep-drawdown candidates
    drove the 1-month winners; sorting promotes them to the top.
    """

    def test_default_sort_puts_deep_drawdown_first(self):
        briefs_df = pd.DataFrame(
            [
                {
                    **_ROW,
                    "ticker": "FORM",
                    "technical_pct_off_52w_high": 0.0,
                    "brief_full_md": "## FORM (extended)",
                },
                {
                    **_ROW,
                    "ticker": "QUBT",
                    "technical_pct_off_52w_high": -67.0,
                    "brief_full_md": "## QUBT (deep)",
                },
                {
                    **_ROW,
                    "ticker": "RGTI",
                    "technical_pct_off_52w_high": -30.0,
                    "brief_full_md": "## RGTI (medium)",
                },
            ]
        )
        bundle = renderer.render_day_bundle(briefs_df, asof_str="2026-04-14")
        # QUBT (-67) before RGTI (-30) before FORM (0)
        i_qubt = bundle.index("## QUBT")
        i_rgti = bundle.index("## RGTI")
        i_form = bundle.index("## FORM")
        self.assertLess(i_qubt, i_rgti)
        self.assertLess(i_rgti, i_form)

    def test_sort_disabled_preserves_input_order(self):
        briefs_df = pd.DataFrame(
            [
                {
                    **_ROW,
                    "ticker": "FORM",
                    "technical_pct_off_52w_high": 0.0,
                    "brief_full_md": "## FORM",
                },
                {
                    **_ROW,
                    "ticker": "QUBT",
                    "technical_pct_off_52w_high": -67.0,
                    "brief_full_md": "## QUBT",
                },
            ]
        )
        bundle = renderer.render_day_bundle(
            briefs_df, asof_str="2026-04-14", sort_by_cherry_pick=False
        )
        self.assertLess(bundle.index("## FORM"), bundle.index("## QUBT"))

    def test_missing_pct_off_52w_high_sorted_last(self):
        # Tickers without 52w data still render, but sorted to the bottom
        # so the operator sees the actionable signals first.
        briefs_df = pd.DataFrame(
            [
                {
                    **_ROW,
                    "ticker": "QUBT",
                    "technical_pct_off_52w_high": -67.0,
                    "brief_full_md": "## QUBT",
                },
                {
                    **_ROW,
                    "ticker": "UNKN",
                    "technical_pct_off_52w_high": None,
                    "brief_full_md": "## UNKN",
                },
            ]
        )
        bundle = renderer.render_day_bundle(briefs_df, asof_str="2026-04-14")
        self.assertLess(bundle.index("## QUBT"), bundle.index("## UNKN"))


if __name__ == "__main__":
    unittest.main()
