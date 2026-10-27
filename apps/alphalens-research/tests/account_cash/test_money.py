"""The summing vocabulary of the cash book (#1689, memo Phase 4).

This is the module where money numbers appear, so four rules carry the whole
layer and each one is pinned by a test that fails without it:

* **Only the ``_acct`` family is ever summed.** ``pnl_cash`` is in the
  INSTRUMENT currency while ``pnl_cash_acct`` is in the ACCOUNT currency, and
  every LIVE pick is USD against a PLN account today (memo section 4).
* **The FX disclosure is never added into any net.** It is already inside the
  account amount via the rate, so adding it double counts at about 0.25% of
  notional per leg (memo section 1.3). Held structurally by the AST test at the
  bottom of this file, not by a convention.
* **A record contributes all of its money or none of it.** A missing fee read
  as ``0.0`` makes a net look better than reality, which is the worst direction
  for a money error (memo section 9).
* **A refusal is a named reason, never a number.** A null total carries why it
  is null; it never publishes ``0.0``, and it never publishes a NaN.
"""

from __future__ import annotations

import ast
import inspect
import math
import unittest
from dataclasses import dataclass, fields
from decimal import Decimal
from pathlib import Path

from alphalens_pipeline.account_cash import balance_check, buckets, episodes, money, report
from alphalens_pipeline.account_cash.money import (
    BALANCE_TOLERANCE_ACCT,
    NULL_ACCT_AMOUNT_NOT_REPORTED,
    NULL_CURRENCY_MIXED,
    NULL_CURRENCY_NOT_STATED,
    NULL_NON_FINITE,
    AcctAmount,
    acct_difference,
    acct_null,
    acct_sum,
)
from alphalens_pipeline.brokers.account_cash import BalanceSnapshot, CashEvent, TradeRow

from tests.account_cash.cashbook_fixture import (
    BALANCE,
    BOOKINGS,
    TRADES,
    acct_pairs,
    event_like,
    events_of_type,
    events_with_one_changed,
    trades_with_one_changed,
)

#: Measured from the capture, not copied from the memo:
#: ``sum(Decimal(str(AmountAccountCurrency)))`` over the 41 ``Share Amount``
#: rows, and over all 104 rows.
SHARE_LEG_ROWS = 41
SHARE_LEG_TOTAL_PLN = -3110.06
ALL_BOOKING_ROWS = 104
ALL_BOOKINGS_TOTAL_PLN = 20738.68


def _share_leg_pairs() -> list[tuple[float | None, str | None]]:
    return acct_pairs(events_of_type(buckets.SHARE_AMOUNT))


def _shifted(amount: float | None, by: str) -> float:
    """``amount`` moved by an exact decimal step, with no float noise added."""
    assert amount is not None
    return float(Decimal(str(amount)) + Decimal(by))


