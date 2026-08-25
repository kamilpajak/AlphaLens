"""The opportunity-level denominator of the #1113 instrument.

The behaviour being corrected is real and measurable: ``edge/api/summary.py``
builds its headline pool from rows that are plannable AND terminal AND carry a
finite ``realized_r``, so every NO_FILL row drops out silently. This file pins
the opposite contract -- a pick whose ladder never filled is an OPPORTUNITY,
appears in the report, and is never folded in as a costless zero.
"""

from __future__ import annotations

import unittest

from alphalens_pipeline.feedback.ladder_replay import replay_ladder
from alphalens_research.diagnostics import fill_partition as fp

MEASURED_BPS = fp.ENTRY_TRAIL_OVERSHOOT_BPS


def _opp(
    ticker: str,
    *,
    filled: tuple[str, ...] = (),
    fill_ts: tuple[int, ...] = (),
    excluded_reason: str | None = None,
    filled_fraction: float = 0.0,
    realised_r: float | None = None,
    forgone_excess_return: float | None = None,
    holding_days: int | None = None,
    mae_r: float | None = None,
    brief_date: str = "2026-08-01",
) -> fp.Opportunity:
    return fp.Opportunity(
        brief_date=brief_date,
        ticker=ticker,
        excluded_reason=excluded_reason,
        filled_tiers=filled,
        fill_bar_ts_ms=fill_ts or tuple(range(len(filled))),
        filled_fraction=filled_fraction,
        realised_r=realised_r,
        forgone_excess_return=forgone_excess_return,
        holding_days=holding_days,
        mae_r=mae_r,
    )


def _report(opps: list[fp.Opportunity]) -> fp.PartitionReport:
    return fp.partition_report(
        opps, fill_model=fp.FILL_MODEL_TOUCH, overshoot_arm=fp.OVERSHOOT_ARM_MEASURED
    )


def _stats(report: fp.PartitionReport, partition: str) -> fp.PartitionStats:
    return next(p for p in report.partitions if p.partition == partition)


