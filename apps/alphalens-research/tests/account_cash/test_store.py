"""The cash book's store: append-only, folded by key, SIM refused (#1689).

Three properties the tests here exist to hold.

**Assert on the FOLD, never on file lines.** The journal is append-only, so an
idempotent re-append leaves TWO lines and ONE folded record, and a line count
says nothing about what the store holds.

**A malformed line is counted, never dropped.** That is ``read_pick_fold``'s own
rule: a reader must be able to say how much of the journal it could not account
for, and a silent ``continue`` hides corruption in a money store.

**On SIM nothing is written at all.** The SIM report endpoints answer with
canned rows belonging to OTHER accounts, and memo section 3 calls storing
fiction in a money store worse than storing nothing. The refusal is on the
WRITE side and over EVERY appender -- not on a nullable field a caller passes
in, and not on one appender with the other three reaching the path builder
directly.

Every test here runs under :class:`IsolatedHomeTestCase`: these ones APPEND, so
an unisolated run would write into the operator's own cash book.
"""

from __future__ import annotations

import ast
import datetime as dt
import inspect
import json
import unittest
from dataclasses import replace
from typing import Any

from alphalens_pipeline.account_cash import codec, paths, store
from alphalens_pipeline.brokers.account_cash import BalanceSnapshot, CashEvent, TradeRow
from alphalens_pipeline.brokers.fill_history import ReadWindow
from alphalens_pipeline.brokers.journal import JournalWriteError

from tests.brokers.automanager.home_isolation import IsolatedHomeTestCase

_LIVE = "live"
_SIM = "sim"


def _event(bk_amount_id: str, **overrides: Any) -> CashEvent:
    base = CashEvent(
        bk_amount_id=bk_amount_id,
        bk_amount_type="Cash Amount",
        amount_class="NotAssigned",
        amount=-13.78,
        currency="USD",
        amount_account_currency=-51.49,
        account_currency="PLN",
        conversion_rate=3.73625234,
        conversion_cost_account_currency=None,
        date="2026-08-10",
        value_date="2026-08-11",
        uic=486,
        instrument_symbol="F:xnys",
        related_trade_id="6824247527",
        ca_event_name=None,
    )
    return replace(base, **overrides)


def _trade(trade_id: str, **overrides: Any) -> TradeRow:
    base = TradeRow(
        trade_id=trade_id,
        order_id="5432611806",
        uic=486,
        instrument_symbol="F:xnys",
        amount=1.0,
        price=13.78,
        trade_event_type="Bought",
        to_open_or_close="ToOpen",
        trade_date="2026-08-10",
        value_date="2026-08-11",
        execution_time=dt.datetime(2026, 8, 10, 13, 58, 40, 720000, tzinfo=dt.UTC),
        booked_amount_account_currency=-51.49,
        account_currency="PLN",
        venue="Exchange",
    )
    return replace(base, **overrides)


def _balance(**overrides: Any) -> BalanceSnapshot:
    base = BalanceSnapshot(
        currency="PLN",
        cash_balance=20738.68,
        non_margin_positions_value=3422.56,
        total_value=24161.24,
        transactions_not_booked=0.0,
        calculation_reliability="Ok",
        asof=dt.datetime(2026, 10, 9, 0, 31, tzinfo=dt.UTC),
    )
    return replace(base, **overrides)


def _sweep(sweep_id: str = "2026-10-09T00:31:00+00:00", **overrides: Any) -> codec.SweepRecord:
    base = codec.SweepRecord(
        sweep_id=sweep_id,
        swept_at=dt.datetime(2026, 10, 9, 0, 31, tzinfo=dt.UTC),
        env=_LIVE,
        since="2026-08-01",
        until="2026-10-03",
        events_window=ReadWindow("2026-08-01", "2026-10-03", 1),
        trades_window=ReadWindow("2026-08-01", "2026-10-03", 1),
        appended={"cash_events": 1},
        already_present={"cash_events": 0},
        restated={"cash_events": 0},
        malformed={"cash_events": 0},
        refused_reason=None,
    )
    return replace(base, **overrides)


def _store_ast() -> ast.Module:
    return ast.parse(inspect.getsource(store))


def _public_appenders() -> list[str]:
    """Every public ``append_*`` function declared in ``store.py``."""
    return sorted(
        node.name
        for node in _store_ast().body
        if isinstance(node, ast.FunctionDef) and node.name.startswith("append_")
    )


