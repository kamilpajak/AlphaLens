"""The entry-watch pass's own helpers: arming geometry, refusals and session lows.

Step 4 of the ``control_loop`` partition. What moved here is the MAXIMAL set of
``control_loop`` definitions that the entry-watch tick stage reaches
EXCLUSIVELY — no other tick stage and no ``build_default_deps`` wiring root
reaches them — and that is CLOSED under every module-level name it uses. The
closure is what makes the move safe: nothing here calls, or names, anything left
behind in ``control_loop``, so there is no back-edge and no import cycle.
Measured, not judged: 20 functions and 5 definitions, 614 lines.

Three groups live here:

* **Session lows and point sampling** — ``_drain_session_lows``,
  ``_point_sample_bids`` and ``_reseed_vetoed_point_lows``: the price evidence
  an entry watch decides on.
* **Watch state** — ``_active_entry_watches``, ``_entry_watch_initial_state``,
  ``_entry_watch_config_from_record``, ``_entry_watch_state_from_kind``: how a
  journal record becomes a live watch.
* **Arm refusals** — ``_ArmRefusal`` with ``_brief_plan_arm_refusal``,
  ``_exit_plan_shape_refusal``, ``_inside_exit_region_note``,
  ``_governing_plan_lookup`` and ``_resolve_arm_refusal``: why a watch that
  fired may still refuse to arm. Step 3 deliberately left
  ``_governing_plan_lookup`` in ``control_loop`` because it answers in an
  ``_ArmRefusal``, which was not a journal concept; here it is the local one.

What deliberately did NOT move, and why — each of these still calls something
that stays in ``control_loop``, so taking it would open a back-edge:

* ``_run_entry_watch_pass`` itself, with ``_advance_one_entry_watch``,
  ``_arm_native_trail``, ``_build_entry_watch_feed`` and
  ``_cancel_working_entry_orders``: they take ``LoopDeps`` and ``TickReport``,
  both defined in ``control_loop`` and used by 42 and 38 functions there.
* ``_entry_measurement_blob`` and ``_entry_trail_mode_tag``, shared with the
  entry-trail reconcile pass; ``_read_entry_order`` and
  ``_entry_order_filled_qty``, shared with that pass and the stop-fill
  reconcile pass; ``_position_uic``, ``_release_feed_scope`` and
  ``_default_live_exits_feed_factory``, shared with the live-exits and
  protection passes. A function shared between two stages belongs to neither.

``control_loop`` reaches everything here through the module prefix
(``entry_watch.<name>``) and never by a ``from`` import: a ``from`` import would
give ``control_loop`` its own binding, and a test patching this module would not
reach it.

One deliberate effect: code moved here logs under ``entry_watch`` instead of
``control_loop``. Nothing outside the test suite keys on the logger name.
"""

from __future__ import annotations

import datetime as dt
import logging
import math
import os
from collections.abc import Mapping
from typing import Any, NamedTuple

from broker_contract.contract import Broker, BrokerError
from broker_contract.price_feed import PriceFeed, SupportsSessionLow
from broker_contract.sizing import TpTranchePlan

from alphalens_pipeline.brokers.automanager import (
    entry_trail_geometry,
    entry_trail_watcher,
    entry_trails,
    stop_journal,
)
from alphalens_pipeline.brokers.automanager.costs import (
    EXIT_EDGE_MIN_BPS,
    apportioned_coverage_violation,
    cost_gate_facts,
    single_full_position_tranche_violation,
)
from alphalens_pipeline.brokers.automanager.labels import entry_label_from_crid
from alphalens_pipeline.brokers.automanager.live_exit_engine import apportion_tranche_quantities

logger = logging.getLogger(__name__)


def _drain_session_lows(
    feed: PriceFeed, uic_to_instrument: Mapping[int, tuple[str, str]]
) -> dict[int, float | None]:
    """Drain the 1 Hz touch-latch ONCE per DISTINCT watched uic this tick.

    A laddered pick has N tiers ALL on the same uic, all active this tick. The
    drain is a POP (read-and-reset), so calling it per-tier would let the FIRST
    tier consume the sub-tick low and starve the deeper tiers (where the miss
    the latch exists to close actually lives) — the winner being dict-iteration
    order. Draining once per uic here and passing the SAME value into every
    tier's combine kills that race.

    Called UNCONDITIONALLY every tick (not only when a point-sample exists) so
    the accumulation window stays inter-tick — the pop resets it. A feed without
    :class:`SupportsSessionLow` (the OFF/degraded null feed) yields no lows,
    which is the safe degraded behaviour (point-sample only)."""
    if not isinstance(feed, SupportsSessionLow):
        return {}
    lows: dict[int, float | None] = {}
    for uic in uic_to_instrument:
        try:
            lows[uic] = feed.session_low(uic)
        # Broad on purpose (mirrors _point_sample_bids): one bad uic must not
        # abort the drain for the others.
        except Exception:
            lows[uic] = None
    return lows


