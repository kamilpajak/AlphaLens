"""The sweep: plan, then apply under a lock (#1689, memo section 3).

``plan_sweep`` folds what came back against what is on disk and writes NOTHING;
``apply_sweep`` takes the lock, appends in order and returns the record. One
code path, one branch: the CLI's ``--dry-run`` runs the first and stops, so a
dry run cannot write and a real run cannot report a different plan than the one
it echoed.

Rows are the LIVE account's own, from
``tests/brokers/automanager/fixtures/trades/live_2026_10_03/venue.json``: 104
bookings rows and 41 trades rows.
"""

from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest import mock

from alphalens_pipeline.account_cash import codec, paths, store, sweep
from alphalens_pipeline.brokers.account_cash import (
    AccountCashRead,
    BalanceSnapshot,
    CashEvent,
    TradeRow,
)
from alphalens_pipeline.brokers.fill_history import ReadWindow
from alphalens_pipeline.brokers.journal import JournalWriteError

from tests.brokers.automanager.home_isolation import IsolatedHomeTestCase

_LIVE = "live"
_SIM = "sim"
_SINCE = dt.date(2026, 8, 1)
_UNTIL = dt.date(2026, 10, 3)
_NOW = dt.datetime(2026, 10, 9, 0, 31, tzinfo=dt.UTC)

_VENUE = json.loads(
    (
        Path(__file__).resolve().parents[1]
        / "brokers"
        / "automanager"
        / "fixtures"
        / "trades"
        / "live_2026_10_03"
        / "venue.json"
    ).read_text(encoding="utf-8")
)

_EVENTS = tuple(CashEvent.from_vendor_row(row) for row in _VENUE["bookings"])
_TRADES = tuple(TradeRow.from_vendor_row(row) for row in _VENUE["trades"])

_BALANCE = BalanceSnapshot(
    currency="PLN",
    cash_balance=20738.68,
    non_margin_positions_value=3422.56,
    total_value=24161.24,
    transactions_not_booked=0.0,
    calculation_reliability="Ok",
    asof=_NOW,
)


def _read(**overrides: Any) -> AccountCashRead:
    base = AccountCashRead(
        events=_EVENTS,
        trades=_TRADES,
        balance=_BALANCE,
        events_window=ReadWindow(_SINCE.isoformat(), _UNTIL.isoformat(), len(_EVENTS)),
        trades_window=ReadWindow(_SINCE.isoformat(), _UNTIL.isoformat(), len(_TRADES)),
        skipped_reason=None,
    )
    return replace(base, **overrides)


def _sim_read() -> AccountCashRead:
    """What ``list_account_cash`` answers on SIM: nothing read, and why."""
    return AccountCashRead(
        events=None,
        trades=None,
        balance=None,
        skipped_reason="sim_reports_unusable",
    )


def _plan(read: AccountCashRead | None = None, *, env: str = _LIVE) -> sweep.SweepPlan:
    return sweep.plan_sweep(read or _read(), env=env, since=_SINCE, until=_UNTIL)


def _snapshot(root: Path) -> dict[str, bytes]:
    if not root.exists():
        return {}
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


class _FakeClock:
    """A monotonic clock that jumps past any deadline on its second reading,
    so the lock's bounded spin is exercised without paying its 1.0 s wall."""

    def __init__(self) -> None:
        self.readings = 0

    def __call__(self) -> float:
        self.readings += 1
        return 0.0 if self.readings == 1 else 1_000.0


