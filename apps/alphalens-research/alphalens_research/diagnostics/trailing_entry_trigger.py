"""The trailing-entry trigger, in the form the broker runs and in the form the
2026-08-12 what-if study used.

Both forms live in ONE function and differ in ONE line, so a measured difference
between them is attributable to the arithmetic and to nothing else. That is the
only reason the superseded form is kept: it is a within-run control, not a
policy anyone can trade.

WHY ADDITIVE IS THE BROKER'S FORM. The server keeps an absolute price distance
between the trigger and the falling low, frozen once when the order is placed.
The authority is our own code rather than vendor documentation, which does not
state the unit: ``entry_trail_geometry.compute_trailing_order_geometry`` computes
``distance = reference * d`` once and ``order_price = reference + distance``, and
that function builds the order the adapter actually sends. A second, independent
sign is a refusal: the broker rejects a distance that is not a multiple of the
instrument tick, and a quantity rounded to a tick is a price rather than a
fraction.

This module DELEGATES the distance to that function instead of copying two
lines, following ``exit_policy_replay``, which imports the daemon's own cost
card for the same reason. The arithmetic then cannot drift from what is sent.

WHAT THIS MODULE STILL DOES NOT MODEL, so a reader does not mistake the additive
form for fidelity:

* The server ratchets the trigger in COARSE steps of ``distance * 0.10``
  (``TRAILING_STEP_FRACTION``, floored to whole ticks at placement), so the live
  trigger lags the falling low. Here the trail is continuous.
* The live reference is a BID; minute bars are trade prices, so a spread sits
  between them.
* The touch bar's own bounce never fires (see :func:`trail_trigger`).
* Production refuses to open a watch at all on a day-1 gap through the open.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

from alphalens_pipeline.brokers.automanager.entry_trail_geometry import (
    compute_trailing_order_geometry,
)

__all__ = [
    "ADDITIVE",
    "FORMS",
    "PROPORTIONAL",
    "arming_reference",
    "trail_distance",
    "trail_trigger",
]

ADDITIVE: Final = "additive"
"""The broker's form: ``trough + reference * d``, the distance frozen at the touch."""

PROPORTIONAL: Final = "proportional"
"""The 2026-08-12 study's form: ``trough * (1 + d)``. Kept ONLY as a control."""

FORMS: Final = (ADDITIVE, PROPORTIONAL)

_BPS_PER_UNIT: Final = 10_000


def arming_reference(touch_bar: Mapping[str, Any], limit: float) -> float:
    """The price the distance is frozen from, on the bar that touched the rung.

    ``min(open, limit)``: the order rests AT the limit, so a bar that merely
    reaches down to it touches at the limit, while a bar whose first print is
    already below it touches at that open. The same rule the daily-bar replay
    uses for its arming reference.
    """
    return min(float(touch_bar["o"]), limit)


def trail_distance(reference: float, d: float) -> float | None:
    """The absolute distance the server would keep, or ``None`` when the live
    geometry refuses the inputs (a non-positive or non-finite reference, or a
    non-positive distance). ``None`` means no order arms at all.
    """
    geometry = compute_trailing_order_geometry(
        reference=reference, trough=reference, d_bps=round(d * _BPS_PER_UNIT)
    )
    return None if geometry is None else geometry.trailing_distance


def trail_trigger(
    bars: Sequence[Mapping[str, Any]],
    touch_idx: int,
    d: float,
    entry_expiry_ms: int,
    *,
    limit: float,
    form: str,
) -> tuple[int, float] | None:
    """``(bar index, fill price)`` of the trailed buy-stop, or ``None`` if it
    never fires inside the entry window.

    The trail arms on the touch bar and the running low seeds from that bar's
    low. From the NEXT bar onward the level is tested against the low of bars
    already seen, and only then does the low update: the order rests at a level
    derived from history, never from the bar being tested. A bar that opens above
    the level fills at the open.

    The touch bar's own bounce is NOT triggered, deliberately. A minute bar does
    not say whether its high came before or after its low, and firing on it would
    raise variant B's fill rate on an assumption the tape cannot support. This is
    a stated divergence from the daily-bar replay, which does fire on its arming
    bar because there the worse resolution is well defined.
    """
    if form not in FORMS:
        raise ValueError(f"unknown trigger form {form!r}; expected one of {FORMS}")
    run_low = float(bars[touch_idx]["l"])
    distance: float | None = None
    if form == ADDITIVE:
        distance = trail_distance(arming_reference(bars[touch_idx], limit), d)
        if distance is None:
            return None
    for i in range(touch_idx + 1, len(bars)):
        bar = bars[i]
        if int(bar["t"]) >= entry_expiry_ms:
            return None
        level = run_low + distance if distance is not None else run_low * (1.0 + d)
        open_, high, low = float(bar["o"]), float(bar["h"]), float(bar["l"])
        if open_ >= level:
            return i, open_
        if high >= level:
            return i, level
        run_low = min(run_low, low)
    return None