class TheAccountCurrencySumIsTheOnlySummer(unittest.TestCase):
    def test_a_sum_of_the_real_share_legs_is_the_measured_total(self) -> None:
        total = acct_sum(_share_leg_pairs(), rows=SHARE_LEG_ROWS)

        self.assertEqual(total.rows, SHARE_LEG_ROWS)
        self.assertEqual(total.currency, "PLN")
        self.assertIsNone(total.null_reason)
        assert total.value is not None
        self.assertAlmostEqual(total.value, SHARE_LEG_TOTAL_PLN, places=6)

    def test_a_sum_of_every_real_booking_row_is_the_measured_total(self) -> None:
        total = acct_sum(acct_pairs(BOOKINGS), rows=ALL_BOOKING_ROWS)

        self.assertEqual(total.rows, ALL_BOOKING_ROWS)
        assert total.value is not None
        self.assertAlmostEqual(total.value, ALL_BOOKINGS_TOTAL_PLN, places=6)

    def test_an_empty_sum_is_zero_with_the_currency_the_caller_states(self) -> None:
        """A bucket at zero still prints its currency (memo section 5(a)).

        Without the stated currency an empty bucket would publish ``None``,
        and the bucket identity -- which sums the five bucket values -- would
        then read that ``None`` as a money field the vendor did not report.
        """
        total = acct_sum([], rows=0, currency="PLN")

        self.assertEqual(total.value, 0.0)
        self.assertEqual(total.currency, "PLN")
        self.assertEqual(total.rows, 0)
        self.assertIsNone(total.null_reason)

    def test_the_accumulation_is_exact_and_not_a_float_sum(self) -> None:
        """The ``Decimal(str(amount))`` claim, as an EQUALITY not a tolerance.

        The real capture cannot tell the two accumulators apart: its 104
        two-decimal rows agree to 4e-13, which is ten orders of magnitude
        under the one-grosz tolerance, so a float sum passes every other test
        in this file. Only a pair of rows whose float sum is inexact pins it,
        and the published value is compared with ``assertEqual`` -- an
        ``assertAlmostEqual`` here would pass for both.
        """
        total = acct_sum([(0.1, "PLN"), (0.2, "PLN")], rows=2)

        self.assertEqual(total.value, 0.3)
        self.assertNotEqual(0.1 + 0.2, 0.3)

    def test_the_row_count_is_the_callers_statement_and_never_the_item_count(self) -> None:
        """``rows`` is passed rather than counted, and that is the point.

        A count taken from the pairs agrees with whatever subset reached the
        summer, so a caller that drops rows before summing would publish an
        agreeing count -- the exact failure the parameter exists to prevent.
        The two numbers are deliberately different here, which is the only
        way to show which of them is published.
        """
        total = acct_sum([(1.0, "PLN")], rows=5)

        self.assertEqual(total.rows, 5)
        self.assertEqual(total.value, 1.0)
        self.assertEqual(acct_sum([], rows=7, currency="PLN").rows, 7)


