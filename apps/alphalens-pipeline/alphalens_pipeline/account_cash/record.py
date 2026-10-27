"""What one window's answer publishes, and what each line rests on (#1689).

Memo: ``docs/research/account_cashbook_design_2026_10_05.md`` sections 4, 5(d)
and 6. The shapes live here and nothing else does: no row is read, no total is
summed, no identity is checked. :mod:`report` composes the numbers and fills
these in.

Three of the five rules that carry the published numbers are rules about THESE
fields, so they are stated here rather than beside the composition (#1805).

**The window-scoped lines say so, and the store-wide ones say that instead.**
The cash-event buckets and ``cash_book_sum_acct`` are over the booking rows
INSIDE the window: a report that states a window in its header and publishes a
store-wide total is a number that lies about its own basis. The CUMULATIVE
balance check is store-wide by definition -- the whole stored cash book against
the latest balance snapshot -- so it publishes its OWN row count and its own
since/until on its own line and can never be read as the window's figure. That
since/until is the span of the rows it SUMMED and NOT
:attr:`~.coverage.Coverage.swept_through`, which stops at the end of the first
contiguous run so a hole cannot be claimed as covered -- the right answer to
"what can the store speak for" and the wrong one to "what does this number
rest on".
``cost_basis_open_acct`` is the third case: it keeps the
FULL cost of every open episode and publishes its own as-of, because the cost
of a still-open position is as-of now and not as-of the window's end. On the
real capture at ``until=2026-09-12`` a window-clipped basis would be -466.98
of the -3594.86 that is actually at risk. Because its as-of is its own, it is
a term of NEITHER derived line -- not of ``residual_acct`` and not of
``subtotal_acct`` -- so no published identity mixes two scopes. It also
publishes
``cost_basis_open_excluded``: ``open`` is a STATE, resolved first-hit, so a
span still holding shares under a ``window_clipped`` or ``imbalanced`` flag is
left out of the amount -- rightly, since neither has a vouchable cost basis --
and the count beside the line is what stops the line reading as the whole
exposure.

**The residual is a published line, not a reconciliation step, and every term
of it is the WINDOW's.** Episodes are summable and their sum is NOT the period
total (memo section 4): the difference is real money -- a dividend arriving
after an episode closed, withholding tax, financing, the entry legs of an
episode that closed outside the window, and the legs of a position still open.
``residual_acct`` is the window's cash book sum less FOUR window-scoped lines
-- the realized net, income, unclassified and the deposits -- computed by the
same summing function as everything else.

``cost_basis_open_acct`` was the fifth of them until 2026-10-26 and is not a
term any more. It keeps its own as-of by the decision above, so it was the one
term of a window-scoped identity that answered as of a different day, and on a
window holding NO booking row the arithmetic published its whole offset:
+3594.86 over 0 rows, under the label "residual not explained above", on a
window whose cash book sum is 0.00. A line that is as-of-now is published on
its own terms instead -- amount, episode count, as-of and the held-but-excluded
count -- the way the deposits line is published below the subtotal rather than
inside it.

The residual is therefore NOT zero at the full span, and the sentence that
said it is zero "only by accident" was the other half of the same mistake.
Over the whole real capture it is -3594.86: the cash that bought the three
positions still open is window cash, and no other line accounts for it once
the basis is out. The two numbers coincide THERE and are two different facts
-- a window total and an as-of-watermark exposure -- which the narrow windows
separate (-7928.96 against the same -3594.86 at ``until=2026-09-12``).

**The balance cross-check comes in two versions and the weaker one says so.**
The cumulative check carries ``detects_restatement: False`` on its face: a
check that only ever says yes cannot see a correction to an old row, and
without that field "reconciles" reads as proof the history is intact, which it
is not (memo section 5(c)).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from alphalens_pipeline.account_cash import balance_check, buckets
from alphalens_pipeline.account_cash.coverage import Coverage
from alphalens_pipeline.account_cash.money import AcctAmount

#: The published lines :attr:`CashBookTotals.residual_acct` SUBTRACTS from the
#: window's cash book sum, in subtraction order. One tuple, read by the
#: subtraction itself and published with the record, because the render has to
#: name what was actually taken off and a renderer that spelled the terms
#: itself wrote a sentence that was true once: the deposits are a term and are
#: rendered BELOW the line, so "the lines above" was wrong by exactly
#: 24 000.00 PLN while every arithmetic test stayed green.
#:
#: ``cost_basis_open_acct`` is deliberately absent; see
#: :attr:`CashBookTotals.residual_acct` for why a term in another scope is
#: published as that term's offset.
RESIDUAL_TERMS: tuple[str, ...] = (
    "net_closed_acct",
    "income_acct",
    "unclassified_acct",
    "deposits_acct",
)


__all__ = [
    "RESIDUAL_TERMS",
    "Basis",
    "CashBookTotals",
]


@dataclass(frozen=True)
class Basis:
    """What every total on this report rests on (memo section 5(d)).

    ``basis`` is structurally inseparable from the numbers: it is an object,
    not prose, and the human render prints it ABOVE the figures. Four
    partitions in here are checked rather than described -- the rows, the
    fills, the episodes, and the buckets against the rows.

    The row counts are in two spaces on purpose and the names say which:
    ``rows_*`` count BOOKING rows (the cash book), ``fills_*`` count TRADES
    rows (the executions). ``episodes_*`` count derived spans. A count in one
    space printed beside a threshold in another is this repo's own named
    defect.

    The row partition has FOUR terms, not three: a row can leave the window by
    falling outside it, by carrying no date, or by carrying one the parse
    refuses -- and the last two are separate counters because they name
    different vendor behaviour.
    """

    currency: str | None
    window_since: str
    window_until: str
    episodes: int
    episodes_summed: int
    episodes_excluded: int
    exclusions: Mapping[str, int]
    #: How many records the cash-events JOURNAL accounted for, from its own
    #: counters (:func:`~.store.folded_records`) rather than from the length of
    #: the folded list the window splits. A record lost between the two is what
    #: the row partition exists to catch, and it cannot catch it from one side.
    rows_in_store: int
    rows_read: int
    rows_outside_window: int
    #: Booking rows carrying no ``Date`` at all. Excluded from the window and
    #: COUNTED: excluding them cannot inflate a total, and this count is what
    #: says it happened.
    rows_undated: int
    #: Booking rows carrying a ``Date`` the parse REFUSES. Its own counter, and
    #: not folded into ``rows_undated``: the codec stores the field as free
    #: text, so a vendor change to a datetime-shaped string decodes, moves
    #: every row out of every window, and would publish "the rows carry no
    #: date" -- naming the vendor's nulls instead of the format. The same
    #: distinction the journals make for a line they cannot read.
    rows_date_malformed: int
    rows_classified: int
    fills_read: int
    fills_assigned: int
    fills_unassigned: Mapping[str, int]
    restated: Mapping[str, int]
    malformed: Mapping[str, int]

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "currency": self.currency,
            "window_since": self.window_since,
            "window_until": self.window_until,
            "episodes": self.episodes,
            "episodes_summed": self.episodes_summed,
            "episodes_excluded": self.episodes_excluded,
            "exclusions": dict(self.exclusions),
            "rows_in_store": self.rows_in_store,
            "rows_read": self.rows_read,
            "rows_outside_window": self.rows_outside_window,
            "rows_undated": self.rows_undated,
            "rows_date_malformed": self.rows_date_malformed,
            "rows_classified": self.rows_classified,
            "fills_read": self.fills_read,
            "fills_assigned": self.fills_assigned,
            "fills_unassigned": dict(self.fills_unassigned),
            "restated": dict(self.restated),
            "malformed": dict(self.malformed),
        }


@dataclass(frozen=True)
class CashBookTotals:
    """One window's answer: the lines, the basis they rest on, the coverage.

    The render order of the money lines is memo section 6's: the realized net,
    the cost basis of what is still open, income, unclassified, the residual,
    then the SUBTOTAL, then the deposits BELOW it labelled not P&L, then the
    cash book sum. Deposits sit below the subtotal because 24 000 PLN of
    funding is the easiest number in this repo to misread as profit.

    **The order is not the arithmetic.** Four lines are the subtotal's terms
    -- the realized net, income, unclassified and the residual -- and two are
    STATED lines that no published identity adds up: ``deposits_acct``, which
    is funding and sits below the subtotal for that reason, and
    ``cost_basis_open_acct``, which is as-of the store's watermark rather than
    as-of the window's end and sits above it. A renderer printing this order
    therefore has to say which lines add, because a column of five numbers
    above a rule reads as five terms of what is under it.
    """

    env: str
    basis: Basis
    coverage: Coverage
    net_closed_acct: AcctAmount
    cost_basis_open_acct: AcctAmount
    #: The as-of of the line above, which is NOT the window's end. The cost of
    #: a position that is still open is as-of the last day the store can speak
    #: for; clipping it to the window's end publishes a fraction of the money
    #: at risk as if it were all of it (amendment W1).
    #:
    #: It is ``swept_through`` -- the end of the CONTIGUOUS run -- and not the
    #: latest sweep's end, even though the basis is summed over every stored
    #: fill including ones swept after a gap. That is the conservative
    #: direction and the only defensible one: past a gap the quantity walk may
    #: have missed an entry, so the position it reports is not known to be the
    #: position held. Stating the later date would claim the basis is current
    #: as of a day whose fills the store never read.
    cost_basis_open_asof: str | None
    #: Episodes still HOLDING quantity that the line above leaves out, because
    #: their state is ``window_clipped`` or ``imbalanced`` rather than
    #: ``open`` (see
    #: :func:`~.report._held_but_not_on_the_line`). Published beside the
    #: line so a non-zero one cannot be read as "nothing else is open"; the
    #: line itself stays the cost this layer can vouch for.
    cost_basis_open_excluded: int
    income_acct: AcctAmount
    unclassified_acct: AcctAmount
    #: The window's cash book sum less the four WINDOW-SCOPED lines (the
    #: realized net, income, unclassified, the deposits). Not a
    #: reconciliation step: the difference is real money the named lines do
    #: not explain -- a dividend after a close, withholding tax, financing,
    #: the entry legs of an episode that closed outside the window, and the
    #: legs of a position still open.
    #:
    #: ``cost_basis_open_acct`` is deliberately NOT a term. It answers as of
    #: the store's watermark while every term here answers as of the window,
    #: and one term in a different scope is published as that term's offset:
    #: measured +3594.86 over 0 rows on a window holding no booking row at
    #: all, under a label that says it is unexplained window cash. Over the
    #: whole store the residual and that line coincide at -3594.86, because
    #: the open positions' legs are then the only window cash the four lines
    #: leave; at ``until=2026-09-12`` they are -7928.96 and -3594.86.
    residual_acct: AcctAmount
    #: The four lines above that ADD: the realized net, income, unclassified
    #: and the residual. Not the open cost basis (a different as-of) and not
    #: the deposits (funding, and the line below it). Because the residual
    #: already subtracted the deposits, ``subtotal + deposits ==
    #: cash_book_sum_acct`` holds algebraically rather than by measurement.
    subtotal_acct: AcctAmount
    deposits_acct: AcctAmount
    cash_book_sum_acct: AcctAmount
    buckets: tuple[buckets.Bucket, ...]
    #: Both versions, cumulative first. The cumulative one carries
    #: ``detects_restatement: False`` on its face, so "reconciles" can never
    #: be read as proof the history is intact (memo section 5(c)). Each states
    #: its OWN interval, and the cumulative one's is the span of the rows it
    #: summed -- not :attr:`Coverage.swept_through`, which answers what the
    #: store can speak for and stops at the first contiguous run.
    #:
    #: The coverage watermark and the cumulative basis are two different
    #: facts, and they are meant to disagree whenever the coverage has a hole.
    balance_checks: tuple[balance_check.BalanceCheck, ...]

    def lines(self) -> dict[str, AcctAmount]:
        """Every published money line by name, in render order."""
        return {
            "net_closed_acct": self.net_closed_acct,
            "cost_basis_open_acct": self.cost_basis_open_acct,
            "income_acct": self.income_acct,
            "unclassified_acct": self.unclassified_acct,
            "residual_acct": self.residual_acct,
            "subtotal_acct": self.subtotal_acct,
            "deposits_acct": self.deposits_acct,
            "cash_book_sum_acct": self.cash_book_sum_acct,
        }

    def to_jsonable(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "env": self.env,
            "basis": self.basis.to_jsonable(),
            "coverage": self.coverage.to_jsonable(),
            "cost_basis_open_asof": self.cost_basis_open_asof,
            "cost_basis_open_excluded": self.cost_basis_open_excluded,
            # The names the residual subtracted, so a consumer -- the prose
            # renderer first of all -- can say what was taken off instead of
            # describing it from memory. Published off the one tuple the
            # subtraction itself reads, so the two cannot come apart.
            "residual_terms": list(RESIDUAL_TERMS),
            "buckets": [bucket.to_jsonable() for bucket in self.buckets],
            "balance_checks": [check.to_jsonable() for check in self.balance_checks],
        }
        body.update({name: line.to_jsonable() for name, line in self.lines().items()})
        return body
