"""How many shares a venue can actually say — the one place that knows.

A pure leaf: stdlib only, no I/O, no vendor imports, no policy. Same discipline
as :mod:`broker_contract.fx` and :mod:`broker_contract.sizing`.

WHY THIS EXISTS. The rail hard-codes whole-share arithmetic — a property of one
broker, not of the system — in the two layers that must not know the venue: the
contract (``sizing.py``'s ``math.floor``) and the pipeline (``round()`` in
``live_exit_engine``). Measured consequences: a fractional venue would open
nothing and exit nothing, and at 0.669 shares held the rail tries to sell 1 it
does not have while cancelling the disaster stop. Design memo:
``docs/research/broker_quantity_quantization_design_2026_08_25.md``.

THE THREE-LAYER SPLIT, copied from ``fx.py`` because it is the strongest
precedent in this package — *the ADAPTER reports, never the contract decides*:

1. :class:`InstrumentQuantityRules` — what the vendor said, verbatim. Every
   field is ``| None``, and ``None`` means *the vendor did not say*. The
   adapter never substitutes a default; that substitution is the present bug.
2. Policy lives pipeline-side (``brokers/execution.build_quantity_lattice``),
   which is where absence becomes a refusal.
3. :class:`QuantityLattice` — validated, no absence left. This is what the
   arithmetic below takes.

FIVE CONCEPTS, NOT ONE NUMBER. ``step`` (the lattice) is the only thing that
constrains arithmetic. ``min_qty`` is a separate validation. ``precision`` is
decimal PLACES, and precision does not imply step: two decimals permit ``1.03``
while a step of ``0.05`` does not. ``min_notional`` is checked after a price is
known. ``round_lot`` is advisory market-structure metadata — carried, never
enforced, because a US equity may have a 100-share round lot and still accept
odd lots.

THE ONE OPERATION. Floor the MAGNITUDE. Never round to nearest, never floor a
signed negative (``floor(-1.23) == -2`` moves away from zero and *increases* a
sale). There is deliberately no rounding-up primitive in this module: with none
available, "sell more than is held" stops being expressible.
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Sequence
from dataclasses import dataclass

# Absorbs binary-float REPRESENTATION error and nothing wider. `0.1 + 0.2` is
# `0.30000000000000004` — an artefact roughly 1e-16 relative, which must scale
# as `0.3`. A literal `2.9999999999` is NOT an artefact: it is a real value 1e-10
# below three, and flooring it to `2.9` is correct.
#
# The slack is stated in ULPs — the float's OWN resolution at that magnitude —
# rather than as a fraction of the quantity. That distinction is not cosmetic:
# a fixed relative bound grows without limit, so at a million shares a 1e-12
# fraction is 1e-6, wide enough to swallow a real gap and hand back a share
# that is not held. An ULP-stated bound tracks representation error by
# construction and can never reach a step, which is what makes the no-exceed
# property in `quantize_down` true rather than approximately true.
#
# 32 ULPs leaves room for a value that arrived through a few arithmetic
# operations while staying ~1e14 times tighter than any venue step.
_ULP_SLACK = 32
# `step`-relative, therefore BOUNDED — safe to use for the membership and
# minimum comparisons, which ask about a distance from a lattice point rather
# than about the magnitude of the quantity itself.
_REL_TOL = 1e-12
_ABS_TOL = 1e-15


def _slack(value: float) -> float:
    """Upward slack allowed at ``value``: its own representation error, no wider."""
    return max(math.ulp(abs(value)) * _ULP_SLACK, _ABS_TOL)


@dataclass(frozen=True)
class InstrumentQuantityRules:
    """What the venue said about quantity for ONE instrument, verbatim.

    Every field is optional because the honest report of "the vendor did not
    tell us" is ``None``, never a substituted default. Policy — what to do
    about an absence — is applied pipeline-side, exactly as
    :class:`~broker_contract.fx.FxRateQuote` reports ``mid: float | None`` and
    lets ``build_fx_conversion`` decide.
    """

    broker_instrument_id: str
    min_quantity: float | None = None
    quantity_step: float | None = None
    quantity_precision: int | None = None
    round_lot: float | None = None
    min_notional: float | None = None
    fractional_enabled: bool | None = None
    currency: str = ""
    source: str = ""
    asof: dt.datetime | None = None


@dataclass(frozen=True)
class QuantityLattice:
    """The validated lattice the arithmetic runs on. No absence left.

    The :class:`~broker_contract.fx.FxConversion` analogue: by the time one of
    these exists, the pipeline has already decided that the venue's report is
    usable, so nothing downstream has to re-ask.
    """

    step: float
    min_qty: float
    precision: int
    min_notional: float | None = None
    round_lot: float | None = None
    source: str = ""

    def __post_init__(self) -> None:
        for name in ("step", "min_qty"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ValueError(f"{name} must be a number, got {value!r}")
            if not math.isfinite(value):
                raise ValueError(f"{name} must be finite, got {value!r}")
        if self.step <= 0.0:
            raise ValueError(f"step must be strictly positive, got {self.step!r}")
        if self.min_qty < 0.0:
            raise ValueError(f"min_qty must not be negative, got {self.min_qty!r}")
        if not isinstance(self.precision, int) or isinstance(self.precision, bool):
            raise ValueError(f"precision must be an int, got {self.precision!r}")
        if self.precision < 0:
            raise ValueError(f"precision must not be negative, got {self.precision!r}")
        # The vendor contradicting itself: a step finer than the precision it
        # says quantities carry. Caught ONCE here rather than assumed at every
        # call site, because `precision` is what makes the arithmetic exact.
        scaled_step = self.step * (10**self.precision)
        step_units = round(scaled_step)
        # `step_units <= 0` is its own refusal and not a special case of the
        # nearness check: a 1e-10 step at zero decimals IS within 1e-9 of an
        # integer — the integer being ZERO. Accepting it made every later
        # division by the step count raise ZeroDivisionError out of a pure
        # function, past every `except BrokerError` on the rail. A lattice is
        # at least one whole unit wide or it is not a lattice.
        if step_units <= 0 or abs(scaled_step - step_units) > 1e-9:
            raise ValueError(
                f"step {self.step!r} is not expressible at precision "
                f"{self.precision!r} as a positive whole number of units "
                f"— the venue's own numbers disagree"
            )


def _scaled_units(qty: float, lattice: QuantityLattice) -> int:
    """``abs(qty)`` in units of ``10**-precision``, floored, float error absorbed."""
    scaled = abs(qty) * (10**lattice.precision)
    return math.floor(scaled + _slack(scaled))


def _step_units(lattice: QuantityLattice) -> int:
    return round(lattice.step * (10**lattice.precision))


def is_finite_quantity(qty: object) -> bool:
    """Whether ``qty`` is a number this module may do arithmetic on.

    ``bool`` is excluded deliberately: ``True`` is an ``int`` in Python and a
    quantity of ``True`` is a bug, not one share.
    """
    if isinstance(qty, bool) or not isinstance(qty, (int, float)):
        return False
    return math.isfinite(qty)


def lattice_units(qty: float, lattice: QuantityLattice) -> int:
    """How many whole steps fit in ``abs(qty)``. Zero for anything unusable."""
    if not is_finite_quantity(qty):
        return 0
    return _scaled_units(qty, lattice) // _step_units(lattice)


def quantize_down(qty: float, lattice: QuantityLattice) -> float:
    """The largest lattice quantity whose magnitude does not exceed ``qty``.

    Sign-preserving by construction: the magnitude is floored and the sign
    restored, so a sell can only ever shrink. ``0.0`` for anything unusable —
    a caller that cannot price a quantity must not act on one.

    The result is rounded to the venue's own ``precision`` so a caller never
    sees float dust like ``3.0000000000000004`` on the wire.
    """
    if not is_finite_quantity(qty):
        return 0.0
    magnitude = lattice_units(qty, lattice) * lattice.step
    rounded = round(magnitude, lattice.precision)
    return -rounded if qty < 0 else rounded


def is_on_lattice(qty: float, lattice: QuantityLattice) -> bool:
    """Whether ``qty`` is exactly a whole number of steps."""
    if not is_finite_quantity(qty):
        return False
    return abs(abs(qty) - abs(quantize_down(qty, lattice))) <= lattice.step * _REL_TOL + _ABS_TOL


def is_tradable(qty: object, lattice: QuantityLattice) -> bool:
    """Whether ``qty`` is a quantity this venue could actually accept.

    The single "is this quantity real" predicate. Fails closed on ``None`` /
    ``bool`` / non-numeric / non-finite / non-positive, which preserves the
    stance the rail already takes: a degraded broker read must end the
    decision, never raise past the pass boundary.

    Note what it does NOT depend on: how small the fraction happens to be.
    Today a 0.3-share tranche is "not real" and a 0.669-share one is, because
    both meet a bare ``0.5``. Here the answer comes from the venue.
    """
    if not is_finite_quantity(qty):
        return False
    value = float(qty)  # type: ignore[arg-type]
    if value <= 0.0:
        return False
    if not is_on_lattice(value, lattice):
        return False
    return value + lattice.step * _REL_TOL + _ABS_TOL >= lattice.min_qty


def same_quantity(a: float, b: float, lattice: QuantityLattice) -> bool:
    """Whether two quantities are the same share count.

    Half a step is the tolerance, and that is a derivation rather than a
    choice: on a whole-share venue it is exactly ``0.5`` — the number
    ``QTY_PRECISION`` has always been. Owned quantities are whole on the wire
    but arrive as floats, so ``45.9999999`` and ``46.0`` must compare equal.
    """
    if not (is_finite_quantity(a) and is_finite_quantity(b)):
        return False
    return abs(a - b) < lattice.step / 2.0


def covers(actual: float, required: float, lattice: QuantityLattice) -> bool:
    """Whether ``actual`` is at least ``required``, within half a step."""
    if not (is_finite_quantity(actual) and is_finite_quantity(required)):
        return False
    return actual > required - lattice.step / 2.0


def exceeds(actual: float, limit: float, lattice: QuantityLattice) -> bool:
    """Whether ``actual`` is genuinely more than ``limit``, beyond half a step."""
    if not (is_finite_quantity(actual) and is_finite_quantity(limit)):
        return False
    return actual > limit + lattice.step / 2.0


def allocate_units(total_units: int, weights: Sequence[float]) -> tuple[int, ...]:
    """Split ``total_units`` whole units across ``weights``, summing EXACTLY.

    Largest fractional remainder. Flooring each ``total * w`` independently
    loses units (three thirds of one unit is ``[0, 0, 0]`` and the position
    never exits); rounding each to nearest overshoots the total. Neither is
    acceptable when the parts must reconstitute a position.
    """
    n = len(weights)
    if n == 0 or total_units <= 0:
        return tuple(0 for _ in weights)
    total_weight = sum(w for w in weights if w > 0)
    if total_weight <= 0:
        return tuple(0 for _ in weights)

    exact = [total_units * (w / total_weight) if w > 0 else 0.0 for w in weights]
    floors = [math.floor(x) for x in exact]
    remainder = total_units - sum(floors)
    # Hand out the leftover to the largest fractional parts, ties by position
    # so the split is deterministic for a given input.
    order = sorted(range(n), key=lambda i: (-(exact[i] - floors[i]), i))
    for i in order[:remainder]:
        floors[i] += 1
    return tuple(floors)


def split_position(
    qty: float, fractions: Sequence[float], lattice: QuantityLattice
) -> tuple[float, ...]:
    """Split a position into lattice-valid parts that sum back to the whole.

    The parts are allocated in integer lattice units, so they reconstitute the
    quantized position exactly — no dust left stranded below the minimum, and
    never more than is held.
    """
    units = lattice_units(qty, lattice)
    # Rounded to the venue precision for the same reason `quantize_down` is:
    # the same share count must not read differently depending on which
    # function produced it.
    return tuple(
        round(u * lattice.step, lattice.precision) for u in allocate_units(units, fractions)
    )


def quantity_refusal(
    qty: float, lattice: QuantityLattice, *, notional: float | None = None
) -> str | None:
    """Why this quantity is not tradable here, or ``None`` when it is.

    Kept separate from :func:`quantize_down` on purpose: quantization produces
    a step-valid CANDIDATE, it does not prove an order is valid. Minimum
    quantity and minimum notional are different questions with different
    answers, and collapsing them is how a venue rejection becomes a surprise.
    """
    if not is_finite_quantity(qty):
        return f"quantity {qty!r} is not a usable number"
    value = float(qty)
    if value <= 0.0:
        return f"quantity {value!r} is not positive"
    if not is_on_lattice(value, lattice):
        return f"quantity {value!r} is not a multiple of the venue step {lattice.step!r}"
    if value + lattice.step * _REL_TOL + _ABS_TOL < lattice.min_qty:
        return f"quantity {value!r} is below the venue minimum {lattice.min_qty!r}"
    if (
        lattice.min_notional is not None
        and notional is not None
        and notional + _ABS_TOL < lattice.min_notional
    ):
        return f"notional {notional!r} is below the venue minimum {lattice.min_notional!r}"
    return None


__all__ = [
    "InstrumentQuantityRules",
    "QuantityLattice",
    "allocate_units",
    "covers",
    "exceeds",
    "is_finite_quantity",
    "is_on_lattice",
    "is_tradable",
    "lattice_units",
    "quantity_refusal",
    "quantize_down",
    "same_quantity",
    "split_position",
]
