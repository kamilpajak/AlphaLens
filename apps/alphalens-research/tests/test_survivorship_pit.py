"""Unit tests for `alphalens_research.diagnostics.survivorship_pit`.

Exercises the C1 cohort-split partition, the C2 selection-bias Fisher
statistic (both elevated and null cases), the C3 wipeout repricing, and
the event loader round-trip. No Polygon calls and no live backtest —
every test builds small in-memory fixtures.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

import pandas as pd
from alphalens_pipeline.data.store.delisting import (
    DelistingEvent,
    load_delisting_events,
)
from alphalens_pipeline.data.store.history import HistoryStore
from alphalens_research.backtest.engine import BacktestReport, RebalanceSnapshot
from alphalens_research.diagnostics.survivorship_pit import (
    compute_selection_bias,
    evaluate_decision_gate,
    reprice_picks_with_wipeout,
    split_universe_by_ipo_cohort,
)


def _synth_history(start: str, n_bars: int = 260) -> pd.DataFrame:
    idx = pd.date_range(start, periods=n_bars, freq="B")
    return pd.DataFrame(
        {
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.0,
            "volume": 1_000_000,
        },
        index=idx,
    )


def _rebalance_snapshot(d: str, tickers: list[str], fwd_returns: list[float]) -> RebalanceSnapshot:
    return RebalanceSnapshot(
        date=pd.Timestamp(d),
        scored_count=len(tickers),
        top_n_tickers=list(tickers),
        top_n_scores=[1.0] * len(tickers),
        top_n_forward_returns=list(fwd_returns),
        portfolio_return=float(sum(fwd_returns) / max(len(fwd_returns), 1)),
        portfolio_return_holding=float(sum(fwd_returns) / max(len(fwd_returns), 1)),
        universe_median_return=0.0,
        ic=0.1,
    )


class TestCohortSplit(unittest.TestCase):
    def test_partition_by_first_bar_date(self):
        store = HistoryStore(
            {
                "OLD_A": _synth_history("2020-01-02"),
                "OLD_B": _synth_history("2020-06-01"),
                "NEW_A": _synth_history("2022-03-01"),
                "NEW_B": _synth_history("2023-07-10"),
            }
        )
        pre, post = split_universe_by_ipo_cohort(
            store, ["OLD_A", "OLD_B", "NEW_A", "NEW_B"], asof=date(2021, 4, 19)
        )
        self.assertEqual(pre, ["OLD_A", "OLD_B"])
        self.assertEqual(post, ["NEW_A", "NEW_B"])

    def test_unknown_tickers_excluded(self):
        store = HistoryStore({"KNOWN": _synth_history("2020-01-02")})
        pre, post = split_universe_by_ipo_cohort(
            store, ["KNOWN", "MYSTERY"], asof=date(2021, 4, 19)
        )
        self.assertEqual(pre, ["KNOWN"])
        self.assertEqual(post, [])


class TestSelectionBias(unittest.TestCase):
    def test_detects_elevated_delisting_rate(self):
        """Every pick is a ticker that delists within 30 days; universe
        wide rate is very low → lift_ratio >> 1, Fisher p < 0.01.
        """
        picks = pd.DataFrame(
            [
                {"pick_date": date(2023, 1, 10), "ticker": "DYING_A", "rank": 1},
                {"pick_date": date(2023, 2, 10), "ticker": "DYING_B", "rank": 1},
                {"pick_date": date(2023, 3, 10), "ticker": "DYING_C", "rank": 1},
                {"pick_date": date(2023, 4, 10), "ticker": "DYING_D", "rank": 1},
                {"pick_date": date(2023, 5, 10), "ticker": "DYING_E", "rank": 1},
            ]
        )
        events = [
            DelistingEvent("DYING_A", date(2023, 1, 20), "bankruptcy"),
            DelistingEvent("DYING_B", date(2023, 2, 20), "bankruptcy"),
            DelistingEvent("DYING_C", date(2023, 3, 20), "bankruptcy"),
            DelistingEvent("DYING_D", date(2023, 4, 20), "bankruptcy"),
            DelistingEvent("DYING_E", date(2023, 5, 20), "bankruptcy"),
        ]
        universe = ["DYING_A", "DYING_B", "DYING_C", "DYING_D", "DYING_E"] + [
            f"ALIVE_{i}" for i in range(95)
        ]
        results = compute_selection_bias(picks, events, universe, windows=(30,))
        self.assertEqual(len(results), 1)
        r = results[0]
        self.assertEqual(r.n_delistings_in_picks, 5)
        self.assertAlmostEqual(r.pick_delisting_rate, 1.0)
        self.assertLess(r.fisher_p, 0.01)
        self.assertGreater(r.lift_ratio, 10.0)

    def test_null_case_rates_match(self):
        """Picks reflect the universe-wide delisting rate exactly → lift
        near 1.0 and Fisher p well above 0.05.
        """
        picks = pd.DataFrame(
            [{"pick_date": date(2023, 1, 10), "ticker": f"T{i}", "rank": 1} for i in range(20)]
        )
        # 2 of 20 picks are delisted within 30d ⇒ 10%
        events = [
            DelistingEvent("T0", date(2023, 1, 20), "unknown"),
            DelistingEvent("T1", date(2023, 1, 25), "unknown"),
        ]
        # Universe has 200 tickers with 20 delistings ⇒ 10%
        universe = [f"U{i}" for i in range(200)]
        events_full = events + [
            DelistingEvent(f"U{i}", date(2023, 6, 15), "unknown") for i in range(20)
        ]
        results = compute_selection_bias(picks, events_full, universe, windows=(30,))
        r = results[0]
        self.assertGreater(r.fisher_p, 0.05)
        # Lift can't be computed exactly to 1.0 since picks and universe
        # partition differently; assert it's "near 1" not >> 1.
        self.assertLess(r.lift_ratio, 2.0)

    def test_fisher_p_compares_picks_vs_non_picks_not_picks_vs_full_universe(self):
        """Regression: the Fisher 2×2 contingency table must compare
        picks against NON-picks (control group), not picks against the
        full universe (which includes picks themselves).

        Set-up: picks are 50 of 100 universe names; all 10 delistings
        concentrate in picks. True effect is extreme — picks rate is 20%
        while non-picks rate is 0%. Under the correct contingency,
        Fisher's exact p must be vanishingly small (< 1e-3).

        Under the buggy table that puts (picks + non-picks) in row 2,
        the 10 picks' delistings get double-counted and the test
        effectively compares 10/50 vs 10/100. Empirically the buggy
        p ≈ 0.125 — fails to reject the null at α=5% despite a clear
        signal — while the correct table gives p ≈ 1.2e-3 (two orders
        of magnitude tighter). The bug completely hides the selection
        bias when picks are a non-trivial fraction of universe.
        """
        pick_tickers = [f"P{i:02d}" for i in range(50)]
        nonpick_tickers = [f"N{i:02d}" for i in range(50)]
        universe = pick_tickers + nonpick_tickers
        picks = pd.DataFrame(
            [
                {"pick_date": date(2023, 1, 1), "ticker": t, "rank": rank}
                for rank, t in enumerate(pick_tickers, start=1)
            ]
        )
        events = [DelistingEvent(f"P{i:02d}", date(2023, 1, 15), "bankruptcy") for i in range(10)]
        results = compute_selection_bias(picks, events, universe, windows=(30,))
        r = results[0]
        self.assertEqual(r.n_picks, 50)
        self.assertEqual(r.n_delistings_in_picks, 10)
        self.assertEqual(r.universe_n_delistings, 10)
        # Picks 20% vs non-picks 0% — clear bias. Correct two-sided Fisher
        # on [10,40;0,50] = 1.187e-3. Buggy table gives 0.125. Cutoff at
        # 2e-3 separates correct from buggy by 2 orders of magnitude.
        self.assertLess(r.fisher_p, 2e-3)


class TestWipeoutReprice(unittest.TestCase):
    def _make_report(self, fwd_a: float = 0.05, fwd_b: float = -0.02) -> BacktestReport:
        rep = BacktestReport(
            scorer_config={},
            holding_period=5,
            top_n=2,
            start=date(2023, 1, 2),
            end=date(2023, 1, 6),
            benchmark="SPY",
            universe_ticker_count=2,
            rebalance_results=[
                _rebalance_snapshot("2023-01-02", ["A", "B"], [fwd_a, fwd_b]),
                _rebalance_snapshot("2023-01-03", ["A", "B"], [fwd_a, fwd_b]),
            ],
        )
        return rep

    def test_mid_holding_delisting_gets_wipeout(self):
        """Ticker delisted 3 days after entry (inside holding=5) should
        be marked −1.0. Unaffected days stay untouched.
        """
        baseline = self._make_report()
        events = [DelistingEvent("A", date(2023, 1, 4), "bankruptcy")]
        repriced = reprice_picks_with_wipeout(baseline, events)

        # Day 1: A delisted on 2023-01-04, entry 2023-01-02, hold 5
        # → inside window → wiped
        day1_fwd = repriced.rebalance_results[0].top_n_forward_returns
        self.assertEqual(day1_fwd[0], -1.0)
        self.assertAlmostEqual(day1_fwd[1], -0.02)  # B unchanged

        # Day 2 entry 2023-01-03, A delisting 2023-01-04 still inside window
        day2_fwd = repriced.rebalance_results[1].top_n_forward_returns
        self.assertEqual(day2_fwd[0], -1.0)

    def test_unaffected_days_preserved(self):
        """If no delisting matches, the report passes through intact."""
        baseline = self._make_report()
        events: list[DelistingEvent] = []  # no events at all
        repriced = reprice_picks_with_wipeout(baseline, events)
        self.assertIs(repriced.rebalance_results[0], baseline.rebalance_results[0])

    def test_wipeout_is_idempotent(self):
        """Re-priced twice produces the same result — already-wiped picks
        aren't double-processed.
        """
        baseline = self._make_report()
        events = [DelistingEvent("A", date(2023, 1, 4), "bankruptcy")]
        once = reprice_picks_with_wipeout(baseline, events)
        twice = reprice_picks_with_wipeout(once, events)
        self.assertEqual(
            twice.rebalance_results[0].top_n_forward_returns,
            once.rebalance_results[0].top_n_forward_returns,
        )


class TestLoadDelistingEvents(unittest.TestCase):
    def test_round_trip_parquet_yaml_merge(self):
        """Events come from both parquet and YAML, de-duped on (ticker, date)."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            # Parquet with 2 events
            df = pd.DataFrame(
                [
                    {
                        "ticker": "AAA",
                        "delisted_date": pd.Timestamp("2022-06-15"),
                        "name": "Alpha",
                        "reason": "acquisition",
                    },
                    {
                        "ticker": "BBB",
                        "delisted_date": pd.Timestamp("2023-03-01"),
                        "name": "Beta",
                        "reason": "unknown",
                    },
                ]
            )
            parquet = tmp_path / "events.parquet"
            df.to_parquet(parquet, index=False)

            # YAML with 1 overlap + 1 new
            yaml_path = tmp_path / "events.yaml"
            yaml_path.write_text(
                "delisted:\n"
                "  - ticker: AAA\n"
                "    delisted: 2022-06-15\n"
                "    name: Alpha (dup)\n"
                "  - ticker: CCC\n"
                "    delisted: 2024-01-20\n"
                "    name: Gamma\n"
            )

            events = load_delisting_events(parquet_path=parquet, yaml_path=yaml_path)
            self.assertEqual({e.ticker for e in events}, {"AAA", "BBB", "CCC"}, "3 unique")
            aaa = next(e for e in events if e.ticker == "AAA")
            # Parquet seeds first → reason preserved as "acquisition", YAML dup ignored
            self.assertEqual(aaa.reason, "acquisition")


