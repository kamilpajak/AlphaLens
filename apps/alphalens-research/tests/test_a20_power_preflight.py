"""Unit tests for the #1227 A20 power preflight (ledger amendment + power sim).

Hermetic: the socket guard forbids network, and every case here feeds synthetic
frames to the pure functions. The one thing that cannot be unit-tested is the
number the preflight finally prints — that is a single recorded run, not a
regression fixture.

The load-bearing case is ``TestHeldOutBlindness``. The held-out panel may
contribute STRUCTURE only: which arrival session an episode falls on, and
whether it is in the confirmation population. Not the label — and, the part a
``sel_ar_*`` denylist would miss, **not the signal columns either**: reading
held-out ``technical_atr_pct`` to learn how the three statistics co-move would
put the held-out dependence structure straight into a Holm power number without
touching a single label value.
"""

from __future__ import annotations

import datetime as dt
import unittest

import pandas as pd
from scripts.ml import a20_power as pre


def _held_out_frame(rows: list[tuple[str, str, str, bool]]) -> pd.DataFrame:
    """(brief_date, ticker, anchor_session, briefed_any_theme) plus noise columns.

    ``briefed_any_theme`` — not ``population`` — is what separates a name the
    brief actually carried from one only the LLM proposed; measured on the real
    store, ``population`` distinguishes the provenance of the row SET
    (briefed_or_llm_proposed vs pre_open_recovered) and not the two arms.

    The noise columns are the point: a real read of this store would hand back
    labels and signals too, so the frame the tests pass in carries them.
    """
    frame = pd.DataFrame(
        rows, columns=["brief_date", "ticker", "anchor_session", "briefed_any_theme"]
    )
    frame["sel_label_status_20"] = "ok"
    frame["sel_ar_20"] = 0.123  # must never be read
    frame["sel_zar_20"] = 1.5  # must never be read
    frame["technical_atr_pct"] = 4.2  # must never be read either
    return frame


class TestHeldOutBlindness(unittest.TestCase):
    def test_the_allowlist_carries_no_outcome_and_no_signal(self):
        # Asserted against the constant rather than the call, so a future column
        # added to the read has to be argued for here first.
        self.assertEqual(
            set(pre.HELD_OUT_COLUMNS),
            {"brief_date", "ticker", "anchor_session", "briefed_any_theme", "sel_label_status_20"},
        )
        for banned in ("sel_ar_20", "sel_zar_20", "technical_atr_pct", "car_10"):
            self.assertNotIn(banned, pre.HELD_OUT_COLUMNS)

    def test_the_structure_read_touches_only_the_allowlist(self):
        frame = _held_out_frame([("2026-07-06", "AAA", "2026-07-07", True)])
        seen: list[str] = []

        class _Watched(pd.DataFrame):
            """Records every column the code asks for by name."""

            @property
            def _constructor(self):
                return _Watched

            def __getitem__(self, key):
                seen.extend([key] if isinstance(key, str) else list(key))
                return super().__getitem__(key)

        pre.held_out_structure(_Watched(frame), population=pre.POPULATION_BRIEFED)

        self.assertTrue(seen, "positive control: the watcher recorded nothing at all")
        self.assertTrue(
            set(seen) <= set(pre.HELD_OUT_COLUMNS),
            f"held-out branch read outside the allowlist: {sorted(set(seen) - set(pre.HELD_OUT_COLUMNS))}",
        )

    def test_the_watcher_can_refute(self):
        # The control for the control: if the code did read a label, the check
        # above must fail rather than pass quietly.
        seen = {"anchor_session", "sel_ar_20"}
        self.assertFalse(seen <= set(pre.HELD_OUT_COLUMNS))


class TestTheStructureMatchesTheSimulatedPopulation(unittest.TestCase):
    """Cluster sizes must come from the population the test will actually run on.

    Reading sizes from every held-out row while the confirmation runs on the
    briefed subset would inflate power by exactly the ratio between them — a
    silent overstatement that no assertion about labels would catch.
    """

    _ROWS = [
        ("2026-07-06", "AAA", "2026-07-07", True),
        ("2026-07-06", "BBB", "2026-07-07", True),
        ("2026-07-06", "CCC", "2026-07-07", False),
        ("2026-07-07", "DDD", "2026-07-08", False),
    ]

    def test_the_briefed_population_sees_only_its_own_clusters(self):
        sizes = pre.held_out_structure(
            _held_out_frame(self._ROWS), population=pre.POPULATION_BRIEFED
        )
        self.assertEqual(sorted(sizes), [2])  # one arrival session, two episodes

    def test_the_wider_population_sees_more(self):
        sizes = pre.held_out_structure(_held_out_frame(self._ROWS), population=pre.POPULATION_ALL)
        self.assertEqual(sorted(sizes), [1, 3])

    def test_an_immature_row_is_not_a_cluster(self):
        frame = _held_out_frame(self._ROWS)
        frame.loc[frame["ticker"] == "AAA", "sel_label_status_20"] = "immature"
        sizes = pre.held_out_structure(frame, population=pre.POPULATION_BRIEFED)
        self.assertEqual(sorted(sizes), [1])