#: How to call each appender with one row. The KEYS are asserted against
#: :func:`_public_appenders` below, so an appender added without a SIM-refusal
#: case here turns that assertion red rather than shipping unguarded.
_APPENDER_CALLS = {
    "append_cash_events": lambda env: store.append_cash_events([_event("1")], env=env),
    "append_trade_rows": lambda env: store.append_trade_rows([_trade("1")], env=env),
    "append_balance": lambda env: store.append_balance(_balance(), env=env, sweep_id="s1"),
    "append_sweep": lambda env: store.append_sweep(_sweep(), env=env),
}


class EveryAppenderRefusesANonLiveEnvironment(IsolatedHomeTestCase):
    """Amendment A5. Naming ONE appender was measured as insufficient: a variant
    where the other three called ``paths.account_cash_root(env)`` directly wrote
    SIM rows with the planned tests green, which is exactly the shape memo
    section 3 calls worse than storing nothing."""

    def test_the_case_list_covers_every_appender_the_source_declares(self) -> None:
        found = _public_appenders()
        self.assertGreaterEqual(len(found), 4, found)
        self.assertEqual(found, sorted(_APPENDER_CALLS))

    def test_a_write_to_a_non_live_environment_is_refused_by_every_appender(self) -> None:
        import shutil

        for name, call in sorted(_APPENDER_CALLS.items()):
            # The root is cleared BETWEEN subTests so each one reports only its
            # own appender: without this, the first appender that wrote a SIM
            # root would fail the existence assertion of every later one too,
            # and the report would not say which appender is broken.
            shutil.rmtree(paths.account_cash_root(_SIM), ignore_errors=True)
            with self.subTest(appender=name):
                with self.assertRaises(store.SimCashBookRefusedError):
                    call(_SIM)
                self.assertFalse(
                    paths.account_cash_root(_SIM).exists(),
                    f"{name} created the SIM root before refusing",
                )

    def test_the_refusal_names_its_reason_and_the_environment(self) -> None:
        with self.assertRaises(store.SimCashBookRefusedError) as ctx:
            store.append_cash_events([_event("1")], env=_SIM)
        self.assertEqual(ctx.exception.reason, store.SIM_STORE_REFUSED)
        self.assertIn(_SIM, str(ctx.exception))

    def test_the_published_reason_is_the_literal_the_cli_will_publish(self) -> None:
        """Written out, not read off the constant under test.

        The assertion above derives its expectation from
        ``store.SIM_STORE_REFUSED`` itself, so the string was free to change to
        anything at all. B2 puts it in ``details.reason``, where it becomes a
        client-visible contract value a shell caller branches on.
        """
        self.assertEqual(store.SIM_STORE_REFUSED, "sim_store_refused")

    def test_the_write_refusal_is_not_the_read_sides_reason(self) -> None:
        """Two different facts that travel together on SIM: one says the
        vendor's SIM reports answer with other accounts' rows, the other says a
        write was aimed at a non-live store. Sharing a string would make them
        indistinguishable to a caller that branches on it."""
        from alphalens_pipeline.brokers.account_cash import SIM_REPORTS_UNUSABLE

        self.assertNotEqual(store.SIM_STORE_REFUSED, SIM_REPORTS_UNUSABLE)

    def test_a_non_live_write_is_refused_before_the_rows_are_even_rendered(self) -> None:
        """Which guard answers a SIM append of an unrenderable row.

        The environment refusal is the OUTER gate: the codec's complaint about
        this batch is about rows that were never going to be written anywhere,
        and answering it would publish ``CashBookDecodeError`` for what is a
        refusal to write to SIM at all. Nothing pinned the order -- the A5 loop
        uses finite rows and the A4 cases use LIVE, so the two guards never met.
        """
        with self.assertRaises(store.SimCashBookRefusedError):
            store.append_cash_events([_event("1", amount=float("nan"))], env=_SIM)
        self.assertFalse(paths.account_cash_root(_SIM).exists())

    def test_an_unknown_environment_is_refused_as_a_value_error(self) -> None:
        """Not a SIM refusal: an unknown env is a caller defect, and answering
        the SIM reason for it would publish a wrong reason to the CLI."""
        with self.assertRaises(ValueError):
            store.append_cash_events([_event("1")], env="prod")

    def test_no_appender_accepts_a_path(self) -> None:
        """There must be no argument a caller can pass that routes around the
        refusal. Paired with the behavioural loop above: this one alone cannot
        see whether an appender consults the write guard at all."""
        appenders = [
            node
            for node in _store_ast().body
            if isinstance(node, ast.FunctionDef) and node.name.startswith("append_")
        ]
        self.assertGreaterEqual(len(appenders), 4)
        for node in appenders:
            args = [*node.args.args, *node.args.posonlyargs, *node.args.kwonlyargs]
            with self.subTest(appender=node.name):
                self.assertNotIn("path", [arg.arg for arg in args])
                annotations = [
                    getattr(arg.annotation, "id", "") for arg in args if arg.annotation is not None
                ]
                self.assertNotIn("Path", annotations)


