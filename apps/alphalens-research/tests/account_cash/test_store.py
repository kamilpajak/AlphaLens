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


class TheWriteGuardCanBeAskedWithoutWriting(IsolatedHomeTestCase):
    """``assert_writable`` exists so a CALLER can authorise before it plans.

    The appenders reach the guard on their way to a path, which is the right
    shape for a write but reaches nobody who is not about to write. The CLI's
    ``--dry-run`` writes nothing by construction, so it never touched an
    appender and therefore never authorised: it reported a success for a sweep
    the same invocation without ``--dry-run`` refuses (#1689 F1). Copying the
    live-only rule into the CLI instead would be one rule computed on two
    sides, which is the defect this repo has already paid for.
    """

    def test_a_non_live_environment_is_refused_with_the_appenders_own_reason(self) -> None:
        with self.assertRaises(store.SimCashBookRefusedError) as ctx:
            store.assert_writable(_SIM)
        self.assertEqual(ctx.exception.reason, store.SIM_STORE_REFUSED)
        self.assertEqual(ctx.exception.env, _SIM)

    def test_asking_about_sim_creates_nothing(self) -> None:
        """The root must not come into existence on SIM, and an authorisation
        question must not be the thing that creates it."""
        with self.assertRaises(store.SimCashBookRefusedError):
            store.assert_writable(_SIM)
        self.assertFalse(paths.account_cash_root(_SIM).exists())

    def test_live_is_allowed_and_asking_creates_nothing_either(self) -> None:
        self.assertIsNone(store.assert_writable(_LIVE))
        self.assertFalse(paths.account_cash_root(_LIVE).exists())

    def test_an_unknown_environment_is_a_value_error_and_not_the_sim_reason(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            store.assert_writable("prod")
        self.assertNotIsInstance(ctx.exception, store.SimCashBookRefusedError)

    def test_it_delegates_to_the_appenders_guard_rather_than_restating_it(self) -> None:
        """The whole reason it lives in this module: one function decides.

        Read off the SOURCE, because a second copy of ``!= ENV_LIVE`` here
        would behave identically today and drift the first time the rule
        changes.
        """
        node = next(
            item
            for item in _store_ast().body
            if isinstance(item, ast.FunctionDef) and item.name == "assert_writable"
        )
        called = {
            child.func.id
            for child in ast.walk(node)
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Name)
        }
        self.assertEqual(called, {"_writable_root"})


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


