"""The cash book's summing vocabulary: ONE summer, in the account currency.

Memo: ``docs/research/account_cashbook_design_2026_10_05.md`` section 4. This
module and the two that import it (``buckets``, ``episodes``) are PURE: they
take rows and return records, they take no ``env``, and they never read the
store. The precedent is #1714 -- the replay engine states every run value and
reads no deployment state -- and the consequence here is that the derivation's
tests need no isolated home at all.

Four rules carry the whole layer.

**Only the ``_acct`` family is ever summed.** ``amount`` and ``currency`` on a
booking row are the INSTRUMENT's; ``amount_account_currency`` and
``account_currency`` are the ACCOUNT's. Every LIVE pick is USD against a PLN
account today, and the venue arc already reaches XWAR, XETR and XPAR, so
summing the instrument-currency field is a currency bug waiting for the first
Warsaw fill. ``pnl_cash``, a price and an ``r_multiple`` are never summed at
all.

**The FX disclosure is never added into any net.**
``CashEvent.conversion_cost_account_currency`` is already in the account
currency, so every guard below lets it through: it is a DISCLOSURE of how much
of the booked rate was markup, already inside the amount via that rate (memo
section 1.3, whose decisive measurement is that the cash book contains no FX
booking type at all). Adding it double counts at about 0.25% of notional per
leg. ``tests/account_cash/test_money.py`` holds this with an AST walk over all
three modules rather than as a convention, because no value guard can see it.

**A record contributes all of its money or none of it.** One absent amount
nulls the whole total with a named reason. ``or 0.0`` on a missing fee makes a
net look better than reality, which is the worst direction for a money error
(memo section 9), and the absence of a stated currency is the same defect in
another dress: an amount nobody said was in the account currency is not an
account amount.

**ONE function for every money sum, on both sides of every partition.** The
same quantity computed by two functions is how the #1227 power gate read "met"
for two days across two simulations, a merged registration and two adversarial
review passes. :func:`acct_sum` is the only accumulator in this layer.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

#: One grosz, in the ACCOUNT currency. The width of a balance cross-check's
#: agreement. Declared HERE and not beside the check that reads it: it is a
#: money-vocabulary constant, and :func:`acct_difference`, the only thing that
#: produces the number it is compared against, already lives in this module.
BALANCE_TOLERANCE_ACCT = 0.01

#: An amount the vendor did not report in the account currency -- the field is
#: null, or no currency was stated for it. Spelled the keeper's way
#: (``trades.NULL_ACCT_AMOUNT_NOT_REPORTED``); the two cannot share the
#: constant, because dependency rule 2 forbids this package from importing
#: ``brokers.automanager``, so a parity test pins the spelling instead.
NULL_ACCT_AMOUNT_NOT_REPORTED = "account_amount_not_reported"
#: Two different stated currencies reached one sum. Refused rather than added:
#: the account has exactly one currency, and a second one means either a
#: vendor change or a bug, never a convertible total.
NULL_CURRENCY_MIXED = "currency_mixed"
#: A NaN or an infinity reached a money field, or a TOTAL stopped fitting in
#: one float. Named here rather than left to the CLI's ``allow_nan=False``,
#: which turns the whole report into a refusal where this turns one line into
#: one -- and rather than left to :class:`AcctAmount`'s own ``ValueError``,
#: which is that same whole-report refusal arriving as a traceback.
NULL_NON_FINITE = "non_finite"
#: An episode that has not returned to flat has no realized net to publish.
NULL_NOT_CLOSED = "not_closed"
#: Nothing was ever opened, so there is no cost basis to report. The consumer
#: is the report's ``cost_basis_open_acct`` line when no episode is open
#: (memo Phase 4's store-side half); no episode can reach it, because a close
#: with no open position is an unassigned fill and starts no episode.
NULL_NEVER_OPENED = "never_opened"
#: On SIM the report endpoints answer with canned rows belonging to OTHER
#: accounts, so there is no number to publish -- never ``0.0``.
NULL_SIM_REPORTS_UNUSABLE = "sim_reports_unusable"

#: Render order, and the registry :func:`acct_null` validates against.
NULL_REASONS: tuple[str, ...] = (
    NULL_ACCT_AMOUNT_NOT_REPORTED,
    NULL_CURRENCY_MIXED,
    NULL_NON_FINITE,
    NULL_NOT_CLOSED,
    NULL_NEVER_OPENED,
    NULL_SIM_REPORTS_UNUSABLE,
)

__all__ = [
    "BALANCE_TOLERANCE_ACCT",
    "NULL_ACCT_AMOUNT_NOT_REPORTED",
    "NULL_CURRENCY_MIXED",
    "NULL_NEVER_OPENED",
    "NULL_NON_FINITE",
    "NULL_NOT_CLOSED",
    "NULL_REASONS",
    "NULL_SIM_REPORTS_UNUSABLE",
    "AcctAmount",
    "acct_difference",
    "acct_null",
    "acct_sum",
]


@dataclass(frozen=True)
class AcctAmount:
    """One money value in the ACCOUNT currency, or a named absence.

    ``value`` is ``None`` exactly when ``null_reason`` is set, and vice versa.
    There is no third state: a money field is a number with its currency, or a
    refusal with its reason. ``rows`` is how many rows the number rests on, so
    a total never stands beside a row count that came from somewhere else.

    The invariant is enforced in ``__post_init__`` rather than documented,
    because the two states it rules out are the two that read as agreement: a
    ``None`` with no reason prints as an empty cell, and a number beside a
    reason prints as a figure that is also a refusal.
    """

    value: float | None
    currency: str | None
    rows: int
    null_reason: str | None = None

    def __post_init__(self) -> None:
        if (self.value is None) != (self.null_reason is not None):
            raise ValueError(
                f"an account amount is a number or a named absence, never both and never "
                f"neither: value={self.value!r} null_reason={self.null_reason!r}"
            )
        if self.null_reason is not None and self.null_reason not in NULL_REASONS:
            raise ValueError(
                f"{self.null_reason!r} is not a registered null reason; a reason nobody "
                f"registered cannot be rendered and cannot be branched on"
            )
        if self.value is not None and not math.isfinite(self.value):
            raise ValueError(
                f"an account amount of {self.value!r} is not a money number; json.dumps "
                f"writes it as the bare token NaN/Infinity, which is not JSON"
            )
        if self.rows < 0:
            raise ValueError(f"an account amount rests on {self.rows} rows")

    @property
    def rendered(self) -> str:
        """The human form, with the currency ON the number (memo section 5(d)).

        A null renders as its reason, never as ``0.00``: the repo's own rule is
        that a number printed beside a threshold in a different unit reads as
        a comfortable pass, and a zero standing in for "unknown" is the same
        defect with no unit at all.
        """
        if self.value is None:
            return str(self.null_reason)
        if self.currency is None:
            return f"{self.value:.2f}"
        return f"{self.value:.2f} {self.currency}"

    def to_jsonable(self) -> dict[str, Any]:
        """The wire form. ``value_rendered`` carries the currency on the number."""
        return {
            "value": self.value,
            "currency": self.currency,
            "rows": self.rows,
            "null_reason": self.null_reason,
            "value_rendered": self.rendered,
        }


def acct_null(reason: str, currency: str | None, *, rows: int) -> AcctAmount:
    """A named absence, with the currency it would have been reported in."""
    return AcctAmount(value=None, currency=currency, rows=rows, null_reason=reason)


def _published(total: Decimal, currency: str | None, *, rows: int) -> AcctAmount:
    """``total`` as a float, or ``non_finite`` when it no longer fits in one.

    The input guards cannot see this: two finite amounts can sum past the float
    range, and :class:`AcctAmount` refuses to hold the infinity ``float()``
    then produces. Published as the SAME named null an infinite input gets,
    because the consumer's question is the same one -- there is no number to
    print on this line -- and raising instead would turn one unprintable line
    into a report-wide traceback. ONE place publishes a total, so neither
    accumulator can grow its own answer to this.
    """
    value = float(total)
    if not math.isfinite(value):
        return acct_null(NULL_NON_FINITE, currency, rows=rows)
    return AcctAmount(value=value, currency=currency, rows=rows)


def acct_sum(
    pairs: Iterable[tuple[float | None, str | None]],
    *,
    rows: int,
    currency: str | None = None,
) -> AcctAmount:
    """Sum amounts that are ALL in the account currency, or refuse.

    Each pair is ``(amount_in_account_currency, account_currency)`` -- the only
    two fields of a booking row, and the only two of a trades row, that any sum
    in this layer may read.

    ``currency`` is the account currency when the CALLER already knows it. It
    is used when no pair states one, so an empty total still prints its
    currency (a bucket at zero is printed rather than absent, memo section
    5(a)), and it joins the distinct set, so a pair that contradicts it is
    ``currency_mixed`` rather than silently preferred.

    ``rows`` is passed rather than counted: the caller knows how many rows the
    total is meant to rest on, and a count taken from the pairs would agree
    with whatever subset reached here -- which is how a dropped row leaves a
    count that agrees with the rows that survived it.

    Refuses in this order: an absent amount or an absent currency
    (``account_amount_not_reported``), a non-finite amount (``non_finite``),
    two stated currencies (``currency_mixed``), and finally a TOTAL that does
    not fit in a float (``non_finite`` again, see :func:`_published`).
    Accumulation is over
    ``Decimal(str(amount))`` and the result is published as a ``float``, so the
    sum of 104 two-decimal rows is exact rather than exact to within a float
    epsilon that then has to be argued about against a one-grosz tolerance.
    """
    items = list(pairs)
    stated = sorted(
        {stated_currency for _, stated_currency in items if stated_currency is not None}
    )
    if currency is not None and currency not in stated:
        stated = sorted([*stated, currency])
    resolved = stated[0] if len(stated) == 1 else None

    if any(amount is None or pair_currency is None for amount, pair_currency in items):
        return acct_null(NULL_ACCT_AMOUNT_NOT_REPORTED, resolved, rows=rows)
    if any(not math.isfinite(amount) for amount, _ in items if amount is not None):
        return acct_null(NULL_NON_FINITE, resolved, rows=rows)
    if len(stated) > 1:
        return acct_null(NULL_CURRENCY_MIXED, None, rows=rows)

    total = sum((Decimal(str(amount)) for amount, _ in items if amount is not None), Decimal(0))
    return _published(total, resolved, rows=rows)


def acct_difference(left: AcctAmount, right: AcctAmount) -> AcctAmount:
    """``left - right``, or the first named absence either side carries.

    ``rows`` is both sides' counts added, because the difference rests on every
    row that went into either sum -- it is not a row count of its own, and
    publishing one side's count would describe half the basis.
    """
    rows = left.rows + right.rows
    stated = sorted({side.currency for side in (left, right) if side.currency is not None})
    resolved = stated[0] if len(stated) == 1 else None

    if left.null_reason is not None:
        return acct_null(left.null_reason, resolved, rows=rows)
    if right.null_reason is not None:
        return acct_null(right.null_reason, resolved, rows=rows)
    if len(stated) > 1:
        return acct_null(NULL_CURRENCY_MIXED, None, rows=rows)

    assert left.value is not None and right.value is not None  # the dataclass invariant
    difference = Decimal(str(left.value)) - Decimal(str(right.value))
    return _published(difference, resolved, rows=rows)