class TestOpportunityDenominator(unittest.TestCase):
    def test_a_never_filled_pick_is_reported_not_dropped(self) -> None:
        report = _report(
            [
                _opp("AAA", forgone_excess_return=0.05),  # NO_FILL -- nothing deployed
                _opp("BBB", filled=("E1",), filled_fraction=0.21, realised_r=0.03),
                _opp("CCC", excluded_reason=fp.EXCLUDE_NOT_PLANNABLE),
            ]
        )
        self.assertEqual(report.n_store_rows, 3)
        self.assertEqual(report.n_opportunities, 2)
        self.assertEqual(report.excluded[fp.EXCLUDE_NOT_PLANNABLE], 1)
        self.assertEqual(_stats(report, fp.PARTITION_UNFILLED).n, 1)
        self.assertEqual(sum(p.n for p in report.partitions), report.n_opportunities)

    def test_the_partition_shares_are_taken_over_the_opportunity_denominator(self) -> None:
        report = _report(
            [
                _opp("AAA"),
                _opp("BBB", filled=("E1",)),
                _opp("CCC", filled=("E1",)),
                _opp("DDD", filled=("E1", "E2")),
            ]
        )
        self.assertAlmostEqual(_stats(report, fp.PARTITION_FIRST_ONLY).share_of_opportunities, 0.5)
        self.assertAlmostEqual(_stats(report, fp.PARTITION_UNFILLED).share_of_opportunities, 0.25)
        self.assertAlmostEqual(sum(p.share_of_opportunities for p in report.partitions), 1.0)

    def test_a_no_fill_opportunity_carries_a_non_null_opportunity_cost(self) -> None:
        report = _report([_opp("AAA", forgone_excess_return=0.07)])
        unfilled = _stats(report, fp.PARTITION_UNFILLED)
        self.assertEqual(unfilled.n, 1)
        self.assertAlmostEqual(unfilled.forgone_excess_return_mean, 0.07)
        self.assertEqual(unfilled.n_missing_forgone, 0)

    def test_an_unfilled_opportunity_is_never_counted_as_a_zero_realised_return(self) -> None:
        report = _report(
            [_opp("AAA", forgone_excess_return=0.07), _opp("BBB", forgone_excess_return=-0.02)]
        )
        unfilled = _stats(report, fp.PARTITION_UNFILLED)
        self.assertIsNone(unfilled.realised_r_mean)
        self.assertEqual(unfilled.n_realised, 0)
        # The zero is structural (no capital was deployed), not a data gap, and the
        # stats row says so rather than leaving the reader to guess.
        self.assertTrue(unfilled.no_capital_deployed)
        self.assertFalse(_stats(report, fp.PARTITION_FIRST_ONLY).no_capital_deployed)

    def test_a_missing_opportunity_cost_is_counted_not_zeroed(self) -> None:
        report = _report(
            [_opp("AAA", forgone_excess_return=0.10), _opp("BBB", forgone_excess_return=None)]
        )
        unfilled = _stats(report, fp.PARTITION_UNFILLED)
        self.assertEqual(unfilled.n, 2)
        self.assertEqual(unfilled.n_missing_forgone, 1)
        self.assertAlmostEqual(unfilled.forgone_excess_return_mean, 0.10)  # not 0.05

    def test_the_win_rate_is_taken_over_the_realised_numbers_only(self) -> None:
        report = _report(
            [
                _opp("AAA", filled=("E1",), realised_r=0.10),
                _opp("BBB", filled=("E1",), realised_r=-0.10),
                _opp("CCC", filled=("E1",), realised_r=None),
            ]
        )
        first = _stats(report, fp.PARTITION_FIRST_ONLY)
        self.assertEqual(first.n, 3)
        self.assertEqual(first.n_realised, 2)
        self.assertAlmostEqual(first.win_rate, 0.5)

    def test_a_partition_with_no_realised_number_reports_none_not_zero(self) -> None:
        report = _report([_opp("AAA", filled=("E1",), realised_r=None)])
        first = _stats(report, fp.PARTITION_FIRST_ONLY)
        self.assertIsNone(first.win_rate)
        self.assertIsNone(first.realised_r_median)


class TestExclusionBuckets(unittest.TestCase):
    def test_every_store_row_lands_in_exactly_one_bucket(self) -> None:
        opps = [
            _opp("AAA", filled=("E1",)),
            _opp("BBB"),
            _opp("CCC", excluded_reason=fp.EXCLUDE_NOT_PLANNABLE),
            _opp("DDD", excluded_reason=fp.EXCLUDE_NOT_DECIDED),
            _opp("EEE", excluded_reason=fp.EXCLUDE_NO_REPLAY),
            _opp("FFF", excluded_reason=fp.EXCLUDE_SPLIT_INVALIDATED),
            _opp("GGG", excluded_reason=fp.EXCLUDE_BAD_GEOMETRY),
        ]
        report = _report(opps)
        self.assertEqual(report.n_store_rows, len(opps))
        self.assertEqual(
            report.n_store_rows, report.n_opportunities + sum(report.excluded.values())
        )

    def test_every_exclusion_key_is_a_declared_constant(self) -> None:
        report = _report([_opp("X", excluded_reason=r) for r in fp.EXCLUSION_REASONS])
        self.assertEqual(set(report.excluded), set(fp.EXCLUSION_REASONS))

    def test_the_bucket_map_always_lists_every_reason_even_at_zero(self) -> None:
        report = _report([_opp("AAA", filled=("E1",))])
        self.assertEqual(set(report.excluded), set(fp.EXCLUSION_REASONS))
        self.assertEqual(sum(report.excluded.values()), 0)

    def test_an_undeclared_exclusion_reason_raises(self) -> None:
        with self.assertRaises(ValueError):
            _report([_opp("AAA", excluded_reason="because")])