class AMoneyRecordContributesAllOfItsMoneyOrNone(unittest.TestCase):
    def test_one_absent_amount_nulls_the_whole_sum(self) -> None:
        """``or 0.0`` is the named worst-direction error (memo section 9)."""
        pairs = acct_pairs(events_with_one_changed(0, amount_account_currency=None))

        total = acct_sum(pairs, rows=ALL_BOOKING_ROWS)

        self.assertIsNone(total.value)
        self.assertEqual(total.null_reason, NULL_ACCT_AMOUNT_NOT_REPORTED)
        self.assertEqual(total.currency, "PLN")
        self.assertEqual(total.rows, ALL_BOOKING_ROWS)

    def test_every_money_field_the_layer_reads_nulls_its_own_total(self) -> None:
        """The all-or-nothing rule on EVERY money field, not just the first.

        The floor is what catches a row quietly dropped from the table: five
        money fields reach this layer, and a table of four has stopped
        describing it.
        """
        money_fields: tuple[tuple[type, str, list[tuple[float | None, str | None]]], ...] = (
            (
                CashEvent,
                "amount_account_currency",
                acct_pairs(events_with_one_changed(0, amount_account_currency=None)),
            ),
            (
                CashEvent,
                "account_currency",
                acct_pairs(events_with_one_changed(0, account_currency=None)),
            ),
            (
                TradeRow,
                "booked_amount_account_currency",
                [
                    (row.booked_amount_account_currency, row.account_currency)
                    for row in trades_with_one_changed(0, booked_amount_account_currency=None)
                ],
            ),
            (
                TradeRow,
                "account_currency",
                [
                    (row.booked_amount_account_currency, row.account_currency)
                    for row in trades_with_one_changed(0, account_currency=None)
                ],
            ),
            (
                BalanceSnapshot,
                "cash_balance",
                [(None, BALANCE.currency)],
            ),
        )

        self.assertGreaterEqual(len(money_fields), 5)
        for owner, field_name, pairs in money_fields:
            with self.subTest(field=f"{owner.__name__}.{field_name}"):
                self.assertIn(field_name, {f.name for f in fields(owner)})
                total = acct_sum(pairs, rows=len(pairs))
                self.assertIsNone(total.value)
                self.assertEqual(total.null_reason, NULL_ACCT_AMOUNT_NOT_REPORTED)

    def test_a_non_finite_amount_is_a_named_null_and_never_a_rendered_nan(self) -> None:
        """Named here, rather than left to ``_emit_json(allow_nan=False)``.

        That guard turns the whole report into a refusal; a named null turns
        one line into a refusal and leaves the rest readable.
        """
        pairs = acct_pairs(events_with_one_changed(0, amount_account_currency=float("inf")))

        total = acct_sum(pairs, rows=ALL_BOOKING_ROWS)

        self.assertIsNone(total.value)
        self.assertEqual(total.null_reason, NULL_NON_FINITE)

    def test_a_total_that_overflows_a_float_is_a_named_null_and_not_a_crash(self) -> None:
        """The non-finite guard covers the PUBLISHED total, not only the inputs.

        Two finite amounts can sum past the float range: the ``Decimal``
        accumulator holds 2e308 exactly and ``float()`` of it is an infinity,
        which :class:`AcctAmount` refuses to hold. Without this branch the
        refusal arrives as a ``ValueError`` raised out of a money derivation,
        which is the same shape as the ``allow_nan=False`` refusal this module
        exists to replace -- it turns the whole report into an error where a
        named null turns one line into one. No vendor row reaches it; the
        branch is here because a traceback is not a report.
        """
        total = acct_sum([(1e308, "PLN"), (1e308, "PLN")], rows=2)

        self.assertIsNone(total.value)
        self.assertEqual(total.null_reason, NULL_NON_FINITE)
        self.assertEqual(total.currency, "PLN")
        self.assertEqual(total.rows, 2)

    def test_a_difference_that_overflows_a_float_is_a_named_null_too(self) -> None:
        left = acct_sum([(1e308, "PLN")], rows=1)
        right = acct_sum([(-1e308, "PLN")], rows=1)

        difference = acct_difference(left, right)

        self.assertIsNone(difference.value)
        self.assertEqual(difference.null_reason, NULL_NON_FINITE)
        self.assertEqual(difference.rows, 2)

    def test_a_non_finite_value_cannot_be_constructed_at_all(self) -> None:
        with self.assertRaises(ValueError):
            AcctAmount(value=float("nan"), currency="PLN", rows=1)

    def test_a_value_and_a_null_reason_cannot_both_be_present(self) -> None:
        """The record has two states, not three (the ``oneOf`` in memo 5(d))."""
        with self.assertRaises(ValueError):
            AcctAmount(value=1.0, currency="PLN", rows=1, null_reason=NULL_NON_FINITE)
        with self.assertRaises(ValueError):
            AcctAmount(value=None, currency="PLN", rows=1)


class TheCurrencyIsReadAndNeverAssumed(unittest.TestCase):
    def test_a_second_account_currency_refuses_rather_than_adding(self) -> None:
        """The capture is PLN on 104 of 104 rows, so only this row can prove
        the field is read at all rather than hard-coded."""
        rows = (*BOOKINGS, event_like(BOOKINGS[0], account_currency="EUR"))

        total = acct_sum(acct_pairs(rows), rows=len(rows))

        self.assertIsNone(total.value)
        self.assertEqual(total.null_reason, NULL_CURRENCY_MIXED)

    def test_the_absent_currency_does_not_count_as_a_second_one(self) -> None:
        """A row with no stated currency is an unreported amount, not a second
        currency: putting ``None`` in the distinct set would read a single
        absent currency as a multi-currency account."""
        pairs = acct_pairs(events_with_one_changed(0, account_currency=None))

        total = acct_sum(pairs, rows=ALL_BOOKING_ROWS)

        self.assertEqual(total.null_reason, NULL_ACCT_AMOUNT_NOT_REPORTED)
        self.assertEqual(total.currency, "PLN")

    def test_a_stated_currency_that_the_rows_contradict_refuses(self) -> None:
        total = acct_sum(acct_pairs(BOOKINGS), rows=ALL_BOOKING_ROWS, currency="EUR")

        self.assertEqual(total.null_reason, NULL_CURRENCY_MIXED)


