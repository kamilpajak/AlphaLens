"""#1402 part 2: instrument the unprotected window between an entry fill and
its disaster stop.

Measured on the LIVE journal (2026-10-13): for 7 entries the gap from the venue
fill to the journaled ``stop_placed`` was 6.2-18.8 s, and in at least two of
them a FULL protection pass ran inside that window and placed no stop (one at
+0.9 s and +11.9 s with the stop at +15.1 s). The leading explanation is that
the fill is not yet VISIBLE to the daemon when that earlier pass runs -- either
the order has not left the order book, or the position is not readable yet.
Nothing journalled the probe's observation, so the explanation could not be
checked.

Two observability-only records close that gap:

* the PROBE's observation (``fill_probe``) -- which armed order id showed what,
  with the filled quantity it saw;
* the PASS THAT COULD NOT ACT (``fill_probe_protection_gap``) -- a protection
  pass ran while an armed entry order already had an observation and the
  position its uic would need was absent from the snapshot that pass read.

Both are best-effort and written at most ONCE per observation: the probe fires
every 5 s while an order rests, so a per-probe record would be noise and a
failing append must never touch the money path.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from unittest import mock

from alphalens_pipeline.brokers.automanager import control_loop as cl
from alphalens_pipeline.brokers.automanager import entry_trails
from alphalens_pipeline.brokers.automanager import stop_journal as sj
from broker_contract.contract import OrderState, OrderStatus

from tests.brokers.automanager.home_isolation import IsolatedHomeTestCase
from tests.brokers.test_trail_wiring import _Broker, _deps, _pos, _seed_planned, _stop_leg

_ORDER_ID = "o1"
_CRID = "BIO-2026-10-13-entry-t0"
_UIC = 43070
_INVISIBLE_UIC = 999999
_NOW = dt.datetime(2026, 10, 13, 15, 30, 1, tzinfo=dt.UTC)


def _book_order(order_id: str, filled: float = 0.0) -> OrderState:
    return OrderState(
        order_id=order_id,
        status=OrderStatus.PARTIALLY_FILLED if filled else OrderStatus.WORKING,
        instrument=None,
        filled_quantity=filled,
        raw_status="Working",
        side="BUY",
        order_type="TrailingStopIfTraded",
    )


class _ResolvingBroker(_Broker):
    """A protection-pass broker that can also classify a gone order and list the
    book — the two gates ``build_entry_fill_probe`` applies."""

    def resolve_order_outcome(self, order_id: str) -> OrderState:  # pragma: no cover - unused
        raise AssertionError("not reached")

    def get_open_position_references(self) -> list[str]:  # pragma: no cover - unused
        return []

    def get_closed_position_rows(self) -> list[dict[str, Any]]:  # pragma: no cover - unused
        return []

    def list_open_orders(self) -> list[OrderState]:
        return []


class _Probe:
    """One ``EntryFillProbe`` over a scripted book, with its journal recorded."""

    def __init__(self, steps: list[list[OrderState]], *, append: Any = None) -> None:
        self._steps = list(steps)
        self.lines: list[Mapping[str, Any]] = []
        self.seen: dict[str, cl.EntryFillObservation] = {}
        self.probe = cl.EntryFillProbe(
            armed_orders=lambda: {_ORDER_ID: cl.ArmedEntryOrder(crid=_CRID, uic=_UIC)},
            list_open_orders=self._book,
            seen=self.seen,
            append=self.lines.append if append is None else append,
        )

    def _book(self) -> list[OrderState]:
        step = self._steps.pop(0) if len(self._steps) > 1 else self._steps[0]
        return list(step)

    def cycle(self, times: int = 1) -> list[bool]:
        """``arm()`` once, then ``times`` probes -- one wait of the daemon."""
        self.probe.arm()
        with mock.patch.object(cl, "_utc_now", return_value=_NOW):
            return [self.probe.fill_seen() for _ in range(times)]


class TestTheProbeJournalsWhatItSaw(IsolatedHomeTestCase):
    """Part (1): the probe's own observation, once per (order id, observation)."""

    def test_a_first_sighting_of_a_resting_order_journals_nothing(self) -> None:
        probe = _Probe([[_book_order(_ORDER_ID)]])

        self.assertEqual(probe.cycle(3), [False, False, False])

        self.assertEqual(probe.lines, [])

    def test_an_order_that_left_the_book_journals_one_line(self) -> None:
        probe = _Probe([[_book_order(_ORDER_ID)], []])

        probe.cycle(2)

        self.assertEqual(
            probe.lines,
            [
                {
                    "kind": entry_trails.KIND_FILL_PROBE,
                    "crid": _CRID,
                    entry_trails.KEY_OBSERVATION: entry_trails.OBSERVATION_ORDER_ABSENT,
                    "order_id": _ORDER_ID,
                    # No quantity: an order that LEFT the book reports none, and
                    # the 0.0 the probe reads is the absence, not a fill size.
                    "uic": _UIC,
                    "ts": _NOW.isoformat(),
                }
            ],
        )

    def test_a_grown_filled_quantity_journals_the_quantity_it_saw(self) -> None:
        probe = _Probe([[_book_order(_ORDER_ID)], [_book_order(_ORDER_ID, filled=3.0)]])

        probe.cycle(2)

        self.assertEqual(len(probe.lines), 1, probe.lines)
        line = probe.lines[0]
        self.assertEqual(
            line[entry_trails.KEY_OBSERVATION], entry_trails.OBSERVATION_FILLED_QTY_GREW
        )
        self.assertEqual(line[entry_trails.KEY_FILLED_QTY], 3.0)
        self.assertEqual(line["crid"], _CRID)
        self.assertEqual(line["uic"], _UIC)

    def test_many_probe_cycles_journal_each_observation_once(self) -> None:
        # The order partially fills (3 -> 7 -> 9) and then leaves the book; the
        # probe keeps reading the last step for the rest of the cycles.
        probe = _Probe(
            [
                [_book_order(_ORDER_ID)],
                [_book_order(_ORDER_ID, filled=3.0)],
                [_book_order(_ORDER_ID, filled=7.0)],
                [_book_order(_ORDER_ID, filled=9.0)],
                [],
            ]
        )

        probe.cycle(12)

        observations = [line[entry_trails.KEY_OBSERVATION] for line in probe.lines]
        self.assertEqual(
            observations,
            [
                entry_trails.OBSERVATION_FILLED_QTY_GREW,
                entry_trails.OBSERVATION_ORDER_ABSENT,
            ],
        )

    def test_the_memo_on_the_deps_survives_the_wait_so_a_later_cycle_is_silent(self) -> None:
        probe = _Probe([[]])
        probe.cycle(2)
        self.assertEqual(len(probe.lines), 1)

        again = _Probe([[]])
        again.seen.update(probe.seen)
        again.probe = cl.EntryFillProbe(
            armed_orders=lambda: {_ORDER_ID: cl.ArmedEntryOrder(crid=_CRID, uic=_UIC)},
            list_open_orders=again._book,
            seen=again.seen,
            append=again.lines.append,
        )

        again.cycle(3)

        self.assertEqual(again.lines, [])

    def test_a_failing_append_neither_stops_the_wake_nor_retries(self) -> None:
        def _explode(_record: Mapping[str, Any]) -> None:
            raise OSError("ENOSPC")

        probe = _Probe([[]], append=_explode)

        with self.assertLogs(cl.logger, level="WARNING") as caught:
            woke = probe.cycle(4)

        self.assertEqual(woke, [True, False, False, False])
        failures = [line for line in caught.output if "observation journal" in line]
        self.assertEqual(len(failures), 1, caught.output)

    def test_a_failing_append_is_not_retried_while_the_reading_KEEPS_changing(self) -> None:
        """The no-retry property has TWO possible causes and the case above can
        only see one of them. There, the book never changes after the order
        goes absent, so the unchanged-reading short-circuit alone would keep the
        append from running again even if nothing marked it done.

        A quantity that grows 3 -> 5 -> 7 is a NEW reading every probe under the
        SAME observation name, so the short-circuit cannot help: only the mark
        taken BEFORE the append keeps this to one attempt. Marking after a
        successful append instead turns a full disk into one failed write per
        probe for the life of the order."""

        def _explode(_record: Mapping[str, Any]) -> None:
            raise OSError("ENOSPC")

        probe = _Probe(
            [
                [_book_order(_ORDER_ID, filled=3.0)],
                [_book_order(_ORDER_ID, filled=5.0)],
                [_book_order(_ORDER_ID, filled=7.0)],
            ],
            append=_explode,
        )

        with self.assertLogs(cl.logger, level="WARNING") as caught:
            woke = probe.cycle(3)

        self.assertEqual(woke, [True, True, True], "every growth is still a wake")
        failures = [line for line in caught.output if "observation journal" in line]
        self.assertEqual(len(failures), 1, f"one attempt, not one per probe: {caught.output}")

    def test_the_production_probe_carries_the_crid_and_uic_from_the_one_fold_it_reads(self) -> None:
        # The crid and the uic are already in the fold the armed-order read does:
        # the tier is KEYED by crid and its watch_open carries the uic. No new read.
        tier = entry_trails.EntryTrailTierState(
            crid=_CRID,
            watch_open={"crid": _CRID, "uic": _UIC, "ticker": "BIO"},
            latest_kind=entry_trails.KIND_TRAIL_ARMED,
            min_trough=None,
            terminal_kind=None,
            armed_order_id=_ORDER_ID,
        )
        deps = _deps(
            _ResolvingBroker(positions=[], sells=[], by_uic={}), feed_factory=None, sink=[]
        )
        with (
            mock.patch.object(entry_trails, "entry_trail_bps", return_value=50),
            mock.patch.object(entry_trails, "read_entry_trail_fold"),
            mock.patch.object(cl, "_resting_armed_tiers", return_value={_CRID: tier}),
        ):
            probe = cl.build_entry_fill_probe(deps)
            assert probe is not None
            self.assertTrue(probe.arm())
            probe.fill_seen()

        self.assertEqual(
            deps.entry_fill_seen,
            {
                _ORDER_ID: cl.EntryFillObservation(
                    book_state=cl._BOOK_STATE_ABSENT,
                    filled_quantity=0.0,
                    crid=_CRID,
                    uic=_UIC,
                    journaled={entry_trails.OBSERVATION_ORDER_ABSENT: None},
                )
            },
        )

    def test_the_default_append_is_the_entry_trails_journal(self) -> None:
        probe = cl.EntryFillProbe(
            armed_orders=lambda: {_ORDER_ID: cl.ArmedEntryOrder(crid=_CRID, uic=_UIC)},
            list_open_orders=list,
            seen={},
        )
        probe.arm()

        probe.fill_seen()

        fold = entry_trails.read_entry_trail_fold()
        self.assertEqual(fold.malformed, 0)
        written = [
            json.loads(line)
            for line in entry_trails._entry_trail_journal_path()
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        self.assertEqual([record["kind"] for record in written], [entry_trails.KIND_FILL_PROBE])


class TestTheProbeKindsAreInvisibleToEveryExistingReader(IsolatedHomeTestCase):
    """The new kinds are deliberately NOT registered entry-trail kinds: the fold
    must ignore them, and the compactor must keep them verbatim (memo G4)."""

    def _line(self) -> str:
        return json.dumps(
            {
                "kind": entry_trails.KIND_FILL_PROBE,
                "crid": _CRID,
                "observation": entry_trails.OBSERVATION_ORDER_ABSENT,
                "order_id": _ORDER_ID,
                "uic": _UIC,
            },
            sort_keys=True,
        )

    def test_neither_kind_is_a_folded_entry_trail_kind(self) -> None:
        for kind in (entry_trails.KIND_FILL_PROBE, entry_trails.KIND_FILL_PROBE_PROTECTION_GAP):
            with self.subTest(kind=kind):
                self.assertNotIn(kind, entry_trails.ENTRY_TRAIL_KINDS)
                self.assertNotIn(kind, entry_trails.ENTRY_TRAIL_TERMINAL_KINDS)

    def test_the_fold_ignores_a_probe_line_without_counting_it_malformed(self) -> None:
        fold = entry_trails.fold_entry_trail_lines([self._line()])

        self.assertEqual(fold.tiers, {})
        self.assertEqual(fold.malformed, 0)

    def test_a_probe_line_changes_nothing_about_a_real_tier(self) -> None:
        watch_open = json.dumps(
            {
                "kind": entry_trails.KIND_WATCH_OPEN,
                "crid": _CRID,
                "uic": _UIC,
                "limit": 10.0,
                "qty": 5.0,
            },
            sort_keys=True,
        )

        without = entry_trails.fold_entry_trail_lines([watch_open])
        with_probe = entry_trails.fold_entry_trail_lines([watch_open, self._line()])

        self.assertEqual(with_probe, without)
        self.assertEqual(
            entry_trails.watching_virtual_gross_acct(with_probe),
            entry_trails.watching_virtual_gross_acct(without),
        )

    def test_the_compactor_preserves_a_probe_line_verbatim(self) -> None:
        line = self._line()
        self.assertEqual(entry_trails.compact_entry_trail_lines([line]), [line])


class TestTheProtectionPassJournalsAFillItCannotSee(IsolatedHomeTestCase):
    """Part (2): the pass that could not act.

    A protection pass that runs while an armed entry order already has an
    observation, and that reads NO position for that order's uic, is exactly the
    pass the LIVE journal shows placing no stop. One line per (uic, observation)
    records it; this is the record that tells "the order had not left the book
    yet" apart from "the position was not readable yet"."""

    def setUp(self) -> None:
        super().setUp()
        self.journal = self.home / "planned.jsonl"
        self.lines: list[Mapping[str, Any]] = []
        patcher = mock.patch.object(
            entry_trails, "append_entry_trail_line", lambda record: self.lines.append(dict(record))
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _deps_for(self, *, positions: list[Any]) -> cl.LoopDeps:
        by_uic = {_UIC: positions[0]} if positions else {}
        broker = _Broker(
            positions=positions,
            sells=[_stop_leg(7.0, uic=_UIC)] if positions else [],
            by_uic=by_uic,
        )
        deps = _deps(broker, feed_factory=None, sink=[])
        # Production shape: the probe reads 0.0 for an order that is GONE, and
        # records no quantity for that observation (see ``_observed_quantity``).
        deps.entry_fill_seen[_ORDER_ID] = cl.EntryFillObservation(
            book_state=cl._BOOK_STATE_ABSENT,
            filled_quantity=0.0,
            crid=_CRID,
            uic=_UIC,
            journaled={entry_trails.OBSERVATION_ORDER_ABSENT: None},
        )
        return deps

    def _pass(self, deps: cl.LoopDeps, times: int = 1) -> None:
        with (
            mock.patch.object(sj, "_standalone_stop_journal_path", lambda: self.journal),
            mock.patch.object(cl, "_utc_now", return_value=_NOW),
        ):
            for _ in range(times):
                cl._run_protection_pass(deps, [], False, cl.TickReport())

    def test_an_invisible_position_is_journaled_once_per_uic_and_observation(self) -> None:
        deps = self._deps_for(positions=[])

        self._pass(deps, times=4)

        self.assertEqual(
            self.lines,
            [
                {
                    "kind": entry_trails.KIND_FILL_PROBE_PROTECTION_GAP,
                    "crid": _CRID,
                    entry_trails.KEY_OBSERVATION: entry_trails.OBSERVATION_ORDER_ABSENT,
                    "order_id": _ORDER_ID,
                    "uic": _UIC,
                    "ts": _NOW.isoformat(),
                }
            ],
        )

    def test_each_gap_line_carries_its_OWN_observation_quantity(self) -> None:
        """The production shape the other cases miss: a PARTIAL fill followed by
        the order leaving the book, so one order id carries two observations.

        The quantity belongs to the observation each line NAMES. Stamping the
        latest reading onto both makes the ``filled_qty_grew`` line report the
        0.0 the probe read once the order was gone -- a record that says the
        quantity grew to zero. An absent order cannot report a fill size at all,
        so that line carries no quantity rather than a misleading one."""
        probe = _Probe([[_book_order(_ORDER_ID)], [_book_order(_ORDER_ID, filled=7.0)], []])
        probe.cycle(3)
        self.assertEqual(
            sorted(probe.seen[_ORDER_ID].journaled),
            [entry_trails.OBSERVATION_FILLED_QTY_GREW, entry_trails.OBSERVATION_ORDER_ABSENT],
            "the fixture must leave BOTH observations on one order id",
        )
        deps = self._deps_for(positions=[])
        deps.entry_fill_seen[_ORDER_ID] = probe.seen[_ORDER_ID]
        self.lines.clear()

        self._pass(deps)

        by_observation = {line[entry_trails.KEY_OBSERVATION]: line for line in self.lines}
        self.assertEqual(
            sorted(by_observation),
            [entry_trails.OBSERVATION_FILLED_QTY_GREW, entry_trails.OBSERVATION_ORDER_ABSENT],
            f"one line per observation: {self.lines}",
        )
        self.assertEqual(
            by_observation[entry_trails.OBSERVATION_FILLED_QTY_GREW][entry_trails.KEY_FILLED_QTY],
            7.0,
            "the grow line must carry the quantity it saw grow, not the later reading",
        )
        self.assertNotIn(
            entry_trails.KEY_FILLED_QTY,
            by_observation[entry_trails.OBSERVATION_ORDER_ABSENT],
            "an absent order reports no fill size, so the key is left out",
        )

    def test_a_visible_position_journals_nothing(self) -> None:
        deps = self._deps_for(positions=[_pos(7.0)])

        self._pass(deps, times=3)

        self.assertEqual(self.lines, [])

    def test_an_observation_the_probe_never_journaled_is_not_a_gap(self) -> None:
        deps = self._deps_for(positions=[])
        deps.entry_fill_seen[_ORDER_ID] = cl.EntryFillObservation(
            book_state=cl._BOOK_STATE_RESTING,
            filled_quantity=0.0,
            crid=_CRID,
            uic=_UIC,
        )

        self._pass(deps)

        self.assertEqual(self.lines, [])

    def test_a_NET_FLAT_row_for_the_uic_counts_as_seen(self) -> None:
        """The visible-position guard has two members, and this is the one the
        long-position map cannot answer. ``_netted_all_positions`` keeps a uic
        the broker still reports at quantity 0.0, which is what a CLOSED
        position looks like; such a uic is absent from the long map. Treating
        that as "could not see the fill" would write a gap line after the
        position had already closed and ended — a false record in the one
        journal this instrument exists to make trustworthy."""
        deps = self._deps_for(positions=[_pos(0.0)])

        self._pass(deps)

        self.assertEqual(self.lines, [], "a uic the broker reports is seen, flat or not")

    def test_TWO_journaled_tiers_on_one_uic_both_record(self) -> None:
        """A pick's tiers share a uic, and each tier has its own armed order, so
        two tiers can hold DIFFERENT observations at the same time: t0 saw a
        partial fill, t1 saw its order leave the book. Both are evidence about
        the same unprotected window and both must survive. Electing one
        observation per uic keeps only the last tier inserted and drops the
        other, which loses exactly the reading the measurement needs."""
        deps = self._deps_for(positions=[])
        deps.entry_fill_seen[_ORDER_ID] = cl.EntryFillObservation(
            book_state=cl._BOOK_STATE_RESTING,
            filled_quantity=5.0,
            crid=_CRID,
            uic=_UIC,
            journaled={entry_trails.OBSERVATION_FILLED_QTY_GREW: 5.0},
        )
        deps.entry_fill_seen["o2"] = cl.EntryFillObservation(
            book_state=cl._BOOK_STATE_ABSENT,
            filled_quantity=0.0,
            crid="BIO-2026-10-13-entry-t1",
            uic=_UIC,
            journaled={entry_trails.OBSERVATION_ORDER_ABSENT: None},
        )

        self._pass(deps)

        self.assertEqual(
            sorted(line[entry_trails.KEY_OBSERVATION] for line in self.lines),
            [entry_trails.OBSERVATION_FILLED_QTY_GREW, entry_trails.OBSERVATION_ORDER_ABSENT],
            f"both tiers' observations must be recorded: {self.lines}",
        )
        by_observation = {line[entry_trails.KEY_OBSERVATION]: line for line in self.lines}
        self.assertEqual(
            by_observation[entry_trails.OBSERVATION_FILLED_QTY_GREW]["order_id"],
            _ORDER_ID,
            "each line names the order it came from, not the uic's last tier",
        )
        self.assertEqual(by_observation[entry_trails.OBSERVATION_ORDER_ABSENT]["order_id"], "o2")

    def test_an_unjournaled_SIBLING_tier_cannot_mask_its_uic(self) -> None:
        """The tiers of one pick share a uic. A tier that has nothing journaled
        yet must not silence the tier that does — which is a property of
        GROUPING the observations per uic rather than electing one of them. The
        empty sibling is inserted LAST on purpose: under an election that is the
        order in which it would win the uic's only slot."""
        deps = self._deps_for(positions=[])
        deps.entry_fill_seen["o2"] = cl.EntryFillObservation(
            book_state=cl._BOOK_STATE_RESTING,
            filled_quantity=0.0,
            crid="BIO-2026-10-13-entry-t1",
            uic=_UIC,
        )

        self._pass(deps)

        self.assertEqual(len(self.lines), 1, f"the journaled tier must still record: {self.lines}")
        self.assertEqual(self.lines[0]["order_id"], _ORDER_ID)

    def test_only_the_observations_the_probe_SAW_are_journaled(self) -> None:
        """One line per observation actually recorded, never a fixed name added
        on the way out. A single partial fill has seen no absence yet, so an
        ``order_absent`` line here would claim an observation that never
        happened."""
        deps = self._deps_for(positions=[])
        deps.entry_fill_seen[_ORDER_ID] = cl.EntryFillObservation(
            book_state=cl._BOOK_STATE_RESTING,
            filled_quantity=6.0,
            crid=_CRID,
            uic=_UIC,
            journaled={entry_trails.OBSERVATION_FILLED_QTY_GREW: 6.0},
        )

        self._pass(deps)

        self.assertEqual(
            [line[entry_trails.KEY_OBSERVATION] for line in self.lines],
            [entry_trails.OBSERVATION_FILLED_QTY_GREW],
            f"exactly the one observation the probe recorded: {self.lines}",
        )
        self.assertEqual(self.lines[0][entry_trails.KEY_FILLED_QTY], 6.0)

    def test_an_observation_without_a_uic_is_skipped(self) -> None:
        deps = self._deps_for(positions=[])
        deps.entry_fill_seen[_ORDER_ID] = cl.EntryFillObservation(
            book_state=cl._BOOK_STATE_ABSENT,
            filled_quantity=0.0,
            crid=_CRID,
            uic=None,
            journaled={entry_trails.OBSERVATION_ORDER_ABSENT: None},
        )

        self._pass(deps)

        self.assertEqual(self.lines, [])

    def test_the_memo_is_released_when_the_order_leaves_the_probe(self) -> None:
        deps = self._deps_for(positions=[])
        self._pass(deps)
        self.assertEqual(len(self.lines), 1)

        # The tier terminalized: arm() dropped the id from the probe memo. A
        # later fill on the same uic must be recordable again.
        deps.entry_fill_seen.clear()
        self._pass(deps)
        self.assertEqual(deps.entry_fill_protection_gap_journaled, set())

        deps.entry_fill_seen[_ORDER_ID] = cl.EntryFillObservation(
            book_state=cl._BOOK_STATE_ABSENT,
            filled_quantity=0.0,
            crid=_CRID,
            uic=_UIC,
            journaled={entry_trails.OBSERVATION_ORDER_ABSENT: None},
        )
        self._pass(deps)

        self.assertEqual(len(self.lines), 2, self.lines)

    def test_a_failing_append_never_breaks_the_protection_pass(self) -> None:
        # A NAKED long (a position with no working SELL leg) plus a gap record
        # whose append raises: the stop must still be placed. The journal write
        # sits before the reconcile, so an escaping OSError would starve it.
        broker = _Broker(positions=[_pos(7.0)], sells=[], by_uic={_UIC: _pos(7.0)})
        deps = _deps(broker, feed_factory=None, sink=[])
        _seed_planned(self.journal, reaction=None)
        deps.entry_fill_seen[_ORDER_ID] = cl.EntryFillObservation(
            book_state=cl._BOOK_STATE_ABSENT,
            filled_quantity=0.0,
            crid=_CRID,
            uic=_INVISIBLE_UIC,
            journaled={entry_trails.OBSERVATION_ORDER_ABSENT: None},
        )

        with (
            mock.patch.object(
                entry_trails, "append_entry_trail_line", side_effect=OSError("ENOSPC")
            ),
            self.assertLogs(cl.logger, level="WARNING"),
        ):
            self._pass(deps)

        self.assertEqual(
            deps.entry_fill_protection_gap_journaled,
            {(_INVISIBLE_UIC, entry_trails.OBSERVATION_ORDER_ABSENT)},
        )
        self.assertEqual([placed[0] for placed in broker.placed], [_UIC])

    def test_the_gap_line_does_not_disturb_the_entry_trail_fold(self) -> None:
        # Written through the REAL append, into this test's isolated home.
        deps = self._deps_for(positions=[])
        with mock.patch.object(entry_trails, "append_entry_trail_line", _real_append):
            self._pass(deps)

        fold = entry_trails.read_entry_trail_fold()
        self.assertEqual(fold.tiers, {})
        self.assertEqual(fold.malformed, 0)
        self.assertTrue(_journal_holds(entry_trails.KIND_FILL_PROBE_PROTECTION_GAP))


def _real_append(record: Mapping[str, Any]) -> None:
    """Call the production append (the test class patches the module attribute,
    so a test that wants the real write names it explicitly)."""
    entry_trails.append_json_line(entry_trails._entry_trail_journal_path(), record, default=str)


def _journal_holds(kind: str) -> bool:
    path: Path = entry_trails._entry_trail_journal_path()
    return any(
        json.loads(line).get("kind") == kind
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )
