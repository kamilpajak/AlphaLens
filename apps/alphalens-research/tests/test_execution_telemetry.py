"""Tests for ``execution_telemetry.build_execution_gauges`` (v3 PR-3).

This is the OBSERVABILITY layer over the existing per-regime execution
aggregation (``execution_modes.recommend_execution_modes``). The tests pin:

* the gauge KEYS (label-in-key Prometheus form, ``{regime="low"}``),
* that fill_rate / gap / MO / realized-mean carry the right VALUES,
* the gap sign convention (``shadow − realized`` = execution drag),
* that PARTIAL and non-finite-shadow rows are excluded (same admissibility
  as ``execution_modes``),
* that None / non-finite stats are SKIPPED (no NaN/inf ever emitted),
* that the realized-return mean counts only finite-realized FILLED rows and
  the pooled realized mean is over the switchable regimes only.

It NEVER reads the ``action`` column — the telemetry is click-independent.
"""

from __future__ import annotations

import math
import unittest

from alphalens_pipeline.feedback.execution_modes import DEFAULT_POOLED_GATE_N
from alphalens_pipeline.feedback.execution_telemetry import (
    _METRIC_PREFIX,
    build_execution_gauges,
)


def _gate_key() -> str:
    return f"{_METRIC_PREFIX}_gate_n_threshold"


def _key(stat: str, regime: str) -> str:
    return f'{_METRIC_PREFIX}_{stat}{{regime="{regime}"}}'