class TestALadderDeeperThanTheDeclaredTiers(unittest.TestCase):
    """A ladder outside the declared three tiers is REPORTED, never fatal.

    ``store_fill_tier_ids`` deliberately keeps a stray ``E4`` so a disagreement
    stays visible, and ``entry_tiers_from_setup`` mints one from a four-tier
    brief. Raising out of ``partition_report`` would kill the whole store run on
    the first such row -- the opposite of the counter it was built for.
    """

    def test_an_undeclared_tier_is_bucketed_rather_than_ending_the_run(self) -> None:
        report = _report([_opp("AAA", filled=("E1", "E4")), _opp("BBB", filled=("E1",))])
        self.assertEqual(report.excluded[fp.EXCLUDE_UNDECLARED_LADDER], 1)
        self.assertEqual(report.n_opportunities, 1)
        self.assertEqual(
            report.n_store_rows, report.n_opportunities + sum(report.excluded.values())
        )

    def test_the_bucketed_row_never_enters_a_conditional_denominator(self) -> None:
        report = _report([_opp("AAA", filled=("E1", "E4"), fill_ts=(1_000, 2_000))])
        rec = next(r for r in report.conditional_fills if r.given_tier == "E1")
        self.assertEqual(rec.n_given, 0)

    def test_a_rows_own_exclusion_still_wins_over_the_ladder_shape(self) -> None:
        report = _report([_opp("AAA", filled=("E1", "E4"), excluded_reason=fp.EXCLUDE_NOT_DECIDED)])
        self.assertEqual(report.excluded[fp.EXCLUDE_NOT_DECIDED], 1)
        self.assertEqual(report.excluded[fp.EXCLUDE_UNDECLARED_LADDER], 0)

    def test_partition_of_itself_stays_strict(self) -> None:
        # The guard still exists; the report just stops letting it end the run.
        with self.assertRaises(ValueError):
            fp.partition_of({"E1", "E4"})

    def test_a_four_tier_brief_is_where_the_undeclared_tier_comes_from(self) -> None:
        tiers = fp.entry_tiers_from_setup(
            {"entry_tiers": [{"limit": lim} for lim in (100.0, 98.0, 96.0, 94.0)]}
        )
        self.assertEqual([t.tier_id for t in tiers][-1], "E4")
        self.assertNotIn("E4", fp.TIER_IDS)


class TestTheTwoOutcomeMeasuresCarryTheirUnits(unittest.TestCase):
    """The realised number is in R; the forgone one is a plain return fraction.

    ``realised_r`` is risk-normalised by the row's own stop distance at the
    overshoot fill. ``forgone_excess_return`` is the store's
    ``market_excess_return`` -- ``forward_return - benchmark_window_return``, a
    decimal fraction. Sitting side by side in one row without their units invites
    exactly the subtraction that cannot be done.
    """

    def test_each_reported_measure_names_its_unit(self) -> None:
        self.assertEqual(
            set(fp.MEASURE_UNITS),
            {"realised_r", "forgone_excess_return", "mae_r", "holding_days", "filled_fraction"},
        )
        self.assertIn("R", fp.MEASURE_UNITS["realised_r"])
        self.assertIn("fraction", fp.MEASURE_UNITS["forgone_excess_return"])

    def test_the_two_measures_do_not_share_a_unit(self) -> None:
        self.assertNotEqual(
            fp.MEASURE_UNITS["realised_r"], fp.MEASURE_UNITS["forgone_excess_return"]
        )

    def test_the_field_names_say_which_unit_they_are_in(self) -> None:
        report = _report([_opp("AAA", filled=("E1",), realised_r=1.0, forgone_excess_return=0.02)])
        first = _stats(report, fp.PARTITION_FIRST_ONLY)
        self.assertAlmostEqual(first.realised_r_mean, 1.0)
        self.assertAlmostEqual(first.forgone_excess_return_mean, 0.02)
        self.assertFalse(hasattr(first, "realised_return_mean"))
        self.assertFalse(hasattr(first, "forgone_return_mean"))

    def test_the_conditional_record_names_its_unit_too(self) -> None:
        report = _report(
            [_opp("AAA", filled=("E1", "E2"), fill_ts=(1_000, 2_000), realised_r=-0.5)]
        )
        rec = next(r for r in report.conditional_fills if r.given_tier == "E1")
        self.assertAlmostEqual(rec.then_realised_r_median, -0.5)
        self.assertFalse(hasattr(rec, "then_realised_return_median"))


