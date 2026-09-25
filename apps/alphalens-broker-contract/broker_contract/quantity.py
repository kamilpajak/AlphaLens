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
# that is not held. An ULP-stated bound tracks representation error instead.
#
# 32 ULPs leaves room for a value that arrived through a few arithmetic
# operations while staying ~1e14 times tighter than any venue step.
_ULP_SLACK = 32
# The CEILING on that slack, in scaled units, and the reason the no-exceed
# property is now true by construction rather than by luck of magnitude
# (issue #1520).
#
# An ULP-stated bound does NOT stay below a step on its own. The slack is
# applied to the value scaled by `10**precision`, and it is `_ULP_SLACK` = 2**5
# ULPs of THAT. Since `ulp(2**47) == 2**-5`, a scaled value of 2**47 makes the
# slack exactly 1.0 — one whole scaled unit, which on a one-unit step is one
# whole STEP. Measured: `quantize_down(1_407_374_883_554.0)` on a 0.01 step
# returned `...554.01`, above its input, off the lattice and not idempotent.
#
# Half a scaled unit is the ceiling, and it is a derivation rather than a
# taste: a step is at least one scaled unit, so an overshoot under half a
# scaled unit is under half a STEP — the same tolerance `same_quantity`,
# `covers` and `exceeds` already use. Measured over 200 000 (quantity,
# lattice) pairs across four lattices at 1e-3..5e6 shares: the ceiling binds
# ZERO times and no answer changes. It starts binding at ~1.4e12 shares on a
# two-decimal venue and ~1.4e10 on a four-decimal one.
_MAX_SCALED_SLACK = 0.5
# Above this the module cannot name the lattice, and `0.0` for anything
# unusable is what `quantize_down` already promises.
#
# The limit is the magnitude at which the slack CAP starts to bind, and that is
# a derivation rather than a knob (issue #1560). `_quantization_slack` returns
# `32 * ulp(scaled)` until the cap takes over, and `32 * ulp(2**46) == 0.5`
# exactly. From there the slack alone is half a scaled unit — on a one-unit
# step that is the WHOLE half-step tolerance, leaving nothing for the error in
# `abs(qty) * 10**precision`. Below it the slack is at most a quarter of a unit
# and the scaling error fits underneath.
#
# This used to read 2**53, reasoning that past it consecutive INTEGERS stop
# being representable. True, and the wrong question: what matters is whether
# the LATTICE survives scale -> floor -> rescale, which fails six binary orders
# earlier. Measured over 200 000 (quantity, lattice) pairs at 2**53: 426
# violations of the half-step bound, worst 2.44 STEPS above the input — two
# lattice points, on the leaf whose premise is that a quantity cannot grow on
# the way through. At 2**46: zero, worst 0.0000, and a dense walk of 120 000
# consecutive floats below the limit tops out at 0.2594 of a step.
#
# What it refuses, in shares: 7.0e13 at one decimal, 7.0e9 at four. The
# property that calls 1e9 "a share count this rail could hold" is seven times
# inside the tightest of those, and nothing below 1e9 changes answer. NOTE that
# `precision` comes from the venue (Saxo `AmountDecimals`) and is bounded only
# from below, so the share ceiling falls by 10x per decimal: a six-decimal
# venue would refuse above 70 million shares. The tree's fixtures report 0 and
# 3. Pinned per precision in test_quantity_properties.py.
_SCALED_EXACT_LIMIT = 2**46
# `step`-relative, therefore BOUNDED — safe to use for the membership and
# minimum comparisons, which ask about a distance from a lattice point rather
# than about the magnitude of the quantity itself.
_REL_TOL = 1e-12
_ABS_TOL = 1e-15


def _quantization_slack(value: float) -> float:
    """Upward slack allowed at a SCALED ``value``, safe to add before flooring.

    Named for the job rather than for the input, and capped inside rather than
    at the call site, because the uncapped version was a footgun: it was called
    ``_slack``, its docstring promised "its own representation error, no wider",
    and above 2**47 scaled units it returned a whole scaled unit — the overshoot
    #1520 exists to remove. A future caller reading that docstring would have
    reasonably added it before a floor and reintroduced the defect.
    """
    return min(max(math.ulp(abs(value)) * _ULP_SLACK, _ABS_TOL), _MAX_SCALED_SLACK)


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
    """``abs(qty)`` in units of ``10**-precision``, floored, float error absorbed.

    ``0`` when the scaling itself overflows. ``is_finite_quantity`` catches the
    infinities, but a FINITE quantity can still scale past the float ceiling —
    ``1e308`` at two decimals — and ``math.floor(inf)`` raises ``OverflowError``
    out of a pure leaf, past every ``except BrokerError`` on the rail. Zero
    units is what the caller's docstring already promises for anything
    unusable (issue #1520).
    """
    scaled = abs(qty) * (10**lattice.precision)
    if not math.isfinite(scaled) or scaled > _SCALED_EXACT_LIMIT:
        return 0
    return math.floor(scaled + _quantization_slack(scaled))


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
    a caller that cannot price a quantity must not act on one, and since
    #1520 that includes a finite quantity whose scaling overflows.

    The result is rounded to the venue's own ``precision`` so a caller never
    sees float dust like ``3.0000000000000004`` on the wire.

    "Does not exceed" is exact up to the representation slack, and the slack is
    now capped so that overshoot can never reach half a step (see
    :data:`_MAX_SCALED_SLACK`). That is weaker than the absolute no-exceed
    #1520 asks for, and it is the strongest statement compatible with having a
    slack at all: a slack of zero would floor ``0.30000000000000004`` to
    ``0.29``, which is the defect the slack exists to prevent. What the cap
    removes is the case where the overshoot reached a WHOLE step, which is the
    one that hands back a share nobody holds.
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
    # Asked BEFORE the lattice question, because since #1520 an unrepresentable
    # quantity also fails `is_on_lattice` — `quantize_down` refuses it and
    # returns 0.0 — and reporting "not a multiple of the venue step" for a
    # quantity that is a perfectly good multiple sends the reader after the
    # wrong thing.
    scaled = abs(value) * (10**lattice.precision)
    if not math.isfinite(scaled) or scaled > _SCALED_EXACT_LIMIT:
        # The threshold is READ from the constant, never spelled out. It moved
        # once already (2**53 -> 2**46, #1560) and a literal here would have
        # left the refusal naming a rule nobody applies.
        return (
            f"quantity {value!r} is too large to name exactly at venue precision "
            f"{lattice.precision!r} — its scaled form exceeds {_SCALED_EXACT_LIMIT!r} "
            f"(above {_SCALED_EXACT_LIMIT / (10**lattice.precision):,.0f} at this precision)"
        )
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
