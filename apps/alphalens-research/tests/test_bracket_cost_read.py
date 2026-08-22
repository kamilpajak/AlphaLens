"""The read side: cluster bootstrap + the contract's three fixed verdicts.

Each test names the clause it pins. Written before the data matured, so no
number here was chosen with knowledge of the answer.
"""

from __future__ import annotations

import unittest

import pandas as pd
from scripts.read_bracket_cost import (
    ARM_DISCARDED,
    ARM_KEPT,
    MIN_TERMINAL_PER_ARM,
    VERDICT_EARNS,
    VERDICT_INCONCLUSIVE,
    VERDICT_NOT_JUSTIFIED,
    cluster_bootstrap_median_diff,
    decide,
    positive_control,
)


def _rows(*, discarded: list[float], kept: list[float], days: int = 6) -> pd.DataFrame:
    """Terminal rows spread across ``days`` so the day-cluster has something to resample."""
    recs = []
    for i, r in enumerate(discarded):
        recs.append(
            {"arm": ARM_DISCARDED, "brief_date": f"2026-08-{6 + i % days:02d}", "realized_r": r}
        )
    for i, r in enumerate(kept):
        recs.append({"arm": ARM_KEPT, "brief_date": f"2026-08-{6 + i % days:02d}", "realized_r": r})
    return pd.DataFrame(recs)


class TestClusterBootstrap(unittest.TestCase):
    def test_is_deterministic_for_a_given_seed(self):
        frame = _rows(discarded=[0.1] * 40, kept=[0.5] * 40)

        a = cluster_bootstrap_median_diff(frame, n_draws=200, seed=1)
        b = cluster_bootstrap_median_diff(frame, n_draws=200, seed=1)

        self.assertEqual(a, b)

    def test_different_seeds_do_not_silently_agree(self):
        """A bootstrap that ignored its seed would pass the determinism test.

        Days must carry DIFFERENT medians for this to have any power: with the
        same median on every day, resampling days cannot move the estimate and
        a seed-ignoring implementation would pass. The first version of this
        fixture had exactly that flaw.
        """
        recs = []
        for day, value in enumerate([-1.5, -0.4, 0.1, 0.8, 2.2, 4.0]):
            for _ in range(6):
                recs.append(
                    {
                        "arm": ARM_DISCARDED,
                        "brief_date": f"2026-08-{6 + day:02d}",
                        "realized_r": value,
                    }
                )
                recs.append(
                    {"arm": ARM_KEPT, "brief_date": f"2026-08-{6 + day:02d}", "realized_r": 0.0}
                )
        frame = pd.DataFrame(recs)

        a = cluster_bootstrap_median_diff(frame, n_draws=200, seed=1)
        b = cluster_bootstrap_median_diff(frame, n_draws=200, seed=2)

        self.assertNotEqual(a, b)

    def test_resamples_days_not_rows(self):
        """Contract §6. Resampling rows would ignore within-day dependence.

        Every row inside a day is identical here and days differ, so a row
        bootstrap would produce a much tighter interval than a day bootstrap.
        """
        recs = []
        for day, value in enumerate([-2.0, -1.0, 0.0, 1.0, 2.0, 3.0]):
            for _ in range(20):
                recs.append(
                    {
                        "arm": ARM_DISCARDED,
                        "brief_date": f"2026-08-{6 + day:02d}",
                        "realized_r": value,
                    }
                )
                recs.append(
                    {"arm": ARM_KEPT, "brief_date": f"2026-08-{6 + day:02d}", "realized_r": 0.0}
                )
        frame = pd.DataFrame(recs)

        lo, hi = cluster_bootstrap_median_diff(frame, n_draws=500, seed=7)

        self.assertGreater(hi - lo, 0.5)