class TestDecisionGate(unittest.TestCase):
    def test_all_pass_returns_pass(self):
        from alphalens_research.diagnostics.survivorship_pit import (
            CohortSplitResult,
            MidHoldingAuditResult,
            SelectionBiasResult,
        )

        cohorts = [
            CohortSplitResult("pre-existing", 95, 1000, 1.5, 0.5, 0.05, 2.0, 2.8, 0.5, 0.3),
            CohortSplitResult("post-IPO", 18, 800, 1.4, 0.4, 0.04, 1.8, 2.5, 0.4, 0.3),
            CohortSplitResult("full", 113, 1000, 1.5, 0.5, 0.05, 2.0, 2.8, 0.5, 0.3),
        ]
        bias = [
            SelectionBiasResult(30, 6300, 10, 0.0016, 113, 2, 0.018, 0.09, 0.3),
            SelectionBiasResult(90, 6300, 15, 0.0024, 113, 3, 0.027, 0.09, 0.4),
            SelectionBiasResult(180, 6300, 20, 0.0032, 113, 4, 0.036, 0.09, 0.5),
        ]
        audit = MidHoldingAuditResult(6300, 5, 0.0008, 1.5, 1.45, -0.05, 2.6, 2.5, -0.1, ())
        gate = evaluate_decision_gate(cohorts, bias, audit)
        self.assertTrue(gate["c1_pass"])
        self.assertTrue(gate["c2_pass"])
        self.assertTrue(gate["c3_pass"])
        self.assertEqual(gate["overall"], "PASS")


if __name__ == "__main__":
    unittest.main()
