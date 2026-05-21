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

    def test_header_includes_rank_in_day_when_present(self):
        # Brief-render sort attaches rank_in_day (1-based). Renderer surfaces
        # it inline so the operator sees relative position without scrolling
        # through the bundle. Absent column → no rank suffix (graceful).
        row = dict(_ROW)
        row["rank_in_day"] = 1
        row["cohort_size_in_day"] = 6
        md = renderer.render_markdown(row, _BRIEF)
        self.assertIn("rank 1/6", md)

    def test_header_includes_catalyst_strength_with_label(self):
        # When catalyst_strength ≥ moderate (per catalyst_signals.py thresholds)
        # the header surfaces the value so the operator sees what's driving
        # the cohort lift.
        row = dict(_ROW)
        row["catalyst_strength"] = 0.78
        md = renderer.render_markdown(row, _BRIEF)
        self.assertIn("catalyst 0.78", md)

    def test_header_includes_insider_dollar_amount_when_positive(self):
        # $1.2M is more salient at the top of the brief than buried in the
        # signal panel — surfaces conviction magnitude on the first scan.
        row = dict(_ROW)
        row["insider_score_usd"] = 1_200_000.0
        md = renderer.render_markdown(row, _BRIEF)
        # Match the compact magnitude marker; precise format is a renderer
        # implementation detail but $1.2M is the operator-readable form.
        self.assertTrue(
            "$1.2M insider" in md or "$1200k insider" in md,
            f"expected compact insider $ in header, got: {md.splitlines()[0]}",
        )

    def test_header_includes_reversal_tag_when_true(self):
        row = dict(_ROW)
        row["deep_drawdown_reversal"] = True
        md = renderer.render_markdown(row, _BRIEF)
        # First line should mention "reversal" — concise tag, not a sentence.
        self.assertIn("reversal", md.splitlines()[0].lower())

    def test_header_omits_reversal_tag_when_false(self):
        row = dict(_ROW)
        row["deep_drawdown_reversal"] = False
        md = renderer.render_markdown(row, _BRIEF)
        # Avoid noise in the header for the negative case.
        self.assertNotIn("reversal", md.splitlines()[0].lower())

    def test_header_includes_also_in_themes_badge_when_present(self):
        # When ticker hit multiple themes (cross-theme appearance), the
        # ``also in: <theme>`` badge surfaces the dropped themes after dedup.
        row = dict(_ROW)
        row["also_in_themes"] = ["quantum_error_correction", "AI_models"]
        md = renderer.render_markdown(row, _BRIEF)
        self.assertIn("also in", md.lower())
        self.assertIn("quantum_error_correction", md)
        self.assertIn("AI_models", md)

    def test_header_omits_also_in_themes_badge_for_single_theme_ticker(self):
        row = dict(_ROW)
        row["also_in_themes"] = []
        md = renderer.render_markdown(row, _BRIEF)
        self.assertNotIn("also in", md.lower())

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
        import itertools

        for prev, nxt in itertools.pairwise(section_markers):
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
        # Magic Formula is the new primary valuation row; the old "Valuation
        # composite" sector-percentile line stays as a secondary "(sector pctile)"
        # transparency row.
        self.assertIn("| Magic Formula |", md)
        self.assertIn("| Mults & ROE |", md)
        self.assertIn("| Valuation (sector pctile) |", md)
        # New catalyst-strength + reversal rows (PR #146)
        self.assertIn("| Catalyst strength |", md)
        self.assertIn("| Reversal setup |", md)
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


class TestRenderMagicFormulaCell(unittest.TestCase):
    """Magic Formula row dispatches on (health_pass, rank):
    health_pass=False → "health-gate fail"; rank=NaN with health passed
    → "rank n/a (cohort n=N)"; both present → "rank R/N · FCFF X% · ROIC Y%".
    """

    def _row(self, **overrides):
        base = {
            **_ROW,
            "magic_formula_rank": 2,
            "magic_formula_cohort_n": 8,
            "magic_formula_health_pass": True,
            "fcff_yield_pct": 4.5,
            "roic_pct": 18.0,
            "valuation_pe": 12.0,
            "valuation_ev_ebitda": 7.5,
            "valuation_ps": 1.8,
            "roe_pct": 22.0,
        }
        base.update(overrides)
        return base

    def test_renders_rank_with_fcff_and_roic_when_present(self):
        md = renderer.render_markdown(self._row(), _BRIEF)
        self.assertIn("rank 2/8 · FCFF 4.5% · ROIC 18.0%", md)

    def test_renders_health_gate_fail_when_gate_failed(self):
        md = renderer.render_markdown(
            self._row(magic_formula_health_pass=False, magic_formula_rank=None),
            _BRIEF,
        )
        self.assertIn("health-gate fail", md)
        # Must not print a misleading "rank n/a" line in the health-fail case.
        self.assertNotIn("rank n/a", md)

    def test_renders_small_cohort_rank_na_with_cohort_size(self):
        md = renderer.render_markdown(
            self._row(magic_formula_rank=None, magic_formula_cohort_n=2),
            _BRIEF,
        )
        self.assertIn("rank n/a (cohort n=2)", md)

    def test_renders_mults_detail_row(self):
        md = renderer.render_markdown(self._row(), _BRIEF)
        self.assertIn("PE 12.0 · EV/EBITDA 7.5 · PS 1.8 · ROE 22.0%", md)

    def test_keeps_sector_percentile_secondary_line(self):
        md = renderer.render_markdown(self._row(valuation_composite_sector_percentile=72.0), _BRIEF)
        self.assertIn("| Valuation (sector pctile) |", md)
        self.assertIn("pctile 72", md)


