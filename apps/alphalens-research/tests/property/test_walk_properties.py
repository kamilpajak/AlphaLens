"""Spec section 6.3 walk properties for `intent_replay`, as properties.

The suite already pins every one of these bullets with one to three hand-authored
examples (`tests/intent_replay/test_walk.py`, `test_measures.py`). Section 6.3
calls them PROPERTIES, and the difference is not cosmetic: the section records
four occasions where running them over generated input refuted the written
design -- monotonicity across fills, the strict `mae <= 0` form, the `snu_bars`
biconditional, and an R value in the document's own section 5 example.

Two generation rules, both taken from `strategies.py` where they carry a measured
justification:

* structural constraints (`low <= open, close <= high`, strictly increasing `t`)
  are built into GENERATION, never enforced with `assume()` / `.filter()`;
* a bar path must SPAN the document's price region. Independently drawn prices
  almost always miss the ladder, and every property then degenerates to the
  no-fill case and asserts nothing. The share of generated runs that do fill is
  measured by `TheGeneratorReachesTheLadderTest`, so the span is a number in the
  record rather than an assumption.

The strategies live here rather than in `strategies.py` because that module's
docstring scopes its emitted shapes to `alphalens_pipeline.feedback.ladder_replay`
(it yields `{t, l, h, c, o}` dicts, not `Bar`). `test_run_config_properties.py`
is the precedent for an `intent_replay` property module carrying its own.
"""

from __future__ import annotations

import math
from typing import Any

from hypothesis import given
from hypothesis import strategies as st
from intent_replay.bars import Bar
from intent_replay.interpreter import DeclaredTranche, PendingEntry, Plan
from intent_replay.walk import walk

from tests.intent_replay.test_walk import REANCHOR, TRAIL, WALK_START, _config
from tests.property.base import PropertyTestCase

MINUTE = 60_000

# What every strategy in this module yields: the entry ladder, the declared
# floor, the take-profit ladder, and the bar path.
_Case = tuple[tuple[PendingEntry, ...], float, tuple[DeclaredTranche, ...], tuple[Bar, ...]]

# The walk reads a bar only while the entry deadline has not passed, and
# `CANONICAL` puts that deadline 1 227 sessions-worth of milliseconds after
# `WALK_START`, so a path of a few minutes is always inside the window.
MAX_BARS = 6
MAX_RUNGS = 3
MAX_TRANCHES = 3


def _price(min_value: float, max_value: float) -> st.SearchStrategy[float]:
    """Positive, finite, bounded floats -- no NaN / inf / subnormals."""
    return st.floats(
        min_value=min_value,
        max_value=max_value,
        allow_nan=False,
        allow_infinity=False,
        allow_subnormal=False,
    )


_SPACING = _price(0.005, 0.05)
_SHARE = _price(0.1, 1.0)


def _ladder(draw: Any) -> tuple[tuple[PendingEntry, ...], float, tuple[DeclaredTranche, ...]]:
    """Rungs strictly descending from a top limit, a floor below them all, and a
    take-profit ladder strictly ascending above the top rung.

    The shape the arming door admits: `limit_price` descending down the entry
    ladder, `disaster_stop` under the last rung, `tp_tranches` above the first.
    Built by construction so no draw is ever rejected.

    The take-profit ladder is always NON-EMPTY and its fractions always sum to
    one. Measured on 2000 examples of an earlier revision that declared no
    tranches: zero runs reached `closed_tp`, so every property over it was blind
    to the whole take-profit path.
    """
    top = draw(_price(20.0, 400.0))
    count = draw(st.integers(min_value=1, max_value=MAX_RUNGS))
    limits = [top]
    for spacing in draw(st.tuples(_SPACING, _SPACING))[: count - 1]:
        limits.append(limits[-1] * (1.0 - spacing))
    budget = draw(_price(500.0, 20_000.0))
    weights = list(draw(st.tuples(_SHARE, _SHARE, _SHARE))[:count])
    total = sum(weights)
    entries = tuple(
        PendingEntry(tier_index=index, limit_price=limit, notional=budget * weight / total)
        for index, (limit, weight) in enumerate(zip(limits, weights, strict=True))
    )
    floor = limits[-1] * (1.0 - draw(_price(0.01, 0.15)))

    tp_count = draw(st.integers(min_value=1, max_value=MAX_TRANCHES))
    targets = [top * (1.0 + draw(_price(0.005, 0.06)))]
    for spacing in draw(st.tuples(_SPACING, _SPACING))[: tp_count - 1]:
        targets.append(targets[-1] * (1.0 + spacing))
    shares = list(draw(st.tuples(_SHARE, _SHARE, _SHARE))[:tp_count])
    share_total = sum(shares)
    tranches = tuple(
        DeclaredTranche(tranche_index=index, price=target, fraction=share / share_total)
        for index, (target, share) in enumerate(zip(targets, shares, strict=True))
    )
    return entries, floor, tranches