class TestPartitionCellHonesty(unittest.TestCase):
    def test_the_deep_only_cell_carries_its_structural_zero_reason(self) -> None:
        report = _report([_opp("AAA", filled=("E1",))])
        deep = _stats(report, fp.PARTITION_DEEP_ONLY)
        self.assertEqual(deep.n, 0)
        self.assertTrue(deep.offline_unreachable)
        self.assertFalse(_stats(report, fp.PARTITION_MIXED).offline_unreachable)

    def test_every_partition_appears_in_the_report_even_when_empty(self) -> None:
        report = _report([_opp("AAA", filled=("E1",))])
        self.assertEqual(tuple(p.partition for p in report.partitions), fp.PARTITIONS)

    def test_the_report_stamps_the_fill_model_and_the_overshoot_arm_it_used(self) -> None:
        report = _report([_opp("AAA")])
        self.assertEqual(report.fill_model, fp.FILL_MODEL_TOUCH)
        self.assertEqual(report.overshoot_arm, fp.OVERSHOOT_ARM_MEASURED)
        self.assertAlmostEqual(report.overshoot_bps, MEASURED_BPS)

    def test_an_unknown_overshoot_arm_raises(self) -> None:
        with self.assertRaises(ValueError):
            fp.partition_report([], fill_model=fp.FILL_MODEL_TOUCH, overshoot_arm="hopeful")


class TestHoldingTimeAndAdverseExcursion(unittest.TestCase):
    def test_holding_days_and_mae_are_reported_per_partition(self) -> None:
        report = _report(
            [
                _opp("AAA", filled=("E1",), holding_days=4, mae_r=-0.5),
                _opp("BBB", filled=("E1",), holding_days=8, mae_r=-1.5),
                _opp("CCC", filled=("E1", "E2"), holding_days=20, mae_r=-0.2),
            ]
        )
        first = _stats(report, fp.PARTITION_FIRST_ONLY)
        self.assertAlmostEqual(first.holding_days_median, 6.0)
        self.assertAlmostEqual(first.mae_r_median, -1.0)
        self.assertAlmostEqual(_stats(report, fp.PARTITION_MIXED).holding_days_median, 20.0)

    def test_holding_days_is_none_for_an_unfilled_opportunity_and_never_zero(self) -> None:
        report = _report([_opp("AAA")])
        unfilled = _stats(report, fp.PARTITION_UNFILLED)
        self.assertIsNone(unfilled.holding_days_median)
        self.assertEqual(unfilled.n_missing_holding, 1)

    def test_a_missing_mae_is_counted_not_dropped_from_the_denominator(self) -> None:
        report = _report(
            [
                _opp("AAA", filled=("E1",), mae_r=-1.0),
                _opp("BBB", filled=("E1",), mae_r=None),
            ]
        )
        first = _stats(report, fp.PARTITION_FIRST_ONLY)
        self.assertEqual(first.n, 2)
        self.assertEqual(first.n_missing_mae, 1)
        self.assertAlmostEqual(first.mae_r_median, -1.0)


