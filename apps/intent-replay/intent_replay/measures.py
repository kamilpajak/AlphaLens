"""The measures the section 5 summary publishes, summed out of the TRACE.

ENGINE module: stdlib and this package only.

Every cash figure here is derived from the events, not read off ``WalkResult``,
so the summary and the trace cannot disagree: a reader who adds the events up
by hand gets the published number. ``WalkResult`` accumulates the same
quantities independently as the walk runs, which makes the agreement a test
rather than a definition.

**The sums fold LEFT from a ``0.0`` seed, and that is load-bearing.** Since
CPython 3.12 the built-in ``sum()`` is Neumaier-compensated and ``math.fsum``
is exact, while the walk accumulates naively (``state.cash += units * price``)
and emits ``EntryFilled(cash=units * price)`` built from the same two floats in
the same order. So a left fold is bitwise identical to the walk BY
CONSTRUCTION, and a compensated sum is not. The construction is the argument;
the measurement only shows the effect is not theoretical. Over 200 000 random
runs on 2026-09-29, of which 125 319 filled and 79 888 filled more than once,
the left fold diverged from ``WalkResult.notional_spent`` on 0 and both
``sum()`` and ``math.fsum`` on 11 750 -- about 15% of the multi-fill runs. The
rate depends on the generator and is not a property of the code; the zero is.
Using a compensated form here would turn the parity test into an approximate
one and lose the only check that the two paths compute the same thing.

One fact about that test, learned the hard way on 2026-09-29: it needs a
fixture with at least THREE fills. At two addends a naive left fold and a
compensated sum return the same bits, because Neumaier's residual rounds back
into the total, so a suite whose deepest ladder fills twice agrees with
``sum()`` and pins nothing at all. The fixture set says so where it is defined.

Three asymmetries in the trace that the arithmetic has to respect, each stated
in section 4.6 rather than inferred from field values:

* ``position_closed`` carries the sale for every reason EXCEPT ``tp_complete``,
  where the cash was already the last ``tp_fired`` and the event is a marker.
  No branch is needed: ``_fire_tranches`` emits a LITERAL ``units=0.0`` on that
  reason, so the marker contributes nothing to a units-weighted sum.
* ``horizon_open`` is a VALUATION at the last bar's close, not a fill. It pays
  no fee and takes no slippage, and the cost gate is not consulted for it.
* ``units_filled`` is NOT a share count. ``PendingEntry.notional`` is a budget
  in the ACCOUNT currency, so ``notional / limit_price`` is shares times the FX
  rate. Every other measure is FX-clean because the rate cancels:
  ``units_filled * denominator`` is an amount in the account currency, which is
  exactly what ``pnl_cash`` is. "Fixing" this to real shares would break
  ``r_multiple`` by precisely that rate, so the quantity stays auxiliary and
  the summary does not publish it (section 5 does not name it).
"""

from __future__ import annotations

from dataclasses import dataclass

from intent_replay.trace import EntryFilled, HorizonOpen, PositionClosed, TpFired, TraceEvent
from intent_replay.walk import WalkResult

__all__ = ["Measures", "summarise"]


@dataclass(frozen=True, slots=True)
class Measures:
    """What the summary renders. Bare numbers: the units are the envelope's
    layer, so this module states one fact per field and nothing about its
    presentation."""

    notional_spent: float
    avg_entry_price: float | None
    pnl_cash: float
    pnl_pct_of_spent: float | None
    denominator: float | None
    r_multiple: float | None
    mfe: float | None
    mae: float | None
    units_filled: float


def _fold(values: tuple[float, ...]) -> float:
    """Left fold from ``0.0``. See the module docstring: this is the only
    summation that reproduces the walk's own accumulation bit for bit."""
    total = 0.0
    for value in values:
        total += value
    return total


def _spent(events: tuple[TraceEvent, ...]) -> tuple[float, float]:
    """Cash out and units in. Selected by TYPE rather than by the ``kind``
    string, so a renamed field is a type error here instead of an empty sum."""
    fills = tuple(event for event in events if isinstance(event, EntryFilled))
    return _fold(tuple(f.cash for f in fills)), _fold(tuple(f.units for f in fills))


def _proceeds(events: tuple[TraceEvent, ...]) -> float:
    """Everything the run sold or marked.

    A tranche reports its own ``proceeds``; a stop-out, a time stop and the
    horizon mark report units and a price. The ``tp_complete`` marker is
    included without a branch because its units are zero.
    """
    sold = tuple(event.proceeds for event in events if isinstance(event, TpFired))
    marked = tuple(
        event.units * event.price
        for event in events
        if isinstance(event, PositionClosed | HorizonOpen)
    )
    return _fold(sold) + _fold(marked)


def summarise(result: WalkResult, *, declared_floor: float) -> Measures:
    """The section 5 measures for one walk.

    ``declared_floor`` is ``spec.disaster_stop``: section 5.1 fixes the R
    denominator as the stop that was actually PLACED, which is that level on
    both deployments, in every document, including one supplying its own
    ``exit.initial_levels``.

    When the denominator is NOT POSITIVE, ``r_multiple``, ``mfe`` and ``mae``
    are ``None``. Not "negative": exactly ``0.0`` is reachable, by a bar that
    opens at the disaster stop and therefore fills there. The suppressed case
    is not a paper one -- a run that lost 109 units reports +1.16 R without it.
    """
    spend, units = _spent(result.events)
    pnl_cash = _proceeds(result.events) - spend
    average = spend / units if units > 0.0 else None
    denominator = average - declared_floor if average is not None else None
    r_multiple: float | None = None
    mfe: float | None = None
    mae: float | None = None
    if average is not None and denominator is not None and denominator > 0.0:
        r_multiple = pnl_cash / (units * denominator)
        mfe = _excursion(result.peak_price, average, denominator)
        mae = _excursion(result.trough_price, average, denominator)
    return Measures(
        notional_spent=spend,
        avg_entry_price=average,
        pnl_cash=pnl_cash,
        pnl_pct_of_spent=100.0 * pnl_cash / spend if spend else None,
        denominator=denominator,
        r_multiple=r_multiple,
        mfe=mfe,
        mae=mae,
        units_filled=units,
    )


def _excursion(extreme: float | None, average: float, denominator: float) -> float:
    """One extreme in R.

    ``WalkResult`` types both marks as optional, but ``_track_extremes`` sets
    them on the same bar as the first fill, and this runs only when there WAS
    a fill. Measured 2026-09-29: neither the 373 replay tests nor the 136
    property runs reach the absent case with a ``raise`` in its place. So it is
    an invariant, not a branch, and it says so rather than returning a null a
    reader would take for a real "no excursion" answer. Same shape as
    ``config._present``.
    """
    if extreme is None:  # pragma: no cover - a fill happened, so both marks are set
        raise RuntimeError("an excursion mark is absent although the position was filled")
    return (extreme - average) / denominator