class TheAppenderWritesThroughTheSharedJournalWriter(IsolatedHomeTestCase):
    def test_no_default_serialiser_is_passed_to_the_appender(self) -> None:
        """``default=str`` would turn an unrepresentable money value into a
        quoted string on disk instead of refusing it. The floor comes first: a
        test that asserts the ABSENCE of a keyword passes vacuously over a
        ``store.py`` that opened the file itself."""
        calls = [
            node
            for node in ast.walk(_store_ast())
            if isinstance(node, ast.Call)
            and getattr(node.func, "id", getattr(node.func, "attr", "")) == "append_json_line"
        ]
        self.assertGreaterEqual(len(calls), 4, "the append_json_line scan found too little")
        for call in calls:
            with self.subTest(line=call.lineno):
                self.assertEqual([kw.arg for kw in call.keywords], [])

    def test_the_appender_creates_the_env_directory_itself(self) -> None:
        self.assertFalse(paths.account_cash_root(_LIVE).exists())
        store.append_cash_events([_event("1")], env=_LIVE)
        self.assertTrue(paths.cash_events_path(_LIVE).is_file())

    def test_a_failed_append_raises_journal_write_error(self) -> None:
        """An unwritable store directory must not report a sweep that wrote
        nothing as done. ``JournalWriteError`` subclasses ``OSError``."""
        parent = paths.account_cash_root(_LIVE).parent
        parent.mkdir(parents=True)
        parent.chmod(0o500)
        self.addCleanup(parent.chmod, 0o700)
        with self.assertRaises(JournalWriteError):
            store.append_cash_events([_event("1")], env=_LIVE)

    def test_an_empty_batch_writes_no_file_at_all(self) -> None:
        store.append_cash_events([], env=_LIVE)
        self.assertFalse(paths.cash_events_path(_LIVE).exists())


class ANonFiniteMoneyValueNeverReachesTheJournal(IsolatedHomeTestCase):
    """Amendment A4: the refusal is tested on the APPEND PATH, not only on the
    codec functions.

    An ``append_cash_events`` that rendered with ``dataclasses.asdict`` instead
    of the codec writes a literal ``NaN`` token and passes every codec-only
    test. The row is then permanently unreadable: the fold counts it malformed
    and the booking silently vanishes from every later read.
    """

    def test_an_append_of_a_non_finite_amount_refuses_and_writes_nothing(self) -> None:
        with self.assertRaises(codec.CashBookDecodeError):
            store.append_cash_events([_event("1", amount_account_currency=float("nan"))], env=_LIVE)
        self.assertFalse(paths.cash_events_path(_LIVE).exists())

    def test_the_whole_batch_is_refused_before_any_row_is_written(self) -> None:
        """Rendering happens before the first append, so a bad row late in a
        batch does not leave the earlier ones half-stored."""
        with self.assertRaises(codec.CashBookDecodeError):
            store.append_cash_events([_event("1"), _event("2", amount=float("inf"))], env=_LIVE)
        self.assertFalse(paths.cash_events_path(_LIVE).exists())

    def test_a_non_finite_trade_amount_refuses_on_the_append_path_too(self) -> None:
        with self.assertRaises(codec.CashBookDecodeError):
            store.append_trade_rows(
                [_trade("1", booked_amount_account_currency=float("nan"))], env=_LIVE
            )
        self.assertFalse(paths.trade_rows_path(_LIVE).exists())

    def test_a_non_finite_balance_refuses_on_the_append_path_too(self) -> None:
        with self.assertRaises(codec.CashBookDecodeError):
            store.append_balance(_balance(cash_balance=float("nan")), env=_LIVE, sweep_id="s1")
        self.assertFalse(paths.balances_path(_LIVE).exists())

    def test_no_nan_token_is_ever_on_disk(self) -> None:
        """The failure this guards is a FILE fact, so it is asserted on the
        file: ``json.dumps({'a': float('nan')})`` answers ``'{"a": NaN}'``."""
        store.append_cash_events([_event("1")], env=_LIVE)
        self.assertNotIn("NaN", paths.cash_events_path(_LIVE).read_text(encoding="utf-8"))