def _point_sample_bids(
    feed: PriceFeed, uic_to_instrument: Mapping[int, tuple[str, str]]
) -> dict[int, float | None]:
    """Point-sample the fresh reference bid ONCE per DISTINCT watched uic this
    tick (memo trap #8: detection is bid-referenced, matching the LIVE V1
    probe). ``None`` on any doubt — a vetoed/None point or a non-finite/
    non-positive bid — which the engine treats as the freshness/trust veto (no
    watch progress this tick).

    ONE shared sample per uic, for the same reason the drain above is once per
    uic: every tier of a laddered pick must read the SAME verdict, and the
    point-veto reseed (:func:`_reseed_vetoed_point_lows`) must judge the SAME
    sample the per-tier combine will use — a second ``latest`` read could
    disagree mid-tick (the stream keeps applying frames underneath) and either
    destroy the drained low again or reseed one that was acted on."""
    points: dict[int, float | None] = {}
    for uic in uic_to_instrument:
        try:
            point = feed.latest(uic)
            bid = None if point is None else point.bid
            points[uic] = bid if bid is not None and math.isfinite(bid) and bid > 0.0 else None
        # Broad on purpose (mirrors _drain_session_lows): one bad uic — a
        # raising feed OR a structurally invalid point (non-numeric bid) —
        # must veto that uic, never abort the sampling for the others or
        # starve the protection pass that runs after this one.
        except Exception:
            points[uic] = None
    return points


def _reseed_vetoed_point_lows(
    feed: PriceFeed,
    uic_points: Mapping[int, float | None],
    uic_lows: Mapping[int, float | None],
) -> None:
    """Hand a drained 1 Hz running low BACK to the feed's accumulator when this
    tick's point-sample for its uic is vetoed (the 2026-08-18 incident: OLN's
    latch held a REAL touch at 18.61 below the 18.6217 tier limit, the
    change-driven stream had sent no frame for >3s so the point-sample was
    veto-stale, and the unconditional drain pop destroyed the touch evidence
    forever). The doctrine "never make a watch decision without a fresh
    point-sample" STANDS — the combine still discards the low on a vetoed point
    — the low merely SURVIVES (min-merged back, so a deeper accrual racing in
    from the reader thread stays the winner) until a tick whose point-sample is
    fresh.

    ONLY the point-veto case reseeds, once per uic. The per-tier combine's other
    discards (the ``awaiting_fresh_low`` re-arm guard, an untrusted latch)
    distrust the LOW itself (G1 anti-gap) and stay FINAL — reseeding those would
    let a stale/pre-session wick survive until trusted and fire into a gap.

    Survival is CEILING-BOUNDED by the recovery gate, not by this function: a
    preserved low can only ever be ACTED on when the recovery tick lands within
    ``STALE_FIRE_GAP`` of the watcher's last fresh tick
    (:meth:`EntryTierWatcher.latch_low_trusted`), and a recovery beyond it
    discards the low FINALLY (a fresh-point tick never re-enters this reseed).
    A LULD pause cannot hide inside that window: the INITIAL pause is a hard
    5 min (== ``STALE_FIRE_GAP``) and starts AFTER the last fresh sample, so an
    LULD-spanning recovery arrives beyond the gate; the pre-open quiet spell is
    the overnight gap, far beyond it.

    TWO CORRECTIONS to what this comment used to claim, both recorded rather
    than left implicit:

    1. The claim was "a market discontinuity cannot hide inside that window for
       US equities". That over-reaches. Only the INITIAL LULD pause carries the
       5 min floor. Nasdaq's T1/T2 (news pending / news released), T12
       (additional information requested), operational halts and M (opening or
       closing imbalance) are CONDITION-based and carry no minimum duration at
       all — a short news halt can end well inside ``STALE_FIRE_GAP`` and let a
       pre-halt low look actionable. That gap is PRE-EXISTING, not introduced
       here; it is tracked separately rather than re-buried in this docstring.
    2. #1397 NARROWED the margin. "The last fresh sample" is now a quote up to
       ``DEFAULT_MAX_AGE_S`` (45 s) old rather than 3 s, so an LULD-spanning
       recovery clears the gate by roughly 4m15s of slack instead of ~5 min.
       It still clears, but the headroom is no longer an order of magnitude,
       and it shrinks further if the bound is ever widened again.

    Deliberately NO tick-count cap on the reseed chain. The ORIGINAL reason was
    that a 3 s point bound against a 45 s tick point-vetoed MOST drain instants
    on a thin change-driven stream (the OLN incident profile), making a
    multi-tick veto chain the normal path a real touch survives. #1397 removes
    that premise: at a 45 s bound most drains now carry a fresh point, so the
    chain becomes rare. The absence of a cap is kept anyway, for the reason that
    outlived the arithmetic — a cap discards a real touch on exactly the ticks
    where the stream is degraded, which is when the evidence matters most."""
    if not isinstance(feed, SupportsSessionLow):
        return
    for uic, low in uic_lows.items():
        if low is None or uic_points.get(uic) is not None:
            continue
        try:
            feed.reseed_session_low(uic, low)
        # Broad on purpose (mirrors _drain_session_lows): one bad uic must not
        # abort the reseed for the others.
        except Exception:
            logger.warning("entry-watch: failed to reseed the drained low for uic %d", uic)


