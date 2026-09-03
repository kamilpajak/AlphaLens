"""Unit tests for the insider-cluster retrospective helpers (pre-registered spec
`docs/research/preregistration/params_insider_cluster_retro_2026_09.json`)."""

from __future__ import annotations

import datetime as dt
import unittest

import numpy as np
import pandas as pd
from alphalens_research.diagnostics import insider_cluster_retro as icr

D = dt.date


def _leg(
    ticker, cik, filed, usd=60_000.0, tx=None, code="P", officer=True, amend=False, accession=None
):
    return {
        "ticker": ticker,
        "reporting_owner_cik": cik,
        "filed_date": filed,
        "transaction_date": tx or filed,
        "transaction_code": code,
        "acquired_disposed": "A",
        "is_amendment": amend,
        "is_officer": officer,
        "is_director": False,
        "is_ten_percent_owner": False,
        "transaction_shares": usd / 10.0,
        "transaction_price_per_share": 10.0,
        "accession_number": accession or f"{cik}-{filed.isoformat()}",
    }


class TestQualifyingLegs(unittest.TestCase):
    def test_keeps_open_market_officer_purchases_above_floor(self):
        df = pd.DataFrame(
            [
                _leg("AAA", "1", D(2020, 3, 2)),
                _leg("AAA", "2", D(2020, 3, 2), code="S"),  # sale
                _leg("AAA", "3", D(2020, 3, 2), officer=False),  # 10% owner only
                _leg("AAA", "4", D(2020, 3, 2), amend=True),  # amendment
                _leg("AAA", "5", D(2020, 3, 2), usd=5_000.0),  # below 10k leg floor
            ]
        )
        df.loc[2, "is_ten_percent_owner"] = True
        out = icr.qualifying_legs(df, leg_min_usd=10_000.0)
        self.assertEqual(list(out.reporting_owner_cik), ["1"])
        self.assertAlmostEqual(float(out.usd.iloc[0]), 60_000.0)

    def test_drops_legs_without_price(self):
        df = pd.DataFrame([_leg("AAA", "1", D(2020, 3, 2))])
        df.loc[0, "transaction_price_per_share"] = np.nan
        self.assertTrue(icr.qualifying_legs(df, leg_min_usd=10_000.0).empty)