class TestDecide(unittest.TestCase):
    def test_floor_binds_before_any_comparison(self):
        """Contract §8. Thin arm -> INCONCLUSIVE whatever the medians say."""
        frame = _rows(discarded=[-5.0] * 100, kept=[5.0] * (MIN_TERMINAL_PER_ARM - 1))

        out = decide(frame, n_draws=200, seed=3)

        self.assertEqual(out.verdict, VERDICT_INCONCLUSIVE)

    def test_floor_binds_on_the_other_arm_too(self):
        frame = _rows(discarded=[-5.0] * (MIN_TERMINAL_PER_ARM - 1), kept=[5.0] * 100)

        out = decide(frame, n_draws=200, seed=3)

        self.assertEqual(out.verdict, VERDICT_INCONCLUSIVE)

    def test_kept_clearly_better_earns_its_keep(self):
        frame = _rows(discarded=[-1.0] * 60, kept=[1.0] * 60)

        out = decide(frame, n_draws=500, seed=3)

        self.assertEqual(out.verdict, VERDICT_EARNS)

    def test_interval_spanning_zero_is_not_justified(self):
        frame = _rows(discarded=[0.2] * 60, kept=[0.2] * 60)

        out = decide(frame, n_draws=500, seed=3)

        self.assertEqual(out.verdict, VERDICT_NOT_JUSTIFIED)

    def test_discarded_arm_higher_is_not_justified(self):
        """Contract §12: arm A higher can never read as the bracket earning its keep."""
        frame = _rows(discarded=[1.0] * 60, kept=[-1.0] * 60)

        out = decide(frame, n_draws=500, seed=3)

        self.assertEqual(out.verdict, VERDICT_NOT_JUSTIFIED)

    def test_reports_the_n_it_ran_on(self):
        """Contract §11: every read states its own sample size."""
        frame = _rows(discarded=[0.1] * 45, kept=[0.2] * 33)

        out = decide(frame, n_draws=200, seed=3)

        self.assertEqual(out.n_discarded, 45)
        self.assertEqual(out.n_kept, 33)


if __name__ == "__main__":
    unittest.main()


class TestPositiveControl(unittest.TestCase):
    """Contract §10. A synthetic-brief path that diverges from production
    invalidates the whole run, so the check must be able to FAIL loudly and must
    not report a comfortable number when there is nothing to compare."""

    def _replayed(self, **over) -> pd.DataFrame:
        base = {
            "brief_date": ["2026-08-06", "2026-08-07"],
            "ticker": ["AAA", "BBB"],
            "arm": [ARM_KEPT, ARM_KEPT],
            "ladder_classification": ["SL_HIT", "TP_FULL"],
        }
        base.update(over)
        return pd.DataFrame(base)

    def _production(self, **over) -> pd.DataFrame:
        base = {
            "brief_date": ["2026-08-06", "2026-08-07"],
            "ticker": ["AAA", "BBB"],
            "ladder_classification": ["SL_HIT", "TP_FULL"],
        }
        base.update(over)
        return pd.DataFrame(base)

    def test_full_agreement_reports_one(self):
        out = positive_control(self._replayed(), self._production())

        self.assertEqual(out["n_overlap"], 2)
        self.assertEqual(out["classification_agreement"], 1.0)

    def test_disagreement_is_visible(self):
        prod = self._production(ladder_classification=["TP_FULL", "TP_FULL"])

        out = positive_control(self._replayed(), prod)

        self.assertEqual(out["classification_agreement"], 0.5)

    def test_no_overlap_reports_none_not_zero(self):
        """Zero overlap is 'nothing was checked', never 'everything disagreed'."""
        prod = self._production(ticker=["ZZZ", "YYY"])

        out = positive_control(self._replayed(), prod)

        self.assertEqual(out["n_overlap"], 0)
        self.assertIsNone(out["classification_agreement"])

    def test_only_the_kept_arm_is_compared(self):
        """The production store holds no discarded rows; including them would
        manufacture a disagreement out of rows that cannot exist there."""
        replayed = self._replayed(
            brief_date=["2026-08-06", "2026-08-07", "2026-08-06"],
            ticker=["AAA", "BBB", "NVDA"],
            arm=[ARM_KEPT, ARM_KEPT, ARM_DISCARDED],
            ladder_classification=["SL_HIT", "TP_FULL", "TP_FULL"],
        )

        out = positive_control(replayed, self._production())

        self.assertEqual(out["n_overlap"], 2)