def _active_entry_watches(
    fold: entry_trails.EntryTrailFold,
) -> dict[str, Mapping[str, Any]]:
    """The non-terminal watch_open record per crid — the watches to advance this
    tick. A tier with a terminal marker or no watch_open is excluded, and so is a
    RESTING native order (PR-T2b): a ``trail_armed`` tier whose ``armed_order_id``
    is set is owned by the broker (the server ratchets + fires; the fill is
    monitored by reconcile), so the watch pass no longer drives it. An
    arm-in-progress tier (``trail_armed`` with a NULL id — the G3 write-ahead
    line before an unconfirmed POST) STAYS active so the executor re-drives it to
    completion."""
    active: dict[str, Mapping[str, Any]] = {}
    for crid, state in fold.tiers.items():
        if state.terminal_kind is not None or state.watch_open is None:
            continue
        if state.latest_kind == entry_trails.KIND_TRAIL_ARMED and state.armed_order_id is not None:
            continue
        active[crid] = state.watch_open
    return active


def _has_feed_context(record: Mapping[str, Any]) -> bool:
    """Whether a watch_open record carries the uic/ticker/mic the price feed
    needs. A pre-WIRE record (reservation-only fields) is simply not fed a price
    — the watcher then vetoes every tick, never crashes."""
    return all(record.get(key) is not None for key in ("uic", "ticker", "exchange_mic"))


def _entry_watch_reference_price(
    uic_points: Mapping[int, float | None], record: Mapping[str, Any]
) -> float | None:
    """The fresh reference scalar for one watch, read from this tick's shared
    per-uic point samples (:func:`_point_sample_bids` — where the veto logic
    lives). ``None`` on any doubt — no feed context, or a vetoed point — which
    the engine treats as the freshness/trust veto (no watch progress this
    tick)."""
    if not _has_feed_context(record):
        return None
    return uic_points.get(int(record["uic"]))


def _entry_watch_initial_state(
    tier_state: entry_trails.EntryTrailTierState | None,
) -> entry_trail_watcher.WatchState:
    """Resolve the resumable watch state on reconstruction (PR-T2b restart).

    A ``trail_armed`` tier resumes to a state that depends on whether its POST
    was confirmed: a REAL ``armed_order_id`` -> TRAIL_ARMED (terminal, the broker
    owns the resting order; a resting tier is already excluded from the active
    set, but this is defensive if one is ever reconstructed); a NULL id (the G3
    write-ahead line before an unconfirmed POST) -> TOUCHED, so the executor
    re-drives the arm to completion. Any other kind resolves via
    :func:`_entry_watch_state_from_kind`."""
    if tier_state is None:
        return entry_trail_watcher.WatchState.WATCHING
    if tier_state.latest_kind == entry_trails.KIND_TRAIL_ARMED:
        if tier_state.armed_order_id is not None:
            return entry_trail_watcher.WatchState.TRAIL_ARMED
        return entry_trail_watcher.WatchState.TOUCHED
    return _entry_watch_state_from_kind(tier_state.latest_kind)