class TestRenderCatalystStrengthCell(unittest.TestCase):
    """Catalyst-strength row reports value + bucket label + event type."""

    def test_strong_strength_renders_strong_label(self):
        row = {**_ROW, "catalyst_strength": 0.78, "catalyst_event_type": "product_launch"}
        md = renderer.render_markdown(row, _BRIEF)
        self.assertIn("0.78 strong (product_launch)", md)

    def test_moderate_strength_renders_moderate_label(self):
        row = {**_ROW, "catalyst_strength": 0.45, "catalyst_event_type": "partnership"}
        md = renderer.render_markdown(row, _BRIEF)
        self.assertIn("0.45 moderate (partnership)", md)

    def test_weak_strength_renders_weak_label(self):
        row = {**_ROW, "catalyst_strength": 0.10, "catalyst_event_type": "other"}
        md = renderer.render_markdown(row, _BRIEF)
        self.assertIn("0.10 weak (other)", md)

    def test_missing_strength_renders_na(self):
        row = {**_ROW, "catalyst_strength": None, "catalyst_event_type": None}
        md = renderer.render_markdown(row, _BRIEF)
        self.assertIn("| Catalyst strength | n/a |", md)


class TestRenderReversalCell(unittest.TestCase):
    def test_renders_yes_when_true(self):
        row = {**_ROW, "deep_drawdown_reversal": True}
        md = renderer.render_markdown(row, _BRIEF)
        self.assertIn("| Reversal setup | yes |", md)

    def test_renders_no_when_false(self):
        row = {**_ROW, "deep_drawdown_reversal": False}
        md = renderer.render_markdown(row, _BRIEF)
        self.assertIn("| Reversal setup | no |", md)

    def test_renders_na_when_missing(self):
        row = {**_ROW, "deep_drawdown_reversal": None}
        md = renderer.render_markdown(row, _BRIEF)
        self.assertIn("| Reversal setup | n/a |", md)


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


class TestRenderDayBundlePreservesUpstreamOrder(unittest.TestCase):
    """Sort order is now owned by the orchestrator (zen-revised 7-key chain
    + dedup), so the renderer must preserve whatever upstream feeds in. The
    legacy ``sort_by_cherry_pick`` behaviour (technical_pct_off_52w_high
    ASC) is gone — its responsibility moved upstream into
    ``orchestrator._sort_and_dedup_for_brief``.
    """

    def test_bundle_preserves_input_dataframe_order(self):
        briefs_df = pd.DataFrame(
            [
                {**_ROW, "ticker": "FIRST", "brief_full_md": "## FIRST"},
                {**_ROW, "ticker": "SECOND", "brief_full_md": "## SECOND"},
                {**_ROW, "ticker": "THIRD", "brief_full_md": "## THIRD"},
            ]
        )
        bundle = renderer.render_day_bundle(briefs_df, asof_str="2026-04-14")
        self.assertLess(bundle.index("## FIRST"), bundle.index("## SECOND"))
        self.assertLess(bundle.index("## SECOND"), bundle.index("## THIRD"))

    def test_bundle_ignores_technical_drawdown_when_choosing_order(self):
        # Even if technical_pct_off_52w_high tells a different story, the
        # bundle no longer re-sorts. Upstream already sorted by the full
        # tiebreaker chain (which includes drawdown via deep_drawdown_reversal).
        briefs_df = pd.DataFrame(
            [
                {
                    **_ROW,
                    "ticker": "EXTENDED",
                    "technical_pct_off_52w_high": 0.0,
                    "brief_full_md": "## EXTENDED",
                },
                {
                    **_ROW,
                    "ticker": "DEEP_DRAWDOWN",
                    "technical_pct_off_52w_high": -67.0,
                    "brief_full_md": "## DEEP_DRAWDOWN",
                },
            ]
        )
        bundle = renderer.render_day_bundle(briefs_df, asof_str="2026-04-14")
        # Despite -67% drawdown on the second row, upstream input order wins.
        self.assertLess(bundle.index("## EXTENDED"), bundle.index("## DEEP_DRAWDOWN"))


class TestFmtNumSignedZeroSuppression(unittest.TestCase):
    """Issue #172 Bug 3a: ``_fmt_num(-3e-6, ".1f")`` previously returned
    ``"-0.0"`` because Python's IEEE-754 negative zero survives rounding
    via the ``f`` format spec. The brief rendered SOUN ROE as ``-0.0%``,
    which the reader naturally parsed as "essentially zero" rather than
    the signed-zero artifact it actually was.
    """

    def test_negative_near_zero_renders_unsigned(self):
        self.assertEqual(renderer._fmt_num(-0.04, ".1f"), "0.0")

    def test_explicit_negative_zero_renders_unsigned(self):
        self.assertEqual(renderer._fmt_num(-0.0, ".1f"), "0.0")

    def test_regular_negative_unchanged(self):
        self.assertEqual(renderer._fmt_num(-3.2, ".1f"), "-3.2")

    def test_regular_positive_unchanged(self):
        self.assertEqual(renderer._fmt_num(7.81, ".2f"), "7.81")

    def test_nan_still_returns_n_a(self):
        self.assertEqual(renderer._fmt_num(float("nan"), ".1f"), "n/a")

    def test_none_still_returns_n_a(self):
        self.assertEqual(renderer._fmt_num(None, ".1f"), "n/a")

    def test_negative_zero_with_higher_precision_unsigned(self):
        # 0.00005 rounds to "0.0001" under .4f but to "0.0" under .1f.
        # The guard kicks only when the rendered string evaluates to 0.
        self.assertEqual(renderer._fmt_num(-0.00005, ".4f"), "-0.0001")


if __name__ == "__main__":
    unittest.main()