class TheToleranceIsAGroszInTheAccountCurrency(unittest.TestCase):
    """Two separate claims: the unit, and the order of magnitude.

    A residual of exactly ``0.0`` would prove neither -- it passes for a
    ``Decimal`` accumulator and for a naive float sum alike. Both cases below
    are constructed so that one of them goes red at a tolerance of ``0.0`` or
    ``0.001`` and the other at a tolerance of ``1.0``.
    """

    def setUp(self) -> None:
        self.real = acct_sum(acct_pairs(BOOKINGS), rows=ALL_BOOKING_ROWS)

    def test_the_tolerance_is_one_grosz(self) -> None:
        self.assertEqual(BALANCE_TOLERANCE_ACCT, 0.01)

    def test_a_half_grosz_disagreement_is_inside_the_tolerance(self) -> None:
        shifted = acct_sum(
            acct_pairs(
                events_with_one_changed(
                    0,
                    amount_account_currency=_shifted(BOOKINGS[0].amount_account_currency, "0.005"),
                )
            ),
            rows=ALL_BOOKING_ROWS,
        )

        difference = acct_difference(shifted, self.real)

        assert difference.value is not None
        self.assertAlmostEqual(difference.value, 0.005, places=9)
        self.assertLess(abs(difference.value), BALANCE_TOLERANCE_ACCT)

    def test_a_fifty_grosz_disagreement_is_outside_the_tolerance(self) -> None:
        shifted = acct_sum(
            acct_pairs(
                events_with_one_changed(
                    0,
                    amount_account_currency=_shifted(BOOKINGS[0].amount_account_currency, "0.50"),
                )
            ),
            rows=ALL_BOOKING_ROWS,
        )

        difference = acct_difference(shifted, self.real)

        assert difference.value is not None
        self.assertAlmostEqual(difference.value, 0.50, places=9)
        self.assertGreaterEqual(abs(difference.value), BALANCE_TOLERANCE_ACCT)


class TheDifferenceOfTwoTotals(unittest.TestCase):
    def test_a_difference_carries_the_currency_and_both_row_counts(self) -> None:
        left = acct_sum([(10.0, "PLN"), (5.0, "PLN")], rows=2)
        right = acct_sum([(3.0, "PLN")], rows=1)

        difference = acct_difference(left, right)

        self.assertEqual(difference.value, 12.0)
        self.assertEqual(difference.currency, "PLN")
        self.assertEqual(difference.rows, 3)

    def test_a_null_on_either_side_propagates_with_its_own_reason(self) -> None:
        real = acct_sum([(10.0, "PLN")], rows=1)
        absent = acct_null(NULL_ACCT_AMOUNT_NOT_REPORTED, "PLN", rows=1)

        self.assertEqual(acct_difference(absent, real).null_reason, NULL_ACCT_AMOUNT_NOT_REPORTED)
        self.assertEqual(acct_difference(real, absent).null_reason, NULL_ACCT_AMOUNT_NOT_REPORTED)

    def test_two_currencies_refuse_rather_than_subtracting(self) -> None:
        difference = acct_difference(
            acct_sum([(10.0, "PLN")], rows=1), acct_sum([(3.0, "EUR")], rows=1)
        )

        self.assertEqual(difference.null_reason, NULL_CURRENCY_MIXED)

    def test_a_unit_less_operand_refuses_instead_of_adopting_the_other_side(self) -> None:
        """A number with no stated currency must not be given one here.

        Measured 2026-10-22 before this fix: ``acct_difference(AcctAmount(0.0,
        None, 0), acct_sum([(5.0, "PLN")], rows=1))`` answered **-5.00 PLN**.
        The unit-less side silently adopted PLN, because the resolved currency
        is the single stated one and only the OTHER side stated anything. This
        is the path that can reach a balance cross-check -- the one consumer
        of ``BALANCE_TOLERANCE_ACCT`` -- so a zero that nobody said was in the
        account currency would be compared against the account's own balance
        as if it were.

        Asserted on BOTH sides: an implementation guarding only ``left`` keeps
        the defect for ``acct_difference(real, unit_less)``.
        """
        unit_less = acct_sum([], rows=0)
        real = acct_sum([(5.0, "PLN")], rows=1)
        self.assertIsNone(unit_less.currency)

        for name, (left, right) in {
            "unit_less_left": (unit_less, real),
            "unit_less_right": (real, unit_less),
        }.items():
            with self.subTest(case=name):
                difference = acct_difference(left, right)

                self.assertIsNone(difference.value)
                self.assertEqual(difference.null_reason, NULL_CURRENCY_NOT_STATED)
                self.assertIsNone(difference.currency)
                self.assertEqual(difference.rows, 1)

    def test_a_named_absence_keeps_its_own_reason_even_with_no_currency(self) -> None:
        """Ordering: the operand's own reason beats the unit-less refusal.

        A null carries no number, so there is nothing for the other side's
        currency to launder; reporting ``currency_not_stated`` instead of
        ``account_amount_not_reported`` would replace the informative reason
        with a less informative one.
        """
        absent = acct_null(NULL_ACCT_AMOUNT_NOT_REPORTED, None, rows=1)
        real = acct_sum([(5.0, "PLN")], rows=1)

        self.assertEqual(acct_difference(absent, real).null_reason, NULL_ACCT_AMOUNT_NOT_REPORTED)
        self.assertEqual(acct_difference(real, absent).null_reason, NULL_ACCT_AMOUNT_NOT_REPORTED)

    def test_the_empty_sum_stays_a_neutral_element_and_not_a_refusal(self) -> None:
        """The other half of the unit-less zero, and it stays as it is.

        ``acct_sum([], rows=0)`` is what the bucket identity adds through: the
        five bucket values are summed, and an empty bucket contributing a
        refusal rather than a zero would turn the identity into a refusal
        whenever any bucket is empty -- which is every window with no
        dividend. A zero with no unit cannot launder a currency onto anything,
        because nothing subtracts it against a stated one any more.
        """
        empty = acct_sum([], rows=0)

        self.assertEqual(empty.value, 0.0)
        self.assertIsNone(empty.currency)
        self.assertIsNone(empty.null_reason)
        self.assertEqual(acct_sum([(5.0, "PLN"), (empty.value, "PLN")], rows=2).value, 5.0)