def _entry_watch_config_from_record(
    record: Mapping[str, Any],
) -> entry_trail_watcher.TierWatchConfig | None:
    """Rebuild the immutable :class:`~entry_trail_watcher.TierWatchConfig` from a
    watch_open journal record. ``None`` (logged) on any missing/malformed field
    — a doubt about a watch's parameters skips it, never crashes the pass."""
    try:
        return entry_trail_watcher.TierWatchConfig(
            crid=str(record["crid"]),
            tier_limit=float(record["limit"]),
            d_bps=int(record["d_bps"]),
            window_end=dt.datetime.fromisoformat(str(record["window_end"])),
            qty=float(record["qty"]),
            fx_rate=None if record.get("fx_rate") is None else float(record["fx_rate"]),
            next_tier_limit=(
                None if record.get("next_tier_limit") is None else float(record["next_tier_limit"])
            ),
        )
    except (KeyError, TypeError, ValueError):
        logger.warning(
            "entry-watch: watch_open record for %s is unreconstructable — skipping",
            entry_label_from_crid(str(record.get("crid"))),
        )
        return None


def _entry_watch_state_from_kind(kind: str | None) -> entry_trail_watcher.WatchState:
    """Map the fold's latest non-terminal kind back to a resumable watch state
    (memo §5 restart). ``touched``/``trough`` resume TOUCHED; ``trail_armed``
    resumes WOULD_FIRE (terminal — a would-fired tier must never re-fire on
    restart); anything else (``watch_open``/unknown) resumes WATCHING."""
    if kind in (entry_trails.KIND_TOUCHED, entry_trails.KIND_TROUGH):
        return entry_trail_watcher.WatchState.TOUCHED
    if kind == entry_trails.KIND_TRAIL_ARMED:
        return entry_trail_watcher.WatchState.WOULD_FIRE
    return entry_trail_watcher.WatchState.WATCHING


def _entry_trail_orders_allowed() -> bool:
    """Whether ``ALPHALENS_BROKER_ALLOW_ORDERS`` is armed (read at call time —
    mirrors :func:`_live_exits_orders_allowed`).

    The SIM/LIVE safety rail: with ALLOW_ORDERS off the executor places NOTHING
    (no write-ahead, no POST), so a flag-ON-but-orders-disabled run is a clean
    no-op — the tier simply stays TOUCHED and re-attempts once orders are
    re-enabled. Defense in depth: :meth:`SaxoBroker.place_trailing_stop` ALSO
    raises ``BrokerCapabilityError`` when the flag is off; gating here first just
    avoids journalling a write-ahead line that could never be completed."""
    from alphalens_pipeline.brokers.automanager import safety

    return os.environ.get(safety.ALLOW_ORDERS_ENV) == "1"


def _journal_trail_armed(
    crid: str,
    *,
    order_id: str | None,
    trigger: float | None,
    ceiling: float | None,
    distance: float | None,
) -> None:
    """Append one ``trail_armed`` line (the G3 write-ahead uses ``order_id=None``
    before the POST; the post-POST line fills the real id in — the fold's
    latest-wins semantics adopt it).

    ``ceiling`` rides the same latest-wins path (#1317): the write-ahead carries
    the GEOMETRY value (all that is known before the POST) and the post-POST line
    the TICK-QUANTIZED value the adapter put on the wire. Journaled rather than
    left to be recomputed later, because the obvious later reconstruction is
    wrong whenever the trough moved between the arm and the terminal.

    ``distance`` rides for the same reason and is the one arm-time number that no
    later read can recover (#1635): the server ratchets the trigger down as
    ``trough + distance`` holding it fixed, while the arm priced it off the
    AMBIENT ``ALPHALENS_BROKER_ENTRY_TRAIL_BPS`` — which the operator can widen
    while this watch is open — and the ``watch_open`` record's ``d_bps`` is frozen
    at drain time and rides a re-arm for days.

    Unlike the ceiling, this is the REQUESTED distance, not a wire value:
    ``PlacedOrder`` reports ``stop_limit_price`` and no distance, and the adapter
    rounds the distance to whole ticks before the POST (``_floor_to_whole_ticks``
    in the Saxo broker — NEAREST tick with a one-tick floor, despite the name).

    That alignment residual is LARGER than the arithmetic this replaces, which is
    the opposite of what an earlier version of this docstring claimed. Measured
    2026-09-30 on the 37 recorded terminals that carry the stamp (#1635 comment):
    the alignment is median 0.51 bps and at most 4.33 bps, against a corrected
    arithmetic term of median 0.00 bps and at most 1.58 bps, and it exceeds the
    correction in 35 of the 37 rows. Cheap names are worst, because at 50 bps the
    distance is a few cents and a penny tick is a large share of it: an $11 name
    asked for 0.0552 and sent 0.06.

    So journaling the distance removes a term no later read can reconstruct; it
    does NOT make the stamp accurate against the order that actually rested."""
    entry_trails.append_entry_trail_line(
        {
            "kind": entry_trails.KIND_TRAIL_ARMED,
            "crid": crid,
            "order_id": order_id,
            entry_trails.KEY_TRIGGER: None if trigger is None else float(trigger),
            entry_trails.KEY_CEILING: None if ceiling is None else float(ceiling),
            entry_trails.KEY_DISTANCE: None if distance is None else float(distance),
        }
    )