def _path(draw: Any, *, low: float, high: float, count: int) -> tuple[Bar, ...]:
    """A bar path inside `[low, high]`, with `t` strictly increasing."""
    bars = []
    for index in range(count):
        first = draw(_price(low, high))
        second = draw(_price(low, high))
        bottom, top = min(first, second), max(first, second)
        bars.append(
            Bar(
                t=WALK_START + index * MINUTE,
                open=draw(_price(bottom, top)),
                high=top,
                low=bottom,
                close=draw(_price(bottom, top)),
            )
        )
    return tuple(bars)


@st.composite
def _ladder_and_spanning_path(draw: Any) -> _Case:
    """A ladder plus a path spanning `[floor, top rung]` with room either side.

    Spanning the ladder's own range is what makes fills, stop-outs and
    take-profit touches FREQUENT. The margins let the path reach below the floor
    (so the stop can close the position) and above the top rung (so a reaction
    can arm) without wandering so far that the interesting region is rarely
    sampled.
    """
    entries, floor, tranches = _ladder(draw)
    top = entries[0].limit_price
    ceiling = tranches[-1].price
    count = draw(st.integers(min_value=1, max_value=MAX_BARS))
    return entries, floor, tranches, _path(draw, low=floor * 0.97, high=ceiling * 1.05, count=count)


@st.composite
def _ladder_and_path_above(draw: Any) -> _Case:
    """A ladder plus a path that stays strictly ABOVE every rung.

    The no-fill arm needs the one shape the spanning strategy makes rare. A
    limit fill needs `low <= limit`, so a path whose LOW never reaches the top
    rung touches nothing.
    """
    entries, floor, tranches = _ladder(draw)
    top = entries[0].limit_price
    count = draw(st.integers(min_value=1, max_value=MAX_BARS))
    return entries, floor, tranches, _path(draw, low=top * 1.001, high=top * 1.20, count=count)


def _plan_of(
    entries: tuple[PendingEntry, ...],
    floor: float,
    tranches: tuple[DeclaredTranche, ...] = (),
    **overrides: Any,
) -> Plan:
    """A `Plan` carrying only what the generated document states."""
    # ``notional`` is the stated budget and ``sizing_notional`` what the ladder
    # splits; they coincide whenever no sizing buffer is stated, which is every
    # generated document here (the conversion is same-currency, so both arms of
    # ``Fx`` return their argument and no number moves).
    fields: dict[str, Any] = {
        "entries": entries,
        "notional": sum(entry.notional for entry in entries),
        "sizing_notional": sum(entry.notional for entry in entries),
        "account_currency": "EUR",
        "declared_floor": floor,
        "declared_stop": None,
        "declared_take_profit": None,
        "declared_tranches": tranches,
        "reaction": None,
        "read": frozenset(),
    }
    fields.update(overrides)
    return Plan(**fields)


def _kinds(result: Any) -> list[str]:
    return [event.kind for event in result.events]