class APlanWritesNothing(IsolatedHomeTestCase):
    def test_a_plan_over_a_seeded_store_leaves_every_byte_alone(self) -> None:
        store.append_cash_events(_EVENTS[:10], env=_LIVE)
        store.append_trade_rows(_TRADES[:5], env=_LIVE)
        root = paths.account_cash_root(_LIVE)
        before = _snapshot(root)

        _plan()

        self.assertEqual(_snapshot(root), before)

    def test_a_plan_on_a_first_run_creates_no_file_at_all(self) -> None:
        """Nothing on disk yet is the ordinary first case, and a plan must not
        be what brings the store into existence."""
        _plan()
        self.assertFalse(paths.account_cash_root(_LIVE).exists())

    def test_a_plan_counts_what_it_would_append(self) -> None:
        plan = _plan()
        self.assertEqual(len(plan.new_events), len(_EVENTS))
        self.assertEqual(len(plan.new_trade_rows), len(_TRADES))
        self.assertEqual(plan.already_present_events, 0)
        self.assertEqual(plan.restated_events, ())
        self.assertIsNone(plan.refused_reason)


class ASecondSweepOfTheSameWindowAppendsNothing(IsolatedHomeTestCase):
    def test_the_second_plan_has_no_new_event(self) -> None:
        sweep.apply_sweep(_plan(), now=_NOW)

        again = _plan()
        self.assertEqual(again.new_events, ())
        self.assertEqual(again.already_present_events, len(_EVENTS))
        self.assertEqual(again.new_trade_rows, ())
        self.assertEqual(again.already_present_trade_rows, len(_TRADES))

    def test_the_store_still_holds_one_record_per_key(self) -> None:
        sweep.apply_sweep(_plan(), now=_NOW)
        sweep.apply_sweep(_plan(), now=_NOW + dt.timedelta(days=1))

        fold = store.read_cash_events(env=_LIVE)
        self.assertEqual(len(fold.events), len(_EVENTS))
        self.assertEqual(fold.restated, 0)


class AChangedAmountOnAKnownIdIsARestatement(IsolatedHomeTestCase):
    """The one shape memo section 9 names as the fold's blind spot. Folding a
    correction in silently is the failure the whole store exists to detect."""

    def test_it_is_planned_as_a_restatement_and_not_as_already_present(self) -> None:
        sweep.apply_sweep(_plan(), now=_NOW)
        corrected = replace(_EVENTS[0], amount_account_currency=-999.99)
        plan = _plan(_read(events=(corrected, *_EVENTS[1:])))

        self.assertEqual(plan.restated_events, (corrected,))
        self.assertEqual(plan.new_events, ())
        self.assertEqual(plan.already_present_events, len(_EVENTS) - 1)

    def test_applying_it_keeps_both_lines_and_reports_one_restatement(self) -> None:
        sweep.apply_sweep(_plan(), now=_NOW)
        corrected = replace(_EVENTS[0], amount_account_currency=-999.99)
        record = sweep.apply_sweep(
            _plan(_read(events=(corrected, *_EVENTS[1:]))), now=_NOW + dt.timedelta(days=1)
        )

        self.assertEqual(record.restated["cash_events"], 1)
        self.assertEqual(record.appended["cash_events"], 1)
        fold = store.read_cash_events(env=_LIVE)
        self.assertEqual(len(fold.events), len(_EVENTS))
        self.assertEqual(fold.restated, 1)
        self.assertEqual(fold.lines, len(_EVENTS) + 1)

    def test_a_restatement_that_also_moved_the_date_is_planned_as_one_key(self) -> None:
        """The plan's own key derivation, which is a SECOND one beside the
        fold's. Every case above corrects only an amount, so a key that gained
        ``date`` would plan a value-date correction as a NEW row and the money
        would be appended twice under one ``BkAmountId``."""
        sweep.apply_sweep(_plan(), now=_NOW)
        corrected = replace(_EVENTS[0], date="1999-01-01", amount_account_currency=-999.99)
        plan = _plan(_read(events=(corrected, *_EVENTS[1:])))

        self.assertEqual(plan.new_events, ())
        self.assertEqual(plan.restated_events, (corrected,))
        self.assertEqual(plan.already_present_events, len(_EVENTS) - 1)

    def test_a_restated_trade_row_is_planned_the_same_way(self) -> None:
        sweep.apply_sweep(_plan(), now=_NOW)
        corrected = replace(_TRADES[0], booked_amount_account_currency=-999.99)
        plan = _plan(_read(trades=(corrected, *_TRADES[1:])))

        self.assertEqual(plan.restated_trade_rows, (corrected,))
        self.assertEqual(plan.already_present_trade_rows, len(_TRADES) - 1)