class TheCashEventFold(IsolatedHomeTestCase):
    def test_a_missing_journal_folds_empty(self) -> None:
        fold = store.read_cash_events(env=_LIVE)
        self.assertEqual((fold.events, fold.malformed, fold.restated, fold.lines), ([], 0, 0, 0))

    def test_a_malformed_line_is_counted_not_dropped(self) -> None:
        store.append_cash_events([_event("1")], env=_LIVE)
        with paths.cash_events_path(_LIVE).open("a", encoding="utf-8") as fh:
            fh.write("not json at all\n")
        store.append_cash_events([_event("2")], env=_LIVE)

        fold = store.read_cash_events(env=_LIVE)
        self.assertEqual(fold.malformed, 1)
        self.assertEqual([event.bk_amount_id for event in fold.events], ["1", "2"])
        self.assertEqual(fold.lines, 3)

    def test_a_line_whose_payload_the_codec_refuses_is_malformed_not_a_crash(self) -> None:
        """A hand-edited ``NaN`` is the reachable non-finite case, and a read
        must report it rather than raise out of a fold."""
        store.append_cash_events([_event("1")], env=_LIVE)
        rendered = codec.cash_event_to_jsonable(_event("2"))
        with paths.cash_events_path(_LIVE).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({**rendered, "amount": float("nan")}) + "\n")

        fold = store.read_cash_events(env=_LIVE)
        self.assertEqual(fold.malformed, 1)
        self.assertEqual([event.bk_amount_id for event in fold.events], ["1"])

    def test_a_blank_line_is_not_malformed(self) -> None:
        """A trailing newline is ordinary in an append-only file, and counting
        it as corruption would make every torn-line repair read as damage."""
        store.append_cash_events([_event("1")], env=_LIVE)
        with paths.cash_events_path(_LIVE).open("a", encoding="utf-8") as fh:
            fh.write("\n   \n")

        fold = store.read_cash_events(env=_LIVE)
        self.assertEqual(fold.malformed, 0)
        self.assertEqual(fold.lines, 1)

    def test_the_latest_line_wins_per_bk_amount_id_and_counts_as_restated(self) -> None:
        """The one shape memo section 9 names as the fold's blind spot: a
        corrected amount on a known id. ``restated`` staying 0 is the failure
        the whole store exists to detect."""
        store.append_cash_events([_event("1", amount_account_currency=-51.49)], env=_LIVE)
        store.append_cash_events([_event("1", amount_account_currency=-61.49)], env=_LIVE)

        fold = store.read_cash_events(env=_LIVE)
        self.assertEqual(len(fold.events), 1)
        self.assertEqual(fold.events[0].amount_account_currency, -61.49)
        self.assertEqual(fold.restated, 1)
        self.assertEqual(fold.lines, 2)

    def test_a_restatement_that_also_corrects_the_date_is_still_one_record(self) -> None:
        """The fold key is the ``BkAmountId`` ALONE, and nothing else.

        Every other restatement case here holds ``date`` fixed, so a key that
        silently gained a second field -- ``bk_amount_id + (date or "")`` is
        the shape -- folds them all identically. A value-date correction is a
        realistic vendor restatement, and under such a key the SAME booking
        would fold as two records: the money double counted, and ``restated``
        reporting 0 while it happened.
        """
        store.append_cash_events([_event("1", date="2026-08-10")], env=_LIVE)
        store.append_cash_events(
            [_event("1", date="2026-08-11", amount_account_currency=-61.49)], env=_LIVE
        )

        fold = store.read_cash_events(env=_LIVE)
        self.assertEqual(len(fold.events), 1)
        self.assertEqual(fold.events[0].date, "2026-08-11")
        self.assertEqual(fold.events[0].amount_account_currency, -61.49)
        self.assertEqual(fold.restated, 1)

    def test_an_identical_re_append_is_one_record_two_lines_and_no_restatement(self) -> None:
        """Assert on the FOLD, never on ``wc -l``: the append-only form makes a
        line count meaningless. And an identical re-append is not a correction
        -- counting it as one would make ``restated`` useless as a signal."""
        store.append_cash_events([_event("1")], env=_LIVE)
        store.append_cash_events([_event("1")], env=_LIVE)

        fold = store.read_cash_events(env=_LIVE)
        self.assertEqual(len(fold.events), 1)
        self.assertEqual(fold.lines, 2)
        self.assertEqual(fold.restated, 0)

    def test_restated_counts_keys_and_not_restatement_events(self) -> None:
        """``CashEventFold.restated`` documents "keys whose payload CHANGED".

        No case restated the same key twice, so counting KEYS and counting
        OCCURRENCES were indistinguishable -- swapping the set for a list left
        every test green. It is the number a later reader compares against
        ``lines - len(events)``, and under the occurrence reading a single
        booking corrected twice reports two corrected bookings.
        """
        for amount in (-51.49, -61.49, -71.49):
            store.append_cash_events([_event("1", amount_account_currency=amount)], env=_LIVE)

        fold = store.read_cash_events(env=_LIVE)
        self.assertEqual(fold.restated, 1)
        self.assertEqual(fold.lines, 3)
        self.assertEqual(len(fold.events), 1)
        self.assertEqual(fold.events[0].amount_account_currency, -71.49)

    def test_two_keys_each_restated_once_count_as_two(self) -> None:
        """The other direction: a count that collapsed to a flag would satisfy
        the test above."""
        store.append_cash_events([_event("1"), _event("2")], env=_LIVE)
        store.append_cash_events([_event("1", amount=-1.0), _event("2", amount=-2.0)], env=_LIVE)
        self.assertEqual(store.read_cash_events(env=_LIVE).restated, 2)

    def test_the_published_order_is_by_date_then_id(self) -> None:
        """Sorted EXPLICITLY. ``read_pick_fold``'s own order is first-seen key
        order carrying the last value, which is the wrong order for a cash
        book."""
        store.append_cash_events(
            [
                _event("b", date="2026-09-01"),
                _event("a", date="2026-10-01"),
                _event("c", date="2026-09-01"),
            ],
            env=_LIVE,
        )
        fold = store.read_cash_events(env=_LIVE)
        self.assertEqual([event.bk_amount_id for event in fold.events], ["b", "c", "a"])

    def test_a_row_with_no_date_sorts_without_raising(self) -> None:
        """``date`` is optional on the vendor row, so the sort key must not
        compare ``None`` with a string -- which raises ``TypeError``."""
        store.append_cash_events(
            [_event("b", date=None), _event("a", date="2026-09-01")], env=_LIVE
        )
        self.assertEqual(
            [event.bk_amount_id for event in store.read_cash_events(env=_LIVE).events],
            ["b", "a"],
        )