class NoTouchIsNoFillTest(PropertyTestCase):
    """Section 6.3: bars that touch no entry produce `no_fill` and zero cash."""

    @given(_ladder_and_path_above())
    def test_a_path_above_every_rung_fills_nothing(self, case: _Case) -> None:
        entries, floor, tranches, bars = case
        result = walk(_plan_of(entries, floor, tranches), _config(), bars)
        self.assertEqual(result.outcome, "no_fill")
        self.assertEqual(result.units_filled, 0.0)
        self.assertEqual(result.notional_spent, 0.0)
        self.assertEqual(result.filled_fraction, 0.0)
        self.assertIsNone(result.avg_entry_price)
        self.assertNotIn("entry_filled", _kinds(result))


class ADocumentWithNoReactionNeverMovesTheStopTest(PropertyTestCase):
    """Section 6.3: a document with `exit: null` produces zero `stop_moved`."""

    @given(_ladder_and_spanning_path())
    def test_no_declared_reaction_moves_no_stop(self, case: _Case) -> None:
        entries, floor, tranches, bars = case
        result = walk(_plan_of(entries, floor, tranches, reaction=None), _config(), bars)
        self.assertNotIn("stop_moved", _kinds(result))


class TheGeneratorReachesTheLadderTest(PropertyTestCase):
    """The span is a MEASUREMENT, not an assumption.

    A property over bars that never touch a rung asserts nothing: every run
    answers `no_fill` and the interesting code is unreached. Measured on 2000
    examples of `_ladder_and_spanning_path`:

        closed_stop 54.7%   closed_tp 28.4%   open 10.6%   no_fill 6.2%

    and on a REFERENCE generator drawing bar prices independently of the ladder
    (the same 20..400 band), 83.2% filled. So the span is worth about ten
    percentage points of fill rate here, not the near-total difference
    `strategies.py` measured for the pipeline's own generator -- both halves are
    drawn from one price band, so an independent draw already overlaps often.

    The floors are chosen by arithmetic rather than by feel, because a floor set
    by feel is either a flake or a blind spot. At the `ci` profile's 300
    examples the binomial spread of the fill share is 0.0139 and of the
    take-profit share 0.0260, so:

        fill        floor 0.85 sits 6.3 spreads under the measured 0.938
        take-profit floor 0.15 sits 5.1 spreads under the measured 0.284

    Both are far enough out that ordinary variation cannot trip them, and close
    enough that a generator which stopped reaching the ladder would be caught
    rather than quietly making every property above it vacuous. A take-profit
    floor of 0.20 would sit only 3.2 spreads out, which over many CI runs is a
    flake someone reruns rather than a finding.
    """

    MIN_FILLED_SHARE = 0.85
    MIN_TAKE_PROFIT_SHARE = 0.15

    _SEEN: dict[str, int] = {}

    @given(_ladder_and_spanning_path())
    def an_outcome_probe(self, case: _Case) -> None:
        """Deliberately NOT named ``test_*``: the test below drives it."""
        entries, floor, tranches, bars = case
        outcome = walk(_plan_of(entries, floor, tranches), _config(), bars).outcome
        self._SEEN[outcome] = self._SEEN.get(outcome, 0) + 1

    def test_the_spanning_generator_fills_and_reaches_a_take_profit(self) -> None:
        self._SEEN.clear()
        self.an_outcome_probe()
        total = sum(self._SEEN.values())
        filled = total - self._SEEN.get("no_fill", 0)
        self.assertGreaterEqual(
            filled / total,
            self.MIN_FILLED_SHARE,
            msg=f"only {filled}/{total} generated runs filled: {self._SEEN}",
        )
        self.assertGreaterEqual(
            self._SEEN.get("closed_tp", 0) / total,
            self.MIN_TAKE_PROFIT_SHARE,
            msg=f"the take-profit path is barely reached: {self._SEEN}",
        )