class TheRenderedForm(unittest.TestCase):
    def test_a_number_renders_with_its_currency_and_its_row_count(self) -> None:
        body = acct_sum(_share_leg_pairs(), rows=SHARE_LEG_ROWS).to_jsonable()

        self.assertEqual(
            sorted(body), ["currency", "null_reason", "rows", "value", "value_rendered"]
        )
        self.assertEqual(body["currency"], "PLN")
        self.assertEqual(body["rows"], SHARE_LEG_ROWS)
        self.assertIsNone(body["null_reason"])
        self.assertEqual(body["value_rendered"], "-3110.06 PLN")

    def test_a_null_renders_its_reason_and_no_number(self) -> None:
        body = acct_null(NULL_NON_FINITE, "PLN", rows=3).to_jsonable()

        self.assertIsNone(body["value"])
        self.assertEqual(body["null_reason"], NULL_NON_FINITE)
        self.assertEqual(body["value_rendered"], "non_finite")

    def test_every_null_reason_is_registered(self) -> None:
        with self.assertRaises(ValueError):
            acct_null("made_up_reason", "PLN", rows=1)
        self.assertEqual(len(set(money.NULL_REASONS)), len(money.NULL_REASONS))


# --- The FX disclosure, held structurally (amendment W3) ----------------------

_SUMMING_FUNCTIONS = frozenset({"acct_sum", "acct_difference"})
_CASH_EVENT_FIELDS = frozenset(field.name for field in fields(CashEvent))

#: The only two booking-row fields a sum in this layer may read.
_SUMMABLE_CASH_EVENT_FIELDS = frozenset({"amount_account_currency", "account_currency"})


@dataclass(frozen=True)
class _MoneySites:
    """What reached money in one module, and how many places did so."""

    #: ``CashEvent`` field names read at any of those places.
    read: frozenset[str]
    #: Calls to :func:`acct_sum` / :func:`acct_difference`.
    calls: int
    #: ``(amount, currency)`` tuples built anywhere, inside a call or not.
    pairs: int


