"""The RESULT on an exit page: R on a stop fill, the target on a submitted exit.

#1621 part 2's tail. On 2026-11-02 two LIVE positions closed on the trail and
the pages the operator received carried no R; both numbers had to be dug out
afterwards with ``alphalens broker trades``.

The hazard this suite exists for is NOT a missing number, it is a SECOND one.
``broker trades`` already publishes an ``r_multiple`` per pick, from the
realized entry blend over ``spec.disaster_stop``. An alert that divided by the
stop RESTING at the fill would publish a different number under the same name:
on the VST round trip below the resting level (138.82) is ABOVE the realized
entry (135.11), so that denominator is negative and the R changes SIGN. So
:class:`TheRAgreesWithBrokerTrades` runs the real ``broker trades`` builder and
the real alert path over ONE set of journal lines and compares both against an
expectation written out by hand from the four raw numbers.

The refusals are the other half: ``broker trades`` publishes a NULL r_multiple
on a partial fill and on any pick that already fired a take-profit. A page that
printed a number there would contradict a null, which is worse than the
disagreement above. :class:`TheRIsRefusedWhereBrokerTradesPublishesNull` pins
each refusal against the command's own null.

Every rendered shape is asserted with the actual WORDS, not against a constant
read back out of the module under test.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import mock

from alphalens_pipeline.brokers.automanager import control_loop as cl
from alphalens_pipeline.brokers.automanager import (
    entry_trails,
    live_exit_engine,
    trade_alerts,
)
from alphalens_pipeline.brokers.automanager import stop_journal as sj
from alphalens_pipeline.brokers.automanager.trades import TradesFilters, build_trades
from broker_contract.contract import OrderState, OrderStatus

from tests.brokers.automanager.home_isolation import IsolatedHomeTestCase
from tests.brokers.automanager.trades_fixture import (
    NOW,
    SIM_RHI_DIR,
    FakeFillHistory,
    install_journals,
    trade,
    value,
)

# --- The VST:2026-09-21 round trip, as the LIVE journals recorded it ---------
# Read off ``fixtures/trades/live_2026_10_03/``: the entry-trails ``watch_open``
# (disaster_stop) and ``fired`` (avg_price, realized_qty) lines, and the
# standalone-stops ``stop_filled`` line. These four numbers are the whole input
# to the R, and the expectation below is derived from them by hand.
VST = "VST:2026-09-21"
VST_PLAN_STOP = 125.85
VST_AVG_ENTRY = 135.11
VST_QTY = 6.0
VST_EXIT_PRICE = 138.66
VST_STOP_ID = "5448023092"
VST_STOP_REF = "VST-2026-09-21-entry-t0-fire-stop-0"
# The level the stop was RESTING at when it filled, from the trail. Not a
# denominator — kept here because the trap is that it looks like one.
VST_RESTING_STOP = 138.82

# By hand: risk per share 135.11 - 125.85 = 9.26; P&L 6 * (138.66 - 135.11) =
# 21.30; R = 21.30 / (6 * 9.26) = 21.30 / 55.56.
VST_EXPECTED_R = 21.30 / 55.56

_UIC = 7300542


def _tier_index_from_crid(crid: str) -> int:
    """The tier index production stamps on the ``watch_open`` line, read back off
    the crid the same line carries (``...-entry-t1`` -> 1). Kept in the fixture
    because ``realized_entry`` refuses a ladder with a GAP in its indices, so a
    fixture that omitted them would make every multi-tier pick look like a gap."""
    _, _, tail = crid.rpartition("-t")
    return int(tail) if tail.isdigit() else 0


def _entry_lines(
    *,
    pick_key: str = VST,
    crid: str = "VST-2026-09-21-entry-t0",
    disaster_stop: float = VST_PLAN_STOP,
    avg_price: float | None = VST_AVG_ENTRY,
    realized_qty: float = VST_QTY,
    tier_index: int | None = None,
) -> list[dict[str, Any]]:
    """One entry tier's journal pair: the arm-time ``watch_open`` that stamped
    the plan stop and the tier index, and the ``fired`` terminal that stamped the
    realized fill."""
    opened: dict[str, Any] = {
        "kind": "watch_open",
        "crid": crid,
        "pick_key": pick_key,
        "ticker": pick_key.split(":", maxsplit=1)[0],
        "disaster_stop": disaster_stop,
        "uic": _UIC,
        "qty": realized_qty,
        "tier_index": _tier_index_from_crid(crid) if tier_index is None else tier_index,
    }
    fired: dict[str, Any] = {"kind": "fired", "crid": crid, "realized_qty": realized_qty}
    if avg_price is not None:
        fired["avg_price"] = avg_price
    return [opened, fired]


class _Journals:
    """A temporary standalone-stops + entry-trails pair, both behind the named
    path seams the production readers go through."""

    def __init__(self, test: unittest.TestCase) -> None:
        tmp = TemporaryDirectory()
        test.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.stops = root / "standalone_stops.jsonl"
        self.entries = root / "entry_trails.jsonl"
        for module, attr, path in (
            (sj, "_standalone_stop_journal_path", self.stops),
            (entry_trails, "_entry_trail_journal_path", self.entries),
        ):
            patcher = mock.patch.object(module, attr, lambda path=path: path)
            patcher.start()
            test.addCleanup(patcher.stop)

    def seed(self, path: Path, *lines: Any) -> None:
        with path.open("a", encoding="utf-8") as fh:
            for line in lines:
                fh.write(json.dumps(line) + "\n")


class _StopBroker:
    """The Saxo shape at a filled stop: the id is gone from the open-orders
    book, and the audit read answers with the terminal outcome."""

    def __init__(self, outcome: OrderState) -> None:
        self._outcome = outcome

    def get_order(self, order_id: str) -> OrderState:
        return OrderState(
            order_id=order_id,
            status=OrderStatus.UNKNOWN,
            instrument=None,
            filled_quantity=0.0,
            raw_status="Unknown",
        )

    def resolve_order_outcome(self, order_id: str) -> OrderState:
        return self._outcome


def _filled(order_id: str, qty: float, price: float, *, partial: bool = False) -> OrderState:
    return OrderState(
        order_id=order_id,
        status=OrderStatus.PARTIALLY_FILLED if partial else OrderStatus.FILLED,
        instrument=None,
        filled_quantity=qty,
        raw_status="PartiallyFilled" if partial else "Filled",
        avg_fill_price=price,
    )


def _deps(broker: Any, pages: list[str]) -> cl.LoopDeps:
    def _throttled(message: str, reason: str) -> bool:
        pages.append(message)
        return True

    return cl.LoopDeps(
        broker=broker,
        kill_file=Path("/nonexistent/KILL"),
        ensure_alive=lambda: type("C", (), {"alive": True, "reason": None})(),  # noqa: PLW0108
        iter_picks=lambda: iter([]),
        place_pick=lambda pick: True,
        read_records=list,
        verdicts_fn=lambda records, broker: [],
        build_position_view=lambda broker, records: None,
        build_protection_view=lambda broker, records: None,
        execute_protection=lambda action, kill, report: None,
        sweep_orphans_fn=lambda broker: [],
        alert=lambda msg: pages.append(msg),  # noqa: PLW0108
        alert_throttled=_throttled,
    )


class _ExitPageCase(IsolatedHomeTestCase):
    """Drives the REAL stop-fill reconcile pass over seeded journal lines and
    returns the one page it sent."""

    def setUp(self) -> None:
        super().setUp()
        self.journals = _Journals(self)

    def page_for(
        self,
        *stop_lines: dict[str, Any],
        entries: list[dict[str, Any]] | None = None,
        outcome: OrderState,
    ) -> str:
        self.journals.seed(self.journals.stops, *stop_lines)
        if entries:
            self.journals.seed(self.journals.entries, *entries)
        pages: list[str] = []
        cl._run_stop_fill_reconcile_pass(_deps(_StopBroker(outcome), pages), cl.TickReport())
        self.assertEqual(len(pages), 1, pages)
        return pages[0]


def _stop_placed(**over: Any) -> dict[str, Any]:
    line: dict[str, Any] = {
        "kind": "stop_placed",
        "uic": _UIC,
        "order_id": VST_STOP_ID,
        "ref": VST_STOP_REF,
        "qty": VST_QTY,
        "stop_price": VST_PLAN_STOP,
        "ts": 100.0,
    }
    line.update(over)
    return line


def _fold_of(lines: list[dict[str, Any]]) -> entry_trails.EntryTrailFold:
    """Fold entry-trail lines through the REAL reader, behind its path seam."""
    tmp = TemporaryDirectory()
    path = Path(tmp.name) / "entry_trails.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for line in lines:
            fh.write(json.dumps(line) + "\n")
    try:
        return entry_trails.read_entry_trail_fold(path=path)
    finally:
        tmp.cleanup()


def _trailed_to(level: float, *, ts: float = 200.0) -> dict[str, Any]:
    return {"kind": "trailed", "uic": _UIC, "level": level, "peak": level, "ts": ts}


class TheRAgreesWithBrokerTrades(IsolatedHomeTestCase):
    """One set of journal lines, two surfaces, one number.

    The expectation is written out by hand from the four raw journal numbers
    (``VST_EXPECTED_R``), so neither surface is being compared against itself.
    """

    env = "sim"

    def setUp(self) -> None:
        super().setUp()
        install_journals(self.home, env=self.env)

    def _command_r(self) -> float:
        report = build_trades(
            self.env,
            broker=FakeFillHistory(),
            filters=TradesFilters(pick=VST, limit=None),
            now=NOW,
        )
        return float(value(trade(report, VST)["outcome"]["r_multiple"]))

    def _alert_r(self) -> float | None:
        lines = list(sj._iter_standalone_stop_journal())
        stop = cl._StandingStop(
            order_id=VST_STOP_ID, ref=VST_STOP_REF, placed_ts=0.0, placed_level=VST_PLAN_STOP
        )
        return cl._exit_result_r(
            _UIC,
            stop,
            lines,
            sj._fold_governing_plan_pick_keys(lines),
            VST_QTY,
            VST_EXIT_PRICE,
            partial=False,
        )

    def test_the_hand_derived_r_is_what_broker_trades_publishes(self) -> None:
        self.assertAlmostEqual(self._command_r(), VST_EXPECTED_R, places=9)

    def test_the_alert_publishes_the_same_number(self) -> None:
        alert_r = self._alert_r()
        self.assertIsNotNone(alert_r)
        self.assertAlmostEqual(float(alert_r), VST_EXPECTED_R, places=9)
        self.assertAlmostEqual(float(alert_r), self._command_r(), places=9)

    def test_the_resting_stop_at_fill_would_have_changed_the_sign(self) -> None:
        # Why the denominator had to be chosen rather than defaulted. The stop
        # the exit actually filled against had trailed ABOVE the entry, so a
        # resting-stop denominator is negative: the page would have called this
        # winning trade a loss.
        self.assertGreater(VST_RESTING_STOP, VST_AVG_ENTRY)
        self.assertIsNone(
            trade_alerts.result_r(
                avg_entry=VST_AVG_ENTRY,
                plan_stop=VST_RESTING_STOP,
                exit_qty=VST_QTY,
                exit_price=VST_EXIT_PRICE,
            ),
            "a non-positive risk must refuse, exactly as broker trades does",
        )


class TheExitFilledPageCarriesTheR(_ExitPageCase):
    def test_the_trailed_vst_round_trip_reads_in_r(self) -> None:
        text = self.page_for(
            _stop_placed(),
            _trailed_to(VST_RESTING_STOP),
            entries=_entry_lines(),
            outcome=_filled(VST_STOP_ID, VST_QTY, VST_EXIT_PRICE),
        )
        self.assertEqual(
            text,
            "SELL VST SL 6 @ 138.66 - trailed stop 138.82 - +0.3834R, position closed "
            "(order 5448023092)",
        )

    def test_a_disaster_stop_exit_reads_as_minus_one_r(self) -> None:
        # The definition of the disaster stop: exiting at it loses exactly the
        # risk that sized the position, so the page must read -1.0000R. A page
        # that printed anything else here would mean the sizing and the result
        # disagree.
        text = self.page_for(
            _stop_placed(stop_price=17.0),
            entries=_entry_lines(disaster_stop=17.0, avg_price=18.0, realized_qty=16.0),
            outcome=_filled(VST_STOP_ID, 16.0, 17.0),
        )
        self.assertEqual(
            text,
            "SELL VST SL 16 @ 17.00 - plan stop 17.00 - -1.0000R, position closed "
            "(order 5448023092)",
        )

    def test_the_r_blends_every_filled_entry_tier(self) -> None:
        # Two tiers, 10 @ 20.00 and 10 @ 22.00: the blend is 21.00, so the risk
        # is 21.00 - 19.00 = 2.00 and an exit at 25.00 made 4.00 / 2.00 = 2R.
        # Reading only the first tier would publish 2.5R.
        text = self.page_for(
            _stop_placed(stop_price=19.0),
            entries=[
                *_entry_lines(
                    crid="VST-2026-09-21-entry-t0",
                    disaster_stop=19.0,
                    avg_price=20.0,
                    realized_qty=10.0,
                ),
                *_entry_lines(
                    crid="VST-2026-09-21-entry-t1",
                    disaster_stop=19.0,
                    avg_price=22.0,
                    realized_qty=10.0,
                ),
            ],
            outcome=_filled(VST_STOP_ID, 20.0, 25.0),
        )
        self.assertIn("- +2.0000R, position closed ", text)

    def test_the_denominator_is_the_entry_stamped_plan_stop_not_the_placed_level(self) -> None:
        # The ONE mutation that matters, as a test. Everywhere else in this
        # suite the stop was PLACED at the plan level, so the two candidate
        # denominators coincide and swapping them changes nothing -- measured:
        # replacing ``entry.plan_stop`` with ``stop.placed_level`` left every
        # other test in this file green.
        #
        # They come apart on the shape the journal actually produces: a pick
        # that places CLIENT geometry writes the document's own stop into the
        # stop record, not ``spec.disaster_stop``. Here the stop rests at
        # 130.00 while the entry stamped 125.85, so the plan stop gives
        # 3.55 / 9.26 = +0.3834R and the placed level would give
        # 3.55 / 5.11 = +0.6947R -- a page overstating the result by 80%.
        text = self.page_for(
            _stop_placed(stop_price=130.0),
            _trailed_to(VST_RESTING_STOP),
            entries=_entry_lines(),
            outcome=_filled(VST_STOP_ID, VST_QTY, VST_EXIT_PRICE),
        )
        self.assertIn("- +0.3834R, position closed ", text)
        self.assertNotIn("0.6947R", text)

    def test_a_tier_that_never_filled_is_left_out_of_the_blend(self) -> None:
        # E1 filled 10 @ 20.00; E2 was cancelled before it ever armed, so it
        # contributes no shares and no price. The pick is priced on E1 alone:
        # risk 20.00 - 18.00 = 2.00, exit at 24.00 makes 4.00 / 2.00 = +2R.
        # Folding the unfilled tier in instead would refuse the R outright,
        # and the operator would get a page with no result on a clean exit.
        opened, fired = _entry_lines(
            crid="VST-2026-09-21-entry-t0", disaster_stop=18.0, avg_price=20.0, realized_qty=10.0
        )
        unfilled, _ = _entry_lines(
            crid="VST-2026-09-21-entry-t1", disaster_stop=18.0, realized_qty=10.0
        )
        text = self.page_for(
            _stop_placed(stop_price=18.0),
            entries=[opened, fired, unfilled, {"kind": "cancelled", "crid": unfilled["crid"]}],
            outcome=_filled(VST_STOP_ID, 10.0, 24.0),
        )
        self.assertEqual(
            text,
            "SELL VST SL 10 @ 24.00 - plan stop 18.00 - +2.0000R, position closed "
            "(order 5448023092)",
        )


class TheRIsRefusedWhereBrokerTradesPublishesNull(_ExitPageCase):
    """Each refusal is paired with the null ``broker trades`` writes for the
    same shape, so the two surfaces stay silent together."""

    def test_a_partial_fill_covering_the_whole_entry_carries_no_r(self) -> None:
        # The ``partial`` refusal on its own. The test below fills 2 of 6, so the
        # per-pick quantity guard answers first and the suite stayed green when
        # ``partial`` was dropped from the guard. Here the stop reports a PARTIAL
        # status while its filled quantity equals the pick's whole entry, so the
        # quantity guard passes and only ``partial`` can refuse. broker trades
        # nulls it as ``not_closed`` either way.
        text = self.page_for(
            _stop_placed(),
            _trailed_to(VST_RESTING_STOP),
            entries=_entry_lines(),
            outcome=_filled(VST_STOP_ID, VST_QTY, VST_EXIT_PRICE, partial=True),
        )
        self.assertNotIn("R,", text)

    def test_a_partial_fill_carries_no_r(self) -> None:
        # broker trades: the pick is not ``closed``, so its whole exit family
        # (r_multiple included) is null with reason ``not_closed``.
        text = self.page_for(
            _stop_placed(),
            _trailed_to(VST_RESTING_STOP),
            entries=_entry_lines(),
            outcome=_filled(VST_STOP_ID, 2.0, VST_EXIT_PRICE, partial=True),
        )
        self.assertEqual(
            text,
            "SELL VST SL 2 @ 138.66 - trailed stop 138.82 - position still open (order 5448023092)",
        )
        self.assertNotIn("R", text.replace("SELL VST SL", ""))

    def test_a_pick_that_already_took_a_take_profit_carries_no_r(self) -> None:
        # broker trades: the market sell's fill price is never journaled, so the
        # exit family nulls for the WHOLE pick -- this stop fill included.
        text = self.page_for(
            {"kind": "tranche_plan", "uic": _UIC, "pick_key": VST, "ts": 50.0},
            _stop_placed(),
            {"kind": "tranche_fired", "uic": _UIC, "tag": "tp1"},
            entries=_entry_lines(),
            outcome=_filled(VST_STOP_ID, VST_QTY, VST_EXIT_PRICE),
        )
        self.assertEqual(
            text,
            "SELL VST SL 6 @ 138.66 - plan stop 125.85 - position closed (order 5448023092)",
        )

    def test_an_entry_that_never_went_through_the_watch_carries_no_r(self) -> None:
        # A bracket pick places client geometry and writes no entry-trail
        # watch, so there is no realized blend to divide by. Nothing is guessed.
        text = self.page_for(
            _stop_placed(),
            entries=None,
            outcome=_filled(VST_STOP_ID, VST_QTY, VST_EXIT_PRICE),
        )
        self.assertEqual(
            text,
            "SELL VST SL 6 @ 138.66 - plan stop 125.85 - position closed (order 5448023092)",
        )

    def test_a_uic_re_planned_onto_another_pick_carries_no_r(self) -> None:
        # The generation reset, the other way round. A newer ``tranche_plan``
        # naming a DIFFERENT pick clears ``_fold_fired_since_latest_plan``'s
        # accumulator, so that fold alone would report no fired tranche for
        # this uic -- while the earlier pick's ``tranche_fired`` line is still
        # in the journal and still nulls ``broker trades``' number. The
        # governing pick key is what catches it.
        text = self.page_for(
            {"kind": "tranche_plan", "uic": _UIC, "pick_key": VST, "ts": 40.0},
            {"kind": "tranche_fired", "uic": _UIC, "tag": "tp1"},
            _stop_placed(),
            {"kind": "tranche_plan", "uic": _UIC, "pick_key": "AVGO:2026-10-01", "ts": 60.0},
            entries=_entry_lines(),
            outcome=_filled(VST_STOP_ID, VST_QTY, VST_EXIT_PRICE),
        )
        self.assertNotIn("R,", text)

    def test_a_keyless_plan_line_after_the_take_profit_still_refuses(self) -> None:
        # The take-profit refusal must survive a generation RESET. A keyless
        # ``tranche_plan`` (the bracket path) appended after the tranche fired
        # clears the executor's fired accumulator AND drops the governing key, so
        # both of the other two guards go quiet -- while ``broker trades`` still
        # lists the take-profit among this pick's exits and nulls the whole
        # family. Measured before the per-pick attribution: the page said
        # ``+0.3834R`` here.
        text = self.page_for(
            {"kind": "tranche_plan", "uic": _UIC, "pick_key": VST, "ts": 40.0},
            {"kind": "tranche_fired", "uic": _UIC, "tag": "tp1"},
            _stop_placed(),
            {"kind": "tranche_plan", "uic": _UIC, "pick_key": None, "ts": 60.0},
            entries=_entry_lines(),
            outcome=_filled(VST_STOP_ID, VST_QTY, VST_EXIT_PRICE),
        )
        self.assertNotIn("R,", text)

    def test_a_retracted_plan_after_the_take_profit_still_refuses(self) -> None:
        # The residual the first version of this code admitted in a docstring and
        # could not close. A retraction cannot un-sell the shares, so what fired
        # under the plan stays attributed to the pick.
        text = self.page_for(
            {"kind": "tranche_plan", "uic": _UIC, "pick_key": VST, "ts": 40.0},
            {"kind": "tranche_fired", "uic": _UIC, "tag": "tp1"},
            _stop_placed(),
            {"kind": "tranche_plan_retracted", "uic": _UIC, "pick_key": VST, "ts": 60.0},
            entries=_entry_lines(),
            outcome=_filled(VST_STOP_ID, VST_QTY, VST_EXIT_PRICE),
        )
        self.assertNotIn("R,", text)

    def test_a_later_picks_take_profit_on_the_same_uic_does_not_silence_this_one(self) -> None:
        # The attribution must not over-refuse either. A uic is one instrument and
        # the journal is append-only, so a LATER pick's fired tranche must not
        # taint this pick's page -- the two are separate keys.
        text = self.page_for(
            {"kind": "tranche_plan", "uic": _UIC, "pick_key": "AVGO:2026-08-01", "ts": 10.0},
            {"kind": "tranche_fired", "uic": _UIC, "tag": "tp1"},
            {"kind": "tranche_plan", "uic": _UIC, "pick_key": VST, "ts": 40.0},
            _stop_placed(),
            entries=_entry_lines(),
            outcome=_filled(VST_STOP_ID, VST_QTY, VST_EXIT_PRICE),
        )
        self.assertIn("R,", text)

    def test_a_nan_fill_price_carries_no_r(self) -> None:
        # broker trades nulls this as ``non_finite``. Before the finiteness check
        # the page rendered ``+nanR``: every comparison against NaN is False, so
        # NaN walked straight through the ``risk <= 0`` guard.
        text = self.page_for(
            _stop_placed(),
            entries=_entry_lines(),
            outcome=_filled(VST_STOP_ID, VST_QTY, float("nan")),
        )
        self.assertNotIn("R,", text)
        self.assertNotIn("nan", text.lower())

    def test_an_infinite_fill_price_carries_no_r(self) -> None:
        # The same null, the other non-finite shape: the page rendered ``+infR``.
        text = self.page_for(
            _stop_placed(),
            entries=_entry_lines(),
            outcome=_filled(VST_STOP_ID, VST_QTY, float("inf")),
        )
        self.assertNotIn("R,", text)
        self.assertNotIn("inf", text.lower())

    def test_an_absent_journal_snapshot_carries_no_r(self) -> None:
        # "Cannot show that no take-profit fired" is not "none fired". With no
        # lines there is nothing to fold, so the function must refuse rather
        # than read an empty fold as a clean round trip.
        stop = cl._StandingStop(
            order_id=VST_STOP_ID, ref=VST_STOP_REF, placed_ts=0.0, placed_level=VST_PLAN_STOP
        )
        self.journals.seed(self.journals.entries, *_entry_lines())
        self.assertIsNone(
            cl._exit_result_r(_UIC, stop, [], {}, VST_QTY, VST_EXIT_PRICE, partial=False)
        )

    def test_a_fired_tier_missing_its_fill_price_carries_no_r(self) -> None:
        text = self.page_for(
            _stop_placed(),
            entries=_entry_lines(avg_price=None),
            outcome=_filled(VST_STOP_ID, VST_QTY, VST_EXIT_PRICE),
        )
        self.assertNotIn("R,", text)

    def test_a_filled_tier_whose_watch_open_stamps_no_plan_stop_carries_no_r(self) -> None:
        # E1 stamped 18.00 and filled 10 @ 20.00; E2 filled 10 @ 30.00 but its
        # ``watch_open`` carries no ``disaster_stop`` (a pre-stamp legacy line).
        # Skipping E2 would price the pick on E1 alone and publish +1.0000R for
        # a round trip that actually blended to 25.00 and made +3.5R. Dropping
        # shares out of a blend is the silent half of a wrong denominator, so
        # the whole pick refuses.
        opened, fired = _entry_lines(
            crid="VST-2026-09-21-entry-t0", disaster_stop=18.0, avg_price=20.0, realized_qty=10.0
        )
        legacy_opened, legacy_fired = _entry_lines(
            crid="VST-2026-09-21-entry-t1", avg_price=30.0, realized_qty=10.0
        )
        del legacy_opened["disaster_stop"]
        text = self.page_for(
            _stop_placed(stop_price=18.0),
            entries=[opened, fired, legacy_opened, legacy_fired],
            outcome=_filled(VST_STOP_ID, 20.0, 28.0),
        )
        self.assertNotIn("R,", text)
        self.assertNotIn("+1.0000R", text)

    def test_two_tiers_stamping_different_plan_stops_carry_no_r(self) -> None:
        # One pick cannot have two disaster stops. A journal that says it does
        # is one this code does not understand, so it refuses rather than
        # picking a winner.
        text = self.page_for(
            _stop_placed(),
            entries=[
                *_entry_lines(crid="VST-2026-09-21-entry-t0", disaster_stop=125.85),
                *_entry_lines(crid="VST-2026-09-21-entry-t1", disaster_stop=120.00),
            ],
            outcome=_filled(VST_STOP_ID, VST_QTY, VST_EXIT_PRICE),
        )
        self.assertNotIn("R,", text)

    def test_a_full_stop_fill_covering_less_than_the_entry_carries_no_r(self) -> None:
        # The per-ORDER ``partial`` flag is not the per-PICK question. With amend
        # off the grow arm places an ADDITIVE second stop and KEEPS the first, so
        # each stop fills FULLY as its own order while the pick stays open --
        # ``trades._state`` reads ``abs(entry_qty - exit_qty) <= _QTY_EPS`` over
        # the WHOLE pick and publishes ``not_closed``. Two tiers fill 6 @ 135.11
        # and 4 @ 141.11 (blend 137.51); a 4-share stop fills at 138.60. Those 4
        # shares made -0.1645R, while the blend over the plan stop prints
        # +0.0935R: a losing exit paged as a winner.
        text = self.page_for(
            _stop_placed(qty=4.0),
            entries=[
                *_entry_lines(crid="VST-2026-09-21-entry-t0", avg_price=135.11, realized_qty=6.0),
                *_entry_lines(crid="VST-2026-09-21-entry-t1", avg_price=141.11, realized_qty=4.0),
            ],
            outcome=_filled(VST_STOP_ID, 4.0, 138.60),
        )
        self.assertNotIn("R,", text)
        self.assertNotIn("0.0935", text)


class TheBlendRefusesAPickItCannotSeeWholly(IsolatedHomeTestCase):
    """A ladder with a tier MISSING from the entry-trail journal is priced on a
    SUBSET of the pick's shares, so it is refused.

    RHI:2026-09-03 is that shape and it is a real journal in this repository:
    tier 0 went out as a client-geometry bracket (701 shares @ 43.75, recorded
    only in ``submissions.jsonl``) and tier 1 as an entry-trail watch (1095 @
    42.34). The per-pick quantity guard cannot catch it, because one trail stop
    for 1095 matches the trail blend's own quantity exactly.
    """

    def test_the_mixed_rhi_pick_is_not_priced_on_its_trail_tier_alone(self) -> None:
        with mock.patch.object(
            entry_trails,
            "_entry_trail_journal_path",
            lambda: SIM_RHI_DIR / "entry_trails.jsonl",
        ):
            fold = entry_trails.read_entry_trail_fold()
        self.assertIsNone(entry_trails.realized_entry(fold, "RHI:2026-09-03"))

    def test_the_trail_blend_would_have_overstated_the_result(self) -> None:
        # Why the refusal is worth a guard rather than a comment. Computed here
        # from the journal's own numbers, independently of the code under test.
        trail_blend, trail_qty, plan_stop = 42.34, 1095.0, 40.5
        pick_blend = (1095 * 42.34 + 701 * 43.75) / (1095 + 701)
        exit_price = 45.00
        stated = (exit_price - trail_blend) / (trail_blend - plan_stop)
        actual = (exit_price - pick_blend) / (pick_blend - plan_stop)
        self.assertAlmostEqual(stated, 1.4457, places=4)
        self.assertAlmostEqual(actual, 0.8826, places=4)
        # And the guard that was supposed to stop it does not fire.
        self.assertLessEqual(abs(trail_qty - 1095.0), 0.5)

    def test_a_ladder_whose_last_tier_was_never_armed_still_prices(self) -> None:
        # The refusal is about a GAP, not about a short ladder. Trailing tiers
        # that were never armed leave the indices running from zero, and
        # ``broker trades`` blends the same tiers this does.
        fold = _fold_of(_entry_lines(crid="VST-2026-09-21-entry-t0"))
        entry = entry_trails.realized_entry(fold, VST)
        self.assertIsNotNone(entry)
        assert entry is not None
        self.assertEqual(entry.qty, VST_QTY)

    def test_two_tiers_that_both_stamp_index_zero_are_refused(self) -> None:
        # An index this code cannot read is an index it cannot prove is not a
        # gap, so the collision refuses rather than blending.
        fold = _fold_of(
            [
                *_entry_lines(crid="VST-2026-09-21-entry-t0", tier_index=0),
                *_entry_lines(crid="VST-2026-09-21-entry-t1", tier_index=0),
            ]
        )
        self.assertIsNone(entry_trails.realized_entry(fold, VST))


class TheBlendRefusesAJournalItCannotRead(IsolatedHomeTestCase):
    """The three refusals inside :func:`entry_trails.realized_entry` that the
    page-level tests reached through a different guard.

    Driven against the function itself rather than through a page, because the
    page refuses for whichever reason comes first and so cannot say WHICH guard
    answered."""

    def test_a_watch_open_with_no_plan_stop_refuses(self) -> None:
        # TWO tiers, and only the second lacks the stop. A single-tier corpus is
        # blind here: skipping the one tier empties ``stops`` and the
        # ``len(stops) != 1`` arm refuses instead, so the suite stayed green when
        # this refusal was turned into a ``continue``. With a sibling tier that
        # DOES stamp a stop, skipping blends the pick on that tier alone.
        lines = [
            *_entry_lines(crid="VST-2026-09-21-entry-t0"),
            *_entry_lines(crid="VST-2026-09-21-entry-t1", realized_qty=4.0),
        ]
        del lines[2]["disaster_stop"]
        self.assertIsNone(entry_trails.realized_entry(_fold_of(lines), VST))

    def test_a_fired_tier_with_no_fill_price_refuses(self) -> None:
        # Two tiers for the same reason: with one tier, skipping it leaves
        # ``pairs`` empty and the ``not pairs`` arm answers. With a sibling, the
        # skip prices the pick on a subset of its shares -- the very defect the
        # ladder-gap refusal exists for, arriving by another route.
        lines = [
            *_entry_lines(crid="VST-2026-09-21-entry-t0"),
            *_entry_lines(crid="VST-2026-09-21-entry-t1", realized_qty=4.0, avg_price=None),
        ]
        self.assertIsNone(entry_trails.realized_entry(_fold_of(lines), VST))

    def test_a_single_tier_with_no_fill_price_refuses(self) -> None:
        self.assertIsNone(entry_trails.realized_entry(_fold_of(_entry_lines(avg_price=None)), VST))

    def test_a_fired_tier_with_a_non_finite_quantity_refuses(self) -> None:
        lines = _entry_lines()
        lines[1]["realized_qty"] = float("nan")
        self.assertIsNone(entry_trails.realized_entry(_fold_of(lines), VST))

    def test_tiers_stamping_two_different_plan_stops_refuse(self) -> None:
        fold = _fold_of(
            [
                *_entry_lines(crid="VST-2026-09-21-entry-t0", disaster_stop=125.85),
                *_entry_lines(crid="VST-2026-09-21-entry-t1", disaster_stop=120.00),
            ]
        )
        self.assertIsNone(entry_trails.realized_entry(fold, VST))

    def test_a_pick_with_no_fired_tier_refuses(self) -> None:
        # Not a crash: the blend's divisor would be zero.
        lines = _entry_lines()
        del lines[1]
        self.assertIsNone(entry_trails.realized_entry(_fold_of(lines), VST))


class TheArithmeticRefusesWhatItCannotDivide(unittest.TestCase):
    """:func:`trade_alerts.result_r` on its own, for the facts a page cannot reach.

    ``exit_qty`` does NOT enter the quotient — it cancels, which is why one exit
    can agree with a whole-pick number — so a non-finite quantity is invisible to
    the finiteness check on the RESULT, and only the check on the INPUTS catches
    it. Measured: dropping that check left the whole brokers suite green."""

    def test_a_non_finite_exit_quantity_refuses(self) -> None:
        for bad in (float("nan"), float("inf")):
            with self.subTest(exit_qty=bad):
                self.assertIsNone(
                    trade_alerts.result_r(
                        avg_entry=VST_AVG_ENTRY,
                        plan_stop=VST_PLAN_STOP,
                        exit_qty=bad,
                        exit_price=VST_EXIT_PRICE,
                    )
                )

    def test_a_non_finite_entry_or_stop_refuses(self) -> None:
        for field in ("avg_entry", "plan_stop"):
            for bad in (float("nan"), float("inf")):
                with self.subTest(field=field, value=bad):
                    facts: dict[str, float | None] = {
                        "avg_entry": VST_AVG_ENTRY,
                        "plan_stop": VST_PLAN_STOP,
                        "exit_qty": VST_QTY,
                        "exit_price": VST_EXIT_PRICE,
                    }
                    facts[field] = bad
                    self.assertIsNone(trade_alerts.result_r(**facts))

    def test_a_missing_fact_refuses_rather_than_raising(self) -> None:
        self.assertIsNone(
            trade_alerts.result_r(
                avg_entry=None,
                plan_stop=VST_PLAN_STOP,
                exit_qty=VST_QTY,
                exit_price=VST_EXIT_PRICE,
            )
        )

    def test_a_non_positive_risk_refuses(self) -> None:
        # The command's ``non_positive_risk``: the entry is at or beyond the stop.
        self.assertIsNone(
            trade_alerts.result_r(
                avg_entry=VST_PLAN_STOP,
                plan_stop=VST_PLAN_STOP,
                exit_qty=VST_QTY,
                exit_price=VST_EXIT_PRICE,
            )
        )


class TheRenderedSignIsTheRealSign(unittest.TestCase):
    """A result that rounds to zero must not carry a minus sign.

    Reachable by construction, not a synthetic input: ``breakeven_trail`` rests
    the stop AT the realized entry (``max(entry, entry + frac * (peak -
    entry))``), so a fill there gives an R whose sign is float noise. At four
    decimals IEEE ``-0.0`` and -1e-9 both print ``-0.0000R``, and a minus sign
    on a page whose whole job is the sign reads as a loss that did not happen.
    """

    def _page(self, result_r: float) -> str:
        return trade_alerts.render(
            trade_alerts.TradeEvent(
                kind=trade_alerts.EventKind.EXIT_FILLED,
                ticker="VST",
                label="SL",
                qty=VST_QTY,
                price=VST_AVG_ENTRY,
                reason=trade_alerts.ExitReason.TRAILED_STOP,
                level=VST_AVG_ENTRY,
                order_id=VST_STOP_ID,
                position_closed=True,
                result_r=result_r,
            )
        )

    def test_a_result_rounding_to_zero_from_below_reads_as_zero(self) -> None:
        for result_r in (-0.0, -1e-9, -0.00004):
            with self.subTest(result_r=result_r):
                self.assertIn("+0.0000R", self._page(result_r))

    def test_a_real_loss_keeps_its_minus_sign(self) -> None:
        # The fix must not swallow the sign of a result that is actually
        # negative at the rendered width.
        self.assertIn("-0.0001R", self._page(-0.0001))
        self.assertIn("-1.0000R", self._page(-1.0))


class TheSubmittedExitPageCarriesItsTarget(unittest.TestCase):
    """A take-profit page says the target the market sell was sent at.

    The engine holds the target at the statement that builds the tranche
    (``_fire_telemetry`` journals it); before this change it dropped it, so the
    page said ``take-profit`` with no number and the operator could not tell
    which rung of the ladder had paid.
    """

    def _page(self, tranche: live_exit_engine.FiredTranche) -> str:
        pages: list[str] = []
        cl._announce_fired_tranches(
            _deps(object(), pages),
            [tranche],
            {_UIC: ("VST", "XNYS")},
            [],
            cl.TickReport(),
        )
        self.assertEqual(len(pages), 1, pages)
        return pages[0]

    def test_the_target_is_in_the_page(self) -> None:
        text = self._page(
            live_exit_engine.FiredTranche(
                uic=_UIC,
                tag="tp1",
                qty=3.0,
                sell_order_id="5447570154",
                position_closed=False,
                target_price=150.0,
            )
        )
        self.assertEqual(
            text,
            "SELL VST TP1 3 - take-profit 150.00, market sell sent - position still open "
            "(order 5447570154)",
        )

    def test_a_tranche_without_a_target_still_pages(self) -> None:
        # A legacy/partially-built tranche must never cost the operator the
        # alert: the number is left out, never rendered as None.
        text = self._page(
            live_exit_engine.FiredTranche(
                uic=_UIC, tag="tp2", qty=3.0, sell_order_id="X-2", position_closed=True
            )
        )
        self.assertEqual(
            text,
            "SELL VST TP2 3 - take-profit, market sell sent - position closed (order X-2)",
        )


if __name__ == "__main__":
    unittest.main()