class TestMaeReAnchoredToTheOvershootFill(unittest.TestCase):
    """The store's MAE is anchored to the tier-limit blend, which is optimistic
    once the fill is known to land ABOVE the limit."""

    BLENDED = 100.0
    STOP_DISTANCE_PCT = 0.10
    MAE_PCT = -0.04

    def test_the_stored_mae_is_recovered_from_the_stored_columns(self) -> None:
        # Identity used to re-anchor without a re-replay: stop = blend*(1-sd) and
        # in-trade low = blend*(1+mae_pct), so mae_r = mae_pct / sd.
        self.assertAlmostEqual(
            fp.mae_r_at_fill(
                stop_distance_pct=self.STOP_DISTANCE_PCT, mae_pct=self.MAE_PCT, overshoot_bps=0.0
            ),
            self.MAE_PCT / self.STOP_DISTANCE_PCT,
            places=12,
        )

    def test_mae_is_re_anchored_to_the_overshoot_fill_not_the_tier_limit(self) -> None:
        stored = self.MAE_PCT / self.STOP_DISTANCE_PCT
        reanchored = fp.mae_r_at_fill(
            stop_distance_pct=self.STOP_DISTANCE_PCT,
            mae_pct=self.MAE_PCT,
            overshoot_bps=MEASURED_BPS,
        )
        ob = MEASURED_BPS / fp.BPS_PER_UNIT
        self.assertAlmostEqual(
            reanchored, (self.MAE_PCT - ob) / (ob + self.STOP_DISTANCE_PCT), places=12
        )
        self.assertLess(reanchored, stored)

    def test_an_excursion_all_the_way_to_the_stop_is_minus_one_r_under_both_anchors(self) -> None:
        for bps in (0.0, MEASURED_BPS):
            with self.subTest(overshoot_bps=bps):
                self.assertAlmostEqual(
                    fp.mae_r_at_fill(
                        stop_distance_pct=self.STOP_DISTANCE_PCT,
                        mae_pct=-self.STOP_DISTANCE_PCT,
                        overshoot_bps=bps,
                    ),
                    -1.0,
                    places=12,
                )

    def test_a_degenerate_stop_distance_yields_none_rather_than_dividing_by_zero(self) -> None:
        for sd in (0.0, -0.1, None):
            with self.subTest(stop_distance_pct=sd):
                self.assertIsNone(
                    fp.mae_r_at_fill(stop_distance_pct=sd, mae_pct=self.MAE_PCT, overshoot_bps=0.0)
                )

    def test_a_missing_mae_pct_yields_none(self) -> None:
        self.assertIsNone(
            fp.mae_r_at_fill(
                stop_distance_pct=self.STOP_DISTANCE_PCT, mae_pct=None, overshoot_bps=0.0
            )
        )


