"""Unit tests for the EDGAR fundamentals validation gate harness.

Covers the tolerance-band classification logic only — no live SEC or yfinance
calls. The live gate run is operator-triggered (see
``scripts/edgar_fundamentals_validation_gate.py``) and its evidence is
committed as a memo; this suite locks down the rules that decide whether
each field's EDGAR-vs-yfinance delta clears the doctrine bar.
"""

from __future__ import annotations

import unittest


class TestCompareField(unittest.TestCase):
    """Per-field comparison applies different tolerance bands by category."""

    def setUp(self):
        from scripts.edgar_fundamentals_validation_gate import compare_field

        self._cmp = compare_field

    def test_instant_field_within_one_percent_passes(self):
        # cash_and_equivalents is an instant balance-sheet field — same XBRL tag
        # in EDGAR and yfinance, should match within ±1%.
        diff = self._cmp("cash_and_equivalents", 1_000_000_000.0, 1_005_000_000.0)
        self.assertTrue(diff.within_tolerance)
        self.assertFalse(diff.exempt)
        self.assertAlmostEqual(diff.pct_delta, 0.5, places=2)

    def test_instant_field_two_percent_fails(self):
        diff = self._cmp("long_term_debt", 1_000_000_000.0, 1_021_000_000.0)
        self.assertFalse(diff.within_tolerance)
        self.assertFalse(diff.exempt)

    def test_ttm_field_within_five_percent_passes(self):
        # revenue_ttm has fiscal-calendar drift — ±5% tolerance.
        diff = self._cmp("revenue_ttm", 1_000_000_000.0, 1_030_000_000.0)
        self.assertTrue(diff.within_tolerance)
        self.assertFalse(diff.exempt)

    def test_ttm_field_six_percent_fails(self):
        diff = self._cmp("revenue_ttm", 1_000_000_000.0, 1_061_000_000.0)
        self.assertFalse(diff.within_tolerance)

    def test_tax_rate_is_exempt(self):
        # EDGAR clamps to [0, 0.35], yfinance reports raw — divergence by design.
        diff = self._cmp("tax_rate", 0.21, 0.42)
        self.assertTrue(diff.exempt)
        self.assertTrue(diff.within_tolerance)  # exempt counts as pass

    def test_fcf_margin_5y_median_is_exempt(self):
        # Known TODO in EDGAR store — None placeholder until 20-quarter rolling
        # median lands. Don't bind the gate on a field that's literally not yet
        # implemented.
        diff = self._cmp("fcf_margin_5y_median", None, 0.15)
        self.assertTrue(diff.exempt)
        self.assertTrue(diff.within_tolerance)

    def test_publish_date_str_is_exempt(self):
        # Informational only; no numeric comparison.
        diff = self._cmp("publish_date_str", "2026-02-15", "2026-02-21")
        self.assertTrue(diff.exempt)

    def test_price_uses_dollar_floor(self):
        # Penny stocks: ±$0.02 floor protects the gate from "0.5% of $1.50 = $0.0075"
        # noise on micro-priced quotes.
        diff = self._cmp("price", 1.50, 1.51)
        self.assertTrue(diff.within_tolerance)
        # But $0.05 delta on a $1.50 price = 3.3% > 1%, would fail purely on
        # percent. The dollar floor saves it.
        diff_fail = self._cmp("price", 1.50, 1.55)
        self.assertFalse(diff_fail.within_tolerance)

    def test_none_on_both_sides_passes_silently(self):
        # Both vendors lacking a field is a non-event for the gate (logged in memo).
        diff = self._cmp("long_term_debt", None, None)
        self.assertTrue(diff.within_tolerance)
        self.assertIn("both none", diff.note.lower())

    def test_one_sided_none_for_non_exempt_fails(self):
        # EDGAR returns a value, yfinance doesn't (or vice versa) — surface as
        # divergence so the operator can investigate. NOT silently pass.
        diff = self._cmp("revenue_ttm", 1_000_000_000.0, None)
        self.assertFalse(diff.within_tolerance)

    def test_unknown_field_routes_to_ttm_band(self):
        # Defensive: any field not explicitly catalogued defaults to TTM ±5%
        # rather than the stricter 1% — avoids false fails on future additions.
        diff = self._cmp("future_field_we_have_not_thought_of", 100.0, 103.0)
        self.assertTrue(diff.within_tolerance)