class NeverSellsMoreThanWasFilledTest(PropertyTestCase):
    """Section 6.3: the walk never sells more than was filled.

    The existing suite asserts one exact equality in one scenario
    (`test_the_stop_sells_only_what_the_take_profits_left`). The bound is the
    claim, so it is asserted as an inequality over generated input, and over
    paths that reach a take-profit 28% of the time -- the ladder is where
    overselling would come from.
    """

    SOLD_KINDS = ("tp_fired", "position_closed")

    @given(_ladder_and_spanning_path())
    def test_everything_sold_was_first_filled(self, case: _Case) -> None:
        entries, floor, tranches, bars = case
        result = walk(_plan_of(entries, floor, tranches), _config(), bars)
        sold = sum(event.units for event in result.events if event.kind in self.SOLD_KINDS)
        # One ULP of the filled total absorbs the float dust of summing the
        # tranche fractions; anything larger is a real oversell.
        self.assertLessEqual(
            sold,
            result.units_filled + math.ulp(result.units_filled),
            msg=f"sold {sold!r} of {result.units_filled!r} filled",
        )


class ExcursionsNeverCrossTheFillPricesTest(PropertyTestCase):
    """Section 6.3, the excursion bullet -- stated WITHOUT a tolerance.

    The section says `mfe` is never negative and `mae` never positive "beyond
    one ulp of the average entry". Two things are wrong with that bound, both
    measured here:

    * the UNIT. `mfe` and `mae` are `(extreme - average) / denominator`, so they
      are in R, while a ULP of a price is in the instrument's currency. Applied
      to the R figure the bound is wrong by a factor of the denominator.
    * the MAGNITUDE. One ULP is not enough even in price space. The average is
      `cash / units` with both sides summed over the fills, so the rounding
      grows with the number of them: measured 1 ULP on a single rung and
      exactly 2 ULP on three rungs filling at one price (the third witness
      below).

    So this property asserts the claim the engine can keep exactly. The extremes
    are tracked from the first fill onward and every fill price lies inside its
    own bar, so the running trough is at or below EVERY fill price and the peak
    at or above every one. No average, no denominator, no tolerance. Measured:
    zero violations in 8798 filled runs across all three strategies here,
    including the case that refutes the average form.
    """

    @given(_ladder_and_spanning_path())
    def test_the_trough_is_under_and_the_peak_over_every_fill(self, case: _Case) -> None:
        entries, floor, tranches, bars = case
        result = walk(_plan_of(entries, floor, tranches), _config(), bars)
        fills = [event.price for event in result.events if event.kind == "entry_filled"]
        if not fills:
            self.assertIsNone(result.avg_entry_price)
            return
        self.assertLessEqual(result.trough_price, min(fills))
        self.assertGreaterEqual(result.peak_price, max(fills))


class TheExcursionBoundHasThreeWitnessesTest(PropertyTestCase):
    """The three measured cases that fix the bound's unit and its size.

    They live beside the property rather than in `test_measures.py` because
    each one justifies a specific word in it, and a reader who meets the
    property without them has no reason to believe the bound is not simply
    sloppy.
    """

    def test_the_spec_case_lands_exactly_one_price_ulp_above_the_average(self) -> None:
        # Section 6.3: one run in 200 000 produced `mae` at +6.447756222868429e-16.
        # The notional must stay UNROUNDED -- the section records that 1270.85
        # makes the effect vanish, so a shortened figure would look refuted.
        result = walk(
            _plan_of((PendingEntry(tier_index=0, limit_price=52.37, notional=2587.43),), 41.35),
            _config(),
            (Bar(t=WALK_START, open=52.60, high=52.63, low=52.37, close=52.49),),
        )
        average = result.avg_entry_price
        self.assertEqual(result.trough_price, 52.37)
        # The average is one ULP BELOW the only fill price, so the trough is one
        # ULP above the average -- in the currency, which is where the error is.
        self.assertEqual(average, 52.36999999999999)
        self.assertEqual(result.trough_price - average, math.ulp(average))

    def test_a_single_bar_refutes_the_r_space_form_of_the_tolerance(self) -> None:
        # One bar, a plain limit ladder, no declared reaction. `mfe` comes out
        # NEGATIVE at -4.179663151529959e-14, which is 5.9x one ULP of the
        # average: the R-space bound `mfe >= -ulp(average)` FAILS here, and the
        # price-space bound holds. Scaling by the denominator also holds, and is
        # exactly attained, which is why the comparison must be inclusive.
        entries = (
            PendingEntry(tier_index=0, limit_price=67.73, notional=1075.18),
            PendingEntry(tier_index=1, limit_price=66.61, notional=659.38),
        )
        result = walk(
            _plan_of(entries, 60.88),
            _config(),
            (Bar(t=WALK_START, open=61.05, high=61.05, low=60.87, close=61.05),),
        )
        average = result.avg_entry_price
        denominator = average - 60.88
        mfe = (result.peak_price - average) / denominator
        self.assertLess(mfe, 0.0)
        self.assertLess(mfe, -math.ulp(average))
        self.assertEqual(mfe, -math.ulp(average) / denominator)
        self.assertGreaterEqual(result.peak_price, average - math.ulp(average))