class TestBuildExecutionGauges(unittest.TestCase):
    """`build_execution_gauges` over synthetic matured rows."""

    def test_mixed_rows_emit_expected_keys_and_values(self):
        # low: 2 FILLED (shadow .05/.07, realized .03/.05) + 1 UNFILLED (shadow .10)
        # high: 1 FILLED (shadow .02, realized -.01) + 1 UNFILLED (shadow .20)
        # unknown: 1 FILLED (shadow .01, realized .01)
        # PARTIAL + non-finite-shadow rows dropped entirely.
        rows = [
            ("low", "FILLED", 0.05, 0.03),
            ("low", "FILLED", 0.07, 0.05),
            ("low", "UNFILLED", 0.10, None),
            ("high", "FILLED", 0.02, -0.01),
            ("high", "UNFILLED", 0.20, None),
            ("unknown", "FILLED", 0.01, 0.01),
            ("low", "PARTIAL", 0.99, 0.99),  # dropped (not FILLED/UNFILLED)
            ("high", "FILLED", float("nan"), 0.5),  # dropped (non-finite shadow)
        ]
        g = build_execution_gauges(rows)

        # Counts per present regime.
        self.assertEqual(g[_key("matured_decisions", "low")], 3)
        self.assertEqual(g[_key("filled", "low")], 2)
        self.assertEqual(g[_key("unfilled", "low")], 1)
        self.assertEqual(g[_key("matured_decisions", "high")], 2)
        self.assertEqual(g[_key("filled", "high")], 1)
        self.assertEqual(g[_key("unfilled", "high")], 1)
        self.assertEqual(g[_key("matured_decisions", "unknown")], 1)
        self.assertEqual(g[_key("filled", "unknown")], 1)
        self.assertEqual(g[_key("unfilled", "unknown")], 0)

        # fill_rate = n_filled / n.
        self.assertAlmostEqual(g[_key("fill_rate", "low")], 2 / 3)
        self.assertAlmostEqual(g[_key("fill_rate", "high")], 1 / 2)

        # gap_mean = mean(shadow − realized), POSITIVE = real fill did worse.
        # low: mean(.05-.03, .07-.05) = mean(.02, .02) = .02
        self.assertAlmostEqual(g[_key("gap_mean", "low")], 0.02)
        # high: .02 - (-.01) = .03
        self.assertAlmostEqual(g[_key("gap_mean", "high")], 0.03)

        # missed_opportunity = mean shadow over UNFILLED.
        self.assertAlmostEqual(g[_key("missed_opportunity_mean", "low")], 0.10)
        self.assertAlmostEqual(g[_key("missed_opportunity_mean", "high")], 0.20)

        # realized_return_mean over FILLED finite-realized rows.
        self.assertAlmostEqual(g[_key("realized_return_mean", "low")], (0.03 + 0.05) / 2)
        self.assertAlmostEqual(g[_key("realized_return_mean", "high")], -0.01)
        self.assertAlmostEqual(g[_key("realized_return_mean", "unknown")], 0.01)

        # Pooled present (switchable scope: low + high; NOT unknown).
        self.assertEqual(g[_key("matured_decisions", "pooled")], 3 + 2)
        self.assertEqual(g[_key("filled", "pooled")], 3)
        self.assertEqual(g[_key("unfilled", "pooled")], 2)
        self.assertAlmostEqual(g[_key("fill_rate", "pooled")], 3 / 5)
        # pooled realized over switchable FILLED only: .03 .05 -.01
        self.assertAlmostEqual(g[_key("realized_return_mean", "pooled")], (0.03 + 0.05 - 0.01) / 3)

        # Constant gate gauge.
        self.assertEqual(g[_gate_key()], float(DEFAULT_POOLED_GATE_N))
        self.assertEqual(g[_gate_key()], 50.0)

        # Never any non-finite float value.
        for value in g.values():
            if isinstance(value, float):
                self.assertTrue(math.isfinite(value), value)

    def test_partial_and_non_finite_shadow_excluded(self):
        rows = [
            ("low", "PARTIAL", 0.5, 0.5),
            ("low", "FILLED", float("inf"), 0.1),
            ("low", "UNFILLED", float("nan"), None),
        ]
        g = build_execution_gauges(rows)
        # No admissible low rows → low cell never appears.
        self.assertNotIn(_key("matured_decisions", "low"), g)
        # Pooled is present but with zero counts.
        self.assertEqual(g[_key("matured_decisions", "pooled")], 0)
        # Only the gate threshold is unlabelled; no fill_rate/gap/realized.
        self.assertNotIn(_key("fill_rate", "pooled"), g)
        self.assertNotIn(_key("gap_mean", "pooled"), g)
        self.assertNotIn(_key("realized_return_mean", "pooled"), g)

    def test_none_stats_are_skipped_no_nan_emitted(self):
        # An all-FILLED low cell has no UNFILLED → missed_opportunity is None.
        rows = [
            ("low", "FILLED", 0.05, 0.03),
            ("low", "FILLED", 0.04, 0.02),
        ]
        g = build_execution_gauges(rows)
        self.assertIn(_key("fill_rate", "low"), g)
        self.assertIn(_key("gap_mean", "low"), g)
        self.assertIn(_key("realized_return_mean", "low"), g)
        # No UNFILLED → MO skipped entirely (not NaN, not 0).
        self.assertNotIn(_key("missed_opportunity_mean", "low"), g)
        for value in g.values():
            if isinstance(value, float):
                self.assertTrue(math.isfinite(value), value)

    def test_empty_rows_emit_only_gate_and_zero_pooled_counts(self):
        g = build_execution_gauges([])
        # gate threshold present.
        self.assertEqual(g[_gate_key()], 50.0)
        # pooled present with zero counts (rec.n == 0).
        self.assertEqual(g[_key("matured_decisions", "pooled")], 0)
        self.assertEqual(g[_key("filled", "pooled")], 0)
        self.assertEqual(g[_key("unfilled", "pooled")], 0)
        # No mean stats (all None over an empty cell).
        self.assertNotIn(_key("fill_rate", "pooled"), g)
        self.assertNotIn(_key("gap_mean", "pooled"), g)
        self.assertNotIn(_key("missed_opportunity_mean", "pooled"), g)
        self.assertNotIn(_key("realized_return_mean", "pooled"), g)
        # No per-regime cell keys at all.
        for regime in ("low", "mid", "high", "unknown"):
            self.assertNotIn(_key("matured_decisions", regime), g)
        # Nothing non-finite.
        for value in g.values():
            if isinstance(value, float):
                self.assertTrue(math.isfinite(value), value)

    def test_realized_excludes_unfilled_and_non_finite_realized(self):
        rows = [
            ("low", "FILLED", 0.05, 0.03),  # counted
            ("low", "FILLED", 0.06, float("nan")),  # realized non-finite, excluded from PnL
            ("low", "UNFILLED", 0.10, 0.99),  # UNFILLED → realized never counted
        ]
        g = build_execution_gauges(rows)
        # Only the first FILLED row's realized counts.
        self.assertAlmostEqual(g[_key("realized_return_mean", "low")], 0.03)
        # gap_mean also only over finite-realized FILLED (first row): .05-.03 = .02
        self.assertAlmostEqual(g[_key("gap_mean", "low")], 0.02)

    def test_regime_label_syntax_and_pooled_key(self):
        rows = [("mid", "FILLED", 0.05, 0.04)]
        g = build_execution_gauges(rows)
        self.assertIn(f'{_METRIC_PREFIX}_matured_decisions{{regime="mid"}}', g)
        self.assertIn(f'{_METRIC_PREFIX}_matured_decisions{{regime="pooled"}}', g)


if __name__ == "__main__":
    unittest.main()