class TestCompare(unittest.TestCase):
    """``compare()`` runs every field and returns the full diff list."""

    def test_compare_returns_one_diff_per_field(self):
        from scripts.edgar_fundamentals_validation_gate import compare

        edgar = {
            "ocf_ttm": 1_000_000.0,
            "capex_ttm": 200_000.0,
            "long_term_debt": 500_000.0,
            "tax_rate": 0.21,
        }
        yf = {
            "ocf_ttm": 1_020_000.0,
            "capex_ttm": 195_000.0,
            "long_term_debt": 505_000.0,
            "tax_rate": 0.30,
        }
        diffs = compare(edgar, yf)
        fields = {d.field for d in diffs}
        self.assertEqual(fields, {"ocf_ttm", "capex_ttm", "long_term_debt", "tax_rate"})


class TestAnchorPassFail(unittest.TestCase):
    """An anchor passes if every non-exempt field's diff is within tolerance."""

    def test_anchor_passes_when_all_in_tolerance(self):
        from scripts.edgar_fundamentals_validation_gate import (
            FieldDiff,
            anchor_passed,
        )

        diffs = [
            FieldDiff("revenue_ttm", 1.0, 1.02, 0.02, 2.0, 5.0, True, False, ""),
            FieldDiff("tax_rate", 0.21, 0.40, 0.19, 90.0, 0.0, True, True, "exempt"),
        ]
        self.assertTrue(anchor_passed(diffs))

    def test_anchor_fails_when_any_non_exempt_excursion(self):
        from scripts.edgar_fundamentals_validation_gate import (
            FieldDiff,
            anchor_passed,
        )

        diffs = [
            FieldDiff("revenue_ttm", 1.0, 1.10, 0.10, 10.0, 5.0, False, False, ""),
            FieldDiff("tax_rate", 0.21, 0.40, 0.19, 90.0, 0.0, True, True, "exempt"),
        ]
        self.assertFalse(anchor_passed(diffs))


class TestMarkdownEmitter(unittest.TestCase):
    """The committed memo must be parseable by the CI evidence guard."""

    def test_format_memo_contains_required_blocks(self):
        from datetime import date

        from scripts.edgar_fundamentals_validation_gate import (
            FieldDiff,
            format_memo,
        )

        results = {
            "MANH": [
                FieldDiff("revenue_ttm", 1.0, 1.02, 0.02, 2.0, 5.0, True, False, "OK"),
                FieldDiff("tax_rate", 0.21, 0.40, 0.19, 90.0, 0.0, True, True, "exempt"),
            ],
            "SYM": [
                FieldDiff("revenue_ttm", 1.0, 1.04, 0.04, 4.0, 5.0, True, False, "OK"),
            ],
        }
        memo = format_memo(date(2026, 5, 20), results)

        # Anchor headers
        self.assertIn("## MANH", memo)
        self.assertIn("## SYM", memo)
        # Verdict block (used by the CI evidence guard)
        self.assertIn("**Gate verdict:** PASS", memo)
        # Date header
        self.assertIn("2026-05-20", memo)
        # Per-field rows
        self.assertIn("revenue_ttm", memo)
        self.assertIn("tax_rate", memo)
        # Exempt label visible
        self.assertIn("exempt", memo.lower())

    def test_format_memo_writes_fail_when_any_anchor_fails(self):
        from datetime import date

        from scripts.edgar_fundamentals_validation_gate import (
            FieldDiff,
            format_memo,
        )

        results = {
            "MANH": [FieldDiff("revenue_ttm", 1.0, 1.10, 0.10, 10.0, 5.0, False, False, "FAIL")],
        }
        memo = format_memo(date(2026, 5, 20), results)
        self.assertIn("**Gate verdict:** FAIL", memo)


if __name__ == "__main__":
    unittest.main()
