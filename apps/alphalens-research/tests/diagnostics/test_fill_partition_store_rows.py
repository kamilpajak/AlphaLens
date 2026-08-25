"""Mapping a population-ladder store row onto an :class:`Opportunity`.

Pure: the row is a plain dict of the columns ``population_ladder_monitor`` writes,
so this pins the exclusion decision and the two re-anchored numbers without
touching a parquet.
"""

from __future__ import annotations

import unittest

from alphalens_pipeline.feedback.corporate_actions import SPLIT_INVALIDATED_CLASSIFICATION
from alphalens_research.diagnostics import fill_partition as fp

MEASURED_BPS = fp.ENTRY_TRAIL_OVERSHOOT_BPS
STOP_DISTANCE_PCT = 0.10


def _row(**over) -> dict:
    row = {
        "brief_date": "2026-08-01",
        "ticker": "AAA",
        "plannable": True,
        "terminal": True,
        "ladder_classification": "TP_FULL",
        "realized_r": 1.5,
        "mae_pct": -0.04,
        "stop_distance_pct": STOP_DISTANCE_PCT,
        "holding_days_elapsed": 6,
        "market_excess_return": 0.02,
    }
    row.update(over)
    return row


def _fill(tier: str, limit: float, ts: int) -> fp.TierFill:
    return fp.TierFill(
        tier_id=tier,
        limit=limit,
        alloc_pct=50.0,
        fill_price=fp.fill_price_from_limit(limit, MEASURED_BPS),
        bar_ts_ms=ts,
    )


class TestStoreRowExclusion(unittest.TestCase):
    def test_a_plannable_terminal_row_is_an_opportunity(self) -> None:
        self.assertIsNone(fp.store_row_exclusion(_row()))

    def test_a_non_plannable_row_is_excluded_with_its_own_reason(self) -> None:
        self.assertEqual(fp.store_row_exclusion(_row(plannable=False)), fp.EXCLUDE_NOT_PLANNABLE)

    def test_a_never_priced_row_is_excluded_as_no_replay(self) -> None:
        self.assertEqual(
            fp.store_row_exclusion(_row(ladder_classification=None, terminal=False)),
            fp.EXCLUDE_NO_REPLAY,
        )

    def test_a_split_invalidated_row_is_excluded_by_name(self) -> None:
        self.assertEqual(
            fp.store_row_exclusion(_row(ladder_classification=SPLIT_INVALIDATED_CLASSIFICATION)),
            fp.EXCLUDE_SPLIT_INVALIDATED,
        )

    def test_a_bad_geometry_row_is_excluded_by_name(self) -> None:
        self.assertEqual(
            fp.store_row_exclusion(_row(ladder_classification="BAD_GEOMETRY")),
            fp.EXCLUDE_BAD_GEOMETRY,
        )

    def test_an_ongoing_row_is_excluded_as_not_decided(self) -> None:
        # Immortal time: an OPEN position can still fill a deeper tier, so its
        # partition is not yet final.
        self.assertEqual(
            fp.store_row_exclusion(_row(terminal=False, ladder_classification="OPEN")),
            fp.EXCLUDE_NOT_DECIDED,
        )

    def test_a_terminal_no_fill_row_is_an_opportunity_not_an_exclusion(self) -> None:
        # The whole point of the denominator: a NO_FILL is a decided outcome.
        self.assertIsNone(
            fp.store_row_exclusion(_row(ladder_classification="NO_FILL", realized_r=None))
        )

    def test_every_reason_it_can_return_is_declared(self) -> None:
        rows = [
            _row(plannable=False),
            _row(ladder_classification=None, terminal=False),
            _row(ladder_classification=SPLIT_INVALIDATED_CLASSIFICATION),
            _row(ladder_classification="BAD_GEOMETRY"),
            _row(terminal=False, ladder_classification="OPEN"),
        ]
        reasons = {fp.store_row_exclusion(r) for r in rows}
        # Every reason a ROW's own columns can carry. The ladder-shape bucket is
        # decided by the report from the filled-tier set, not by these columns.
        self.assertEqual(reasons, set(fp.EXCLUSION_REASONS) - {fp.EXCLUDE_UNDECLARED_LADDER})
        self.assertIn(fp.EXCLUDE_UNDECLARED_LADDER, fp.EXCLUSION_REASONS)