class _EntryOrderLookup(NamedTuple):
    """The result of the G3 adopt read. ``read_ok`` is False when the book could
    not be read at all, which is NOT the same fact as "no order rests": a caller
    that terminates the watch must only do so on a read that actually
    succeeded (issue #1112)."""

    order_id: str | None
    read_ok: bool


def _find_working_entry_order(broker: Broker, external_reference: str) -> _EntryOrderLookup:
    """The order id of a WORKING order whose ``ExternalReference`` matches
    (idempotent re-arm, memo §3 G3): a crash between the POST and the id-journal
    leaves a native order at Saxo the journal recorded only with a null id — on
    the next TOUCHED tick, adopt it rather than resting a second trail. A
    ``BrokerError`` reading the book returns ``(None, read_ok=False)`` — the
    re-POST path treats that as not-found (the deterministic ``request_id`` +
    Saxo's 15 s dedup still guard the short re-POST window), while any path that
    would TERMINATE the watch must stand down until the book is readable."""
    try:
        for state in broker.list_open_orders():
            if state.external_reference == external_reference:
                return _EntryOrderLookup(str(state.order_id), True)
    except BrokerError as exc:
        logger.warning(
            "entry-trail arm: list_open_orders failed for dedup check (%s) — "
            "relying on request-id dedup",
            exc,
        )
        return _EntryOrderLookup(None, False)
    return _EntryOrderLookup(None, True)


def _stamped_exit_target(record: Mapping[str, Any]) -> float | None:
    """The exit target already stamped on this watch's ``watch_open`` line, or
    ``None`` when there is none to compare against (issue #1112 step 1).

    Reads ONLY data already in scope — the ``geometry`` blob
    :func:`_placed_geometry_stamp` wrote at routing time, whose ``geometry_tp``
    is the very number :func:`_geometry_tranche_ladder` turns into the single
    tranche the live exit engine fires on. No policy is resolved and no
    environment is read here (that would reintroduce the per-tick resolve the
    ExitPolicy refactor removed).

    ``None`` (fail open, arm as before) when: the line carries no stamp, the
    stamp is not a mapping, ``applied`` is falsey (the placed exit is the
    brief's own ladder, not this target), or ``geometry_tp`` is absent /
    unparseable / non-finite / non-positive.
    """
    stamp = record.get("geometry")
    if not isinstance(stamp, Mapping) or not stamp.get("applied"):
        return None
    raw = stamp.get("geometry_tp")
    try:
        target = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(target) or target <= 0.0:
        return None
    return target


