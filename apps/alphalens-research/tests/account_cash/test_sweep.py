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