class TestDetectClusters(unittest.TestCase):
    def _legs(self, rows):
        return icr.qualifying_legs(pd.DataFrame(rows), leg_min_usd=10_000.0)

    def test_two_distinct_insiders_within_two_sessions_form_one_event(self):
        legs = self._legs(
            [
                _leg("AAA", "1", D(2020, 3, 2)),  # Monday
                _leg("AAA", "2", D(2020, 3, 4)),  # Wednesday = +2 sessions
            ]
        )
        ev = icr.detect_clusters(
            legs, window_sessions=2, min_insiders=2, min_usd=100_000.0, dedup_sessions=20
        )
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev.iloc[0].event_date, D(2020, 3, 4))  # completion = 2nd insider's filing
        self.assertEqual(ev.iloc[0].first_leg_date, D(2020, 3, 2))
        self.assertEqual(ev.iloc[0].n_insiders, 2)
        self.assertAlmostEqual(ev.iloc[0].cluster_usd, 120_000.0)
        self.assertEqual(ev.iloc[0].completing_accession, "2-2020-03-04")

    def test_same_insider_twice_is_not_a_cluster(self):
        legs = self._legs([_leg("AAA", "1", D(2020, 3, 2)), _leg("AAA", "1", D(2020, 3, 3))])
        self.assertTrue(
            icr.detect_clusters(
                legs, window_sessions=2, min_insiders=2, min_usd=100_000.0, dedup_sessions=20
            ).empty
        )

    def test_legs_three_sessions_apart_do_not_cluster(self):
        legs = self._legs(
            [_leg("AAA", "1", D(2020, 3, 2)), _leg("AAA", "2", D(2020, 3, 5))]
        )  # Mon, Thu = +3
        self.assertTrue(
            icr.detect_clusters(
                legs, window_sessions=2, min_insiders=2, min_usd=100_000.0, dedup_sessions=20
            ).empty
        )

    def test_usd_floor_applies_to_the_cluster_sum(self):
        legs = self._legs(
            [
                _leg("AAA", "1", D(2020, 3, 2), usd=40_000.0),
                _leg("AAA", "2", D(2020, 3, 3), usd=40_000.0),
            ]
        )
        self.assertTrue(
            icr.detect_clusters(
                legs, window_sessions=2, min_insiders=2, min_usd=100_000.0, dedup_sessions=20
            ).empty
        )

    def test_three_in_five_definition_detects_three_distinct_insiders(self):
        legs = self._legs(
            [
                _leg("AAA", "1", D(2020, 3, 2)),
                _leg("AAA", "2", D(2020, 3, 5)),  # +3 sessions from leg 1
                _leg("AAA", "3", D(2020, 3, 9)),  # +5 sessions from leg 1, +2 from leg 2
            ]
        )
        two_in_two = icr.detect_clusters(
            legs, window_sessions=2, min_insiders=2, min_usd=100_000.0, dedup_sessions=20
        )
        three_in_five = icr.detect_clusters(
            legs, window_sessions=5, min_insiders=3, min_usd=100_000.0, dedup_sessions=20
        )
        # legs 2 and 3 are within 2 sessions -> one 2-in-2 event completing on 03-09 (2 insiders)
        self.assertEqual(len(two_in_two), 1)
        self.assertEqual(two_in_two.iloc[0].n_insiders, 2)
        # the 3-in-5 definition sees all three insiders and completes at the 3rd filing
        self.assertEqual(len(three_in_five), 1)
        self.assertEqual(three_in_five.iloc[0].event_date, D(2020, 3, 9))
        self.assertEqual(three_in_five.iloc[0].n_insiders, 3)

    def test_dedup_keeps_first_event_per_ticker_per_window(self):
        legs = self._legs(
            [
                _leg("AAA", "1", D(2020, 3, 2)),
                _leg("AAA", "2", D(2020, 3, 3)),
                _leg("AAA", "3", D(2020, 3, 10)),
                _leg("AAA", "4", D(2020, 3, 11)),  # inside 20 sessions -> dropped
                _leg("AAA", "5", D(2020, 6, 1)),
                _leg("AAA", "6", D(2020, 6, 2)),  # far -> new event
            ]
        )
        ev = icr.detect_clusters(
            legs, window_sessions=2, min_insiders=2, min_usd=100_000.0, dedup_sessions=20
        )
        self.assertEqual(list(ev.event_date), [D(2020, 3, 3), D(2020, 6, 2)])


class TestArrivalSession(unittest.TestCase):
    def test_pre_open_acceptance_maps_to_same_session(self):
        acc = dt.datetime(2020, 3, 4, 8, 15)  # ET, before 09:00
        self.assertEqual(icr.arrival_session(D(2020, 3, 4), acc), D(2020, 3, 4))

    def test_intraday_or_post_close_acceptance_maps_to_next_session(self):
        self.assertEqual(
            icr.arrival_session(D(2020, 3, 4), dt.datetime(2020, 3, 4, 10, 0)), D(2020, 3, 5)
        )
        self.assertEqual(
            icr.arrival_session(D(2020, 3, 4), dt.datetime(2020, 3, 4, 17, 30)), D(2020, 3, 5)
        )

    def test_unknown_acceptance_is_conservative_next_session(self):
        self.assertEqual(
            icr.arrival_session(D(2020, 3, 6), None), D(2020, 3, 9)
        )  # Friday -> Monday


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


if __name__ == "__main__":
    unittest.main()


class TestParamsParity(unittest.TestCase):
    """The frozen JSON and the module constants must agree (spec drift guard)."""

    def test_constants_match_frozen_params(self):
        import json
        from pathlib import Path

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