class TheTradeRowFold(IsolatedHomeTestCase):
    def test_a_missing_journal_folds_empty(self) -> None:
        fold = store.read_trade_rows(env=_LIVE)
        self.assertEqual((fold.rows, fold.malformed, fold.restated, fold.lines), ([], 0, 0, 0))

    def test_the_latest_line_wins_per_trade_id_and_counts_as_restated(self) -> None:
        store.append_trade_rows([_trade("1", price=13.78)], env=_LIVE)
        store.append_trade_rows([_trade("1", price=13.79)], env=_LIVE)

        fold = store.read_trade_rows(env=_LIVE)
        self.assertEqual(len(fold.rows), 1)
        self.assertEqual(fold.rows[0].price, 13.79)
        self.assertEqual(fold.restated, 1)
        self.assertEqual(fold.lines, 2)

    def test_a_restatement_that_also_corrects_the_trade_date_is_still_one_record(self) -> None:
        """The trades-side half of the same rule: the key is ``TradeId`` alone."""
        store.append_trade_rows([_trade("1", trade_date="2026-08-10")], env=_LIVE)
        store.append_trade_rows([_trade("1", trade_date="2026-08-11", price=13.79)], env=_LIVE)

        fold = store.read_trade_rows(env=_LIVE)
        self.assertEqual(len(fold.rows), 1)
        self.assertEqual(fold.rows[0].trade_date, "2026-08-11")
        self.assertEqual(fold.restated, 1)

    def test_the_published_order_is_by_trade_date_then_id(self) -> None:
        store.append_trade_rows(
            [
                _trade("b", trade_date="2026-09-01"),
                _trade("a", trade_date="2026-10-01"),
            ],
            env=_LIVE,
        )
        self.assertEqual(
            [row.trade_id for row in store.read_trade_rows(env=_LIVE).rows], ["b", "a"]
        )

    def test_the_two_keyed_journals_are_separate_files(self) -> None:
        store.append_cash_events([_event("1")], env=_LIVE)
        store.append_trade_rows([_trade("1")], env=_LIVE)
        self.assertEqual(len(store.read_cash_events(env=_LIVE).events), 1)
        self.assertEqual(len(store.read_trade_rows(env=_LIVE).rows), 1)
        self.assertNotEqual(paths.cash_events_path(_LIVE), paths.trade_rows_path(_LIVE))