@st.composite
def _ladder_and_staircase_path(draw: Any) -> _Case:
    """One bar per rung, each dipping to exactly that rung, then a rising climb.

    A claim about the ORDER of stop moves needs traces that carry two of them,
    and the spanning strategy barely produces any: measured on 2000 examples,
    1.25% of trail runs moved the stop twice and no re-anchor run ever did,
    because every rung filled on one bar and so only one average ever existed.

    Filling the rungs on SEPARATE bars creates one new average per rung, and
    climbing afterwards gives the ratchet somewhere to go. Measured on 1500
    examples of this strategy:

        trail     28.5% of runs move the stop twice or more (up to 6)
        re-anchor 15.1% do, and 90.7% have moves EQUAL to fills

    That last number is what gives the latch property its teeth: the bound is
    tight in most runs, so a latch that re-anchored twice on one average would
    push the count past the number of fills.

    This generator is CONDITIONED on reaching that region. It is not a sample of
    plausible market paths and nothing about frequencies should be read off it.
    """
    entries, floor, tranches = _ladder(draw)
    top = entries[0].limit_price
    r_unit = top - floor
    bars = []
    moment = WALK_START
    for entry in entries:
        limit = entry.limit_price
        bars.append(Bar(t=moment, open=limit, high=limit, low=limit * 0.9995, close=limit))
        moment += MINUTE
    level = top
    for _ in range(draw(st.integers(min_value=2, max_value=6))):
        level = level + r_unit * draw(_price(0.05, 0.6))
        bars.append(Bar(t=moment, open=level * 0.999, high=level, low=level * 0.995, close=level))
        moment += MINUTE
    return entries, floor, tranches, tuple(bars)


class TheTrailRatchetNeverLowersTheStopTest(PropertyTestCase):
    """Section 6.3, the trail half -- stated across the WHOLE trace.

    The section scopes monotonicity to "within one average fill". For the trail
    that is strictly WEAKER than the truth and does not need stating: the
    ratchet compares the clamped level against `last_trailed_level`, which
    persists across fills, so the whole trace is monotone. Measured: zero
    decreases in 11 997 generated runs.

    Scoping it to an epoch would also force the test to RECONSTRUCT the epoch
    from the fill events, which is `cash / units` plus the latch predicate --
    the production arithmetic itself. A test that recomputes what it is checking
    passes whenever its own copy agrees with the original, including when both
    are wrong.
    """

    @given(_ladder_and_staircase_path())
    def test_every_trailed_level_is_at_or_above_the_one_before(self, case: _Case) -> None:
        entries, floor, tranches, bars = case
        result = walk(_plan_of(entries, floor, tranches, reaction=TRAIL), _config(), bars)
        levels = [event.after for event in result.events if event.kind == "stop_moved"]
        self.assertEqual(levels, sorted(levels), msg=f"the trail lowered a stop: {levels}")