class AnUnreadableLineIsCountedAndNeverRaisedOutOfTheFold(IsolatedHomeTestCase):
    """The fold's stated rule is that it REPORTS a line it cannot read.

    ``TheCashEventFold`` covers the two shapes the fold already knew about: a
    line that is not JSON, and a line the codec refuses. Both arrive as a
    ``ValueError``, so the whole module read as if every unreadable line did --
    and a line that fails in some OTHER way escaped the fold instead of being
    counted. One such line anywhere in the journal then makes the WHOLE cash
    book unreadable, which is the opposite of the rule.

    One test per member of the named exception set, so the set cannot quietly
    shrink back to the narrower one it was.
    """

    def _seed_and_append_bytes(self, tail: bytes) -> None:
        store.append_cash_events([_event("1")], env=_LIVE)
        path = paths.cash_events_path(_LIVE)
        with path.open("ab") as fh:
            fh.write(tail)

    def test_an_unbounded_json_integer_amount_is_counted_not_raised(self) -> None:
        """``10**400`` is valid JSON and a valid Python ``int``, so it survives
        ``json.loads`` and every type check in the codec, then raises
        ``OverflowError`` at ``float(value)``. The same shape this repo has
        already paid for once, on ``validate_intent``.
        """
        rendered = codec.cash_event_to_jsonable(_event("2"))
        self._seed_and_append_bytes(
            (json.dumps({**rendered, "amount": 10**400}) + "\n").encode("utf-8")
        )

        fold = store.read_cash_events(env=_LIVE)
        self.assertEqual(fold.malformed, 1)
        self.assertEqual([event.bk_amount_id for event in fold.events], ["1"])
        self.assertEqual(fold.lines, 2)

    def test_the_account_currency_amount_overflows_the_same_way(self) -> None:
        """The field memo Phase 4 sums. Every money field runs through the same
        ``_finite``, so one case cannot tell whether the guard sits on the
        decode or on one key of it."""
        rendered = codec.cash_event_to_jsonable(_event("2"))
        self._seed_and_append_bytes(
            (json.dumps({**rendered, "amount_account_currency": 10**400}) + "\n").encode("utf-8")
        )
        self.assertEqual(store.read_cash_events(env=_LIVE).malformed, 1)

    def test_a_line_whose_bytes_are_not_utf8_is_counted_not_raised(self) -> None:
        """A line that ends mid-way through a multibyte sequence.

        This appender cannot write one itself -- ``json.dumps`` escapes every
        non-ASCII character by default, and a tear of ASCII bytes yields
        invalid JSON rather than invalid UTF-8. The reachable route is the one
        the hand-edited ``NaN`` case above already covers: an edit, or a restore
        from a copy whose bytes were mangled. The cost of not counting it is
        the same either way -- ONE undecodable byte makes every row of the cash
        book unreadable, because the decode happens while the reader iterates
        the file and not inside any guard.
        """
        rendered = codec.cash_event_to_jsonable(_event("2"))
        # Written as an escape, not as the character: a non-ASCII literal in a
        # source file is what `test_no_polish_chars.py` refuses, and the test
        # needs the BYTES rather than the glyph.
        euro_sign = "€"
        whole = json.dumps(
            {**rendered, "ca_event_name": f"fee {euro_sign}"}, ensure_ascii=False
        ).encode("utf-8")
        self.assertIn(b"\xe2\x82\xac", whole)
        # Cut on the LEAD byte of that three-byte sequence, so the line ends
        # with a sequence that has no continuation byte.
        torn = whole[: whole.index(b"\xe2") + 1]
        self.assertNotEqual(torn, whole)
        self._seed_and_append_bytes(torn)

        fold = store.read_cash_events(env=_LIVE)
        self.assertEqual(fold.malformed, 1)
        self.assertEqual([event.bk_amount_id for event in fold.events], ["1"])

    def test_an_unreadable_line_hides_none_of_the_readable_ones_after_it(self) -> None:
        """The cost the finding is about: not the counter, but the rows BELOW
        the bad line. A fold that raises reports nothing at all, so a reader
        of a money store loses every booking over one corrupt byte."""
        rendered = codec.cash_event_to_jsonable(_event("2"))
        self._seed_and_append_bytes(
            (json.dumps({**rendered, "amount": 10**400}) + "\n").encode("utf-8")
        )
        store.append_cash_events([_event("3")], env=_LIVE)

        fold = store.read_cash_events(env=_LIVE)
        self.assertEqual([event.bk_amount_id for event in fold.events], ["1", "3"])
        self.assertEqual((fold.malformed, fold.lines), (1, 3))

    def test_a_stamp_that_overflows_its_utc_conversion_is_counted_too(self) -> None:
        """A SECOND route to the same exception, in a different codec helper.

        ``0001-01-01T00:00:00+14:00`` parses, so ``fromisoformat``'s own
        ``ValueError`` guard never fires; the ``astimezone(UTC)`` on the next
        line raises ``OverflowError`` because the instant falls before
        ``datetime.min``. A guard put on ``_finite`` instead of on the fold
        would stop the money cases above and leave this one raising.

        Asserted on ``sweeps.jsonl`` as well, because ``_fold_by_key`` serves
        all four journals while only the two keyed ones publish a counter --
        so a guard at one reader's call site would leave the other two raising.
        """
        store.append_sweep(_sweep("s1"), env=_LIVE)
        rendered = codec.sweep_to_jsonable(_sweep("s2"))
        with paths.sweeps_path(_LIVE).open("ab") as fh:
            fh.write(
                (json.dumps({**rendered, "swept_at": "0001-01-01T00:00:00+14:00"}) + "\n").encode(
                    "utf-8"
                )
            )

        with self.assertLogs("alphalens_pipeline.account_cash.store", level="WARNING"):
            self.assertEqual([record.sweep_id for record in store.read_sweeps(env=_LIVE)], ["s1"])

    def test_the_fold_catches_named_exceptions_and_never_a_bare_exception(self) -> None:
        """The remedy must not become ``except Exception``.

        This store's whole value is that a problem announces itself, and a
        blanket handler would count a real defect in the codec as corruption on
        disk. A structural check because the behavioural ones above cannot see
        the difference: a blanket handler passes every one of them.
        """
        handlers = [node for node in ast.walk(_store_ast()) if isinstance(node, ast.ExceptHandler)]
        self.assertGreaterEqual(len(handlers), 2, "the except-handler scan found too little")
        for handler in handlers:
            with self.subTest(line=handler.lineno):
                self.assertIsNotNone(
                    handler.type, "a bare `except:` catches even a KeyboardInterrupt"
                )
                names = [node.id for node in ast.walk(handler.type) if isinstance(node, ast.Name)]
                self.assertNotIn("Exception", names)
                self.assertNotIn("BaseException", names)


