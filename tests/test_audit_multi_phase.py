"""Tests for `scripts/audit_multi_phase.py` orchestration helpers.

The audit driver wraps experiment scripts as subprocesses and parses
their stderr to aggregate per-phase results. The regex parser, log-prefix
stripping, and per-config grouping logic are silently fragile to log
format changes — these tests pin the contract using synthetic stderr
fixtures (no real subprocess invocation needed).
"""

from __future__ import annotations

import unittest

from scripts.audit_multi_phase import (
    _config_key_from_line,
    _group_by_config,
    _parse_results,
)

# Representative stderr lines emitted by the experiment scripts'
# `assess()` logging in `scripts/experiment_*.py`. Format must stay in
# sync with `_RESULT_LINE` regex in audit_multi_phase.py.
_TRI_FACTOR_RESULT = (
    "2026-04-29 14:08:22,978 INFO __main__: IS 2019-2022 | rw=1.0 vw=1.0 "
    "ADV≥$5M cost=5bps | n=201 topN=15.0 turn=24.9% | Sh gross=0.83 "
    "net=0.65 | excess gross=42.1% net=39.6% | α 4F=63.1% t=2.24 R²=0.049"
)

_MOM_LOWVOL_RESULT = (
    "2026-04-29 12:18:51,690 INFO __main__: IS 2015-2022 | vw=1.0 ADV≥$5M "
    "cost=5bps | n=403 topN=15.0 turn=25.5% | Sh gross=0.42 net=0.21 | "
    "excess gross=18.7% net=16.1% | α 4F=27.8% t=1.37"
)

_PROGRESS_LINE = (
    "2026-04-29 11:57:57,967 INFO alphalens.backtest.engine: backtest "
    "progress: 20/403 days (5%) — latest snap 2015-05-20 scored=341"
)


class ParseResultsTests(unittest.TestCase):
    def test_extracts_all_metrics_from_typical_log_line(self):
        rows = _parse_results(_TRI_FACTOR_RESULT, phase_offset=4)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertAlmostEqual(r["sharpe_gross"], 0.83)
        self.assertAlmostEqual(r["sharpe_net"], 0.65)
        # Excess returns scaled from percent to fraction.
        self.assertAlmostEqual(r["excess_gross_ann"], 0.421)
        self.assertAlmostEqual(r["excess_net_ann"], 0.396)
        self.assertAlmostEqual(r["alpha_t"], 2.24)
        self.assertEqual(r["phase_offset"], 4)
        self.assertIn("rw=1.0", r["raw_line"])

    def test_skips_non_matching_lines(self):
        # Mix of header, progress, and one result line.
        text = "\n".join(["=== IS 2015-2022 ===", _PROGRESS_LINE, _MOM_LOWVOL_RESULT])
        rows = _parse_results(text, phase_offset=0)
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["alpha_t"], 1.37)

    def test_handles_negative_values(self):
        line = (
            "2026-04-29 12:33:36,279 INFO __main__: IS 2015-2018 | vw=1.0 ADV≥$5M "
            "cost=5bps | n=202 topN=15.0 turn=17.4% | Sh gross=-0.05 net=-0.23 | "
            "excess gross=0.4% net=-1.3% | α 4F=-3.7% t=-0.15"
        )
        rows = _parse_results(line, phase_offset=2)
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["sharpe_gross"], -0.05)
        self.assertAlmostEqual(rows[0]["alpha_t"], -0.15)
        # 0.4% gross stays positive after the /100 conversion
        self.assertAlmostEqual(rows[0]["excess_gross_ann"], 0.004)
        self.assertAlmostEqual(rows[0]["excess_net_ann"], -0.013)

    def test_empty_stderr_returns_empty_list(self):
        self.assertEqual(_parse_results("", phase_offset=0), [])


class ConfigKeyTests(unittest.TestCase):
    def test_strips_log_prefix_and_stats_suffix(self):
        # Should keep the period + parameter combo, drop timestamp prefix
        # AND the per-phase counts/stats after " | n=...".
        key = _config_key_from_line(_TRI_FACTOR_RESULT)
        self.assertEqual(key, "IS 2019-2022 | rw=1.0 vw=1.0 ADV≥$5M cost=5bps")

    def test_two_phases_of_same_config_produce_same_key(self):
        # Different timestamps + different per-phase stats → identical key.
        line_a = _TRI_FACTOR_RESULT
        line_b = _TRI_FACTOR_RESULT.replace("14:08:22,978", "14:11:35,123").replace(
            "Sh gross=0.83 net=0.65", "Sh gross=0.65 net=0.46"
        )
        self.assertEqual(_config_key_from_line(line_a), _config_key_from_line(line_b))


class GroupByConfigTests(unittest.TestCase):
    def test_aggregates_phases_for_same_config(self):
        # Two phases × one config each → one config key, two rows.
        phase_0 = _parse_results(_TRI_FACTOR_RESULT, phase_offset=0)
        phase_4 = _parse_results(_TRI_FACTOR_RESULT, phase_offset=4)
        grouped = _group_by_config([phase_0, phase_4])
        self.assertEqual(len(grouped), 1)
        only_key = next(iter(grouped))
        self.assertEqual(len(grouped[only_key]), 2)
        self.assertEqual({r["phase_offset"] for r in grouped[only_key]}, {0, 4})

    def test_distinct_configs_produce_separate_groups(self):
        # Same phase, two different configs (tri_factor + mom_lowvol-style).
        rows_phase_0 = _parse_results(
            _TRI_FACTOR_RESULT + "\n" + _MOM_LOWVOL_RESULT, phase_offset=0
        )
        grouped = _group_by_config([rows_phase_0])
        self.assertEqual(len(grouped), 2)


if __name__ == "__main__":
    unittest.main()