class TheReanchorLatchAllowsOneMovePerFillTest(PropertyTestCase):
    """Section 6.3, the re-anchor half -- stated without reconstructing an epoch.

    Across fills the re-anchored stop is deliberately NOT monotone: a later,
    lower rung re-anchors LOWER, because the clamp compares the target with the
    declared floor and not with the level standing
    (`test_a_later_rung_fill_reanchors_LOWER` pins the case). So monotonicity is
    the wrong claim here, and the latch is the right one.

    The latch says one re-anchor per fill-complete AVERAGE. Every new average
    comes from a fill, so the observable form is `stop_moved` events no more
    numerous than `entry_filled` events -- readable straight off the trace, with
    no average recomputed. Measured: zero runs with two moves inside one average
    in 14 913 generated runs.
    """

    @given(_ladder_and_staircase_path())
    def test_no_more_reanchors_than_fills(self, case: _Case) -> None:
        entries, floor, tranches, bars = case
        result = walk(_plan_of(entries, floor, tranches, reaction=REANCHOR), _config(), bars)
        kinds = _kinds(result)
        self.assertLessEqual(
            kinds.count("stop_moved"),
            kinds.count("entry_filled"),
            msg=f"more re-anchors than fills: {kinds}",
        )


class TheStaircaseGeneratorReachesMultipleStopMovesTest(PropertyTestCase):
    """The positive control the ORDERING properties cannot supply themselves.

    A claim that a list is non-decreasing is satisfied by a list of one element,
    and by an empty one. So a mutation that does not silence the trail but caps
    it at a single move leaves `TheTrailRatchetNeverLowersTheStopTest` green --
    measured: flipping the ratchet comparison in
    `broker_contract.stop_decision` (`clamped <= floor + TRAIL_STEP_EPS` to
    `>=`) collapsed the move count from a baseline where 28.5% of runs moved the
    stop twice or more to a run where NONE did, and all nine properties stayed
    green.

    This control is what fails there. It asserts the EXISTENCE the ordering
    claim presupposes, which is a different claim and belongs in a different
    test.
    """

    MIN_MULTI_MOVE_SHARE = 0.10

    _MOVES: dict[int, int] = {}

    @given(_ladder_and_staircase_path())
    def a_move_count_probe(self, case: _Case) -> None:
        """Deliberately NOT named ``test_*``: the test below drives it."""
        entries, floor, tranches, bars = case
        result = walk(_plan_of(entries, floor, tranches, reaction=TRAIL), _config(), bars)
        moved = sum(1 for event in result.events if event.kind == "stop_moved")
        self._MOVES[moved] = self._MOVES.get(moved, 0) + 1

    def test_the_trail_moves_the_stop_more_than_once_often_enough_to_order_it(self) -> None:
        self._MOVES.clear()
        self.a_move_count_probe()
        total = sum(self._MOVES.values())
        multi = sum(count for moves, count in self._MOVES.items() if moves >= 2)
        self.assertGreaterEqual(
            multi / total,
            self.MIN_MULTI_MOVE_SHARE,
            msg=(
                f"only {multi}/{total} generated runs moved the stop twice, so the "
                f"ordering property has nothing to order: {dict(sorted(self._MOVES.items()))}"
            ),
        )

    def test_three_rungs_at_one_price_put_the_trough_two_ulps_above_the_average(self) -> None:
        # Hypothesis found this one while the property still used the average.
        # All three rungs clear the bar's open, so all three fill at 27.0625 --
        # and `cash / units`, summed over three fills, lands TWO ULPs below that
        # single fill price. One ULP is therefore not a bound even in price
        # space, and the size of the error grows with the number of fills.
        entries = (
            PendingEntry(tier_index=0, limit_price=30.5, notional=2749.047970479705),
            PendingEntry(tier_index=1, limit_price=29.929038112127184, notional=2749.047970479705),
            PendingEntry(tier_index=2, limit_price=28.99375567112321, notional=322.1540590405904),
        )
        result = walk(
            _plan_of(entries, 25.36953621223281, (DeclaredTranche(0, 31.453125, 1.0),)),
            _config(),
            (Bar(t=WALK_START, open=27.0625, high=28.0, low=27.0625, close=28.0),),
        )
        average = result.avg_entry_price
        self.assertEqual(
            [event.price for event in result.events if event.kind == "entry_filled"],
            [27.0625, 27.0625, 27.0625],
        )
        self.assertEqual(result.trough_price - average, 2.0 * math.ulp(average))
        # The tolerance-free form holds on the very case that breaks the other.
        self.assertLessEqual(result.trough_price, 27.0625)


