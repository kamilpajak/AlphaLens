"""The windows the cash book will not answer, and why (#1689).

Memo: ``docs/research/account_cashbook_design_2026_10_05.md`` section 5's
table. One question lives here: given an environment and a window, is there an
answer at all. The vocabulary is :data:`TOTALS_REFUSALS`, and a refusal is
always ONE of those names and never a ``0.0``.

**A refusal exists because a smaller number reads as a complete answer.** Each
of the four names below is a state in which the arithmetic would succeed and
publish a figure for rows nobody read: a SIM store whose report endpoints serve
other accounts' rows, a store no sweep ever wrote, a ``--since`` earlier than
the first day anyone asked about, and a window with no day in common with any
sweep. The last of those answered ``0.00`` over 0 rows beside a RECONCILING
balance check, which is the most confident shape this layer can produce.

**An ``until`` beyond the swept range is NOT refused**, and that asymmetry is
deliberate. The store can be asked about a day nobody swept and answer that it
holds nothing for it, as long as part of the window WAS read and the coverage
block states the shortfall beside every number (see :mod:`coverage`). A
``since`` before ``history_begins`` is different: there the store holds rows
whose own entries may predate it, so a smaller number would read as complete
(memo section 5's table, amendment W8).

**The watermarks ride ON the refusal.** A refused report publishes nothing on
stdout, so the facts a caller needs to ask an answerable question instead
cannot be read off the report -- they are carried by
:class:`CashBookTotalsRefusedError`.

Pure over what it is handed: it validates an environment NAME and reads
already-decoded sweep records. It resolves no path and calls no reader; the one
read (``store.read_sweeps``) belongs to :mod:`report`, which hands the result
in.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from typing import Any

from alphalens_pipeline.account_cash import paths, store
from alphalens_pipeline.account_cash.codec import SweepRecord
from alphalens_pipeline.account_cash.coverage import Coverage

#: The window could not be answered at all. One of these, never a ``0.0``.
REFUSED_SIM_REPORTS_UNUSABLE = "sim_reports_unusable"
REFUSED_BEFORE_FIRST_SWEEP = "before_first_sweep"
#: No sweep ever ASKED about a single day of this window -- it lies past every
#: asked window, or inside a hole between two of them. The right edge the
#: ``before_first_sweep`` rule never guarded: a window entirely outside the
#: swept range answered ``0.00`` over 0 rows with a reconciling balance check,
#: which is the "a total for an account nobody read" shape
#: :data:`REFUSED_SIM_REPORTS_UNUSABLE` exists to prevent, published as a
#: confident answer with only the coverage block to disagree with it.
REFUSED_WINDOW_NEVER_SWEPT = "window_never_swept"
REFUSED_NOTHING_SWEPT = "nothing_swept"
TOTALS_REFUSALS: tuple[str, ...] = (
    REFUSED_SIM_REPORTS_UNUSABLE,
    REFUSED_BEFORE_FIRST_SWEEP,
    REFUSED_WINDOW_NEVER_SWEPT,
    REFUSED_NOTHING_SWEPT,
)

__all__ = [
    "REFUSED_BEFORE_FIRST_SWEEP",
    "REFUSED_NOTHING_SWEPT",
    "REFUSED_SIM_REPORTS_UNUSABLE",
    "REFUSED_WINDOW_NEVER_SWEPT",
    "TOTALS_REFUSALS",
    "CashBookTotalsRefusedError",
    "refuse_a_non_live_environment",
    "refuse_an_unanswerable_window",
]


class CashBookTotalsRefusedError(Exception):
    """The window cannot be answered. ``reason`` is one of :data:`TOTALS_REFUSALS`.

    ``details`` carries the facts a caller needs to ask an answerable question
    instead -- the watermarks, for the two reasons that are about where the
    window sits. A refusal publishes nothing on stdout, so a recovery the
    published table tells an operator to follow has to arrive on the failure
    object itself; sending them to the report they were just refused is not a
    recovery.
    """

    def __init__(self, reason: str, env: str, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(f"the account cash book will not answer this window on {env}: {reason}")
        self.reason = reason
        self.env = env
        self.details: Mapping[str, Any] = dict(details or {})


def refuse_a_non_live_environment(env: str) -> None:
    """``sim_reports_unusable``, BEFORE the first read.

    The store's READERS do not refuse on SIM -- only the appenders do -- and
    ``_iter_payloads`` returns silently on a missing path, so a sum over a SIM
    store answers ``0.0`` for an account whose report endpoints serve OTHER
    accounts' rows. Refused ahead of every read rather than merely ahead of
    every sum, so the order in the code says the same thing this docstring
    does: nothing about SIM is read at all.
    """
    if paths.validate_environment(env) != paths.ENV_LIVE:
        raise CashBookTotalsRefusedError(REFUSED_SIM_REPORTS_UNUSABLE, env)


def _overlaps_an_asked_window(
    sweeps: Sequence[SweepRecord], *, since: dt.date, until: dt.date
) -> bool:
    """True when some sweep ASKED about at least one day of this window.

    Against every asked window rather than against
    :attr:`~.coverage.Coverage.swept_through`, and the two readings differ
    wherever the coverage has a hole: the watermark stops at the end of the
    FIRST contiguous run, so a window inside the SECOND run is past it and was
    nonetheless read. Refusing that one would refuse days the store holds rows
    for. A window inside the HOLE overlaps nothing and is refused, which is the
    same fact as one past the end -- nobody read those days.
    """
    asked = (window for window in map(store.sweep_window, sweeps) if window is not None)
    return any(asked_since <= until and since <= asked_until for asked_since, asked_until in asked)


def refuse_an_unanswerable_window(
    env: str, coverage: Coverage, sweeps: Sequence[SweepRecord], *, since: dt.date, until: dt.date
) -> None:
    """``nothing_swept``, ``before_first_sweep``, ``window_never_swept``.

    All three before any sum, and in this order. "Nothing swept" comes first
    because a store with no sweep record cannot say what window it covers, so
    there is no watermark to compare ``since`` against. The overlap check comes
    last because it is the narrowest: a ``since`` before the store's own first
    day is the more specific fact about that invocation.

    **An ``until`` beyond the swept range is NOT refused here**, and that
    distinction is the point of the overlap test. The store can be asked about
    a day nobody swept and answer that it holds nothing for it, as long as part
    of the window WAS read and the coverage block states the shortfall beside
    every number. A window with no day in common with any sweep is a different
    thing: there is nothing to state a shortfall against, and the answer would
    be ``0.00`` over 0 rows for a period nobody ever read.
    """
    if coverage.history_begins is None:
        raise CashBookTotalsRefusedError(REFUSED_NOTHING_SWEPT, env)
    if since < dt.date.fromisoformat(coverage.history_begins):
        # The earliest answerable `--since`, ON the refusal. The published
        # recovery is to ask for that day, and a refusal publishes nothing on
        # stdout -- so reading it off `coverage.history_begins` means running
        # the report, which answers with this same refusal. The only other
        # source is the sweeps journal, read by hand.
        raise CashBookTotalsRefusedError(
            REFUSED_BEFORE_FIRST_SWEEP, env, {"history_begins": coverage.history_begins}
        )
    if not _overlaps_an_asked_window(sweeps, since=since, until=until):
        raise CashBookTotalsRefusedError(
            REFUSED_WINDOW_NEVER_SWEPT,
            env,
            # Both watermarks, because this refusal is about WHERE the window
            # sits: an operator holding the refusal alone can then ask a window
            # the store can speak for, which is what the published recovery
            # promises and what an empty stdout cannot deliver.
            {
                "history_begins": coverage.history_begins,
                "swept_through": coverage.swept_through,
            },
        )