def _is_currency_attribute(node: ast.expr) -> bool:
    """True for ``<something>.<name ending in currency>``.

    This is what makes a two-element tuple a MONEY PAIR rather than any other
    pair: :func:`acct_sum`'s published shape is
    ``(amount_in_account_currency, account_currency)``, so the second slot
    naming a currency is the pair's signature. Keying on the shape instead of
    on "a tuple of two attributes" is what keeps ``(event.uic,
    event.instrument_symbol)`` -- both ``CashEvent`` field names -- from being
    read as money, which is the false positive that would otherwise make this
    guard fire on an honest grouping key.
    """
    return isinstance(node, ast.Attribute) and node.attr.endswith("currency")


def _cash_event_fields_reaching_money(source: str) -> _MoneySites:
    """Which ``CashEvent`` fields reach a money total in ``source``.

    Two kinds of site, because one of them is not enough. The arguments of a
    call to :func:`acct_sum` or :func:`acct_difference` catch ``buckets.py``,
    which builds its pairs inline in the call. But ``episodes.py`` builds its
    pairs in ``_Accumulator.assign`` and in ``_unassigned``, and sums them
    later from an attribute -- so no field name is inside any summing call at
    all, and a walk scoped to calls reports ``{account_currency}`` for the
    whole module. Measured 2026-10-22: adding
    ``+ row.conversion_cost_account_currency`` to the accumulator's pair left
    the call-scoped walk's answer byte for byte unchanged. The second kind of
    site is therefore every ``(amount, currency)`` tuple, wherever it is
    built.

    Neither kind walks every attribute access in the module: ``uic``,
    ``amount``, ``instrument_symbol``, ``value_date`` and ``account_currency``
    are fields of BOTH ``CashEvent`` and ``TradeRow``, and ``episodes.py``
    legitimately reads the quantity and the instrument off a trade row.

    The match is by NAME, so the walk is conservative: an attribute named like
    a booking field counts even when it is read off another object (an
    ``AcctAmount``'s own ``.currency``, say). That is the safe direction for a
    guard whose whole job is to refuse a field, and it is measured -- pointing
    ``all_rows`` at the buckets instead of the rows turns this test red through
    exactly that path.
    """
    read: set[str] = set()
    calls = 0
    pairs = 0

    def collect(node: ast.AST) -> None:
        for inner in ast.walk(node):
            if isinstance(inner, ast.Attribute) and inner.attr in _CASH_EVENT_FIELDS:
                read.add(inner.attr)

    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Tuple)
            and len(node.elts) == 2
            and _is_currency_attribute(node.elts[1])
        ):
            pairs += 1
            collect(node)
        if not isinstance(node, ast.Call):
            continue
        name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
        if name not in _SUMMING_FUNCTIONS:
            continue
        calls += 1
        for argument in [*node.args, *(keyword.value for keyword in node.keywords)]:
            collect(argument)

    return _MoneySites(read=frozenset(read), calls=calls, pairs=pairs)


#: A pair built AWAY from any summing call, the way ``_Accumulator.assign``
#: and ``_unassigned`` build theirs. The control for the widened half of the
#: walk: with the walk scoped to call arguments this source reads as clean.
_PAIR_OUTSIDE_A_CALL = (
    "def assign(self, row):\n"
    "    pair = (\n"
    "        row.booked_amount_account_currency\n"
    "        + row.conversion_cost_account_currency,\n"
    "        row.account_currency,\n"
    "    )\n"
    "    self.all_pairs.append(pair)\n"
)

#: A two-element tuple of two ``CashEvent`` field names that is NOT money.
#: The negative control: a guard that fired here would go red on an honest
#: grouping key, which is the shape that gets a guard switched off.
_PAIR_THAT_IS_NOT_MONEY = "def key(event):\n    return (event.uic, event.instrument_symbol)\n"


#: Every module of the package that builds a money total. Walked by both
#: guards below. ``report`` and ``balance_check`` joined when they landed
#: (#1689, Phase 4's store-side half); the anti-rot test below is what makes
#: forgetting the next one red rather than silent.
_SUMMING_MODULES = (money, buckets, episodes, balance_check, report)