class TheBalanceAndSweepJournalsDedupeOnTheSweepId(IsolatedHomeTestCase):
    def test_a_missing_journal_reads_empty(self) -> None:
        self.assertEqual(store.read_balances(env=_LIVE), [])
        self.assertEqual(store.read_sweeps(env=_LIVE), [])

    def test_one_balance_and_one_sweep_record_per_sweep_id(self) -> None:
        """A crash between the two appends leaves a window a re-sweep closes
        with ONE record per sweep rather than two -- which is what makes the
        retry of a partly-written sweep safe."""
        store.append_balance(_balance(), env=_LIVE, sweep_id="s1")
        store.append_sweep(_sweep("s1"), env=_LIVE)
        store.append_balance(_balance(), env=_LIVE, sweep_id="s1")
        store.append_sweep(_sweep("s1"), env=_LIVE)

        self.assertEqual(len(store.read_balances(env=_LIVE)), 1)
        self.assertEqual(len(store.read_sweeps(env=_LIVE)), 1)
        self.assertEqual(
            len(paths.sweeps_path(_LIVE).read_text(encoding="utf-8").strip().splitlines()), 2
        )

    def test_two_sweeps_keep_both_records_in_order(self) -> None:
        later = dt.datetime(2026, 10, 10, 0, 31, tzinfo=dt.UTC)
        store.append_sweep(_sweep("s1"), env=_LIVE)
        store.append_sweep(_sweep("s2", swept_at=later), env=_LIVE)
        self.assertEqual([record.sweep_id for record in store.read_sweeps(env=_LIVE)], ["s1", "s2"])

    def test_three_balances_under_distinct_sweep_ids_are_published_oldest_first(self) -> None:
        """``read_balances`` documents "oldest first", and every other
        multi-balance case here puts both snapshots under ONE ``sweep_id`` --
        so the fold collapses them and a one-element list satisfies any
        ordering. The property was true and empty: reversing the sort was
        green. Appended out of order on purpose, since an append-ordered
        return would otherwise look sorted."""
        middle = dt.datetime(2026, 10, 9, 0, 31, tzinfo=dt.UTC)
        oldest = dt.datetime(2026, 10, 8, 0, 31, tzinfo=dt.UTC)
        newest = dt.datetime(2026, 10, 10, 0, 31, tzinfo=dt.UTC)
        store.append_balance(_balance(asof=middle), env=_LIVE, sweep_id="s2")
        store.append_balance(_balance(asof=newest), env=_LIVE, sweep_id="s3")
        store.append_balance(_balance(asof=oldest), env=_LIVE, sweep_id="s1")

        self.assertEqual(
            [snapshot.asof for snapshot in store.read_balances(env=_LIVE)],
            [oldest, middle, newest],
        )

    def test_two_balances_sharing_an_instant_are_ordered_by_sweep_id(self) -> None:
        """The tie-break, so the published order is total rather than
        whatever the fold's insertion order happened to be."""
        store.append_balance(_balance(), env=_LIVE, sweep_id="s2")
        store.append_balance(_balance(cash_balance=1.0), env=_LIVE, sweep_id="s1")
        self.assertEqual(
            [snapshot.cash_balance for snapshot in store.read_balances(env=_LIVE)],
            [1.0, 20738.68],
        )

    def test_a_restated_balance_for_one_sweep_keeps_the_latest(self) -> None:
        store.append_balance(_balance(cash_balance=1.0), env=_LIVE, sweep_id="s1")
        store.append_balance(_balance(cash_balance=2.0), env=_LIVE, sweep_id="s1")
        balances = store.read_balances(env=_LIVE)
        self.assertEqual(len(balances), 1)
        self.assertEqual(balances[0].cash_balance, 2.0)

    def test_an_absent_balance_money_field_survives_the_journal_as_null(self) -> None:
        """The append path's half of the balance's absent-versus-zero rule. The
        codec half is in ``test_codec.py``; this one is what actually reaches
        disk, and it reads the FILE because ``null`` versus ``0.0`` is a file
        fact that Phase 4's balance cross-check will parse."""
        empty = _balance(
            currency=None,
            cash_balance=None,
            non_margin_positions_value=None,
            total_value=None,
            transactions_not_booked=None,
            calculation_reliability=None,
        )
        store.append_balance(empty, env=_LIVE, sweep_id="s1")

        line = json.loads(paths.balances_path(_LIVE).read_text(encoding="utf-8").strip())
        for name in ("cash_balance", "non_margin_positions_value", "total_value"):
            with self.subTest(money_field=name):
                self.assertIsNone(line[name])
        # The field memo section 1.7 P5 calls out: it parks a whole day's
        # trading, so a null read as 0.0 reads as "nothing unbooked".
        self.assertIsNone(line["transactions_not_booked"])
        self.assertEqual(store.read_balances(env=_LIVE), [empty])

    def test_the_balance_line_carries_the_sweep_key_beside_the_snapshot(self) -> None:
        store.append_balance(_balance(), env=_LIVE, sweep_id="s1")
        line = json.loads(paths.balances_path(_LIVE).read_text(encoding="utf-8").strip())
        self.assertEqual(line["sweep_id"], "s1")
        self.assertIn("cash_balance", line)

    def test_a_malformed_balance_line_is_reported_not_a_crash(self) -> None:
        store.append_balance(_balance(), env=_LIVE, sweep_id="s1")
        with paths.balances_path(_LIVE).open("a", encoding="utf-8") as fh:
            fh.write("{not json\n")
        with self.assertLogs("alphalens_pipeline.account_cash.store", level="WARNING"):
            self.assertEqual(len(store.read_balances(env=_LIVE)), 1)


