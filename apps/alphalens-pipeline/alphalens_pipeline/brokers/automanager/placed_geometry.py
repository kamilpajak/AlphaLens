"""What geometry an order was actually placed with, and how it is journaled.

Step 5 of the ``control_loop`` partition. Three groups live here:

* **Observed geometry** — ``_places_client_geometry``,
  ``_placed_geometry_stamp``, ``_geometry_without_entry_trail_note`` and
  ``_refuse_geometry_without_trail``: whether the broker took the instruction
  the client declared, and what to do when it did not.
* **Tranche journal** — ``_journal_tranche_plan`` with
  ``_journal_tranche_plan_core``, ``_geometry_tranche_ladder``,
  ``_planned_exit_levels`` and ``_is_journalable_price``: the record of what
  ladder was planned, written so a later tick can read it back.
* **Operator alerts** — ``_announce_client_geometry`` and the two alert
  prefixes it shares with the refusal path.

``_now_cost_gate_violation`` travels with this module although it reads as a
now-tranche concern: it is in the same closed block as the geometry helpers, so
taking it out would open a back-edge. Measured, not judged.

What moved is CLOSED under every module-level name it uses — nothing here names
anything left behind in ``control_loop``, so there is no import cycle.
``control_loop`` reaches it through the module prefix
(``placed_geometry.<name>``), never by a ``from`` import, so a test patching
this module reaches the binding the code actually reads.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from typing import Any

from broker_contract.sizing import TpTranchePlan

from alphalens_pipeline.brokers.automanager import (
    entry_trail_geometry,
    entry_trails,
    stop_journal,
)
from alphalens_pipeline.brokers.automanager.costs import (
    apportioned_coverage_violation,
    cost_gate_facts,
)
from alphalens_pipeline.brokers.automanager.live_exit_engine import apportion_tranche_quantities

logger = logging.getLogger(__name__)


def _sizing_currency_of(fx: Any, instrument: Any) -> str:
    """The account/sizing currency for a journal currency stamp (#1238 PR 3).

    ``fx`` is None on the same-currency path, where the sizing currency IS the
    instrument currency by construction; a stub without the attribute yields
    "" (no stamp -> the gates keep the conservative legacy facts)."""
    if fx is not None:
        return str(getattr(fx, "account_currency", "") or "")
    return str(getattr(instrument, "currency", "") or "")


def _is_journalable_price(value: float | None) -> bool:
    """A price the journal may carry verbatim: present, finite and strictly
    positive. ``stop_journal._build_tranche_plan_line`` writes ``float(...)`` straight through,
    so a None/NaN/zero level from a future geometry policy must be caught HERE
    rather than poisoning the ladder the live-exit engine folds back."""
    return value is not None and math.isfinite(value) and value > 0


def _places_client_geometry(exit_spec: Any) -> bool:
    """Whether the CLIENT's own stop/TP levels are the ones to place (#1414).

    The document answers alone: supply ``initial_levels`` and they are placed,
    omit them and the brief's own ladder is. Until #1414 a process-wide
    environment variable answered half of it (``policy.applies_geometry``), so a
    producer could declare how its stop was MANAGED but not what was PLACED —
    and the deployed value said "never place the client's levels", which is why
    the brief path computed a bracket for months that no broker ever saw.

    One predicate rather than a guard at each call site: there are several sites,
    each dereferencing ``initial_levels.stop`` / ``.tp``, and a forgotten one is
    an ``AttributeError`` inside the unattended placement drain. Callers that
    dereference the levels after this returns True can do so unconditionally.

    Note what is NOT here any more: a fleet-wide veto. Nothing lets this
    deployment ignore levels a document supplies. The refusals that remain are
    the door (``validate_intent``, the only producer of levels besides the brief
    path), ``_refuse_geometry_without_trail`` and the #1112 arm gates — plus
    KILL and ALLOW_ORDERS, which stop everything rather than the geometry.
    """
    return exit_spec is not None and exit_spec.initial_levels is not None


def _placed_geometry_stamp(exit_spec: Any) -> dict[str, Any] | None:
    """The ``"geometry"`` stamp journaled alongside a ``planned`` line.

    A RECORD OF WHAT WAS PLACED, and since #1414 nothing else. It is written at
    routing time and re-read off disk on a later hop (the fire-arm planned
    writer), which is why it exists at all: the intent is not in scope there.

    It used to carry ten more fields — ``planned_blend``, ``k_atr``, ``atr``,
    ``ceiling_price``, ``anchor_mode``, ``tp_floor_frac``, ``policy_name``,
    ``policy_version``, ``exit_policy_name``. Those were the shadow of the
    2026-08-24 exit-policy comparison, which was voided on 2026-08-27 before its
    cohort opened; no row was ever produced under it. Grepped before removal:
    nothing in the tree read any of them — not the daemon, not a lens, not a
    dashboard, not a script. ``applied`` and ``geometry_tp`` are different, and
    are why the stamp survives: ``entry_watch._stamped_exit_target`` reads them to choose
    WHICH family of #1112 arm gates prices a tier.

    ``None`` when no ``exit_spec`` exists, which keeps that line byte-identical.
    """
    if exit_spec is None:
        return None
    levels = exit_spec.initial_levels
    return {
        # A declaration-only exit carries no levels. The stamp records their
        # ABSENCE rather than refusing, so the line still says what happened.
        "geometry_stop": None if levels is None else levels.stop,
        "geometry_tp": None if levels is None else levels.tp,
        "applied": _places_client_geometry(exit_spec),
    }


def _geometry_tranche_ladder(exit_spec: Any) -> tuple[tuple[TpTranchePlan, ...], float] | None:
    """The (ladder, stop) pair the geometry policy implies: its ONE (stop, tp)
    level becomes a single tranche that exits 100% of the position at that
    take-profit. ``None`` when either level is not journalable — the caller then
    journals NOTHING rather than falling back to the static ladder, which the
    geometry policy never placed."""
    from broker_contract.sizing import TpTranchePlan

    levels = exit_spec.initial_levels
    if levels is None:
        return None  # a declaration-only exit places no ladder of its own
    geo_stop = levels.stop
    geo_tp = levels.tp
    if not (_is_journalable_price(geo_stop) and _is_journalable_price(geo_tp)):
        return None
    ladder = (
        TpTranchePlan(
            tranche_index=0,
            target_price=float(geo_tp),
            tranche_frac=1.0,
            r_multiple=0.0,
            tag="geometry",
        ),
    )
    return ladder, geo_stop


def _journal_tranche_plan_core(
    *,
    plan: Any,
    exit_spec: Any,
    stop_price: float,
    reference_qty: float,
    uic: int,
    pick_key: str | None = None,
    instrument_currency: str | None = None,
    sizing_currency: str | None = None,
    exchange_mic: str | None = None,
) -> None:
    """The ladder-choice + line-build core shared by BOTH placement paths
    (bracket ``_journal_tranche_plan`` and the entry-trail watch routing).
    ``pick_key`` is the optional trade identity stamped into the line (watch
    path only — see :func:`stop_journal._build_tranche_plan_line`).
    Source the ladder from whatever is actually placed: a document that supplies
    ``initial_levels`` places its single ``.tp`` level (and the passed
    ``stop_price`` is REPLACED by its ``.stop``); one that supplies none places
    ``plan.tp_tranches`` with ``stop_price`` journaled verbatim. Takes
    explicit ``stop_price``/``reference_qty``/``uic`` so the caller decides the
    plan-vs-placement source of each — the bracket path reads
    ``placement.disaster_stop_price`` and sums ALL entry tiers, the watch path
    reads ``plan.disaster_stop`` and sums only the tiers that actually watch."""
    # #1414: the document is the whole answer, so this asks it directly rather
    # than taking a `use_geometry` its callers used to compute from a
    # process-wide policy and half-compute at that.
    if _places_client_geometry(exit_spec):
        geometry = _geometry_tranche_ladder(exit_spec)
        if geometry is None:
            # Otherwise this skip is invisible: the live-exit engine finds no
            # ladder for the uic and the position sits stop-only, which reads in
            # the journal exactly like a pre-INC-5 pick.
            logger.warning(
                "tranche_plan uic %d: geometry levels unusable (stop=%r, tp=%r) — "
                "no TP ladder journaled, the position stays stop-only",
                uic,
                getattr(exit_spec.initial_levels, "stop", None),
                getattr(exit_spec.initial_levels, "tp", None),
            )
            return
        ladder, stop_price = geometry
    else:
        ladder = getattr(plan, "tp_tranches", None) or ()
    if not ladder and pick_key is None:
        # #1511: a document declaring ``tp_tranches: []`` is legal at the
        # arming door -- a stop-only pick that runs to its disaster stop. The
        # vacuity is journaled below as a POSITIVE fact, so a later gate can
        # tell "the author declared no take-profit" from "the writer never
        # ran"; conflating the two is what used to cancel such a watch
        # terminally at its first touch.
        #
        # The discriminator is RETRACTABILITY, not which path called. A line
        # is written only when it carries a ``pick_key``, because
        # ``_retract_stale_tranche_plans`` skips a keyless plan -- a vacuous
        # keyless line would govern its uic FOREVER and be kept by every
        # compaction. Both identity-carrying callers therefore write it (the
        # watch router, and the now-tranche split via ``override``); only the
        # plain bracket call, which has no identity to stamp, keeps its
        # silence. A non-empty ladder is journaled either way, as before.
        return
    stop_journal._append_standalone_stop_journal(
        stop_journal._build_tranche_plan_line(
            uic=uic,
            tp_tranches=ladder,
            reference_qty=reference_qty,
            stop_price=stop_price,
            pick_key=pick_key,
            instrument_currency=instrument_currency,
            sizing_currency=sizing_currency,
            exchange_mic=exchange_mic,
        )
    )


def _journal_tranche_plan(
    *,
    plan: Any,
    exit_spec: Any,
    placement: Any,
    instrument: Any,
    fx: Any = None,
    override: tuple[str, float] | None = None,
) -> None:
    """INC-5: journal ONE ``tranche_plan`` line per uic so the live-exit engine can
    rebuild the TP ladder from the journal alone — see
    :func:`_journal_tranche_plan_core` for which ladder the DOCUMENT
    sources it from. This is the BRACKET-path wrapper: gating on
    ``plan.tp_tranches`` alone silently dropped every geometry pick (the brief
    expresses a geometry exit as ``exit_spec``, not static tranches), so the
    guard here is ``entry_tiers`` only. ``getattr`` keeps a bare-stub plan
    (unrelated failure-path unit doubles with no ``entry_tiers``/
    ``tp_tranches``) from crashing — it simply journals nothing."""
    entry_tiers = getattr(plan, "entry_tiers", None) if plan is not None else None
    if not entry_tiers:
        return
    if override is not None:
        # #1247 split pick: ONE keyed tranche_plan for the whole pick with the
        # FULL ladder's reference_qty (now + pullback) — a keyless line here
        # would reset the generation the watch route's keyed re-append opens.
        pick_key, reference_qty = override
    else:
        pick_key, reference_qty = None, sum(t.qty for t in entry_tiers)
    _journal_tranche_plan_core(
        plan=plan,
        exit_spec=exit_spec,
        stop_price=placement.disaster_stop_price,
        reference_qty=reference_qty,
        uic=int(instrument.broker_instrument_id),
        pick_key=pick_key,
        instrument_currency=str(getattr(instrument, "currency", "") or ""),
        sizing_currency=_sizing_currency_of(fx, instrument),
        exchange_mic=str(getattr(instrument, "exchange_mic", "") or ""),
    )


def _planned_exit_levels(exit_spec: Any, placement: Any, tier: Any) -> tuple[float, float | None]:
    """``(stop_price, take_profit)`` for the journaled ``planned`` line: the
    document's own levels when it supplies them, else the brief's static
    disaster stop / tier TP.

    It used to return the ``use_geometry`` flag as well, for the stamp to
    record. Since #1414 the stamp asks the document the same question directly,
    and a flag carried between two callers that can both ask is a chance for
    them to disagree."""
    if _places_client_geometry(exit_spec):
        levels = exit_spec.initial_levels
        return levels.stop, levels.tp
    return placement.disaster_stop_price, tier.tp


_GEOMETRY_WITHOUT_TRAIL_ALERT_PREFIX = "geometry-without-entry-trail"

_CLIENT_GEOMETRY_ALERT_PREFIX = "client-geometry-placed"


def _announce_client_geometry(  # NOSONAR -- returns `placed` by design, see docstring
    placed: bool,
    exit_spec: Any,
    ticker: str,
    alert_throttled: Callable[[str, str], bool] | None,
) -> bool:
    """Page once per ticker when a pick HAS PLACED the levels its document
    supplied, and pass the verdict through unchanged.

    Takes the verdict rather than sitting at the top of ``_place_pick`` for two
    reasons, both found in review. The message says "placing", and at the top it
    said that about picks the fee floor, the gross cap or the exit-region gate
    then refused. And the drain re-decodes every armed pick each ~45 s tick, so a
    pick held by a NON-terminal refusal — `_refuse_geometry_without_trail` is
    exactly one, and it fires only on levels-carrying documents — would repeat
    the unthrottled ``logger.info`` forever.

    Deliberately NOT a gate: the document is the authority on what is placed
    (#1414), and a rail that could refuse here would be the fleet-wide veto that
    change removed, reintroduced under another name. Hence the pass-through
    return — it wraps a verdict, it never changes one."""
    if not placed or not _places_client_geometry(exit_spec):
        return placed
    levels = exit_spec.initial_levels
    message = (
        f"place_pick {ticker}: placed the DOCUMENT's own exit levels "
        f"(stop {levels.stop}, tp {levels.tp}) rather than the brief ladder"
    )
    logger.info(message)
    if alert_throttled is not None:
        alert_throttled(message, f"{_CLIENT_GEOMETRY_ALERT_PREFIX}:{ticker}")
    return placed


def _geometry_without_entry_trail_note(
    exit_spec: Any,
) -> str | None:
    """Why a NEW entry must not be armed right now, or ``None`` when it may be
    (issue #1112 round 2, point 4).

    The #1112 exit-region arm gate (:func:`entry_watch._inside_exit_region_note`) and the
    single-tranche contract (:func:`entry_watch._exit_plan_shape_refusal`) exist ONLY on the
    trailing-entry path. With ``ALPHALENS_BROKER_ENTRY_TRAIL_BPS`` at 0 a pick
    falls through to the classic ``_place_tiers`` bracket path, which has
    neither — so the exact defect #1112 fixed (an entry filling inside its own
    exit region) is reachable again. 0 is what this repo's own systemd unit
    sets; production only runs the gated path because of an untracked drop-in
    (issue #1121), which is a config fact no test can see.

    Deliberately scoped to ARMING. Refusing at daemon startup instead would
    leave every already-open LIVE position unmanaged — no take-profit pass, no
    stop re-anchor — which is far worse than the defect being prevented. The
    live-exits and protection passes are untouched by this.

    ``None`` when the document places no geometry: the placed exit IS the
    brief's own ladder, and the arm gate never priced anything, so the classic
    path is no worse than it has always been.
    """
    if not _places_client_geometry(exit_spec):
        return None
    if entry_trails.entry_trail_bps() > 0:
        return None
    return (
        f"exit geometry is active but the entry trail is off "
        f"({entry_trails.ENTRY_TRAIL_BPS_ENV}=0) — the classic bracket path has no "
        f"exit-region gate, so a new entry could fill inside its own exit region"
    )


def _now_cost_gate_violation(
    plan: Any,
    fx: Any,
    instrument: Any,
    exit_spec: Any,
    *,
    cap: float,
) -> str | None:
    """Memo §3.3 — the #1112 parity gate at drain: TP1 must clear round-trip
    cost at the CAP (the worst-case fill). Mirrors ``entry_watch._brief_plan_arm_refusal``
    with ``fill_estimate = cap``; a pick with no take-profit has no TP1 to gate —
    vacuous by design (stop-only plan, the group manages exits)."""
    reference_qty = float(sum(t.qty for t in plan.entry_tiers if t.qty > 0))
    if _places_client_geometry(exit_spec):
        target = float(exit_spec.initial_levels.tp)
        qty = reference_qty
    else:
        tranches = getattr(plan, "tp_tranches", ()) or ()
        if not tranches:
            logger.info("now cost gate: pick has no TP tranches — gate vacuous")
            return None
        quantities = apportion_tranche_quantities(
            reference_qty=reference_qty,
            tranche_fracs=tuple(t.tranche_frac for t in tranches),
        )
        violation = apportioned_coverage_violation(
            tranche_quantities=quantities, reference_qty=reference_qty
        )
        if violation is not None:
            return f"now tranche: {violation}"
        first = next(((t, q) for t, q in zip(tranches, quantities, strict=True) if q > 0.0), None)
        if first is None:
            return "now tranche: exit plan has no sellable tranche"
        target, qty = float(first[0].target_price), float(first[1])
    facts = cost_gate_facts(
        instrument_currency=str(getattr(instrument, "currency", "") or ""),
        sizing_currency=_sizing_currency_of(fx, instrument),
        exchange_mic=str(getattr(instrument, "exchange_mic", "") or ""),
    )
    if entry_trail_geometry.arms_inside_exit_region(
        fill_estimate=cap, exit_target=target, qty=qty, facts=facts
    ):
        return (
            f"now tranche would fill inside the exit region at the cap: cap {cap:.4f}, "
            f"first take-profit {target:.4f} ({qty:g} share(s)) does not clear "
            "round-trip cost + E_min"
        )
    return None


def _refuse_geometry_without_trail(
    exit_spec: Any,
    ticker: str,
    alert_throttled: Callable[[str, str], bool] | None,
) -> bool:
    """The pick is about to take the CLASSIC bracket path, which carries
    neither #1112 arm gate. Refuse a new entry while the geometry exit is
    active and the trail is off (issue #1112 round 2, point 4). NOT terminal:
    this is a configuration rail like KILL / ALLOW_ORDERS, so the pick stays
    armed and places itself once the trail is on — a terminal refusal would
    destroy the armed queue over an operator setting."""
    no_trail_note = _geometry_without_entry_trail_note(exit_spec)
    if no_trail_note is None:
        return False
    logger.warning("place_pick %s: refused — %s", ticker, no_trail_note)
    if alert_throttled is not None:
        alert_throttled(
            f"place_pick {ticker}: {no_trail_note}",
            f"{_GEOMETRY_WITHOUT_TRAIL_ALERT_PREFIX}:{ticker}",
        )
    return True