#: The four that CONSUME the summer. ``money`` declares it and calls it
#: nowhere, so it is walked -- its own helpers could still point a pair at the
#: FX field -- but exempt from the per-module non-vacuity floor.
_SUMMING_CONSUMERS = tuple(module for module in _SUMMING_MODULES if module is not money)


class TheWalkedSetIsEveryModuleThatSums(unittest.TestCase):
    """Anti-rot. The guards below are only as complete as this list.

    Read off the package's own source rather than maintained by hand: a sixth
    module calling :func:`acct_sum` would otherwise be unwalked and green,
    which is how a guard comes to claim a reach it does not have.
    """

    def _modules_that_sum(self) -> set[str]:
        package = Path(money.__file__).resolve().parent
        found: set[str] = set()
        for source in sorted(package.rglob("*.py")):
            sites = _cash_event_fields_reaching_money(source.read_text(encoding="utf-8"))
            if sites.calls:
                found.add(f"alphalens_pipeline.account_cash.{source.stem}")
        return found

    def test_every_module_that_calls_the_summer_is_walked(self) -> None:
        """The direction that matters: a module that builds a total and is NOT
        walked would be unguarded. ``money`` is walked without calling the
        summer, because it declares it."""
        self.assertLessEqual(
            self._modules_that_sum(), {module.__name__ for module in _SUMMING_MODULES}
        )
        self.assertEqual(
            self._modules_that_sum(), {module.__name__ for module in _SUMMING_CONSUMERS}
        )
        self.assertIn(money, _SUMMING_MODULES)

    def test_the_scan_is_not_vacuous(self) -> None:
        """The positive control for the scan itself: a package it could not
        read would return an empty set and satisfy nothing."""
        self.assertGreaterEqual(len(self._modules_that_sum()), 4)