def _inside_exit_region_note(
    record: Mapping[str, Any], d_bps: int, reference: float, trough: float, qty: float
) -> str | None:
    """A one-line operator note when this tier's own exit target cannot pay for
    the position its realistic fill would open, else ``None`` (issue #1112
    step 1: refuse unless ``exit_target > fill_estimate + round_trip_cost +
    E_min``).

    LIVE 2026-08-24 (SMG): the top tier's limit 59.786017 sat above the exit
    target 59.6277 the policy derived from the alloc-weighted PLANNED blend of
    the whole ladder, so the fill at 59.9261 was already past its take-profit
    and the exit engine sold it 62 seconds later for about -380 bps net.

    The fill estimate comes from :func:`entry_trail_geometry.entry_fill_estimate`
    — the armed order's own broker-enforced ceiling — NOT from the tier limit:
    the live fill printed 23 bps ABOVE its limit, so a check on the nominal
    limit would have seen nothing wrong. Pinned end to end by
    ``test_entry_watch_wiring.py::
    test_the_gate_uses_the_realistic_fill_estimate_not_the_nominal_tier_limit``.

    Fails OPEN (``None``, arm as before) on any unusable input — no stamp, a
    degenerate geometry, a non-positive qty.
    """
    target = _stamped_exit_target(record)
    estimate = entry_trail_geometry.entry_fill_estimate(
        reference=reference, trough=trough, d_bps=d_bps
    )
    facts = cost_gate_facts(
        instrument_currency=record.get("instrument_currency"),
        sizing_currency=record.get("sizing_currency"),
        exchange_mic=record.get("exchange_mic"),
    )
    if not entry_trail_geometry.arms_inside_exit_region(
        fill_estimate=estimate, exit_target=target, qty=qty, facts=facts
    ):
        return None
    if estimate is None or target is None:
        return None  # unreachable: the gate above returns False on either being None
    return (
        f"tier would fill inside the exit region: fill estimate {estimate:.4f}, "
        f"exit target {target:.4f} does not clear round-trip cost + E_min "
        f"{EXIT_EDGE_MIN_BPS:.0f} bps"
    )


_ARM_REFUSAL_INSIDE_EXIT_REGION = "inside-exit-region"

_ARM_REFUSAL_EXIT_PLAN_SHAPE = "exit-plan-shape"


class _ArmRefusal(NamedTuple):
    """One reason a tier must not arm. ``terminal`` False means "we do not know
    yet" — the tier neither arms nor ends, and settles on a later tick."""

    note: str
    reason: str
    terminal: bool


def _governing_plan_lookup(
    record: Mapping[str, Any],
) -> tuple[tuple[tuple[TpTranchePlan, ...], float, float] | None, _ArmRefusal | None]:
    """The journaled ``tranche_plan`` governing this watch's uic, or the refusal
    that stands in for it — the shared read half of the two plan-reading arm
    gates (:func:`_exit_plan_shape_refusal`, :func:`_brief_plan_arm_refusal`).

    Exactly one of the pair is non-None. The stances are the gates' contract:
    a record with no uic and a uic with no plan on record refuse TERMINALLY
    (the router journals the plan BEFORE the ``watch_open`` lines, so a missing
    plan means the exit shape is unknown); a journal READ failure refuses
    NON-terminally — it is not evidence about the plan, and this runs inside
    ``_run_entry_watch_pass``, which has no per-watch exception boundary, so an
    OSError let out would abort the tick for every other watch too.
    """
    uic = stop_journal._coerce(record, "uic", int)
    if uic is None:
        return None, _ArmRefusal(
            "entry watch carries no uic — the exit plan cannot be resolved",
            _ARM_REFUSAL_EXIT_PLAN_SHAPE,
            terminal=True,
        )
    try:
        plan = stop_journal.fold_tranche_plans(stop_journal._iter_standalone_stop_journal()).get(
            uic
        )
    # Broad on purpose, mirroring _retract_stale_tranche_plans' sweep: a journal
    # read failure degrades to "unknown", never to an aborted pass.
    except Exception:
        logger.warning(
            "entry-trail arm: exit-plan read failed for uic %d — deferring the check",
            uic,
            exc_info=True,
        )
        return None, _ArmRefusal(
            f"exit plan for uic {uic} could not be read",
            _ARM_REFUSAL_EXIT_PLAN_SHAPE,
            terminal=False,
        )
    if plan is None:
        return None, _ArmRefusal(
            f"no exit plan on record for uic {uic}",
            _ARM_REFUSAL_EXIT_PLAN_SHAPE,
            terminal=True,
        )
    return plan, None