class TestStoreFillTierIds(unittest.TestCase):
    """The store's own view of which tiers filled, for COMPARISON only.

    The instrument re-derives the fill set from bars because this column drops the
    crossing timestamps. Parsing it anyway lets the driver count how often the two
    disagree instead of quietly preferring one.
    """

    def test_the_entry_tokens_are_pulled_out_of_the_crossing_sequence(self) -> None:
        self.assertEqual(fp.store_fill_tier_ids(_row(sequence_str="E1->E2->TP1->SL")), ("E1", "E2"))

    def test_a_sequence_without_an_entry_yields_nothing(self) -> None:
        for value in (None, "", "TP1->SL", float("nan")):
            with self.subTest(sequence_str=value):
                self.assertEqual(fp.store_fill_tier_ids(_row(sequence_str=value)), ())

    def test_a_repeated_token_is_recorded_once_in_first_crossing_order(self) -> None:
        self.assertEqual(fp.store_fill_tier_ids(_row(sequence_str="E2->E1->E2")), ("E2", "E1"))

    def test_a_tier_outside_the_declared_ladder_is_returned_not_swallowed(self) -> None:
        # A four-tier ladder would write E4. Returning it means the mismatch is
        # visible; dropping it here would make a real disagreement look like
        # agreement.
        self.assertEqual(fp.store_fill_tier_ids(_row(sequence_str="E1->E4")), ("E1", "E4"))

    def test_the_time_stop_marker_is_not_read_as_an_entry(self) -> None:
        self.assertEqual(fp.store_fill_tier_ids(_row(sequence_str="E1->TIME_STOP")), ("E1",))


class TestRealizedRReAnchoredToTheOvershootFill(unittest.TestCase):
    def test_the_zero_bps_arm_returns_the_stored_number_unchanged(self) -> None:
        self.assertAlmostEqual(
            fp.realized_r_at_fill(
                realized_r=1.5, stop_distance_pct=STOP_DISTANCE_PCT, overshoot_bps=0.0
            ),
            1.5,
            places=12,
        )

    def test_a_winner_is_worth_less_once_the_fill_lands_above_the_limit(self) -> None:
        ob = MEASURED_BPS / fp.BPS_PER_UNIT
        expected = (1.5 * STOP_DISTANCE_PCT - ob) / (ob + STOP_DISTANCE_PCT)
        got = fp.realized_r_at_fill(
            realized_r=1.5, stop_distance_pct=STOP_DISTANCE_PCT, overshoot_bps=MEASURED_BPS
        )
        self.assertAlmostEqual(got, expected, places=12)
        self.assertLess(got, 1.5)

    def test_a_stop_out_is_still_minus_one_r_under_either_anchor(self) -> None:
        for bps in (0.0, MEASURED_BPS):
            with self.subTest(overshoot_bps=bps):
                self.assertAlmostEqual(
                    fp.realized_r_at_fill(
                        realized_r=-1.0, stop_distance_pct=STOP_DISTANCE_PCT, overshoot_bps=bps
                    ),
                    -1.0,
                    places=12,
                )

    def test_a_missing_or_degenerate_input_yields_none(self) -> None:
        cases = (
            {"realized_r": None, "stop_distance_pct": STOP_DISTANCE_PCT},
            {"realized_r": 1.0, "stop_distance_pct": None},
            {"realized_r": 1.0, "stop_distance_pct": 0.0},
            {"realized_r": 1.0, "stop_distance_pct": -0.2},
        )
        for case in cases:
            with self.subTest(**case):
                self.assertIsNone(fp.realized_r_at_fill(overshoot_bps=0.0, **case))