class TheFxDisclosureIsNeverSummedIntoAnyTotal(unittest.TestCase):
    """``ConversionRateAccountCurrency`` is a DISCLOSURE, not a cash movement.

    It is already in the account currency, so every guard ``acct_sum`` has --
    null, non-finite, mixed currency -- lets it through. The cash book contains
    no FX booking type at all (memo section 1.3 measurement 3), which is what
    settles that the charge is inside the rate the leg was booked at. Adding it
    into a net double counts at about 0.25% of notional per leg.

    The walk covers the arguments of a summing call AND every
    ``(amount, currency)`` pair, wherever it is built. The second half is what
    reaches ``episodes.py``, which builds its pairs in the accumulator and in
    the unassigned helper and sums them later from an attribute.

    **The per-module floor is a reach check, not a coverage measure**, and
    ``report.py`` is the case that shows the difference. Measured 2026-10-26:
    ``buckets`` reads ``{account_currency, amount_account_currency}``,
    ``episodes`` and ``balance_check`` read theirs, and ``report`` reads
    NOTHING with ``calls=6, pairs=0`` -- it sums already-published
    :class:`AcctAmount` totals, and ``_as_pairs`` puts a bare name in the
    second slot, which the pair detector does not recognise as a money pair.
    So ``report.py`` satisfies ``calls + pairs >= 1`` on its call count alone.
    What shows the guard really reaches it is the injected positive control
    below, not the floor; a ``pairs >= 1`` floor would not hold for this
    module at all.
    """

    def test_the_fx_disclosure_is_never_summed_into_any_total(self) -> None:
        measured = {
            module.__name__: _cash_event_fields_reaching_money(inspect.getsource(module))
            for module in _SUMMING_MODULES
        }
        # Accumulated BEFORE asserting: a failing subTest skips the rest of
        # its block, so counting inside the loop made the two floors below
        # fail along with the real assertion and report a second defect that
        # was not there.
        read = frozenset().union(*(sites.read for sites in measured.values()))
        calls = sum(sites.calls for sites in measured.values())
        pairs = sum(sites.pairs for sites in measured.values())

        for name, sites in measured.items():
            with self.subTest(module=name):
                self.assertNotIn("conversion_cost_account_currency", sites.read)
                # Per MODULE, not only over the union: a module added to the
                # walked set that builds no total through the one summer is
                # walked vacuously, and the union's floors below cannot see
                # it because the other modules satisfy them.
                if name != money.__name__:
                    self.assertGreaterEqual(sites.calls + sites.pairs, 1, name)

        self.assertEqual(read, set(_SUMMABLE_CASH_EVENT_FIELDS))
        self.assertGreaterEqual(
            calls,
            3,
            "the three pure modules must build their totals through acct_sum, or this "
            "walk sees nothing and proves nothing",
        )
        self.assertGreaterEqual(
            pairs,
            4,
            "the money pairs must be written as (amount, currency) tuples, or the "
            "widened half of this walk sees nothing: 2 in buckets.py and 2 in "
            "episodes.py when this floor was measured",
        )

    def test_the_walk_goes_red_when_a_sum_reads_the_fx_disclosure(self) -> None:
        """The positive control. Without it the walk above could be vacuous."""
        control = (
            "def total(events):\n"
            "    return acct_sum(\n"
            "        [\n"
            "            (\n"
            "                event.amount_account_currency\n"
            "                + event.conversion_cost_account_currency,\n"
            "                event.account_currency,\n"
            "            )\n"
            "            for event in events\n"
            "        ],\n"
            "        rows=len(events),\n"
            "    )\n"
        )

        sites = _cash_event_fields_reaching_money(control)

        self.assertEqual(sites.calls, 1)
        self.assertIn("conversion_cost_account_currency", sites.read)
        self.assertNotEqual(sites.read, set(_SUMMABLE_CASH_EVENT_FIELDS))

    def test_the_walk_goes_red_when_a_pair_outside_a_call_reads_it(self) -> None:
        """The control for the widened half, and why the widening was needed.

        Measured 2026-10-22: with the walk scoped to the arguments of a
        summing call, this exact mutation applied to ``_Accumulator.assign``
        left the reported fields unchanged at ``{account_currency}`` and the
        test GREEN, while every fill's money then carried the FX disclosure
        added in -- about 0.25% of notional per leg, double counted. No live
        failure, because ``TradeRow`` has no disclosure field today; the point
        is that the guard claimed a reach it did not have.
        """
        sites = _cash_event_fields_reaching_money(_PAIR_OUTSIDE_A_CALL)

        self.assertEqual(sites.calls, 0)
        self.assertEqual(sites.pairs, 1)
        self.assertIn("conversion_cost_account_currency", sites.read)

    def test_a_pair_that_is_not_money_is_not_read_as_money(self) -> None:
        """The negative control, against the false positive of the widening.

        ``uic`` and ``instrument_symbol`` are both ``CashEvent`` field names,
        so a rule keyed on "a two-element tuple of booking fields" would go
        red on an honest grouping key. The rule is keyed on the pair's
        published SHAPE instead: a currency in the second slot.
        """
        sites = _cash_event_fields_reaching_money(_PAIR_THAT_IS_NOT_MONEY)

        self.assertEqual(sites.pairs, 0)
        self.assertEqual(sites.read, frozenset())


class TheSumIsNeverPointedAtTheInstrumentCurrency(unittest.TestCase):
    def test_no_production_sum_reads_the_instrument_currency_amount(self) -> None:
        """``amount`` and ``currency`` are the INSTRUMENT's (memo section 4)."""
        for module in _SUMMING_MODULES:
            with self.subTest(module=module.__name__):
                read = _cash_event_fields_reaching_money(inspect.getsource(module)).read
                self.assertNotIn("amount", read)
                self.assertNotIn("currency", read)
                self.assertNotIn("conversion_rate", read)

    def test_the_fixture_pairs_are_the_account_currency_ones(self) -> None:
        """A floor on the fixture helper the tests above sum through."""
        self.assertEqual(len(acct_pairs(BOOKINGS)), ALL_BOOKING_ROWS)
        self.assertEqual(
            acct_pairs(BOOKINGS)[0],
            (BOOKINGS[0].amount_account_currency, BOOKINGS[0].account_currency),
        )
        self.assertTrue(all(math.isfinite(row.amount or 0.0) for row in TRADES))


if __name__ == "__main__":
    unittest.main()