class TheSweepRecordPublishesWhatTheVendorServed(IsolatedHomeTestCase):
    def test_the_window_is_the_vendors_and_not_the_one_asked_for(self) -> None:
        """Discriminated by a read whose window ``rows`` disagrees with
        ``len(events)``: a record built from the ARGUMENTS would publish the
        asked-for window and the two could never differ."""
        served = ReadWindow("2026-08-02", "2026-10-02", 7)
        record = sweep.apply_sweep(_plan(_read(events_window=served)), now=_NOW)

        self.assertEqual(record.events_window, served)
        self.assertNotEqual(record.events_window.rows, len(_EVENTS))
        self.assertEqual(record.since, _SINCE.isoformat())
        self.assertEqual(record.until, _UNTIL.isoformat())

    def test_the_trades_window_is_passed_through_as_well_and_not_swapped(self) -> None:
        """The trades window comes from a SEPARATE vendor call with its own row
        count, and only ``events_window`` was pinned -- so dropping this one to
        ``None`` was silent. The two served values differ here, so swapping
        them fails too, which equal placeholders could not detect."""
        events_served = ReadWindow("2026-08-02", "2026-10-02", 7)
        trades_served = ReadWindow("2026-08-03", "2026-10-01", 5)
        record = sweep.apply_sweep(
            _plan(_read(events_window=events_served, trades_window=trades_served)), now=_NOW
        )

        self.assertEqual(record.trades_window, trades_served)
        self.assertEqual(record.events_window, events_served)
        self.assertNotEqual(record.trades_window, record.events_window)

    def test_a_read_that_served_no_trades_window_records_none(self) -> None:
        """The other direction: ``None`` must stay ``None`` rather than be
        back-filled from the asked-for window, or the record would claim a
        window the vendor never served."""
        record = sweep.apply_sweep(_plan(_read(trades_window=None)), now=_NOW)
        self.assertIsNone(record.trades_window)
        self.assertIsNotNone(record.events_window)

    def test_the_record_carries_the_counts_and_the_environment(self) -> None:
        record = sweep.apply_sweep(_plan(), now=_NOW)
        self.assertEqual(record.env, _LIVE)
        self.assertEqual(record.appended["cash_events"], len(_EVENTS))
        self.assertEqual(record.appended["trade_rows"], len(_TRADES))
        self.assertEqual(record.appended["balances"], 1)
        self.assertEqual(record.appended["sweeps"], 1)
        self.assertEqual(record.malformed, {"cash_events": 0, "trade_rows": 0})
        self.assertEqual(record.swept_at, _NOW)

    def test_the_sweep_id_is_derived_from_the_supplied_instant(self) -> None:
        record = sweep.apply_sweep(_plan(), now=_NOW)
        self.assertEqual(record.sweep_id, _NOW.isoformat())
        self.assertEqual([r.sweep_id for r in store.read_sweeps(env=_LIVE)], [record.sweep_id])

    def test_a_naive_instant_is_refused_before_anything_is_written(self) -> None:
        with self.assertRaises(ValueError):
            sweep.apply_sweep(_plan(), now=dt.datetime(2026, 10, 9, 0, 31))
        self.assertFalse(paths.account_cash_root(_LIVE).exists())

    def test_a_read_that_carried_no_balance_appends_none(self) -> None:
        record = sweep.apply_sweep(_plan(_read(balance=None)), now=_NOW)
        self.assertEqual(record.appended["balances"], 0)
        self.assertEqual(store.read_balances(env=_LIVE), [])