def _exit_plan_shape_refusal(record: Mapping[str, Any], position_qty: float) -> _ArmRefusal | None:
    """Why this tier must not arm on the exit plan governing its uic, else
    ``None`` (issue #1112 round 2, point 2).

    :func:`_inside_exit_region_note` charges the round trip at the quantity of
    the position the arm would OPEN. The exit engine charges it at the quantity
    of the tranche it SELLS, and the per-fill USD minimum makes the smaller of
    the two draw the higher bar. Whole-position pricing at arm time is therefore
    conservative only while the exit plan is one tranche selling everything —
    which is what the geometry policy produces today
    (:func:`_geometry_tranche_ladder`), and what all three LIVE ``tranche_plan``
    records carried on 2026-08-25. This turns that into a checked contract.

    FAILS CLOSED, unlike the exit-region note: a uic with no governing plan on
    record is refused too. The router journals the ``tranche_plan`` BEFORE the
    ``watch_open`` lines, so a missing plan means the exit shape this gate
    depends on is unknown — arming into that would open a position whose
    take-profit the rail cannot describe.

    Scoped to the arm gate's own reach: ``None`` when no applied geometry target
    is stamped, because there the arm gate does not price anything. The read
    stances (missing plan terminal, read failure deferred) live in
    :func:`_governing_plan_lookup`.
    """
    if _stamped_exit_target(record) is None:
        return None
    plan, lookup_refusal = _governing_plan_lookup(record)
    if plan is None:
        return lookup_refusal
    tranches, reference_qty, _stop = plan
    violation = single_full_position_tranche_violation(
        # Exactly how live_exit_engine.plan_tranche_exits sizes each tranche.
        tranche_quantities=apportion_tranche_quantities(
            reference_qty=reference_qty,
            tranche_fracs=tuple(t.tranche_frac for t in tranches),
        ),
        position_qty=reference_qty,
    )
    if violation is None:
        return None
    return _ArmRefusal(
        f"{violation} (arm gate priced {position_qty:g} share(s))",
        _ARM_REFUSAL_EXIT_PLAN_SHAPE,
        terminal=True,
    )


def _brief_plan_arm_refusal(
    record: Mapping[str, Any], d_bps: int, reference: float, trough: float
) -> _ArmRefusal | None:
    """Why this tier must not arm against the BRIEF's own take-profit ladder,
    else ``None`` (issue #1112, breakeven_trail follow-up).

    A brief pick supplies no ``initial_levels`` (#1414), so the placed exit is
    its multi-tranche ladder, ``_stamped_exit_target`` returns ``None``, and the
    two geometry-scoped gates above price nothing. This gate closes that hole
    with the same issue-#1112 condition, evaluated against the plan that will
    ACTUALLY govern the position:

        refuse unless  tp1 > fill_estimate + round_trip_cost + E_min

    where tp1 is the shallowest ACTIVE tranche of the journaled ``tranche_plan``
    and the cost is priced at THAT tranche's apportioned share count — the exact
    quantity ``live_exit_engine.plan_tranche_exits`` will sell there, so the arm
    bar and the exit bar coincide for the tranche this gate prices.

    VACUOUS when the plan declares NO tranche (#1511): there is no tp1 to
    price, so the gate has nothing to say, and the position is protected by its
    disaster stop and managed by whatever exit policy it declared — neither of
    which reads this ladder. Same answer :func:`_now_cost_gate_violation` has
    always given on the immediate path. That branch became reachable only when
    the watch path started journaling a declared-empty ladder as a plan; before
    that such a uic had no plan at all and was refused by the lookup below.

    Scoped to watches WITHOUT an applied geometry target (the geometry path
    keeps its own pair of gates above). The journal-read stances — read failure
    defers (non-terminal), a missing plan refuses terminally (the router writes
    the plan before the watch, so a missing one means the exit shape is
    unknown) — are shared with the geometry shape gate via
    :func:`_governing_plan_lookup`. A plan whose apportioned tranches do not
    cover the whole position refuses terminally
    (:func:`~alphalens_pipeline.brokers.automanager.costs.apportioned_coverage_violation`).
    The COST comparison itself fails open on degenerate geometry, mirroring
    ``arms_inside_exit_region``.
    """
    if _stamped_exit_target(record) is not None:
        return None
    plan, lookup_refusal = _governing_plan_lookup(record)
    if plan is None:
        return lookup_refusal
    tranches, reference_qty, _stop = plan
    if not tranches:
        # #1511: this gate prices tp1, so a document that declared NO
        # take-profit gives it nothing to price -- vacuous, the same answer
        # `_now_cost_gate_violation` has always given on the immediate path.
        # Such a position is protected by its disaster stop and managed by
        # whatever exit policy it declared, and neither reads this ladder.
        # Reachable only since the watch path began journaling the vacuity;
        # before that this uic had no plan at all and was refused above.
        return None
    quantities = apportion_tranche_quantities(
        reference_qty=reference_qty, tranche_fracs=tuple(t.tranche_frac for t in tranches)
    )
    violation = apportioned_coverage_violation(
        tranche_quantities=quantities, reference_qty=reference_qty
    )
    if violation is not None:
        return _ArmRefusal(f"{violation}", _ARM_REFUSAL_EXIT_PLAN_SHAPE, terminal=True)
    first_active = next(
        ((t, q) for t, q in zip(tranches, quantities, strict=True) if q > 0.0), None
    )
    if first_active is None:  # unreachable: coverage above guarantees >= 1 share
        return _ArmRefusal(
            "exit plan has no sellable tranche",
            _ARM_REFUSAL_EXIT_PLAN_SHAPE,
            terminal=True,
        )
    tranche, tranche_qty = first_active
    estimate = entry_trail_geometry.entry_fill_estimate(
        reference=reference, trough=trough, d_bps=d_bps
    )
    facts = cost_gate_facts(
        instrument_currency=record.get("instrument_currency"),
        sizing_currency=record.get("sizing_currency"),
        exchange_mic=record.get("exchange_mic"),
    )
    if not entry_trail_geometry.arms_inside_exit_region(
        fill_estimate=estimate, exit_target=tranche.target_price, qty=tranche_qty, facts=facts
    ):
        return None
    return _ArmRefusal(
        f"tier would fill inside the exit region of the brief's own ladder: fill "
        f"estimate {estimate:.4f}, first take-profit {tranche.target_price:.4f} "
        f"({tranche.tag}, {tranche_qty:g} share(s)) does not clear round-trip cost "
        f"+ E_min {EXIT_EDGE_MIN_BPS:.0f} bps",
        _ARM_REFUSAL_INSIDE_EXIT_REGION,
        terminal=True,
    )


