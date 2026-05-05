"""Tests for ``scripts/aggregate_v9d_retrospective_verdict.py``."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import aggregate_v9d_retrospective_verdict as agg


def _make_cell_payload(
    *,
    universe: str,
    sub_period: str,
    phase: int,
    alpha_t: float = 2.5,
    alpha_pct: float = 5.0,
    alpha_pct_se: float = 2.0,
    sharpe_net: float = 1.5,
    n: int = 60,
    n_returns: int = 60,
) -> dict:
    """Construct a minimal cell JSON shape the aggregator can consume."""
    # Generate unique business-day dates so multiple cells can concat on axis=1
    # without InvalidIndexError. Each phase uses a different starting offset to
    # mimic the strided rebalance calendar.
    import pandas as pd

    base = pd.Timestamp("2010-01-04") + pd.tseries.offsets.BDay(phase)
    asof_dates = [
        (base + pd.tseries.offsets.BDay(5 * i)).strftime("%Y-%m-%d") for i in range(n_returns)
    ]
    long_net = [0.001 * (i % 11 - 5) for i in range(n_returns)]
    return {
        "cell": {
            "universe": universe,
            "sub_period": sub_period,
            "phase_offset": phase,
        },
        "config": {"n_asofs": n_returns},
        "stats": {
            "alpha_t_4f": alpha_t,
            "alpha_gross_4f": alpha_pct,
            "alpha_se_4f": alpha_pct_se,
            "sharpe_net": sharpe_net,
            "n": n,
        },
        "raw_returns_for_pooling": {
            "asof": asof_dates,
            "long_net": long_net,
            "long_gross": [r + 0.0001 for r in long_net],
            "benchmark": [0.0 for _ in long_net],
        },
    }


def _seed_full_battery(cells_dir: Path, *, alpha_t: float = 2.5, alpha_pct: float = 5.0) -> None:
    """Write all 45 cell JSONs into ``cells_dir`` with the same headline αt."""
    for u in agg.UNIVERSES:
        for s in agg.SUB_PERIODS:
            for p in agg.PHASES:
                payload = _make_cell_payload(
                    universe=u,
                    sub_period=s,
                    phase=p,
                    alpha_t=alpha_t,
                    alpha_pct=alpha_pct,
                )
                (cells_dir / f"{u}_{s}_p{p}.json").write_text(json.dumps(payload))


class CoverageTests(unittest.TestCase):
    def test_coverage_check_full(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            _seed_full_battery(d)
            cells = agg.load_cells(d)
            missing, extra = agg.coverage_check(cells)
            self.assertEqual(missing, [])
            self.assertEqual(extra, [])
            self.assertEqual(len(cells), 45)

    def test_coverage_check_partial(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            payload = _make_cell_payload(universe="U2", sub_period="GFC_recovery", phase=0)
            (d / "U2_GFC_recovery_p0.json").write_text(json.dumps(payload))
            cells = agg.load_cells(d)
            missing, _ = agg.coverage_check(cells)
            self.assertEqual(len(cells), 1)
            self.assertEqual(len(missing), 44)


class SummaryTests(unittest.TestCase):
    def test_summary_aggregates_per_universe_per_subperiod(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            _seed_full_battery(d, alpha_t=2.0)
            cells = agg.load_cells(d)
            summary = agg.per_universe_per_subperiod_summary(cells)
            self.assertEqual(len(summary), len(agg.UNIVERSES) * len(agg.SUB_PERIODS))
            self.assertTrue((summary["alpha_t_mean"].abs() - 2.0).abs().max() < 1e-9)
            self.assertTrue(summary["n_phases_present"].eq(5).all())

    def test_summary_handles_missing_phases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            # Only seed 2 of 5 phases for U3 / GFC_recovery
            for p in (0, 2):
                payload = _make_cell_payload(
                    universe="U3", sub_period="GFC_recovery", phase=p, alpha_t=3.0
                )
                (d / f"U3_GFC_recovery_p{p}.json").write_text(json.dumps(payload))
            cells = agg.load_cells(d)
            summary = agg.per_universe_per_subperiod_summary(cells)
            row = summary[
                (summary["universe"] == "U3") & (summary["sub_period"] == "GFC_recovery")
            ].iloc[0]
            self.assertEqual(int(row["n_phases_present"]), 2)
            self.assertAlmostEqual(float(row["alpha_t_mean"]), 3.0, places=6)


class VerdictTests(unittest.TestCase):
    def test_partial_battery_returns_INCOMPLETE(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            payload = _make_cell_payload(universe="U2", sub_period="GFC_recovery", phase=0)
            (d / "U2_GFC_recovery_p0.json").write_text(json.dumps(payload))
            cells = agg.load_cells(d)
            result = agg.build_verdict_payload(cells, n_bootstrap=10, seed=0)
            self.assertEqual(result["verdict"], "INCOMPLETE")
            self.assertIsNone(result["bounds_andrews_manski"])

    def test_full_strong_battery_returns_pass_robust(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            # Strong αt across all 45 cells with consistent αpct → PASS_ROBUST
            _seed_full_battery(d, alpha_t=4.0, alpha_pct=10.0)
            cells = agg.load_cells(d)
            result = agg.build_verdict_payload(cells, n_bootstrap=200, seed=0)
            # alpha_t=4.0, alpha_pct=10.0, alpha_pct_se=2.0 → bounds lower for B=2 = 4.0 - 2.0/2.0 = 3.0 > 0
            self.assertEqual(result["verdict"], "PASS_ROBUST")
            bnd = result["bounds_andrews_manski"]
            self.assertGreater(bnd["alpha_t_lower"], 0.0)

    def test_full_weak_battery_returns_fail_robust(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            _seed_full_battery(d, alpha_t=0.5, alpha_pct=1.0)
            cells = agg.load_cells(d)
            result = agg.build_verdict_payload(cells, n_bootstrap=200, seed=0)
            self.assertEqual(result["verdict"], "FAIL_ROBUST")

    def test_full_marginal_battery_returns_inconclusive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            # alpha_t=1.5, αpct=3.0, αpct_se=2.0 → bounds lower (B=2) = 1.5 - 1.0 = 0.5
            # > 0 BUT αt < 2.5 → INCONCLUSIVE not PASS_MARGINAL
            _seed_full_battery(d, alpha_t=1.5, alpha_pct=3.0)
            cells = agg.load_cells(d)
            result = agg.build_verdict_payload(cells, n_bootstrap=200, seed=0)
            self.assertEqual(result["verdict"], "INCONCLUSIVE")


class RenderingTests(unittest.TestCase):
    def test_renders_header_for_partial(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            payload = _make_cell_payload(universe="U1", sub_period="GFC_recovery", phase=0)
            (d / "U1_GFC_recovery_p0.json").write_text(json.dumps(payload))
            cells = agg.load_cells(d)
            v = agg.build_verdict_payload(cells, n_bootstrap=10, seed=0)
            md = agg.render_postmortem_md(v)
            self.assertIn("# v9D Retrospective Pre-2018", md)
            self.assertIn("INCOMPLETE", md)
            # Abbreviated rendering: header line shows count, not full list.
            self.assertIn("Missing cells: 44", md)

    def test_renders_full_battery_table(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            _seed_full_battery(d, alpha_t=3.0, alpha_pct=8.0)
            cells = agg.load_cells(d)
            v = agg.build_verdict_payload(cells, n_bootstrap=200, seed=0)
            md = agg.render_postmortem_md(v)
            self.assertIn("Per-universe × per-sub-period", md)
            for u in agg.UNIVERSES:
                self.assertIn(f"| {u} |", md)


if __name__ == "__main__":
    unittest.main()