class OneInstantIsOneSweepWhateverOffsetItArrivesWith(IsolatedHomeTestCase):
    """``sweep_id`` is the dedupe key of BOTH ``sweeps.jsonl`` and
    ``balances.jsonl``, so a retry of the same sweep must fold onto one record
    of each instead of two.

    ``apply_sweep`` checks only that ``now`` is AWARE, and every fixture in
    this module hands it UTC -- a column uniform in the value under test. The
    codec normalises ``swept_at`` to UTC, the id kept the caller's offset, and
    the two then disagree for the same instant: ``00:31+00:00`` and
    ``02:31+02:00`` compare equal and produced two records carrying one
    identical ``swept_at``.

    A caller that reaches this with a local offset is ordinary: the VPS runs
    Europe/Warsaw, and ``dt.datetime.now().astimezone()`` is the shortest aware
    ``now`` there is.
    """

    _WARSAW = _NOW.astimezone(dt.timezone(dt.timedelta(hours=2)))

    def test_the_two_offsets_really_are_one_instant(self) -> None:
        """The premise, stated rather than assumed: if these were two instants
        the test below would be asserting that a sweep loses a record."""
        self.assertEqual(self._WARSAW, _NOW)
        self.assertNotEqual(self._WARSAW.isoformat(), _NOW.isoformat())

    def test_a_retry_under_a_different_offset_folds_onto_one_sweep_record(self) -> None:
        sweep.apply_sweep(_plan(), now=_NOW)
        sweep.apply_sweep(_plan(), now=self._WARSAW)

        records = store.read_sweeps(env=_LIVE)
        self.assertEqual([record.sweep_id for record in records], [_NOW.isoformat()])
        self.assertEqual(len(store.read_balances(env=_LIVE)), 1)

    def test_the_id_is_the_utc_rendering_of_the_instant(self) -> None:
        """Asserted against a literal rather than against ``now.isoformat()``,
        which is the expression under test."""
        record = sweep.apply_sweep(_plan(), now=self._WARSAW)
        self.assertEqual(record.sweep_id, "2026-10-09T00:31:00+00:00")

    def test_the_id_agrees_with_the_stamp_the_codec_stored(self) -> None:
        """The invariant the two halves have to meet in: the stamp is
        normalised by the codec, so deriving the id any other way lets a
        journal hold two keys for one ``swept_at``."""
        sweep.apply_sweep(_plan(), now=self._WARSAW)
        (stored,) = store.read_sweeps(env=_LIVE)
        self.assertEqual(stored.sweep_id, stored.swept_at.isoformat())

    def test_the_balance_line_carries_that_same_key(self) -> None:
        """The balance journal is keyed on the id the sweep passes it, so a
        fix applied to the record alone would leave the pair disagreeing."""
        sweep.apply_sweep(_plan(), now=self._WARSAW)
        line = json.loads(paths.balances_path(_LIVE).read_text(encoding="utf-8").strip())
        self.assertEqual(line["sweep_id"], "2026-10-09T00:31:00+00:00")