def _resolve_arm_refusal(
    record: Mapping[str, Any], d_bps: int, reference: float, trough: float, qty: float
) -> _ArmRefusal | None:
    """The fresh-arm refusal lattice, in gate order: inside-exit-region first;
    then the exit-plan shape check (the whole-position pricing the region gate
    just did is only conservative while the exit side sells the whole position
    in one tranche — checked, never assumed, issue #1112 round 2, point 2);
    both are scoped to an APPLIED geometry target, so under a no-geometry exit
    policy (breakeven_trail, since #1183) the brief's own ladder is priced by
    its own gate last. ``None`` means the arm may proceed."""
    region_note = _inside_exit_region_note(record, d_bps, reference, trough, qty)
    refusal = (
        _ArmRefusal(region_note, _ARM_REFUSAL_INSIDE_EXIT_REGION, terminal=True)
        if region_note is not None
        else _exit_plan_shape_refusal(record, qty)
    )
    if refusal is None:
        refusal = _brief_plan_arm_refusal(record, d_bps, reference, trough)
    return refusal


_RESTING_BEARING_TERMINALS = frozenset(
    {entry_trail_watcher.WatchState.SUSPENDED, entry_trail_watcher.WatchState.EXPIRED}
)


def _terminal_leaves_a_resting_order(result: entry_trail_watcher.TickResult) -> bool:
    """Whether this tick's engine terminal (SUSPENDED/EXPIRED) could have left a
    native ``-entry-`` order resting at the broker (memo §3 G6/G9)."""
    return result.state in _RESTING_BEARING_TERMINALS


def _entry_terminal_rewritten_as_fired(
    crid: str,
    result: entry_trail_watcher.TickResult,
    order_id: str,
    filled_qty: float,
) -> entry_trail_watcher.TickResult:
    """Swap the SUSPENDED/EXPIRED terminal intent for a ``fired`` one carrying the
    real order id + realized qty (memo §5 ``fired{realized_qty}`` / G6). Any
    non-terminal intent this tick (e.g. the suspend tick's ``trough``) is kept.

    The engine's own alert is DROPPED: a filled tier did not suspend/expire, so
    the stale "suspended below next tier" alert would contradict the ``fired``
    journal line — the fill surfaces through never-naked + reconcile instead."""
    rewritten = tuple(
        entry_trail_watcher.JournalIntent(
            crid=crid,
            kind=entry_trails.KIND_FIRED,
            payload={"order_id": order_id, "realized_qty": filled_qty},
        )
        if intent.kind in entry_trails.ENTRY_TRAIL_TERMINAL_KINDS
        else intent
        for intent in result.journal_intents
    )
    return entry_trail_watcher.TickResult(rewritten, (), result.state)
