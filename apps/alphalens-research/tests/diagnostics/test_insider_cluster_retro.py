"""Unit tests for the insider-cluster retrospective helpers (pre-registered spec
`docs/research/preregistration/params_insider_cluster_retro_2026_09.json`).

The event rules moved to the pipeline tier with epic #1293 and are tested in
``tests/events/test_insider_cluster.py``; this module keeps the retrospective's
own machinery (event CAR, matching, bootstrap, planning rule) and pins that the
promoted names still resolve through ``icr.*`` for the runner script.
"""

from __future__ import annotations

import datetime as dt
import json
import unittest
from pathlib import Path

import numpy as np
import pandas as pd
from alphalens_pipeline.events import insider_cluster as ic
from alphalens_research.diagnostics import insider_cluster_retro as icr

D = dt.date


class TestReexports(unittest.TestCase):
    def test_promoted_names_resolve_to_pipeline_module(self):
        for name in (
            "qualifying_legs",
            "detect_clusters",
            "arrival_session",
            "fetch_acceptance",
            "accession_urls",
        ):
            self.assertIs(getattr(icr, name), getattr(ic, name), name)
        for name in (
            "LEG_MIN_USD",
            "CLUSTER_MIN_USD",
            "CLUSTER_MIN_USD_CHECK",
            "CLUSTER_WINDOW_SESSIONS",
            "CLUSTER_MIN_INSIDERS",
            "DEDUP_SESSIONS",
            "PRE_OPEN_CUTOFF_ET",
            "HORIZON_SESSIONS_PRIMARY",
            "HORIZON_SESSIONS_SECONDARY",
            "FEE_ROUND_TRIP",
        ):
            self.assertEqual(getattr(icr, name), getattr(ic, name), name)


class TestEventCar(unittest.TestCase):
    def _prices(self, opens, closes, start=D(2020, 3, 2)):
        idx = pd.bdate_range(start, periods=len(closes))
        return pd.DataFrame(
            {"open": opens, "high": closes, "low": opens, "close": closes, "volume": 1.0}, index=idx
        )

    def test_car_is_open_to_close_buy_and_hold_minus_benchmark(self):
        stock = self._prices([10.0] * 25, [10.0] * 19 + [11.0] * 6)  # +10% by close of arr+19
        bench = self._prices([100.0] * 25, [100.0] * 19 + [102.0] * 6)  # +2%
        car = icr.event_car(stock, bench, arrival=D(2020, 3, 2), horizon_sessions=19)
        self.assertAlmostEqual(car, 0.10 - 0.02, places=9)

    def test_missing_anchor_or_horizon_returns_none(self):
        stock = self._prices([10.0] * 5, [10.0] * 5)
        bench = self._prices([100.0] * 25, [100.0] * 25)
        self.assertIsNone(icr.event_car(stock, bench, arrival=D(2020, 3, 2), horizon_sessions=19))

    def test_split_guard_drops_window(self):
        closes = [10.0] * 10 + [4.0] * 15  # 0.4 ratio -> below 0.55 guard
        stock = self._prices([10.0] * 25, closes)
        bench = self._prices([100.0] * 25, [100.0] * 25)
        self.assertIsNone(icr.event_car(stock, bench, arrival=D(2020, 3, 2), horizon_sessions=19))


class TestMatchControls(unittest.TestCase):
    def test_picks_nearest_neighbours_within_caliper_and_excludes_treated_pool(self):
        rng = np.random.default_rng(0)
        pool = pd.DataFrame(
            {
                "ticker": [f"C{i}" for i in range(50)],
                "ret_20d": rng.normal(0, 0.05, 50),
                "ret_6m": rng.normal(0, 0.2, 50),
                "vol_20d": rng.normal(0.3, 0.05, 50),
                "log_dv_20d": rng.normal(15, 1, 50),
            }
        )
        pool.loc[0, ["ret_20d", "ret_6m", "vol_20d", "log_dv_20d"]] = [0.0, 0.0, 0.30, 15.0]
        treated = pd.Series(
            {"ret_20d": 0.0, "ret_6m": 0.0, "vol_20d": 0.30, "log_dv_20d": 15.0}
        )  # near the pool centre
        picked = icr.match_controls(treated, pool, k=5, caliper_sd=1.5)
        self.assertEqual(len(picked), 5)
        self.assertEqual(picked.iloc[0].ticker, "C0")  # exact match ranks first

    def test_returns_fewer_when_caliper_excludes_pool(self):
        pool = pd.DataFrame(
            {
                "ticker": ["C1", "C2"],
                "ret_20d": [0.5, 0.6],
                "ret_6m": [1.0, 1.2],
                "vol_20d": [0.9, 0.8],
                "log_dv_20d": [20.0, 21.0],
            }
        )
        treated = pd.Series(
            {"ret_20d": -0.10, "ret_6m": -0.30, "vol_20d": 0.30, "log_dv_20d": 15.0}
        )
        picked = icr.match_controls(
            treated,
            pool,
            k=5,
            caliper_sd=1.5,
            pool_sd={"ret_20d": 0.05, "ret_6m": 0.2, "vol_20d": 0.05, "log_dv_20d": 1.0},
        )
        self.assertEqual(len(picked), 0)


class TestPairedDifferenceCI(unittest.TestCase):
    def test_ci_brackets_the_mean_and_shrinks_with_n(self):
        rng = np.random.default_rng(0)
        d = rng.normal(0.01, 0.1, 400)
        clusters = np.repeat(np.arange(100), 4)
        res = icr.paired_difference_ci(d, clusters, n_boot=999, seed=0)
        self.assertAlmostEqual(res["mean"], d.mean(), places=12)
        self.assertLess(res["ci90"][0], res["mean"])
        self.assertGreater(res["ci90"][1], res["mean"])
        self.assertLess(res["ci90"][1] - res["ci90"][0], res["ci95"][1] - res["ci95"][0])
        self.assertEqual(res["n_clusters"], 100)

    def test_planning_rule_is_net_of_fees(self):
        self.assertTrue(
            icr.planning_rule(mean=0.012, ci90_low=0.002, fee_round_trip=0.0066, bound=-0.005)
        )
        self.assertFalse(
            icr.planning_rule(mean=0.012, ci90_low=-0.006, fee_round_trip=0.0066, bound=-0.005)
        )
        self.assertFalse(
            icr.planning_rule(mean=-0.001, ci90_low=-0.002, fee_round_trip=0.0, bound=-0.005)
        )


class TestParamsParity(unittest.TestCase):
    """The frozen JSON and the module constants must agree (spec drift guard)."""

    def test_constants_match_frozen_params(self):
        root = Path(__file__).resolve().parents[4]
        params = json.loads(
            (
                root / "docs/research/preregistration/params_insider_cluster_retro_2026_09.json"
            ).read_text()
        )
        self.assertEqual(params["controls"]["per_event"], icr.CONTROLS_PER_EVENT)
        self.assertEqual(tuple(params["controls"]["match_vars"]), icr.MATCH_VARS)
        self.assertEqual(params["inference"]["bootstrap"]["seed"], 0)
        self.assertAlmostEqual(params["planning_rule"]["fee_round_trip"], icr.FEE_ROUND_TRIP)
        self.assertIn("100000", params["event"]["cluster"])
        self.assertIn("10000 USD", params["event"]["leg"])
        self.assertIn("2 trading sessions", params["event"]["cluster"])
        self.assertIn("20 sessions", params["event"]["dedup"])


if __name__ == "__main__":
    unittest.main()