class AnUnusableSweepKeyIsRefusedBeforeItReachesTheJournal(IsolatedHomeTestCase):
    """The write side is where the key is checked, because a value that cannot
    be read back must not reach the journal at all.

    ``_balance_line_from_jsonable`` refuses an empty or non-string ``sweep_id``
    on the READ side, and nothing refused one on the write side: the line was
    written, ``read_balances`` then answered zero rows with only a log line,
    and no counter reported it -- a row permanently unreadable, in the store
    whose claim is that storing a value it cannot read back is structurally
    impossible.

    The pair disagreed as well: ``append_sweep`` accepted an empty id and
    ``read_sweeps`` read it back, so the same id survived in one journal of
    the pair and was lost in the other, with both keyed on it.
    """

    #: Both appenders that take the key, and how to call each with one.
    _WITH_KEY = {
        "append_balance": lambda env, key: store.append_balance(_balance(), env=env, sweep_id=key),
        "append_sweep": lambda env, key: store.append_sweep(_sweep(key), env=env),
    }

    def test_an_empty_key_is_refused_by_both_appenders(self) -> None:
        for name, call in sorted(self._WITH_KEY.items()):
            with self.subTest(appender=name):
                with self.assertRaises(codec.CashBookDecodeError):
                    call(_LIVE, "")

    def test_a_non_string_key_is_refused_by_both_appenders(self) -> None:
        """``sweep_id: str`` is an annotation, not a runtime check, and the
        read side names the ``isinstance`` case explicitly -- so the write side
        has to answer the same two shapes or the pair disagrees again."""
        for name, call in sorted(self._WITH_KEY.items()):
            with self.subTest(appender=name):
                with self.assertRaises(codec.CashBookDecodeError):
                    call(_LIVE, 7)  # type: ignore[arg-type]

    def test_the_refused_key_leaves_no_line_on_disk(self) -> None:
        """The point of refusing on the write side rather than counting on the
        read side: the journal is append-only and never compacted, so a line
        written under an unusable key would stay unreadable forever."""
        with self.assertRaises(codec.CashBookDecodeError):
            store.append_balance(_balance(), env=_LIVE, sweep_id="")
        with self.assertRaises(codec.CashBookDecodeError):
            store.append_sweep(_sweep(""), env=_LIVE)

        self.assertFalse(paths.balances_path(_LIVE).exists())
        self.assertFalse(paths.sweeps_path(_LIVE).exists())

    def test_a_usable_key_still_writes_and_reads_back(self) -> None:
        """The positive control: a refusal that refused everything would
        satisfy every assertion above."""
        store.append_balance(_balance(), env=_LIVE, sweep_id="s1")
        store.append_sweep(_sweep("s1"), env=_LIVE)
        self.assertEqual(len(store.read_balances(env=_LIVE).snapshots), 1)
        self.assertEqual([record.sweep_id for record in store.read_sweeps(env=_LIVE)], ["s1"])

    def test_the_sim_refusal_stays_the_outer_gate(self) -> None:
        """A SIM write of an unusable key must answer the SIM reason: the key
        was never going to be written anywhere, and publishing
        ``CashBookDecodeError`` for it would name the wrong refusal."""
        with self.assertRaises(store.SimCashBookRefusedError):
            store.append_balance(_balance(), env=_SIM, sweep_id="")
        self.assertFalse(paths.account_cash_root(_SIM).exists())


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
        self.assertEqual(store.read_balances(env=_LIVE).snapshots, [])
        self.assertEqual(store.read_sweeps(env=_LIVE), [])

    def test_one_balance_and_one_sweep_record_per_sweep_id(self) -> None:
        """A crash between the two appends leaves a window a re-sweep closes
        with ONE record per sweep rather than two -- which is what makes the
        retry of a partly-written sweep safe."""
        store.append_balance(_balance(), env=_LIVE, sweep_id="s1")
        store.append_sweep(_sweep("s1"), env=_LIVE)
        store.append_balance(_balance(), env=_LIVE, sweep_id="s1")
        store.append_sweep(_sweep("s1"), env=_LIVE)

        self.assertEqual(len(store.read_balances(env=_LIVE).snapshots), 1)
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
            [at.snapshot.asof for at in store.read_balances(env=_LIVE).snapshots],
            [oldest, middle, newest],
        )

    def test_two_balances_sharing_an_instant_are_ordered_by_sweep_id(self) -> None:
        """The tie-break, so the published order is total rather than
        whatever the fold's insertion order happened to be."""
        store.append_balance(_balance(), env=_LIVE, sweep_id="s2")
        store.append_balance(_balance(cash_balance=1.0), env=_LIVE, sweep_id="s1")
        self.assertEqual(
            [at.snapshot.cash_balance for at in store.read_balances(env=_LIVE).snapshots],
            [1.0, 20738.68],
        )

    def test_a_restated_balance_for_one_sweep_keeps_the_latest(self) -> None:
        store.append_balance(_balance(cash_balance=1.0), env=_LIVE, sweep_id="s1")
        store.append_balance(_balance(cash_balance=2.0), env=_LIVE, sweep_id="s1")
        balances = store.read_balances(env=_LIVE).snapshots
        self.assertEqual(len(balances), 1)
        self.assertEqual(balances[0].snapshot.cash_balance, 2.0)

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
        self.assertEqual([at.snapshot for at in store.read_balances(env=_LIVE).snapshots], [empty])

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
            self.assertEqual(len(store.read_balances(env=_LIVE).snapshots), 1)

    def test_a_balance_line_whose_sweep_key_is_unusable_is_refused_on_read(self) -> None:
        """The READ side of the key rule, which the write-side guard hides.

        No appender can write such a line any more, so this is the only thing
        that can refuse one that is already on disk -- a hand edit, or a line
        written before the write-side guard landed. Without this test, deleting
        the read-side check leaves the whole package green: the key would then
        fold under ``None`` or ``''``, so two snapshots from two different
        sweeps would collapse onto one record, and ``read_balances`` would
        answer a balance that belongs to neither.

        The malformed line is valid JSON carrying a complete snapshot body, so
        only the key rule can reject it; the ``{not json`` case beside this one
        never reaches the key at all. The doctored line differs from the good
        one in the key ALONE, so the row count is the discriminator: refused it
        is one snapshot, folded under an unusable key it is two.

        Whitespace is deliberately absent from the cases. The rule is a
        non-empty string, and a key of spaces folds the same way on both sides,
        so asserting a refusal here would assert a rule the store does not
        have."""
        store.append_balance(_balance(), env=_LIVE, sweep_id="s1")
        good = json.loads(paths.balances_path(_LIVE).read_text(encoding="utf-8").strip())
        for unusable in ("", None, 7):
            with self.subTest(sweep_id=unusable):
                doctored = dict(good, sweep_id=unusable)
                paths.balances_path(_LIVE).write_text(
                    json.dumps(good) + "\n" + json.dumps(doctored) + "\n", encoding="utf-8"
                )
                with self.assertLogs(
                    "alphalens_pipeline.account_cash.store", level="WARNING"
                ) as caught:
                    rows = store.read_balances(env=_LIVE).snapshots
                self.assertEqual(len(rows), 1)
                self.assertIn("sweep_id", "\n".join(caught.output))


