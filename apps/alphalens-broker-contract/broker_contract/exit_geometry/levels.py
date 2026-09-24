"""Pure price-level computations for the ATR bracket exit geometry.

Stdlib-only (``math`` + typing). No I/O, no broker/replay dependency — every
function here is a total, side-effect-free mapping from primitive inputs to
either a price tuple or ``None`` on a degenerate / missing input.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any


def ceiling_from_52w_high(
    trade_setup: Mapping[str, Any] | None, pct_off_52w_high: float | None
) -> float | None:
    """Reconstruct the trailing 52w-high price from the brief's distance column.

    ``technical_pct_off_52w_high`` is ``100 * (last - peak) / peak`` (<= 0 by
    construction; 0 = at the high), so ``peak = asof_close / (1 + pct/100)``.
    Returns ``None`` (-> UNCAPPED TP, memo §4.2) when the pct or the setup's
    ``asof_close`` is missing / non-finite / degenerate — a missing 52w history
    is coverage, not a null.

    The RESULT is guarded too (issue #1521). The ``denom <= 0`` check does not
    stop a positive DENORMAL denominator from overflowing the division, and an
    ``inf`` peak is the worst possible answer here: downstream it reads as "no
    ceiling", which is the opposite of the cap this value exists to impose.

    The guard is ``not finite or <= 0``, and the second half is not decoration:
    an underflowing division reaches exactly ``0.0`` (``asof_close=1.1e-308``,
    ``pct=4.5e17``), and a zero peak is not a price. A denormal that stays
    ABOVE zero — ``5e-324`` — is still returned, because that is a finite
    positive price and honest arithmetic, and it already fails safe where it is
    used: a ceiling below the cost floor makes the bracket non-constructible
    and :func:`atr_bracket_levels` answers ``None``. The guard is for "not a
    usable price", never for "small".
    """
    if trade_setup is None or pct_off_52w_high is None:
        return None
    try:
        pct = float(pct_off_52w_high)
        asof_close = float(trade_setup.get("asof_close"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(pct) or not math.isfinite(asof_close) or asof_close <= 0:
        return None
    denom = 1.0 + pct / 100.0
    if denom <= 0:
        return None
    peak = asof_close / denom
    if not math.isfinite(peak) or peak <= 0.0:
        return None
    return peak


def atr_bracket_levels(
    blended: float,
    atr: float,
    *,
    stop_atr_mult: float,
    tp_atr_mult: float,
    tp_floor_frac: float,
    ceiling_price: float | None = None,
) -> tuple[float, float] | None:
    """Compute the (stop, tp) pair for a symmetric ATR bracket exit.

    Returns ``None`` for any degenerate input: a non-finite ``blended`` /
    ``atr`` / ``stop_atr_mult`` / ``tp_atr_mult`` / ``tp_floor_frac``, a
    non-positive ``atr``, a non-positive risk (``stop_atr_mult <= 0``), a
    bracket stop at/below zero (ATR wider than ~1/stop_atr_mult of the entry),
    a ceiling at/below the cost floor (bracket not constructible), or a
    take-profit that is non-finite or does not sit ABOVE the entry. A ``None``
    / non-finite ``ceiling_price`` leaves the TP uncapped.

    The take-profit side is judged on its RESULT, not on its parameters. An
    earlier draft refused ``tp_atr_mult <= 0`` and ``tp_floor_frac < 0``, and
    measuring showed that rejects three coherent configurations out of five:
    a zero ATR multiple with a positive floor is a bracket whose upside is the
    cost floor alone, a negative multiple with a positive floor is the same
    thing, and a negative floor with a positive multiple just means the floor
    never binds. What is degenerate is a target at or below the entry.

    ALL FOUR float parameters are self-guarded, not just ``atr`` (issue #1521).
    The reason the docstring already gave for guarding ``atr`` — so a direct
    caller cannot poison the arithmetic into NaN levels — never applied to one
    parameter only, and the other three were reachable:

    * a NaN ``blended`` passed the ``bracket_stop <= 0`` check, because every
      comparison against NaN is False, and returned ``(nan, nan)``;
    * a NaN ``stop_atr_mult`` returned ``(nan, 100.5)``;
    * a NaN ``tp_atr_mult`` was the dangerous one and did NOT surface as NaN:
      ``max(tp_floor, blended + nan)`` returns ``tp_floor``, so the function
      handed back a finite, plausible take-profit built from a poisoned input,
      which nothing downstream could detect.
    """
    for value in (blended, atr, stop_atr_mult, tp_atr_mult, tp_floor_frac):
        if not math.isfinite(value):
            return None
    if atr <= 0:
        return None
    if stop_atr_mult <= 0:
        return None
    # NOTE: `tp_atr_mult` and `tp_floor_frac` are checked for FINITENESS only.
    # A first draft also refused `tp_atr_mult <= 0` and `tp_floor_frac < 0`,
    # and measuring showed that refused three coherent configurations out of
    # five: `tp_atr_mult=0` with a positive floor is a bracket whose upside is
    # the cost floor alone; a negative multiplier with a positive floor is the
    # same thing; and a negative floor with a positive multiplier simply means
    # the floor never binds. What is actually degenerate is the RESULT, and it
    # is checked as such below.
    bracket_stop = blended - stop_atr_mult * atr
    if bracket_stop <= 0:
        return None
    tp_floor = blended * (1.0 + tp_floor_frac)
    tp = max(tp_floor, blended + tp_atr_mult * atr)
    if ceiling_price is not None and math.isfinite(ceiling_price):
        if ceiling_price <= tp_floor:
            return None
        tp = min(tp, ceiling_price)
    # The honest invariant, checked on the RESULT rather than guessed at from
    # the inputs: a take-profit at or below the entry is not a take-profit.
    # It catches `tp_atr_mult=0, tp_floor_frac=0` (tp == blended, zero profit)
    # and `tp_atr_mult=-2, tp_floor_frac=-1` (tp == 0.0, not a price), while
    # letting through every combination whose target really does sit above the
    # entry. It also covers a ceiling that caps below the entry, which the
    # `ceiling_price <= tp_floor` check misses when the floor is negative.
    # `not finite` is the third way in, and the property found it rather than
    # any reading: with a finite but enormous `tp_floor_frac`, the product
    # `blended * (1 + tp_floor_frac)` overflows even though every INPUT is
    # finite (blended=1.5e150, tp_floor_frac large -> tp == inf). Guarding the
    # inputs cannot catch that; guarding the product can.
    if not math.isfinite(tp) or tp <= blended:
        return None
    return bracket_stop, tp


def reanchor_target(avg_price: float, atr: float, *, k: float) -> float | None:
    """Fill-complete re-anchor level for a long: ``avg_price - k*atr``.

    The shared arithmetic behind BOTH re-anchoring policies — the
    registry-resolved ``AtrBracketPolicy``, whose ``k`` comes from its wrapped
    geometry, and the per-document ``ReanchorOnFillPolicy`` (#1236), whose ``k``
    is declared by the intent. The bracket used to be selected for the whole
    process by an environment variable; #1414 deleted it, and today only the
    ``/edge`` replay resolves that policy. Two copies of one formula is the #1114 fork-the-arithmetic defect,
    which is why the multiplier is a parameter and the leaf is one function.

    Returns ``None`` on any degenerate input or a non-positive target — never a
    bad stop.
    """
    if not math.isfinite(avg_price) or avg_price <= 0:
        return None
    if not math.isfinite(atr) or atr <= 0:
        return None
    target = avg_price - k * atr
    if not math.isfinite(target) or target <= 0:
        return None
    return target


def fractional_giveback_target(entry: float, peak: float, *, kept_gain_frac: float) -> float | None:
    """Trailing-stop level for a long that KEEPS ``kept_gain_frac`` of the open
    gain and gives back the rest: ``max(entry, entry + kept_gain_frac*(peak -
    entry))`` (ratchets up via the caller's peak). ``1.0`` therefore parks the
    stop at the peak and gives back nothing. The distance to the peak is a
    FRACTION of the gain, not an ATR offset, so it widens as the gain grows —
    this is the ``be_0p5r_trail0p6`` lens formula. Floors at ``entry`` so a
    direct call with ``peak < entry`` still returns a break-even stop, never a
    loosen. Returns ``None`` on any degenerate price or a ``kept_gain_frac``
    outside ``(0, 1]`` — never a bad stop.

    The parameter names the GAIN it is a fraction of, not the position: a bare
    ``frac`` said neither how much nor of what, and it was the only unqualified
    fraction in this package (``tp_floor_frac``, ``min_distance_frac``,
    ``tranche_frac`` and ``trail_frac`` all name theirs). ``tranche_frac`` in
    ``sizing`` is a fraction of the POSITION, which is why this one says
    ``gain``."""
    for value in (entry, peak):
        if not math.isfinite(value) or value <= 0:
            return None
    if not math.isfinite(kept_gain_frac) or kept_gain_frac <= 0.0 or kept_gain_frac > 1.0:
        return None
    return max(entry, entry + kept_gain_frac * (peak - entry))


def clamp_reanchor_target(
    prior_stop: float,
    proposed_target: float,
    *,
    anchor_price: float,
    min_distance_frac: float,
) -> float | None:
    """Economic safety envelope for a reanchored disaster stop (memo section 3.1).

    ``prior_stop`` is the placement-time planned disaster stop (the brief
    disaster floor). Returns ``None`` = "do NOT reanchor — leave the resting stop
    where it is" on any degenerate input or when the target would drop below
    ``prior_stop``. NOTE: this enforces NEVER-BELOW-BRIEF-FLOOR, not
    never-loosen-vs-the-current-live-stop (``OrderState`` carries no stop price).
    The min-distance floor caps how close the stop may sit to ``anchor_price``
    (a too-close proposal is pushed FARTHER from price); it is chosen so it never
    binds the 1.5x-ATR policy and exists mainly for a future stochastic policy.
    """
    for value in (prior_stop, proposed_target, anchor_price):
        if not math.isfinite(value) or value <= 0:
            return None
    floor_price = anchor_price * (1.0 - min_distance_frac)
    # The min() pushes the stop AWAY from anchor_price (down / farther from
    # market) — always safe, it only ever buys more room before the stop
    # triggers. For the trail arm (anchor_price=last_price) this caps the
    # stop just below the live market (the OnWrongSideOfMarket guard). It is
    # the CALLER's ratchet on this function's CLAMPED return value (never
    # below the last confirmed trailed level, see position_manager.py
    # _maybe_trail) that guarantees the placed stop stays monotone-up —
    # this clamp alone does not.
    target = min(proposed_target, floor_price)
    if target < prior_stop:
        return None
    return target