class TheMalformedCountReachesTheSweepRecord(IsolatedHomeTestCase):
    """``record.malformed`` is the only thing that tells a later reader how much
    of the journal the sweep could not account for.

    Every other sweep fixture seeds a clean store, so both counters are 0 in
    all of them -- a column uniform in the value under test. The store's own
    malformed counting is pinned in ``test_store.py``; nothing carried it
    through the plan into the record, so a hardcoded
    ``{cash_events: 0, trade_rows: 0}`` was green everywhere.
    """

    def _corrupt_one_line_of_each_keyed_journal(self) -> None:
        store.append_cash_events(_EVENTS[:2], env=_LIVE)
        store.append_trade_rows(_TRADES[:2], env=_LIVE)
        for journal in (paths.cash_events_path(_LIVE), paths.trade_rows_path(_LIVE)):
            with journal.open("a", encoding="utf-8") as fh:
                fh.write("not json at all\n")

    def test_the_plan_reports_what_the_folds_could_not_read(self) -> None:
        self._corrupt_one_line_of_each_keyed_journal()
        self.assertEqual(_plan().malformed, {"cash_events": 1, "trade_rows": 1})

    def test_the_record_carries_the_plans_counts_and_not_a_zero(self) -> None:
        self._corrupt_one_line_of_each_keyed_journal()
        record = sweep.apply_sweep(_plan(), now=_NOW)
        self.assertEqual(record.malformed, {"cash_events": 1, "trade_rows": 1})

    def test_the_two_journals_are_counted_apart(self) -> None:
        """One count standing in for both would satisfy the tests above, so
        exactly one journal is corrupted here."""
        store.append_cash_events(_EVENTS[:2], env=_LIVE)
        store.append_trade_rows(_TRADES[:2], env=_LIVE)
        with paths.cash_events_path(_LIVE).open("a", encoding="utf-8") as fh:
            fh.write("not json at all\n")

        record = sweep.apply_sweep(_plan(), now=_NOW)
        self.assertEqual(record.malformed, {"cash_events": 1, "trade_rows": 0})

    def test_the_count_survives_onto_the_sweeps_journal(self) -> None:
        """Through the codec and back: the number a later reader actually gets
        is the one on disk, not the one the call returned."""
        self._corrupt_one_line_of_each_keyed_journal()
        sweep.apply_sweep(_plan(), now=_NOW)

        (stored,) = store.read_sweeps(env=_LIVE)
        self.assertEqual(stored.malformed, {"cash_events": 1, "trade_rows": 1})


class TheAppendOrderIsLoadBearing(IsolatedHomeTestCase):
    """The keyed journals first, the sweep record LAST. Reordering leaves a
    sweep record on disk claiming rows that are not, which is what makes a
    retry of ``queue_write_failed`` unsafe."""

    def test_the_keyed_journals_are_appended_before_the_sweep_record(self) -> None:
        order: list[str] = []

        def record_call(name: str, real):
            def wrapper(*args: Any, **kwargs: Any):
                order.append(name)
                return real(*args, **kwargs)

            return wrapper

        with (
            mock.patch.object(
                store, "append_cash_events", record_call("cash_events", store.append_cash_events)
            ),
            mock.patch.object(
                store, "append_trade_rows", record_call("trade_rows", store.append_trade_rows)
            ),
            mock.patch.object(
                store, "append_balance", record_call("balances", store.append_balance)
            ),
            mock.patch.object(store, "append_sweep", record_call("sweeps", store.append_sweep)),
        ):
            sweep.apply_sweep(_plan(), now=_NOW)

        self.assertEqual(order, ["cash_events", "trade_rows", "balances", "sweeps"])


class TheFourAppendsHappenWhileTheLockIsHeld(IsolatedHomeTestCase):
    """``sweep.py`` states the lock is "held across all four appends", and that
    -- not merely taking it -- is what stops two sweeps interleaving.

    :class:`TheLockKeepsTwoSweepsApart` asserts that a sweep REFUSES while
    someone else holds the lock and that the lock is released afterwards. Both
    stay green when the ``with`` block is emptied to ``pass`` and the four
    appends moved below it, because nothing in them observes WHEN the appends
    happen. So this one asks the question from inside each appender.
    """

    def _lock_is_held_by_someone(self) -> bool:
        """Whether a SECOND descriptor on the lock file is refused right now.

        Same-process contention is observable on this platform: a second
        descriptor on the same path with ``LOCK_EX | LOCK_NB`` raises
        ``BlockingIOError`` (errno 35), so no second process is needed.
        """
        lock_path = paths.sweep_lock_path(_LIVE)
        if not lock_path.exists():
            return False
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
        finally:
            os.close(fd)

    def _probe_each_appender(self) -> dict[str, bool]:
        held: dict[str, bool] = {}

        def probing(name: str, real):
            def wrapper(*args: Any, **kwargs: Any):
                held[name] = self._lock_is_held_by_someone()
                return real(*args, **kwargs)

            return wrapper

        with (
            mock.patch.object(
                store, "append_cash_events", probing("cash_events", store.append_cash_events)
            ),
            mock.patch.object(
                store, "append_trade_rows", probing("trade_rows", store.append_trade_rows)
            ),
            mock.patch.object(store, "append_balance", probing("balances", store.append_balance)),
            mock.patch.object(store, "append_sweep", probing("sweeps", store.append_sweep)),
        ):
            sweep.apply_sweep(_plan(), now=_NOW)
        return held

    def test_every_one_of_the_four_appends_runs_under_the_lock(self) -> None:
        self.assertEqual(
            self._probe_each_appender(),
            {"cash_events": True, "trade_rows": True, "balances": True, "sweeps": True},
        )

    def test_the_probe_answers_false_once_the_sweep_has_finished(self) -> None:
        """The negative control. Without it the test above passes for a probe
        that can only ever answer ``True`` -- and it is the same observation
        that says the lock was released, from the outside this time."""
        sweep.apply_sweep(_plan(), now=_NOW)
        self.assertTrue(paths.sweep_lock_path(_LIVE).exists())
        self.assertFalse(self._lock_is_held_by_someone())