class ARestatementSurvivesEveryLaterRead(IsolatedHomeTestCase):
    def test_both_lines_stay_on_disk_and_the_fold_reports_the_correction(self) -> None:
        """The journal form exists for exactly this: a parquet overwrite
        replaces yesterday's number silently, the journal keeps both and says
        ``restated: 1``."""
        store.append_cash_events([_event("1", amount_account_currency=-51.49)], env=_LIVE)
        store.append_cash_events([_event("1", amount_account_currency=-61.49)], env=_LIVE)

        on_disk = paths.cash_events_path(_LIVE).read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(on_disk), 2)
        amounts = sorted(json.loads(line)["amount_account_currency"] for line in on_disk)
        self.assertEqual(amounts, [-61.49, -51.49])

        fold = store.read_cash_events(env=_LIVE)
        self.assertEqual(fold.restated, 1)
        self.assertEqual(len(fold.events), 1)


class AReadIsAllowedOnEveryEnvironment(IsolatedHomeTestCase):
    def test_reading_a_sim_journal_is_not_refused_and_creates_nothing(self) -> None:
        """The refusal is a WRITE rule. A read of an absent SIM journal folds
        empty, which is the honest answer, and it must not create the root."""
        self.assertEqual(store.read_cash_events(env=_SIM).events, [])
        self.assertFalse(paths.account_cash_root(_SIM).exists())

    def test_an_unknown_environment_is_refused_by_every_reader(self) -> None:
        """The refusal comes from the PATH BUILDER, not from a line in the
        reader: each reader's explicit ``validate_environment`` call was a
        duplicate of a check the builder runs unconditionally, so it could
        never be what refused anything and no test could tell it apart from
        nothing. The rule itself is what matters, so all four are asserted."""
        for reader in (
            store.read_cash_events,
            store.read_trade_rows,
            store.read_balances,
            store.read_sweeps,
        ):
            with self.subTest(reader=reader.__name__):
                with self.assertRaises(ValueError) as ctx:
                    reader(env="prod")
                self.assertIn("prod", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