class ThePrefixOfAWalkDoesNotDependOnTheFutureTest(PropertyTestCase):
    """The engine's own promise: single-pass and CAUSAL.

    Section 3 calls the walk single-pass and causal, and section 6.3 never
    states the property that follows from it -- that what the walk did up to
    some bar cannot change because LATER bars exist. It is the one claim here
    about the engine's shape rather than about a measure, and it is what would
    catch future-bar leakage.

    `horizon_open` is excluded, and the exclusion is the whole subtlety: it is a
    terminal marker emitted when the series ends with the position still open,
    so it MUST differ between a prefix and the full path. A first version of
    this check compared it too and reported 186 differences in 400 runs, none of
    them a defect. With it excluded: zero differences in 3000 runs.
    """

    TERMINAL_KINDS = frozenset({"horizon_open"})

    def _causal_events(self, result: Any, until: int) -> list[tuple[Any, ...]]:
        return [
            (event.t, event.kind, getattr(event, "after", None), getattr(event, "price", None))
            for event in result.events
            if event.kind not in self.TERMINAL_KINDS and event.t <= until
        ]

    @given(_ladder_and_spanning_path(), st.integers(min_value=1, max_value=MAX_BARS))
    def test_a_prefix_walk_agrees_with_the_full_walk_over_that_prefix(
        self, case: _Case, cut: int
    ) -> None:
        entries, floor, tranches, bars = case
        prefix = bars[:cut]
        plan = _plan_of(entries, floor, tranches, reaction=TRAIL)
        full = walk(plan, _config(), bars)
        partial = walk(plan, _config(), prefix)
        until = prefix[-1].t
        self.assertEqual(self._causal_events(full, until), self._causal_events(partial, until))


class TheAmbiguousBarCounterNeedsALadderTest(PropertyTestCase):
    """Section 6.3, the `snu_bars` bullet -- the part one ordering can settle.

    The section's own claim is that the counter is positive ONLY when a bar's
    unsettleable ordering changed the money. That needs replaying the same bars
    under a SECOND intra-bar ordering, and `intrabar_rule` is declared and
    single, so there is nothing to compare against. The two exceptions the
    section names by hand are already pinned in `test_walk.py`.

    Two weaker claims ARE settleable under one ordering, and both discriminate:

    * with no take-profit ladder and no reaction, nothing competes with the
      declared stop and the counter is zero. Measured: 0 of 3000 runs count a
      bar without a ladder, against 18.2% with one.
    * a bar is counted at most once, so the counter never exceeds the number of
      bars. This generalises `test_a_bar_the_stop_closed_is_counted_once_by_row_one`.
    """

    @given(_ladder_and_spanning_path())
    def test_a_document_with_no_ladder_and_no_reaction_counts_no_bar(self, case: _Case) -> None:
        entries, floor, _tranches, bars = case
        result = walk(_plan_of(entries, floor, ()), _config(), bars)
        self.assertEqual(result.snu_bars, 0)

    @given(_ladder_and_spanning_path())
    def test_no_bar_is_counted_twice(self, case: _Case) -> None:
        entries, floor, tranches, bars = case
        result = walk(_plan_of(entries, floor, tranches, reaction=TRAIL), _config(), bars)
        self.assertLessEqual(result.snu_bars, len(bars))