class ARetryAfterAPartialSweepLeavesOneRecordPerSweep(IsolatedHomeTestCase):
    def test_a_crash_before_the_sweep_record_is_closed_by_a_re_sweep(self) -> None:
        """``balances.jsonl`` and ``sweeps.jsonl`` both key on ``sweep_id``, so
        re-running the SAME sweep folds onto one record of each instead of
        two. That is what makes the retry safe across four non-atomic
        appends."""
        with mock.patch.object(store, "append_sweep", side_effect=JournalWriteError("disk full")):
            with self.assertRaises(JournalWriteError):
                sweep.apply_sweep(_plan(), now=_NOW)

        self.assertEqual(store.read_sweeps(env=_LIVE), [])
        self.assertEqual(len(store.read_balances(env=_LIVE)), 1)

        sweep.apply_sweep(_plan(), now=_NOW)

        self.assertEqual(len(store.read_sweeps(env=_LIVE)), 1)
        self.assertEqual(len(store.read_balances(env=_LIVE)), 1)
        self.assertEqual(len(store.read_cash_events(env=_LIVE).events), len(_EVENTS))

    def test_the_balance_journal_holds_two_lines_and_one_record(self) -> None:
        with mock.patch.object(store, "append_sweep", side_effect=JournalWriteError("disk full")):
            with self.assertRaises(JournalWriteError):
                sweep.apply_sweep(_plan(), now=_NOW)
        sweep.apply_sweep(_plan(), now=_NOW)

        lines = paths.balances_path(_LIVE).read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(len(store.read_balances(env=_LIVE)), 1)


class OnSimTheSweepRefusesAndWritesNothing(IsolatedHomeTestCase):
    def test_a_sim_read_is_planned_as_refused(self) -> None:
        plan = _plan(_sim_read(), env=_SIM)
        self.assertEqual(plan.refused_reason, "sim_reports_unusable")
        self.assertEqual(plan.new_events, ())
        self.assertEqual(plan.new_trade_rows, ())

    def test_applying_a_refused_plan_raises_and_every_journal_stays_absent(self) -> None:
        plan = _plan(_sim_read(), env=_SIM)
        with self.assertRaises(sweep.SweepRefusedError) as ctx:
            sweep.apply_sweep(plan, now=_NOW)
        self.assertEqual(ctx.exception.reason, "sim_reports_unusable")
        self.assertFalse(paths.account_cash_root(_SIM).exists())

    def test_a_sim_environment_is_refused_even_when_the_read_came_back_full(self) -> None:
        """The two halves are separate rules: this one is the WRITE rule, and
        it must hold on its own in case a read ever answers rows on SIM."""
        plan = _plan(_read(), env=_SIM)
        self.assertIsNone(plan.refused_reason)
        with self.assertRaises(store.SimCashBookRefusedError):
            sweep.apply_sweep(plan, now=_NOW)
        self.assertFalse(paths.account_cash_root(_SIM).exists())

    def test_a_refused_plan_on_live_is_refused_too(self) -> None:
        """A read that answered nothing has nothing storable, whatever the
        environment: a sweep record with no rows reads as a swept window."""
        plan = _plan(_sim_read(), env=_LIVE)
        with self.assertRaises(sweep.SweepRefusedError):
            sweep.apply_sweep(plan, now=_NOW)
        self.assertFalse(paths.account_cash_root(_LIVE).exists())