class TestHolm(unittest.TestCase):
    """Holm step-down at FWER 0.05 across the three #1227 hypotheses."""

    def test_the_sorted_bars_are_alpha_over_three_two_one(self):
        self.assertEqual(pre.holm_bars(3, 0.05), [0.05 / 3, 0.05 / 2, 0.05])

    def test_all_three_clear_when_every_p_is_tiny(self):
        self.assertEqual(
            pre.holm_reject({"atr": 1e-6, "ma50": 1e-6, "press": 1e-6}), {"atr", "ma50", "press"}
        )

    def test_the_step_down_stops_at_the_first_failure(self):
        # p = 0.01 clears 0.05/3; 0.03 fails 0.05/2, and the step-down then
        # rejects nothing further EVEN THOUGH 0.04 < 0.05. That early stop is
        # the whole difference from three independent tests.
        rejected = pre.holm_reject({"atr": 0.01, "ma50": 0.03, "press": 0.04})
        self.assertEqual(rejected, {"atr"})

    def test_nothing_clears_when_the_smallest_p_misses_its_bar(self):
        self.assertEqual(pre.holm_reject({"atr": 0.02, "ma50": 0.03, "press": 0.04}), set())

    def test_a_tie_at_the_bar_is_rejected(self):
        # p <= bar, not p < bar: the boundary belongs to the rejection region.
        self.assertEqual(pre.holm_reject({"a": 0.05 / 3}, alpha=0.05), {"a"})


class TestTheGateDateArithmetic(unittest.TestCase):
    """Turning 'we need N clusters' into a date.

    The first version of this class asserted that the missing arrivals had to
    HAPPEN and then mature, and the implementation obeyed it — so the test
    passed and confirmed a bug that put the gate five weeks late. Arrivals
    accrue daily and are already in flight; what is missing is maturity, not
    the arrivals. Measured 2026-09-22: 55 arrivals existed, 34 had matured.
    """

    @staticmethod
    def _arrivals(n: int = 50, start: dt.date = dt.date(2026, 7, 6)) -> list[str]:
        """``n`` consecutive XNYS sessions — arrivals are sessions, not dates.

        Built from the real calendar rather than a date range: a fixture with
        weekends in it does not round-trip through the session arithmetic, and
        the first version of this test failed for that reason rather than for
        the behaviour it is about.
        """
        from alphalens_pipeline.paper.calendar import advance_trading_sessions

        return [advance_trading_sessions(start, i).isoformat() for i in range(n)]

    def test_an_arrival_that_already_happened_only_has_to_mature(self):
        # The 38th arrival in this list is in the past; the answer is its
        # maturity date, NOT today plus four sessions plus the lag.
        got = pre.gate_date(
            observed_arrivals=self._arrivals(),
            clusters_needed=38,
            accrual_per_session=1.0,
            maturity_lag_sessions=20,
            today=dt.date(2026, 9, 22),
        )
        nth = dt.date.fromisoformat(sorted(self._arrivals())[37])
        self.assertEqual(pre.sessions_between(nth, got), 20)
        self.assertLess(got, dt.date(2026, 10, 14))

    def test_a_count_beyond_the_observed_arrivals_falls_back_to_accrual(self):
        # The projection still exists for the case it is for.
        got = pre.gate_date(
            observed_arrivals=self._arrivals(10),
            clusters_needed=20,
            accrual_per_session=1.0,
            maturity_lag_sessions=20,
            today=dt.date(2026, 9, 22),
        )
        self.assertEqual(pre.sessions_between(dt.date(2026, 9, 22), got), 30)

    def test_a_gate_already_met_is_never_in_the_past(self):
        got = pre.gate_date(
            observed_arrivals=self._arrivals(),
            clusters_needed=5,
            accrual_per_session=1.0,
            maturity_lag_sessions=20,
            today=dt.date(2026, 9, 22),
        )
        self.assertEqual(got, dt.date(2026, 9, 22))


class TestShrinkage(unittest.TestCase):
    def test_the_grid_is_the_one_1227_pre_registered(self):
        self.assertEqual(pre.SHRINKAGE_GRID, (0.25, 0.50, 0.75))

    def test_shrinking_scales_the_effect_and_keeps_its_sign(self):
        # The July ATR effect is NEGATIVE (higher ATR -> worse outcome); a
        # shrinkage that flipped the sign would simulate the wrong alternative.
        self.assertAlmostEqual(pre.shrink(-0.35, 0.50), -0.175)
        self.assertLess(pre.shrink(-0.35, 0.25), 0)


if __name__ == "__main__":
    unittest.main()