class TestConditionalFillRate(unittest.TestCase):
    def test_a_deeper_tier_filling_in_a_later_bar_is_recorded_as_later(self) -> None:
        report = _report([_opp("AAA", filled=("E1", "E2"), fill_ts=(1_000, 2_000))])
        rec = next(r for r in report.conditional_fills if r.given_tier == "E1")
        self.assertEqual(rec.then_tier, "E2")
        self.assertEqual(rec.n_given, 1)
        self.assertEqual(rec.n_then, 1)
        self.assertEqual(rec.n_then_later, 1)
        self.assertEqual(rec.n_then_same_bar, 0)

    def test_a_deeper_tier_filling_in_the_same_bar_is_recorded_as_same_bar(self) -> None:
        report = _report([_opp("AAA", filled=("E1", "E2"), fill_ts=(1_000, 1_000))])
        rec = next(r for r in report.conditional_fills if r.given_tier == "E1")
        self.assertEqual(rec.n_then_same_bar, 1)
        self.assertEqual(rec.n_then_later, 0)

    def test_the_conditional_denominator_is_the_tier_k_filled_set_not_the_population(self) -> None:
        report = _report(
            [
                _opp("AAA"),  # nothing filled: not in the E1 denominator
                _opp("BBB", filled=("E1",)),
                _opp("CCC", filled=("E1", "E2"), fill_ts=(1_000, 2_000)),
                _opp("DDD", excluded_reason=fp.EXCLUDE_NOT_DECIDED),
            ]
        )
        rec = next(r for r in report.conditional_fills if r.given_tier == "E1")
        self.assertEqual(rec.n_given, 2)
        self.assertEqual(rec.n_then, 1)
        self.assertAlmostEqual(rec.rate, 0.5)

    def test_an_undecided_opportunity_never_enters_the_conditional_denominator(self) -> None:
        # The immortal-time trap: a row whose entry TTL is still open can still
        # gain a deeper fill, so counting it now would understate the rate.
        report = _report([_opp("AAA", filled=("E1",), excluded_reason=fp.EXCLUDE_NOT_DECIDED)])
        rec = next(r for r in report.conditional_fills if r.given_tier == "E1")
        self.assertEqual(rec.n_given, 0)
        self.assertIsNone(rec.rate)

    def test_the_records_cover_every_consecutive_tier_pair(self) -> None:
        report = _report([_opp("AAA", filled=("E1",))])
        self.assertEqual(
            [(r.given_tier, r.then_tier) for r in report.conditional_fills],
            [("E1", "E2"), ("E2", "E3")],
        )

    def test_what_happened_next_is_reported_for_the_deeper_fill_cohort(self) -> None:
        report = _report(
            [
                _opp("AAA", filled=("E1",), realised_r=0.20),
                _opp("BBB", filled=("E1", "E2"), fill_ts=(1_000, 2_000), realised_r=-0.10),
                _opp("CCC", filled=("E1", "E2"), fill_ts=(1_000, 2_000), realised_r=-0.30),
            ]
        )
        rec = next(r for r in report.conditional_fills if r.given_tier == "E1")
        self.assertEqual(rec.n_then, 2)
        self.assertAlmostEqual(rec.then_realised_r_median, -0.20)
        self.assertEqual(rec.n_missing_then_realised, 0)

    def test_a_missing_next_outcome_is_counted_not_zeroed(self) -> None:
        report = _report(
            [
                _opp("AAA", filled=("E1", "E2"), fill_ts=(1_000, 2_000), realised_r=-0.10),
                _opp("BBB", filled=("E1", "E2"), fill_ts=(1_000, 2_000), realised_r=None),
            ]
        )
        rec = next(r for r in report.conditional_fills if r.given_tier == "E1")
        self.assertEqual(rec.n_then, 2)
        self.assertEqual(rec.n_missing_then_realised, 1)
        self.assertAlmostEqual(rec.then_realised_r_median, -0.10)


class TestWhyTheInstrumentReReplaysInsteadOfReadingTheColumn(unittest.TestCase):
    """``sequence_str`` is order-only, so it cannot answer "did it fill LATER"."""

    SETUP = {
        "status": "OK",
        "disaster_stop": 90.0,
        "entry_tiers": [
            {"limit": 100.0, "alloc_pct": 40.0},
            {"limit": 97.0, "alloc_pct": 60.0},
        ],
        "tp_tranches": [{"target": 130.0, "tranche_pct": 100.0}],
    }

    @staticmethod
    def _bar(ts: int, low: float) -> dict:
        return {"t": ts, "l": low, "h": low + 0.5, "c": low + 0.2, "o": low + 0.2}

    def test_sequence_str_alone_cannot_separate_same_bar_from_later(self) -> None:
        same_bar = replay_ladder(self.SETUP, [self._bar(1_000, 96.0)])
        later = replay_ladder(self.SETUP, [self._bar(1_000, 99.0), self._bar(2_000, 96.0)])
        self.assertEqual(same_bar.sequence_str(), later.sequence_str())
        self.assertEqual(same_bar.entries_filled, later.entries_filled)
        # The crossing timestamps DO separate them -- they just never reach the
        # parquet, which is why the driver re-replays from the cached minute bars.
        self.assertEqual({c.bar_ts_ms for c in same_bar.sequence}, {1_000})
        self.assertEqual([c.bar_ts_ms for c in later.sequence], [1_000, 2_000])


if __name__ == "__main__":
    unittest.main()