class TheBalanceFoldCarriesItsCountersAndTheSweepEachSnapshotBelongsTo(IsolatedHomeTestCase):
    """``read_balances`` used to throw both away (#1689, Phase 4).

    It returned ``list[BalanceSnapshot]``: the three counters the fold already
    computed were discarded into ``_, _, _``, and the ``sweep_id`` the journal
    is KEYED on was popped by the decode and dropped by the comprehension. Its
    own docstring named Phase 4 as the consumer that would give the counters a
    meaning, and the balance cross-check is that consumer -- so both halves are
    published here.

    Why the key matters, and not merely the snapshot: a cash event carries a
    trade DATE and no timestamp, while the settlement batch fires at about
    23:48 UTC (memo section 1.7 P5), so the interval between two snapshots is
    in TRADE DATES taken from the sweep record each one belongs to, never in
    ``asof`` instants. Without the key there is nothing to join a snapshot to
    its window on.
    """

    def test_a_missing_journal_folds_empty_with_zero_counters(self) -> None:
        fold = store.read_balances(env=_LIVE)
        self.assertEqual(fold.snapshots, [])
        self.assertEqual((fold.malformed, fold.restated, fold.lines), (0, 0, 0))

    def test_a_snapshot_carries_the_sweep_it_belongs_to(self) -> None:
        """**Mutation it catches:** dropping ``sweep_id`` again in the
        comprehension, which leaves the incremental check unable to find the
        trade-date interval its snapshots were read over."""
        older = dt.datetime(2026, 10, 8, 0, 31, tzinfo=dt.UTC)
        store.append_balance(_balance(asof=older), env=_LIVE, sweep_id="s1")
        store.append_balance(_balance(cash_balance=1.0), env=_LIVE, sweep_id="s2")

        fold = store.read_balances(env=_LIVE)
        self.assertEqual([at.sweep_id for at in fold.snapshots], ["s1", "s2"])
        self.assertEqual([at.snapshot.cash_balance for at in fold.snapshots], [20738.68, 1.0])

    def test_a_corrupt_balance_line_is_now_counted_and_not_only_logged(self) -> None:
        """**Mutation it catches:** the ``latest, _, _, _ =`` discard coming
        back, which makes a corrupt balance line invisible to the cross-check
        that is now its only consumer."""
        store.append_balance(_balance(), env=_LIVE, sweep_id="s1")
        with paths.balances_path(_LIVE).open("a", encoding="utf-8") as fh:
            fh.write("{not json\n")

        with self.assertLogs("alphalens_pipeline.account_cash.store", level="WARNING"):
            fold = store.read_balances(env=_LIVE)

        self.assertEqual(fold.malformed, 1)
        self.assertEqual(fold.lines, 2)
        self.assertEqual(len(fold.snapshots), 1)

    def test_a_restated_balance_is_counted_as_one_correction(self) -> None:
        """The counter the journal form exists for, on the third journal.

        ``cash_events.jsonl`` and ``trade_rows.jsonl`` have reported
        ``restated`` since Phase 3; ``balances.jsonl`` computed it and dropped
        it, so a balance snapshot rewritten under one ``sweep_id`` was a
        correction nothing could report."""
        store.append_balance(_balance(cash_balance=1.0), env=_LIVE, sweep_id="s1")
        store.append_balance(_balance(cash_balance=2.0), env=_LIVE, sweep_id="s1")

        fold = store.read_balances(env=_LIVE)
        self.assertEqual((fold.restated, fold.lines), (1, 2))
        self.assertEqual([at.snapshot.cash_balance for at in fold.snapshots], [2.0])


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
