"""Every booking row in exactly one bucket (#1689, memo section 5(a)).

The bucket identity -- ``sum(buckets) == sum(all rows given)`` -- is the first
of the memo's three mechanisms by which an incomplete number announces itself.
Two properties hold it up, and a change must not break either.

**The classifier reads ``BkAmountType`` FIRST, and reads nothing else.** Two
nearby fields look like they would do and both are wrong on the real rows.
``RelatedTradeId`` is NON-ZERO on all three deposits (6820472599 / 6833231458 /
6849524900, none of them a trade id), so "non-zero means a trade" folds
+24 000.00 PLN of funding into trading and flips the sign of the headline
number. ``AmountClass`` puts the withholding tax with the commissions
(``TransactionCosts`` 59 rows) and the three deposits with the 41 share legs
(``NotAssigned`` 44 rows), so it cannot separate a fee from a tax or a deposit
from a fill. Both traps are measured on the capture, which discriminates the
three classifiers without a constructed row.

**``all_rows`` is summed over the ROWS, never over the buckets.** Summing the
buckets would make the identity a tautology: a row dropped by the
classification would be missing from both sides and nothing would say so.

``unclassified`` is published even at zero. A ``BkAmountType`` nobody has seen
then cannot shrink the total -- it can only move money INTO that bucket, which
reads as a CHANGED number rather than as a new row appearing. There is no
``financing`` bucket: no financing ``BkAmountType`` has ever been observed, and
a bucket keyed on an unmeasured vendor string is one that can never fill, while
``unclassified`` covers it by construction.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from alphalens_pipeline.account_cash.money import AcctAmount, acct_sum
from alphalens_pipeline.brokers.account_cash import CashEvent

#: The six ``BkAmountType`` values measured on the 2026-10-03 LIVE capture.
#: Spelled exactly as the vendor sends them, because the mapping below is keyed
#: on the raw string and a prettier spelling would classify nothing.
SHARE_AMOUNT = "Share Amount"
COMMISSION = "Commission"
EXCHANGE_FEE = "Exchange Fee"
CASH_AMOUNT = "Cash Amount"
CA_CASH_DIVIDENDS = "Corporate Actions - Cash Dividends"
CA_WITHHOLDING_TAX = "Corporate Actions - Withholding Tax"

BUCKET_SHARE_LEGS = "share_legs"
BUCKET_FEES = "fees"
BUCKET_INCOME = "income"
BUCKET_DEPOSITS = "deposits"
BUCKET_UNCLASSIFIED = "unclassified"

#: Render order, and the set of buckets EVERY fold publishes -- a bucket at
#: zero is printed, never omitted.
BUCKET_ORDER: tuple[str, ...] = (
    BUCKET_SHARE_LEGS,
    BUCKET_FEES,
    BUCKET_INCOME,
    BUCKET_DEPOSITS,
    BUCKET_UNCLASSIFIED,
)

#: ``BkAmountType`` -> bucket. The ONLY field consulted (see the module
#: docstring for the two fields that look like they would do and do not).
#: ``income`` holds the dividend and its withholding tax together because the
#: tax is what the dividend cost, and the memo wants both as counted rows
#: rather than as the static sentence ``FEES_NOT_INCLUDED``.
BUCKET_OF_TYPE: Mapping[str, str] = {
    SHARE_AMOUNT: BUCKET_SHARE_LEGS,
    COMMISSION: BUCKET_FEES,
    EXCHANGE_FEE: BUCKET_FEES,
    CASH_AMOUNT: BUCKET_DEPOSITS,
    CA_CASH_DIVIDENDS: BUCKET_INCOME,
    CA_WITHHOLDING_TAX: BUCKET_INCOME,
}

__all__ = [
    "BUCKET_DEPOSITS",
    "BUCKET_FEES",
    "BUCKET_INCOME",
    "BUCKET_OF_TYPE",
    "BUCKET_ORDER",
    "BUCKET_SHARE_LEGS",
    "BUCKET_UNCLASSIFIED",
    "CASH_AMOUNT",
    "CA_CASH_DIVIDENDS",
    "CA_WITHHOLDING_TAX",
    "COMMISSION",
    "EXCHANGE_FEE",
    "SHARE_AMOUNT",
    "Bucket",
    "BucketFold",
    "bucket_of",
    "classify_buckets",
]


@dataclass(frozen=True)
class Bucket:
    """One bucket's total, with the booking types it actually holds.

    ``bk_amount_types`` is what was OBSERVED in this bucket, not what the
    mapping routes to it: for ``unclassified`` the mapping routes nothing, so
    the observed set is the only thing that can name the type that arrived.
    """

    name: str
    bk_amount_types: tuple[str, ...]
    total: AcctAmount
    rows: int

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "bk_amount_types": list(self.bk_amount_types),
            "total": self.total.to_jsonable(),
            "rows": self.rows,
        }


@dataclass(frozen=True)
class BucketFold:
    """The five buckets, and the row-wise total they must add up to."""

    buckets: tuple[Bucket, ...]
    all_rows: AcctAmount
    rows: int

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "buckets": [bucket.to_jsonable() for bucket in self.buckets],
            "all_rows": self.all_rows.to_jsonable(),
            "rows": self.rows,
        }


def bucket_of(bk_amount_type: str) -> str:
    """The bucket for one booking type, or ``unclassified`` for an unseen one."""
    return BUCKET_OF_TYPE.get(bk_amount_type, BUCKET_UNCLASSIFIED)


def _account_currency(events: Sequence[CashEvent]) -> str | None:
    """The one currency the rows state, or ``None`` when they disagree.

    Passed into every bucket's sum so that a bucket with no rows still prints
    its currency. When the rows disagree this is ``None`` and the row-wise sum
    refuses with ``currency_mixed`` on its own, which is the answer: a total
    over two currencies is not a number.
    """
    stated = {event.account_currency for event in events if event.account_currency is not None}
    return next(iter(stated)) if len(stated) == 1 else None


def classify_buckets(events: Sequence[CashEvent]) -> BucketFold:
    """Fold booking rows into the published buckets, by ``BkAmountType``.

    Window scoping is the caller's: this takes the rows it is given and states
    a total over exactly those. ``rows`` on the fold counts every row handed
    in, including one whose type nobody has seen, so the published count can
    never agree with a subset that survived the classification.

    **The ACCOUNT CURRENCY is also the caller's, and this reads it off the
    rows only because it can.** With no rows there is nothing to read it from,
    so an empty window publishes six zeros (five buckets and ``all_rows``)
    with no unit on them -- ``"0.00"``, not ``"0.00 PLN"``. That is kept
    rather than refused: the bucket identity sums the five bucket values, and
    an empty bucket refusing would turn the identity into a refusal on every
    window with no dividend. The zero is a neutral element and cannot launder
    a unit onto anything -- :func:`~.money.acct_difference` refuses a unit-less
    operand rather than adopting the other side's currency. Naming the
    account's currency on an empty window is therefore the CALLER's job; the
    store-side half knows it from the balance snapshot.
    """
    rows = list(events)
    currency = _account_currency(rows)

    grouped: dict[str, list[CashEvent]] = {name: [] for name in BUCKET_ORDER}
    for event in rows:
        name = bucket_of(event.bk_amount_type)
        # A bucket name removed from BUCKET_ORDER must not drop the row from
        # the row-wise total below, which is what keeps the identity honest.
        grouped.setdefault(name, []).append(event)

    published = tuple(
        Bucket(
            name=name,
            bk_amount_types=tuple(
                sorted({event.bk_amount_type for event in grouped.get(name, [])})
            ),
            total=acct_sum(
                [
                    (event.amount_account_currency, event.account_currency)
                    for event in grouped.get(name, [])
                ],
                rows=len(grouped.get(name, [])),
                currency=currency,
            ),
            rows=len(grouped.get(name, [])),
        )
        for name in BUCKET_ORDER
    )

    return BucketFold(
        buckets=published,
        all_rows=acct_sum(
            [(event.amount_account_currency, event.account_currency) for event in rows],
            rows=len(rows),
            currency=currency,
        ),
        rows=len(rows),
    )
