"""The read side: cluster bootstrap + the contract's three fixed verdicts.

Each test names the clause it pins. Written before the data matured, so no
number here was chosen with knowledge of the answer.
"""

from __future__ import annotations

import datetime as dt
import unittest

import pandas as pd
from scripts.read_bracket_cost import (
    ARM_DISCARDED,
    ARM_KEPT,
    MIN_PAIRED_DAYS,
    MIN_TERMINAL_PER_ARM,
    PROMPT_CHANGE_DATE,
    VERDICT_EARNS,
    VERDICT_INCONCLUSIVE,
    VERDICT_NOT_JUSTIFIED,
    cluster_bootstrap_median_diff,
    decide,
    excluded_verdict_counts,
    positive_control,
    report,
)


def _rows(
    *, discarded: list[float], kept: list[float], days: int = MIN_PAIRED_DAYS
) -> pd.DataFrame:
    """Terminal rows spread across ``days`` so the day-cluster has something to resample.

    The default is the paired-day floor: with fewer days every verdict test would
    be short-circuited to INCONCLUSIVE by Amendment 3 and would stop testing what
    it names.
    """
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

    def test_unclassified_rows_are_not_counted_as_disagreement(self):
        """A row this store has not classified holds no opinion to disagree with.

        Same rule as the empty-overlap case one level up, which this file
        already pins: 'not checked' and 'disagreed' are opposite facts. Letting
        an unresolved row score as a disagreement made the control read 61%
        when the rows it had actually resolved agreed 86% of the time.
        """
        replayed = self._replayed(
            brief_date=["2026-08-06", "2026-08-07"],
            ticker=["AAA", "BBB"],
            arm=[ARM_KEPT, ARM_KEPT],
            ladder_classification=["SL_HIT", None],
        )

        out = positive_control(replayed, self._production())

        self.assertEqual(out["n_comparable"], 1)
        self.assertEqual(out["classification_agreement"], 1.0)
        self.assertEqual(out["n_unclassified_here"], 1)

    def test_all_unclassified_reports_none(self):
        replayed = self._replayed(ladder_classification=[None, None])

        out = positive_control(replayed, self._production())

        self.assertEqual(out["n_comparable"], 0)
        self.assertIsNone(out["classification_agreement"])
        self.assertEqual(out["n_unclassified_here"], 2)


class TestReportedNMatchesTheMedian(unittest.TestCase):
    """Contract §7: every figure is reported with ITS OWN N.

    A count of rows beside a median computed over a subset of them is two
    different denominators wearing one label.
    """

    def test_mega_split_n_counts_the_rows_the_median_used(self):
        frame = pd.DataFrame(
            {
                "arm": [ARM_DISCARDED] * 4,
                "ticker": ["AAA", "BBB", "CCC", "DDD"],
                "brief_date": ["2026-08-06"] * 4,
                "terminal": [True] * 4,
                "market_cap": [60e9, 70e9, 80e9, 1e9],
                "realized_r": [0.5, None, 1.5, 9.9],
                "ladder_classification": ["TP_FULL", "NO_FILL", "TP_FULL", "TP_FULL"],
                "market_excess_return": [None] * 4,
            }
        )

        out = report(frame)

        self.assertEqual(out["mega_split"]["n"], 2)
        self.assertEqual(out["mega_split"]["median_realized_r"], 1.0)