class TestOpportunityFromStoreRow(unittest.TestCase):
    def test_the_filled_tiers_and_timestamps_come_from_the_walk_not_the_column(self) -> None:
        opp = fp.opportunity_from_store_row(
            _row(),
            fills=(_fill("E1", 100.0, 1_000), _fill("E2", 97.0, 2_000)),
            tiers=(
                fp.EntryTier("E1", 100.0, 50.0),
                fp.EntryTier("E2", 97.0, 50.0),
            ),
            overshoot_bps=MEASURED_BPS,
        )
        self.assertEqual(opp.filled_tiers, ("E1", "E2"))
        self.assertEqual(opp.fill_bar_ts_ms, (1_000, 2_000))
        self.assertAlmostEqual(opp.filled_fraction, 1.0)

    def test_the_returns_are_re_anchored_to_the_fill(self) -> None:
        opp = fp.opportunity_from_store_row(
            _row(),
            fills=(_fill("E1", 100.0, 1_000),),
            tiers=(fp.EntryTier("E1", 100.0, 50.0), fp.EntryTier("E2", 97.0, 50.0)),
            overshoot_bps=MEASURED_BPS,
        )
        self.assertAlmostEqual(
            opp.realised_r,
            fp.realized_r_at_fill(
                realized_r=1.5, stop_distance_pct=STOP_DISTANCE_PCT, overshoot_bps=MEASURED_BPS
            ),
        )
        self.assertAlmostEqual(
            opp.mae_r,
            fp.mae_r_at_fill(
                stop_distance_pct=STOP_DISTANCE_PCT, mae_pct=-0.04, overshoot_bps=MEASURED_BPS
            ),
        )
        self.assertAlmostEqual(opp.filled_fraction, 0.5)

    def test_the_opportunity_cost_is_the_market_excess_move(self) -> None:
        opp = fp.opportunity_from_store_row(
            _row(market_excess_return=0.031),
            fills=(),
            tiers=(fp.EntryTier("E1", 100.0, 100.0),),
            overshoot_bps=MEASURED_BPS,
        )
        self.assertAlmostEqual(opp.forgone_excess_return, 0.031)

    def test_an_unfilled_row_carries_no_realised_return_and_no_holding_time(self) -> None:
        opp = fp.opportunity_from_store_row(
            _row(ladder_classification="NO_FILL", realized_r=None, holding_days_elapsed=None),
            fills=(),
            tiers=(fp.EntryTier("E1", 100.0, 100.0),),
            overshoot_bps=MEASURED_BPS,
        )
        self.assertEqual(opp.filled_tiers, ())
        self.assertIsNone(opp.realised_r)
        self.assertIsNone(opp.holding_days)
        self.assertEqual(opp.filled_fraction, 0.0)

    def test_the_mae_re_anchor_is_refused_on_a_non_terminal_row(self) -> None:
        # The stop-recovery identity holds on a settled row and breaks on an
        # ongoing one, so the instrument reports nothing rather than a wrong number.
        opp = fp.opportunity_from_store_row(
            _row(terminal=False, ladder_classification="OPEN"),
            fills=(_fill("E1", 100.0, 1_000),),
            tiers=(fp.EntryTier("E1", 100.0, 100.0),),
            overshoot_bps=MEASURED_BPS,
        )
        self.assertIsNone(opp.mae_r)
        self.assertIsNone(opp.realised_r)
        self.assertEqual(opp.excluded_reason, fp.EXCLUDE_NOT_DECIDED)

    def test_the_exclusion_reason_travels_with_the_opportunity(self) -> None:
        opp = fp.opportunity_from_store_row(
            _row(plannable=False),
            fills=(),
            tiers=(),
            overshoot_bps=MEASURED_BPS,
        )
        self.assertEqual(opp.excluded_reason, fp.EXCLUDE_NOT_PLANNABLE)

    def test_a_nan_column_is_read_as_missing_not_as_a_number(self) -> None:
        # Parquet nulls arrive as float NaN through pandas; a NaN that survived
        # into a mean would silently poison the whole partition.
        opp = fp.opportunity_from_store_row(
            _row(realized_r=float("nan"), mae_pct=float("nan"), market_excess_return=float("nan")),
            fills=(_fill("E1", 100.0, 1_000),),
            tiers=(fp.EntryTier("E1", 100.0, 100.0),),
            overshoot_bps=MEASURED_BPS,
        )
        self.assertIsNone(opp.realised_r)
        self.assertIsNone(opp.mae_r)
        self.assertIsNone(opp.forgone_excess_return)


if __name__ == "__main__":
    unittest.main()