class TheLockKeepsTwoSweepsApart(IsolatedHomeTestCase):
    """Same-process contention is observable on this platform: a second
    descriptor on the same path with ``LOCK_EX | LOCK_NB`` raises
    ``BlockingIOError`` (errno 35), so no second process is needed."""

    def _hold_the_lock(self) -> int:
        lock_path = paths.sweep_lock_path(_LIVE)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd

    def test_a_second_concurrent_sweep_refuses_with_the_lock_named(self) -> None:
        self._hold_the_lock()
        clock = _FakeClock()
        slept: list[float] = []

        with self.assertRaises(sweep.SweepBusyError) as ctx:
            sweep.apply_sweep(_plan(), now=_NOW, clock=clock, sleep=slept.append)

        self.assertEqual(str(paths.sweep_lock_path(_LIVE)), ctx.exception.lock)
        self.assertGreaterEqual(clock.readings, 2, "the bounded spin never read its clock")

    def test_nothing_is_appended_while_another_sweep_holds_the_lock(self) -> None:
        self._hold_the_lock()
        before = _snapshot(paths.account_cash_root(_LIVE))

        with self.assertRaises(sweep.SweepBusyError):
            sweep.apply_sweep(_plan(), now=_NOW, clock=_FakeClock(), sleep=lambda _s: None)

        self.assertEqual(_snapshot(paths.account_cash_root(_LIVE)), before)
        self.assertEqual(store.read_cash_events(env=_LIVE).events, [])

    def test_the_lock_is_released_when_the_sweep_finishes(self) -> None:
        """Otherwise the FIRST sweep of a process poisons every later one, and
        the test above would pass for the wrong reason."""
        sweep.apply_sweep(_plan(), now=_NOW)
        sweep.apply_sweep(_plan(), now=_NOW + dt.timedelta(days=1))
        self.assertEqual(len(store.read_sweeps(env=_LIVE)), 2)

    def test_the_lock_is_released_after_a_failed_append(self) -> None:
        with mock.patch.object(store, "append_sweep", side_effect=JournalWriteError("disk full")):
            with self.assertRaises(JournalWriteError):
                sweep.apply_sweep(_plan(), now=_NOW)

        sweep.apply_sweep(_plan(), now=_NOW)
        self.assertEqual(len(store.read_sweeps(env=_LIVE)), 1)

    def test_the_lock_file_is_not_one_of_the_journals(self) -> None:
        sweep.apply_sweep(_plan(), now=_NOW)
        self.assertTrue(paths.sweep_lock_path(_LIVE).exists())
        self.assertEqual(paths.sweep_lock_path(_LIVE).suffix, ".lock")


class TheSweepModuleIsPureOfTheCli(unittest.TestCase):
    def test_it_imports_no_typer(self) -> None:
        """The sweep is the thing a command drives, never the other way round;
        a ``typer`` import here would also put the CLI's option objects on the
        import path of every store reader."""
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(sweep))
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
        self.assertNotIn("typer", imported)
        self.assertFalse([name for name in imported if name.startswith("alphalens_cli")])

    def test_the_record_type_is_the_codecs_own(self) -> None:
        """One SweepRecord, so what the sweep returns is what the store writes."""
        self.assertIs(sweep.SweepRecord, codec.SweepRecord)


if __name__ == "__main__":
    unittest.main()