class TestClausesTheFirstDraftSkipped(unittest.TestCase):
    """Clauses the read script did not implement until a clause-by-clause walk.

    Contract §3 requires the excluded verdicts to be counted and reported rather
    than silently omitted; §9 requires the 2026-08-18 prompt change to be
    reported as a stratum and a theme-stratified primary as a secondary. All
    three were absent from the first draft.
    """

    def _terminal(self) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "arm": [ARM_DISCARDED, ARM_DISCARDED, ARM_KEPT, ARM_KEPT],
                "ticker": ["AAA", "BBB", "CCC", "DDD"],
                "theme": ["t1", "t2", "t1", "t2"],
                "brief_date": [
                    dt.date(2026, 8, 7),
                    dt.date(2026, 8, 20),
                    dt.date(2026, 8, 7),
                    dt.date(2026, 8, 20),
                ],
                "terminal": [True] * 4,
                "market_cap": [50e9, 60e9, 3e9, 4e9],
                "realized_r": [1.0, 2.0, 0.5, 1.5],
                "ladder_classification": ["TP_FULL"] * 4,
                "market_excess_return": [None] * 4,
            }
        )

    def test_prompt_change_stratum_splits_on_the_named_date(self):
        out = report(self._terminal())

        strata = out["prompt_change_strata"]
        self.assertEqual(strata["before"]["discarded"]["n"], 1)
        self.assertEqual(strata["on_or_after"]["discarded"]["n"], 1)
        self.assertEqual(strata["boundary_date"], str(PROMPT_CHANGE_DATE))

    def test_theme_stratified_primary_is_reported(self):
        out = report(self._terminal())

        self.assertIn("t1", out["by_theme"])
        self.assertEqual(out["by_theme"]["t1"]["discarded"]["n"], 1)
        self.assertEqual(out["by_theme"]["t1"]["discarded"]["median_realized_r"], 1.0)

    def test_excluded_verdicts_are_counted_not_omitted(self):
        funnel = pd.DataFrame(
            {
                "ticker": ["A", "B", "C", "D", "E"],
                "bracket_verdict": [
                    "too_big",
                    "in_bracket",
                    "too_small",
                    "no_mcap",
                    "too_small",
                ],
            }
        )

        out = excluded_verdict_counts(funnel)

        self.assertEqual(out, {"too_small": 2, "no_mcap": 1})


class TestPairedDayFloor(unittest.TestCase):
    """Amendment 3. A percentile cluster bootstrap needs enough CLUSTERS.

    Measured on the real store at read 1: 9 days carried a realised R but the
    kept arm appeared on only 3 of them, so the effective cluster count for a
    DIFFERENCE was 3. A row-count floor cannot see that.
    """

    def _paired(self, *, paired_days: int, rows_per_arm: int) -> pd.DataFrame:
        recs = []
        for day in range(paired_days):
            for i in range(rows_per_arm):
                for arm in (ARM_DISCARDED, ARM_KEPT):
                    recs.append(
                        {
                            "arm": arm,
                            "brief_date": f"2026-08-{1 + day:02d}",
                            "realized_r": 0.1 * (i + 1) + (0.5 if arm == ARM_DISCARDED else 0.0),
                        }
                    )
        return pd.DataFrame(recs)

    def test_too_few_paired_days_is_inconclusive_even_with_many_rows(self):
        frame = self._paired(paired_days=MIN_PAIRED_DAYS - 1, rows_per_arm=40)

        out = decide(frame, n_draws=200, seed=5)

        self.assertEqual(out.verdict, VERDICT_INCONCLUSIVE)
        self.assertGreaterEqual(out.n_discarded, MIN_TERMINAL_PER_ARM)

    def test_enough_paired_days_allows_a_verdict(self):
        frame = self._paired(paired_days=MIN_PAIRED_DAYS, rows_per_arm=3)

        out = decide(frame, n_draws=200, seed=5)

        self.assertNotEqual(out.verdict, VERDICT_INCONCLUSIVE)

    def test_days_carrying_only_one_arm_do_not_count_as_paired(self):
        frame = self._paired(paired_days=2, rows_per_arm=20)
        solo = pd.DataFrame(
            {
                "arm": [ARM_DISCARDED] * 30,
                "brief_date": [f"2026-09-{1 + i:02d}" for i in range(30)],
                "realized_r": [0.3] * 30,
            }
        )

        out = decide(pd.concat([frame, solo], ignore_index=True), n_draws=200, seed=5)

        self.assertEqual(out.n_paired_days, 2)
        self.assertEqual(out.verdict, VERDICT_INCONCLUSIVE)

    def test_skipped_draw_fraction_is_reported_not_hidden(self):
        frame = self._paired(paired_days=MIN_PAIRED_DAYS, rows_per_arm=3)

        out = decide(frame, n_draws=200, seed=5)

        self.assertIsNotNone(out.skipped_draw_fraction)
        self.assertGreaterEqual(out.skipped_draw_fraction, 0.0)


if __name__ == "__main__":
    unittest.main()
