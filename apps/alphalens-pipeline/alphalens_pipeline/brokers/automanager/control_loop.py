"""Control-loop — the always-on daemon shell (design Approach 1).

Each tick: kill-gate -> session-keeper -> orphan-sweep (start only) ->
drain+place armed picks -> reconcile-bridge -> position_manager.advance ->
execute Action. State lives entirely in the append-only journals; status is
recomputed every tick by reconcile (crash-recovery = re-run the read-only
verdict engine). All Task 1-10 seams arrive via LoopDeps so the tick logic is
testable against stubs; build_default_deps() is the only site that wires the
real modules.
"""

from __future__ import annotations

import functools
import logging
import math
import os
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from broker_contract.contract import (
    _QTY_EPS,
    BrokerCapabilityError,
    BrokerError,
    OrderRejectedError,
    PlacedOrder,
    Position,
    SupportsAmendStop,
    SupportsOcoExit,
    SupportsStandaloneStop,
    _is_sell_orders_already_exist,
    _is_too_far_from_market,
)
from broker_contract.exit_geometry import (
    ExitPolicy,
    SetupStaticPolicy,
    resolve_exit_policy,
)

from alphalens_pipeline.brokers.automanager.live_exit_engine import (
    ManagedExit,
    run_live_exits,
)
from alphalens_pipeline.brokers.automanager.position_manager import (
    _OCO_LAG_HOLD_REASON,
    Action,
    AlertOnly,
    AmendStop,
    BrokerView,
    CancelRemaining,
    CancelSellLegs,
    NoOp,
    PlaceStop,
    PlannedExit,
    ProtectionView,
    ReanchorFacts,
    UpgradeToOco,
    _amend_enabled,
    _exit_oco_ref,
    _exit_policy,
    _exit_stop_ref,
    _oco_enabled,
    advance,
    reconcile_protection,
)

if TYPE_CHECKING:
    import threading

    from broker_contract.contract import Broker
    from broker_contract.price_feed import PriceFeed
    from broker_contract.sizing import TpTranchePlan

    from alphalens_pipeline.brokers.automanager.streaming_trigger import StreamTrigger
    from alphalens_pipeline.brokers.notifications import NotificationPort
    from alphalens_pipeline.brokers.reconcile import ReconcileVerdict

logger = logging.getLogger(__name__)

# The rung 1 -> 2 OCO exit placer signature (SupportsOcoExit.place_oco_exit):
# (uic, side, qty, stop_price, take_profit, request_id, position_id) -> PlacedOrder.
OcoPlacer = Callable[[int, str, float, float, float, str, "str | None"], PlacedOrder]

# The Stage-3 in-place stop-resize primitive (SupportsAmendStop.amend_stop_amount):
# (uic, order_id, side, order_type, new_qty, stop_price, request_id) -> PlacedOrder.
AmendStopPlacer = Callable[[int, str, str, str, float, float, str], PlacedOrder]

# The runtime data root ($HOME/.alphalens) and its broker-orders subtree have ONE
# home here so the literal is not duplicated across the kill-file + journal +
# briefs paths below.
_ALPHALENS_HOME = Path.home() / ".alphalens"
_BROKER_ORDERS_DIR = _ALPHALENS_HOME / "broker_orders"

KILL_FILE_PATH = _BROKER_ORDERS_DIR / "KILL"

# Prometheus heartbeat gauge (Task 13 wires _default_emit_heartbeat as the
# run_daemon default; the metric name has one home here).
HEARTBEAT_METRIC = 'alphalens_broker_manager_last_tick_timestamp_seconds{job="broker-manager"}'

# Prometheus KILL-active gauge (level, 0/1): 1 while the KILL file is present, 0 when
# absent, so Prometheus can alert on an active emergency stop (KILL was journald-only
# before, invisible to monitoring — the heartbeat kept ticking under KILL). It is
# CO-EMITTED with HEARTBEAT_METRIC in the SAME emit_domain_metrics("broker-manager",
# {...}) call: that write atomically OVERWRITES the whole broker-manager textfile, so
# a separate call to this domain would clobber the heartbeat gauge and vice-versa.
KILL_ACTIVE_METRIC = 'alphalens_broker_manager_kill_active{job="broker-manager"}'

# --- Streaming (dark, SIM-only) env gates + liveness metric --------------------
# Master gate for the Saxo WebSocket early-wake reader (design memo
# saxo_streaming_design_2026_07_24.md). DEFAULTS OFF: unset -> wake_event=None and
# run_daemon is byte-identical to today's blocking sleep. Mirrors the OCO/AMEND
# gates — read at call time (no import-time snapshot), restart-consistent.
_STREAMING_ENABLED_ENV = "ALPHALENS_BROKER_STREAMING_ENABLED"

# Main-thread stale-alert threshold (seconds). Kept <= poll_seconds so the alert
# never lags a full poll cycle behind the already-covered protection, and >= the
# observed ~20-30s SIM heartbeat cadence so a quiet-but-alive stream is not flagged.
_STREAM_STALE_ENV = "ALPHALENS_BROKER_STREAM_STALE_S"
_DEFAULT_STREAM_STALE_S = 45.0

# Prometheus liveness gauge: seconds since the last streamed message (age). Watched
# by an AlphalensBrokerStreamStale rule, distinct from the per-poll heartbeat gauge
# (a dead stream still emits heartbeats — the poll backstop keeps running).
STREAM_LAST_MESSAGE_METRIC = (
    'alphalens_broker_manager_stream_last_message_age_seconds{job="broker-manager"}'
)
# The stream gauge writes to its OWN domain textfile, NOT "broker-manager". Both
# emit_domain_metrics(...) writes atomically OVERWRITE alphalens_domain_<job>.prom,
# and _emit_stream_gauge runs AFTER heartbeat_fn every tick — sharing the
# "broker-manager" job would clobber the heartbeat gauge and break the liveness
# alert while streaming is on. node_exporter merges every *.prom in the dir, so a
# distinct file keeps BOTH series scraped (the metric name + {job} label are
# unchanged — only the containing file differs).
_STREAM_GAUGE_DOMAIN_JOB = "broker-manager-stream"

# Consecutive-tick threshold for the persistent OCO-lag monitor (issue #5). The M1
# guard NoOp'ing a clean over-covered OCO pair is SAFE for a tick or two (a TP-read
# lag behind Q9's symmetric propagation, or a skipped downsize amend), but a genuine
# stall — Q9 never propagating — is otherwise invisible. When a uic emits the M1
# hold for this many consecutive protection ticks, the driver pages ONCE (throttled).
_OCO_LAG_ALERT_TICKS = 5


@dataclass(frozen=True)
class LoopDeps:
    broker: Broker
    kill_file: Path
    ensure_alive: Callable[[], Any]  # () -> ChainStatus(alive, reason)
    iter_picks: Callable[[], Iterator[Any]]
    place_pick: Callable[[Any], bool]  # safety.check + classify + place + journal; True if placed
    read_records: Callable[[], list[Mapping[str, Any]]]
    verdicts_fn: Callable[[list[Mapping[str, Any]], Broker], list[ReconcileVerdict]]
    build_position_view: Callable[[Broker, list[Mapping[str, Any]]], BrokerView]
    # Broker-state-truth protection (saxo-oco memo §6): ONE snapshot per tick,
    # then a pure reconcile_protection diff executed action-by-action. The
    # executor closes over the broker + the alert throttle; run_once wires the
    # per-action BrokerError boundary around each call.
    build_protection_view: Callable[[Broker, list[Mapping[str, Any]]], ProtectionView]
    execute_protection: Callable[[Action, bool, TickReport], None]
    sweep_orphans_fn: Callable[[Broker], list[Any]]
    alert: Callable[[str], None]
    # Throttled alert sink (message, reason_key) -> was-sent. Shares the daemon-
    # lifetime _AlertThrottle with the protection pass, so a PERSISTENT per-tick
    # condition (a stuck FILLED-but-unmatched divergence, a sustained broker
    # outage) pages ONCE per re-alert interval instead of every tick — the
    # overnight-spam incident 2026-07-23. Keyed per reason so distinct conditions
    # stay independent; a NEW divergence (different crid) alerts immediately.
    alert_throttled: Callable[[str, str], bool]
    # Rung 1 -> 2 OCO exit placer (saxo-oco memo §10), or None when the wired
    # broker lacks SupportsOcoExit -> the loop runs stop-only. Detected once in
    # build_default_deps and injected into the protection executor closure (a
    # bare LoopDeps field would be unreachable by the pre-built executor); kept
    # here for symmetry / introspection.
    place_oco_exit: OcoPlacer | None = None
    # Stage-3 in-place stop-resize primitive (saxo Stage-3 memo), or None when the
    # wired broker lacks SupportsAmendStop -> the loop uses the additive-stop
    # fallback. Detected once in build_default_deps (which FAIL-FASTS if the amend
    # flag is on but the capability is absent) and injected into the protection
    # executor closure; kept here for symmetry / introspection.
    amend_stop: AmendStopPlacer | None = None
    # Daemon-lifetime per-uic consecutive-count of M1 oco-lag-hold NoOps (issue #5).
    # A MUTABLE dict on the (frozen) deps — built once in build_default_deps and
    # carried across every tick — so the pure reconcile module stays stateless. The
    # protection driver increments a uic's count each tick it holds and resets it
    # (drops the key) the moment any other action fires; crossing _OCO_LAG_ALERT_TICKS
    # pages once via the shared throttle. Frozen forbids REBINDING the field, not
    # mutating the dict it points at.
    oco_lag_counts: dict[int, int] = field(default_factory=dict)
    # Daemon-lifetime single-slot holder of the PREVIOUS tick's KILL state, so the
    # edge-triggered KILL alert (run_once) fires ONCE per False->True / True->False
    # transition instead of every tick while KILL is held. Edges are rare and each
    # must send, so they use deps.alert (guaranteed-send), NOT the throttle (which is
    # for sustained level conditions). A MUTABLE dict on the (frozen) deps — built
    # once in build_default_deps, carried across ticks; frozen forbids REBINDING the
    # field, not mutating the dict. A missing key == "was False", so a startup WITH a
    # KILL already present alerts once (the operator sees it) while a clean no-KILL
    # startup stays silent.
    kill_state: dict[str, bool] = field(default_factory=dict)
    # Streaming early-wake handles (design memo saxo_streaming_design_2026_07_24.md),
    # ALL None unless ALPHALENS_BROKER_STREAMING_ENABLED=1 AND the broker is Saxo
    # (SIM rail) AND the provider is OAuth AND the reader actually started. When
    # None the daemon runs poll-only, byte-identical to today. ``wake_event`` is
    # the Event run_daemon waits on; ``stream_tick`` is the per-tick push-token +
    # stale-alert hook (main thread); ``stream_trigger`` is retained so the manage
    # command can stop() the reader on shutdown (DELETE subs + join the thread).
    wake_event: threading.Event | None = None
    stream_tick: Callable[[], None] | None = None
    stream_trigger: StreamTrigger | None = None
    # Behavioral exit policy, resolved ONCE from ALPHALENS_BROKER_EXIT_POLICY in
    # build_default_deps and cached here so the hot protection/placement paths read
    # the instance instead of re-resolving the env string every tick (a ValueError
    # there would starve the unconditional protection — adversarial-review P0). The
    # default is the inert setup_static policy so every test/second-broker LoopDeps
    # built without it behaves like today's dark path.
    exit_policy: ExitPolicy = field(default_factory=SetupStaticPolicy)
    # Live TP-tranche exit price-feed factory (INC-5), or None -> the pass falls
    # back to _default_live_exits_feed_factory (Saxo LIVE streaming behind
    # ALPHALENS_SAXO_LIVE_PRICES, else a vetoing feed). Injected so tests can
    # hand the pass a fake feed without touching the Saxo LIVE stream; mirrors
    # the place_oco_exit / amend_stop optional-capability pattern above. Only
    # ever consulted when the live-exits pass is armed (flag ON, ALLOW_ORDERS
    # ON). Keyed by uic -> (ticker, exchange_mic): the venue is load-bearing,
    # see _default_live_exits_feed_factory's docstring.
    live_exits_feed_factory: Callable[[Mapping[int, tuple[str, str]]], PriceFeed] | None = None
    # Daemon-lifetime per-uic high-water mark for the trailing_atr policy (Task
    # 2's pure _maybe_trail reconcile arm reads peak/last_price off the
    # ProtectionView this feeds — wiring lands in Task 4, NOT here). A MUTABLE
    # dict on the (frozen) deps — built once in build_default_deps, carried
    # across ticks — mirrors oco_lag_counts/kill_state above. Restart reset is
    # automatic: a fresh daemon starts with an empty dict, so the first
    # observed price seeds peak = price rather than inventing a higher past
    # peak (see _update_peaks). Frozen forbids REBINDING the field, not
    # mutating the dict it points at.
    peak_tracker: dict[int, float] = field(default_factory=dict)


@dataclass
class TickReport:
    picks_placed: int = 0
    exits_placed: int = 0  # protective exits placed this tick (rung 0 -> 1 stop, or 1 -> 2 OCO)
    cancels: int = 0
    alerts: int = 0
    orphans: int = 0
    verdict_count: int = 0
    actions: list[tuple[str, str]] = field(default_factory=list)  # (ticker, Action class)


def _always() -> bool:
    return True


def _submitted_pick_keys(records: Iterable[Mapping[str, Any]]) -> set[tuple[str, str]]:
    """The (ticker, brief_date) pairs already present in the submissions journal.

    Design §Data-flow step 4: the drain places only picks NOT yet joined to
    submissions.jsonl. Without this join every armed pick is re-submitted on
    every tick with a fresh client_request_id (execution.py mints uuid4 per
    bracket), which Saxo's 15 s x-request-id dedup cannot catch."""
    keys: set[tuple[str, str]] = set()
    for record in records:
        ticker = record.get("ticker")
        brief_date = record.get("brief_date")
        if ticker and brief_date:
            keys.add((str(ticker).upper(), str(brief_date)))
    return keys


def _pick_key(intent: Any) -> tuple[str, str]:
    """The (ticker, brief_date) join key for one armed intent."""
    return (str(intent.instrument.ticker).upper(), str(intent.meta.brief_date))


def _default_emit_heartbeat(kill: bool = False) -> None:
    """Write the per-tick Prometheus heartbeat + KILL-active gauges. A Type=simple
    daemon rarely triggers ExecStopPost, so the emit-job-metrics last_success clock is
    the wrong health signal — the heartbeat gauge (watched by
    AlphalensBrokerManagerHeartbeatStale) is. The KILL-active gauge co-emits here (1
    when the KILL file is present, 0 when absent) so an emergency stop is visible to
    Prometheus. BOTH gauges MUST go in ONE emit call: the write atomically overwrites
    the whole broker-manager textfile, so a separate call would clobber the other
    gauge. Best-effort: a textfile-dir hiccup must never crash the loop."""
    from alphalens_pipeline.observability.textfile import emit_domain_metrics

    try:
        emit_domain_metrics(
            "broker-manager",
            {HEARTBEAT_METRIC: int(time.time()), KILL_ACTIVE_METRIC: int(kill)},
        )
    except OSError:
        logger.warning("broker-manager heartbeat emit failed", exc_info=True)


def run_once(deps: LoopDeps, *, sweep_orphans: bool = False) -> TickReport:
    """One control-loop tick. Placement is gated on (no KILL) AND (chain alive);
    reconcile + Action execution ALWAYS run so a KILL still cancels and a dead
    chain still surfaces terminal state. The tick is a sequence of independent
    phases (each with its OWN BrokerError boundary in its helper) so one phase
    failing never starves the safety-critical protection pass."""
    report = TickReport()
    kill = deps.kill_file.exists()
    _alert_kill_transition(deps, kill)
    chain = deps.ensure_alive()
    alive = bool(getattr(chain, "alive", False))
    if not alive:
        deps.alert(
            f"session-keeper: chain dead — {getattr(chain, 'reason', None)}; placement halted"
        )

    if sweep_orphans:
        _run_orphan_sweep(deps, report)

    if not kill and alive:
        _run_placement_drain(deps, report)

    # The verdict-level advance loop (terminal / round-trip CancelRemaining +
    # divergence alerts) and the broker-state protection pass are INDEPENDENT: a
    # reconcile-bridge or position-view BrokerError must not skip protection (the
    # safety-critical path). Each reads the journal fresh and owns its boundary.
    records = deps.read_records()
    _run_verdict_advance(deps, records, report)
    # Live TP-tranche exits (INC-5) run IMMEDIATELY BEFORE protection so the
    # engine's synchronous shrink-SL-then-market-sell fully settles on this
    # thread before the protection pass re-asserts SL == owned as the
    # never-naked backstop — protection stays the LAST pass every tick.
    # Internally gated on the flag (default OFF): a no-op when off, so this
    # call site is unconditional and the flag alone controls behaviour.
    _run_live_exits_pass(deps, report)
    _run_protection_pass(deps, records, kill, report)
    return report


def _alert_kill_transition(deps: LoopDeps, kill: bool) -> None:
    """Edge-triggered KILL alert (observability only — placement/protection gating is
    UNCHANGED). Fire deps.alert ONCE when KILL goes False->True and ONCE True->False,
    never every tick while it stays True (that would spam the operator). Uses the
    guaranteed-send deps.alert sink — NOT alert_throttled — because edges are rare and
    each transition must deliver. The previous state lives in the daemon-lifetime
    deps.kill_state holder; a missing key == "was False", so a startup WITH a KILL
    already present alerts once (the operator sees it) but a clean no-KILL startup is
    silent."""
    previous = deps.kill_state.get("active", False)
    if kill != previous:
        if kill:
            deps.alert(
                "KILL active — emergency stop; new placements halted, "
                "existing positions still protected"
            )
        else:
            deps.alert("KILL cleared — placement resumed")
    deps.kill_state["active"] = kill


def _run_orphan_sweep(deps: LoopDeps, report: TickReport) -> None:
    # A BrokerError here (list_open_orders etc.) must not crash the tick — the
    # sweep is a diagnostic read; alert and carry on to reconcile.
    try:
        orphans = deps.sweep_orphans_fn(deps.broker)
    except BrokerError as exc:
        deps.alert(f"orphan-sweep failed (broker error) — skipped this tick: {exc}")
        report.alerts += 1
        orphans = []
    for orphan in orphans:
        deps.alert(f"orphan (placed but never journaled): {orphan}")
        report.orphans += 1


def _run_placement_drain(deps: LoopDeps, report: TickReport) -> None:
    # Drain only picks NOT yet joined to submissions.jsonl (design §Data-flow
    # step 4). Read the journal ONCE before the drain — this snapshot is the
    # CROSS-tick join (an out-of-tick placement is caught by the next tick's
    # fresh read). ``placed_this_tick`` is the WITHIN-tick guard: it starts empty
    # each tick and records every pick we ATTEMPT to place, so two armed lines
    # with the same (ticker, brief_date) in ONE tick never both drive placement —
    # even when the first attempt returns False (refused / zero-sized / partial-
    # then-failed). Recording the attempt (not just a success) guards the
    # never-double-commit invariant against a retry inside the same tick.
    already_submitted = _submitted_pick_keys(deps.read_records())
    placed_this_tick: set[tuple[str, str]] = set()
    for pick in deps.iter_picks():
        key = _pick_key(pick)
        if key in already_submitted or key in placed_this_tick:
            continue
        placed_this_tick.add(key)
        if deps.place_pick(pick):
            report.picks_placed += 1


def _run_verdict_advance(
    deps: LoopDeps, records: list[Mapping[str, Any]], report: TickReport
) -> None:
    """The verdict-level advance loop (terminal / round-trip CancelRemaining +
    divergence alerts). A reconcile-bridge or position-view BrokerError skips only
    this phase; the protection pass runs regardless."""
    try:
        verdicts = deps.verdicts_fn(records, deps.broker)
    except BrokerError as exc:
        # THROTTLED (static reason): a sustained broker outage must not page every
        # tick — one alert per re-alert interval (overnight-spam incident 2026-07-23).
        if deps.alert_throttled(
            f"reconcile failed (broker error) — verdicts skipped this tick: {exc}",
            "reconcile-fail",
        ):
            report.alerts += 1
        verdicts = []
    report.verdict_count = len(verdicts)
    if not verdicts:
        return
    try:
        position_view = deps.build_position_view(deps.broker, records)
    except BrokerError as exc:
        if deps.alert_throttled(
            f"position-view build failed (broker error) — actions skipped this tick: {exc}",
            "posview-fail",
        ):
            report.alerts += 1
        return
    for verdict in verdicts:
        _advance_and_execute(deps, verdict, position_view, report)


def _advance_and_execute(
    deps: LoopDeps, verdict: ReconcileVerdict, position_view: BrokerView, report: TickReport
) -> None:
    action = advance(verdict)
    report.actions.append((verdict.ticker, type(action).__name__))
    # One position's broker call (a cancel of leftover exits) failing must not take
    # down the tick — alert and skip only that verdict.
    try:
        _execute_action(deps, verdict, action, position_view, report=report)
    except BrokerError as exc:
        deps.alert(
            f"{verdict.ticker}: {type(action).__name__} failed (broker error) — skipped: {exc}"
        )
        report.alerts += 1


# --- Live TP-tranche exits (INC-5) --------------------------------------------
# The live-exit engine (live_exit_engine.py, INC-3) is complete and INERT: it has
# no daemon caller. This tick phase is the caller — flag-gated, default OFF, so
# merging it is behaviour-neutral. Runs IMMEDIATELY BEFORE _run_protection_pass
# (see run_once): the engine's shrink-SL-then-market-sell is synchronous on the
# main thread, so it fully settles before protection re-asserts SL == owned —
# a tranche sell that fails after its SL shrink is re-covered by the SAME tick's
# protection pass (the never-naked backstop), never left naked until the NEXT
# poll. Only positions carrying a journaled tranche_plan (Task 1) are managed —
# pre-INC-5 positions have none on record and stay stop-only, the deliberate
# gradual-rollout boundary (§Non-negotiable design rules).

_LIVE_MARKET_EXITS_ENV = "ALPHALENS_LIVE_MARKET_EXITS"


def _live_market_exits_enabled() -> bool:
    """Whether the live TP-tranche exit tick phase is armed (read at call time,
    mirrors ``_streaming_enabled`` / ``_oco_enabled`` / ``_amend_enabled``).
    Defaults OFF — unset (or any value other than ``"1"``) means the pass never
    builds a ``ManagedExit`` and never calls ``run_live_exits``, byte-identical
    to today's tick."""
    return os.environ.get(_LIVE_MARKET_EXITS_ENV) == "1"


def _live_exits_orders_allowed() -> bool:
    """Whether ``ALPHALENS_BROKER_ALLOW_ORDERS`` is armed (read at call time).

    The engine's full-close branch (``live_exit_engine.execute_tranche_exit``)
    calls ``broker.cancel_order`` on the standalone SL — Saxo's ``cancel_order``
    is DELIBERATELY not behind the ALLOW_ORDERS gate (cancelling is always
    safe) — immediately followed by ``broker.place_market_order``, which IS
    gated. With ALLOW_ORDERS off this would cancel the covering SL and then
    raise before the sell ever reaches the wire, leaving the position naked
    until the next protection pass re-covers it. Gating the WHOLE live-exits
    pass on ALLOW_ORDERS turns a flag-ON-but-orders-disabled run into a clean
    no-op instead of a transient naked window (self-review finding, INC-5)."""
    from alphalens_pipeline.brokers.automanager import safety

    return os.environ.get(safety.ALLOW_ORDERS_ENV) == "1"


def _fold_fired_since_latest_plan(lines: Iterable[Mapping[str, Any]]) -> dict[int, frozenset[str]]:
    """Fired-tranche tags per uic, RESET on each new ``tranche_plan`` line.

    A uic is stable per instrument (Saxo nets by uic), and the standalone-stop
    journal is append-only and NEVER cleared — so a position that fully exits
    (every tranche fired) and is later RE-ENTERED on the same uic would, under
    the engine's own ``fold_fired_tranches`` (which folds every
    ``tranche_fired`` line ever written for the uic), inherit the PRIOR
    trade's fired tags and silently suppress the new trade's whole TP ladder
    forever. Processing the journal in write order (``_iter_standalone_stop_
    journal`` already yields it that way), a new ``tranche_plan`` line for a
    uic clears its accumulator — only ``tranche_fired`` lines AFTER the LATEST
    plan for that uic count. The live-exit engine's own ``fold_fired_tranches``
    is untouched (still used by its own tests) — this is a control_loop-side
    wrapper around the same append-only journal, not an engine change."""
    fired: dict[int, set[str]] = {}
    for line in lines:
        raw_uic = line.get("uic")
        if raw_uic is None:
            continue
        try:
            uic = int(raw_uic)
        except (TypeError, ValueError):
            continue
        kind = line.get("kind")
        if kind == "tranche_plan":
            fired.pop(uic, None)
        elif kind == "tranche_fired":
            tag = line.get("tag")
            if tag:
                fired.setdefault(uic, set()).add(str(tag))
    return {u: frozenset(t) for u, t in fired.items()}


def _build_managed_exits(
    *,
    long_positions: Iterable[Position],
    tranche_plans: Mapping[int, tuple[tuple[TpTranchePlan, ...], float, float]],
    fired: Mapping[int, frozenset[str]],
) -> list[ManagedExit]:
    """Build this tick's managed-position list. Pure — no broker/journal I/O.

    A live long position whose uic has a folded ``tranche_plan`` (Task 1)
    becomes ONE ``ManagedExit``; a live long with NO ``tranche_plan`` on record
    is SKIPPED — positions placed before this deploys carry no ladder and stay
    stop-only forever (the deliberate gradual-rollout boundary)."""
    managed: list[ManagedExit] = []
    skipped = 0
    for pos in long_positions:
        uic = _position_uic(pos)
        if uic is None:
            skipped += 1
            continue
        plan = tranche_plans.get(uic)
        if plan is None:
            skipped += 1
            continue
        tp_tranches, reference_qty, stop_price = plan
        managed.append(
            ManagedExit(
                uic=uic,
                tp_tranches=tp_tranches,
                reference_qty=reference_qty,
                stop_price=stop_price,
                already_fired=fired.get(uic, frozenset()),
            )
        )
    logger.info(
        "live-exits: %d position(s) managed, %d skipped (no tranche_plan on record)",
        len(managed),
        skipped,
    )
    return managed


_SAXO_LIVE_PRICES_ENV = "ALPHALENS_SAXO_LIVE_PRICES"


def _saxo_live_prices_enabled() -> bool:
    return os.environ.get(_SAXO_LIVE_PRICES_ENV) == "1"


class _NullPriceFeed:
    """Vetoes everything. The OFF state of the Saxo feed is 'no prices', never a
    quiet downgrade to a weaker source (see the INC-2 design memo)."""

    def latest(self, uic: int) -> None:
        return None


def _default_live_exits_feed_factory(
    uic_to_instrument: Mapping[int, tuple[str, str]],
) -> PriceFeed:
    """The production price feed: Saxo LIVE streaming, or nothing.

    yfinance is NOT a fallback here. It remains in the tree, unwired, and its
    PricePoint carries no event time so the freshness gate would veto it
    anyway. Behind ``ALPHALENS_SAXO_LIVE_PRICES`` (default OFF); when off this
    returns a feed that vetoes every uic rather than quietly downgrading."""
    if not _saxo_live_prices_enabled():
        return _NullPriceFeed()
    from alphalens_pipeline.brokers.automanager.saxo_live_price_feed import SaxoLivePriceFeed
    from alphalens_pipeline.data.alt_data.saxo_price_stream import get_shared_price_stream

    stream = get_shared_price_stream()
    live_uics = {
        sim_uic: stream.live_uic_for(ticker, exchange_mic=mic)
        for sim_uic, (ticker, mic) in uic_to_instrument.items()
    }
    stream.ensure_subscribed([u for u in live_uics.values() if u is not None])
    return SaxoLivePriceFeed(stream=stream, resolve_live_uic=live_uics.get)


def _build_live_exits_feed(
    deps: LoopDeps,
    uic_to_instrument: Mapping[int, tuple[str, str]],
    report: TickReport,
) -> PriceFeed:
    """Builds this tick's price feed via the injected/default factory, with a
    boundary around it whose entire job is that NOTHING that happens while
    building the feed may reach the tick.

    With ``ALPHALENS_SAXO_LIVE_PRICES`` on, the default factory reaches out to
    real Saxo LIVE auth/REST/streaming machinery (env config, an OAuth token
    store, a WebSocket) that this pass has no contract with — a missing env
    var or an unbootstrapped token store is the single most likely rollout
    mistake, and it must not be able to stop the never-naked protection pass
    that runs immediately after this one. Deliberately catches ``Exception``,
    not just ``BrokerError``: every doubt becomes a veto here, never a crash —
    a construction failure degrades to a feed that vetoes every uic, exactly
    like an OFF flag or a stale quote. The failure is still surfaced via the
    same throttled-alert mechanism the surrounding pass already uses for its
    ``BrokerError`` boundaries, so an operator sees "no exits ever fire"
    explained rather than silently swallowed."""
    feed_factory = deps.live_exits_feed_factory or _default_live_exits_feed_factory
    try:
        return feed_factory(uic_to_instrument)
    # Deliberately broad: nothing that happens while building the price feed
    # may reach the tick. Do NOT narrow this to a specific exception type -
    # the whole point of this boundary is that it does not need to know what
    # can go wrong inside the factory, only that a doubt becomes a veto.
    except Exception as exc:
        if deps.alert_throttled(
            f"live-exits: price feed construction failed — degrading to no-prices: {exc}",
            "live-exits-feed-build-fail",
        ):
            report.alerts += 1
        return _NullPriceFeed()


def _update_peaks(
    deps: LoopDeps, long_positions: Iterable[Position]
) -> tuple[dict[int, float], dict[int, float]]:
    """Per-tick high-water peak update for the ``trailing_atr`` policy (Task 2's
    pure ``_maybe_trail`` reconcile arm reads ``peak``/``last_price`` off the
    ``ProtectionView`` this is meant to feed). NOT wired into the protection
    pass here — Task 4 does that; this helper only maintains the daemon-lifetime
    high-water state and hands back this tick's snapshot of it.

    Builds ``uic_to_instrument`` from the live long positions (mirrors
    ``_run_live_exits_pass``'s ``uic_to_ticker`` construction) and fetches ONE
    feed for the tick via the same injected/default factory the live-exits pass
    uses, so trailing and TP-fire read prices from the identical source.

    For each long uic: ``point = feed.latest(uic)``. A ``None`` point (stream-
    health veto) or a non-finite/non-positive price leaves ``deps.peak_tracker``
    untouched and OMITS the uic from both returned maps — the pure
    ``_maybe_trail`` arm then sees no peak/last_price this tick and makes no
    move on a stale feed. Otherwise ``price = point.bid`` (mirrors
    ``run_live_exits``: selling a long executes at the bid, so trailing and
    TP-fire must agree on "the price"), and ``deps.peak_tracker[uic]`` ratchets
    to ``max(existing, price)`` — monotone non-decreasing for the daemon's
    lifetime.

    Restart reset is automatic: a fresh daemon starts with an empty
    ``peak_tracker``, so the first observed price after restart seeds
    ``peak = price`` rather than inventing a higher past peak (the ratchet
    floor persisted in the journal-folded stop can therefore never loosen).

    Prunes ``peak_tracker`` keys that are no longer in this tick's
    ``long_positions`` at the end, so a closed position can never resurrect a
    stale peak if the uic is re-picked later."""
    uic_to_instrument = {
        uic: (pos.instrument.ticker, pos.instrument.exchange_mic)
        for pos in long_positions
        if (uic := _position_uic(pos)) is not None
    }
    feed_factory = deps.live_exits_feed_factory or _default_live_exits_feed_factory
    feed = feed_factory(uic_to_instrument)
    peak_by_uic: dict[int, float] = {}
    last_price_by_uic: dict[int, float] = {}
    for uic in uic_to_instrument:
        point = feed.latest(uic)
        if point is None:
            continue  # stream-health veto — leave peak_tracker untouched
        price = point.bid
        if not math.isfinite(price) or price <= 0.0:
            continue  # a doubt about the price becomes a veto, never a crash
        deps.peak_tracker[uic] = max(deps.peak_tracker.get(uic, price), price)
        peak_by_uic[uic] = deps.peak_tracker[uic]
        last_price_by_uic[uic] = price
    for stale_uic in set(deps.peak_tracker) - set(uic_to_instrument):
        del deps.peak_tracker[stale_uic]
    return peak_by_uic, last_price_by_uic


def _run_live_exits_pass(deps: LoopDeps, report: TickReport) -> None:
    """The live TP-tranche exit tick phase (INC-5), behind
    ``ALPHALENS_LIVE_MARKET_EXITS`` (default OFF) AND ``ALLOW_ORDERS``
    (``_live_exits_orders_allowed`` — see its docstring for why). Early-returns
    a no-op when either gate is closed: no ``ManagedExit`` is built and
    ``run_live_exits`` is never called.

    Reads live positions + the standalone-stop journal ONCE this tick, builds
    the managed set, and delegates to the inert engine. Each broker-facing step
    has its OWN ``BrokerError`` boundary (mirrors ``_run_protection_pass``) so a
    live-exits failure alerts and returns rather than starving the protection
    pass that runs immediately after (``run_once``).

    Unlike the other tick-phase helpers, this one has NO ``records`` parameter:
    the tranche ladder and fired markers come from the SEPARATE standalone-stop
    journal, never the submissions journal — a signature carrying an unused
    param would be misleading, not merely symmetric."""
    if not _live_market_exits_enabled() or not _live_exits_orders_allowed():
        return
    try:
        long_positions = deps.broker.get_long_positions()
    except BrokerError as exc:
        if deps.alert_throttled(
            f"live-exits: position read failed (broker error) — skipped: {exc}",
            "live-exits-posread-fail",
        ):
            report.alerts += 1
        return
    journal_lines = list(_iter_standalone_stop_journal())
    managed = _build_managed_exits(
        long_positions=long_positions,
        tranche_plans=fold_tranche_plans(journal_lines),
        fired=_fold_fired_since_latest_plan(journal_lines),
    )
    if not managed:
        return
    # uic -> (ticker, venue) off the live positions just read. The venue must
    # survive: resolving a LIVE instrument by bare ticker is ambiguous for
    # cross-listed names.
    uic_to_instrument = {
        uic: (pos.instrument.ticker, pos.instrument.exchange_mic)
        for pos in long_positions
        if (uic := _position_uic(pos)) is not None
    }
    feed = _build_live_exits_feed(deps, uic_to_instrument, report)
    try:
        fired_count = run_live_exits(deps.broker, feed, managed)
    except BrokerError as exc:
        if deps.alert_throttled(
            f"live-exits: pass failed (broker error) — skipped: {exc}",
            "live-exits-run-fail",
        ):
            report.alerts += 1
        return
    if fired_count:
        report.exits_placed += fired_count
        report.actions.append(("live-exits", f"fired={fired_count}"))


def _run_protection_pass(
    deps: LoopDeps, records: list[Mapping[str, Any]], kill: bool, report: TickReport
) -> None:
    """Broker-state-truth protection pass (saxo-oco memo §6): ONE snapshot, then a
    pure desired-vs-actual diff over live positions + live SELL legs, each action
    executed inside its OWN per-action BrokerError boundary so one uic's failure
    never aborts the tick or the other uics. This is the ONLY path that places /
    resizes protective stops now (advance no longer does)."""
    try:
        protection_view = deps.build_protection_view(deps.broker, records)
    except BrokerError as exc:
        if deps.alert_throttled(
            f"protection-view build failed (broker error) — protection skipped: {exc}",
            "protview-fail",
        ):
            report.alerts += 1
        return
    actions = reconcile_protection(protection_view)
    for action in actions:
        report.actions.append(("protection", type(action).__name__))
        try:
            deps.execute_protection(action, kill, report)
        except BrokerError as exc:
            deps.alert(f"protection {type(action).__name__} failed (broker error) — skipped: {exc}")
            report.alerts += 1
    _track_oco_lag(deps, actions, report)


def _track_oco_lag(deps: LoopDeps, actions: list[Action], report: TickReport) -> None:
    """Daemon-lifetime per-uic monitor for a persistently-stuck OCO propagation lag
    (issue #5). The M1 guard NoOp'ing a clean over-covered OCO pair is SAFE for a
    tick or two but must not be invisible if Q9 never propagates. Increment a uic's
    consecutive-hold count each tick it emits an ``oco-lag-hold`` NoOp; RESET (drop
    the key) the moment that uic emits ANY other action — a real place/amend/cancel
    means the lag cleared. Crossing ``_OCO_LAG_ALERT_TICKS`` pages ONCE (the shared
    throttle dedups the repeat per-tick calls into a single alert per interval)."""
    counts = deps.oco_lag_counts
    lag_uics: set[int] = set()
    resolved_uics: set[int] = set()
    for action in actions:
        uic = getattr(action, "uic", None)
        if uic is None:
            continue
        if isinstance(action, NoOp) and action.reason == _OCO_LAG_HOLD_REASON:
            lag_uics.add(uic)
        else:
            resolved_uics.add(uic)
    # Any non-lag action for a uic wins — the hold cleared, so reset even if some
    # (impossible-in-practice) second action on the same uic was a lag NoOp.
    for uic in resolved_uics:
        counts.pop(uic, None)
        lag_uics.discard(uic)
    for uic in lag_uics:
        counts[uic] = counts.get(uic, 0) + 1
        if counts[uic] >= _OCO_LAG_ALERT_TICKS and deps.alert_throttled(
            f"uic {uic}: OCO exit propagation lag held {counts[uic]} consecutive ticks "
            f"(>= {_OCO_LAG_ALERT_TICKS}) — Q9 may be stalled, check the resting OCO pair",
            f"oco-lag-persistent:{uic}",
        ):
            report.alerts += 1


def _execute_action(
    deps: LoopDeps,
    verdict: ReconcileVerdict,
    action: Any,
    position_view: BrokerView,
    *,
    report: TickReport,
) -> None:
    """Execute one verdict-level ``advance`` Action. Stop placement is NOT here —
    the protection pass owns it; ``advance`` only ever yields NoOp / AlertOnly /
    CancelRemaining now."""
    request_id = str(verdict.details.get("client_request_id") or "")
    if isinstance(action, NoOp):
        return
    if isinstance(action, AlertOnly):
        # Reconcile-verdict alerts (e.g. a stuck FILLED-but-unmatched divergence)
        # are THROTTLED per client_request_id — a persistent divergence pages once
        # per re-alert interval, not every tick (overnight-spam incident
        # 2026-07-23). A different crid is a distinct key -> alerts immediately.
        # Fall back to the ticker when the crid is absent so two unattributable
        # divergences on different tickers are not deduped into one (the key only;
        # request_id stays the crid for the CancelRemaining lookup below).
        divergence_key = f"divergence:{request_id or verdict.ticker}"
        if deps.alert_throttled(action.reason, divergence_key):
            report.alerts += 1
        return
    if isinstance(action, CancelRemaining):
        for order_id in position_view.working_children.get(request_id, ()):  # ungated safe op
            deps.broker.cancel_order(order_id)
            report.cancels += 1


def run_daemon(
    deps: LoopDeps,
    *,
    once: bool,
    poll_seconds: float,
    sleep_fn: Callable[[float], None] = time.sleep,
    is_running: Callable[[], bool] = _always,
    heartbeat_fn: Callable[[bool], None] = _default_emit_heartbeat,
    wake_event: threading.Event | None = None,
    on_tick: Callable[[], None] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> None:
    """Drive run_once forever (orphan sweep on the FIRST tick only), or once.

    ``wake_event`` toggles the streaming early-wake path (design memo
    saxo_streaming_design_2026_07_24.md). When ``None`` (streaming off / disabled
    / non-Saxo / static token) the loop is BYTE-IDENTICAL to the historical
    blocking ``sleep_fn(poll_seconds)`` — same call, same cadence, ``monotonic``
    never consulted.

    When an Event is supplied, the blocking sleep becomes an ABSOLUTE-DEADLINE
    interruptible wait: the loop waits until the next fixed ``poll_seconds``
    wall-clock grid point, but the stream thread's ``wake_event.set()`` makes that
    wait return EARLY, giving an EXTRA run_once pass. The grid deadline advances
    ONLY on a timeout-driven pass (``now >= deadline``), NEVER on an early wake, so
    any number of stale early wakes cannot push the guaranteed backstop pass past
    ``poll_seconds`` from the last full-cadence pass (adversary-2 timer-reset fix).
    The advance uses ``pass_end`` (the clock read right after ``run_once``, BEFORE
    ``on_tick``) so a slow ``on_tick`` is absorbed into the wait, never added on top
    of a fresh poll (PR #900 review).
    A woken pass calls the SAME ``run_once`` — kill/records/view all recomputed —
    so it is behaviourally identical to a poll pass at that instant. The single
    guarantee holds: a stream that never fires the Event degrades to EXACTLY the
    poll-only floor.

    ``on_tick`` (main thread) runs once per pass after the heartbeat — it pushes
    the current bearer to the reader and raises the throttled stream-stale alert.
    It is best-effort by construction (the streaming closure swallows its own
    errors) so it can never crash the protective loop."""
    if wake_event is not None and not (math.isfinite(poll_seconds) and poll_seconds > 0):
        # Event.wait(inf/None) would block forever and Event.wait with a 0/neg grid
        # would busy-spin — either breaks the never-naked backstop. Fail loud at
        # startup rather than silently run without a backstop (adversary-1).
        raise ValueError(
            f"poll_seconds must be finite and positive for the interruptible wait, "
            f"got {poll_seconds!r}"
        )
    first = True
    deadline = monotonic() + poll_seconds if wake_event is not None else 0.0
    while is_running():
        run_once(deps, sweep_orphans=first)
        # Anchor the backstop to the moment protection COMPLETED (before on_tick),
        # NOT to a clock read taken after on_tick. A slow on_tick (a hung Telegram
        # POST inside the stale/breaker alert can block ~tens of seconds) must only
        # SHRINK the remaining wait below — never inflate ``now`` so the timeout
        # branch schedules a fresh full poll ON TOP of the block, which would push
        # the next pass past poll_seconds (worse than poll-only). PR #900 review.
        pass_end = monotonic() if wake_event is not None else 0.0
        # Task 13: writes the Prometheus heartbeat gauge + the KILL-active gauge
        # (co-emitted so an emergency stop is visible to Prometheus, not just journald).
        heartbeat_fn(deps.kill_file.exists())
        if on_tick is not None:
            on_tick()  # push_token + stream stale/breaker alert + liveness gauge (main thread)
        first = False
        if once:
            return
        if wake_event is None:
            sleep_fn(poll_seconds)  # legacy/disabled path — byte-identical to today
            continue
        if pass_end >= deadline:  # a TIMEOUT pass just ran -> schedule the next grid point
            deadline = pass_end + poll_seconds
        # Remaining wait to the grid point, read fresh so any on_tick block already
        # elapsed is absorbed here (never added on top). Early wakes shrink it too.
        wake_event.wait(max(0.0, deadline - monotonic()))
        wake_event.clear()


def _streaming_enabled() -> bool:
    """Whether the dark streaming early-wake reader is enabled (read at call time).

    Master gate, mirroring ``_oco_enabled`` / ``_amend_enabled``. Defaults OFF —
    unset -> the daemon runs poll-only, byte-identical to today."""
    return os.environ.get(_STREAMING_ENABLED_ENV) == "1"


def _stream_stale_s() -> float:
    """The main-thread stale-alert threshold in seconds (read at call time).

    ``ALPHALENS_BROKER_STREAM_STALE_S`` override, falling back to
    ``_DEFAULT_STREAM_STALE_S``. A non-finite / non-positive / unparsable value
    falls back to the default (a bad env value must never disable the backstop
    alert with a zero threshold or a never-firing infinity)."""
    raw = os.environ.get(_STREAM_STALE_ENV)
    if raw is None:
        return _DEFAULT_STREAM_STALE_S
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_STREAM_STALE_S
    if not math.isfinite(value) or value <= 0:
        return _DEFAULT_STREAM_STALE_S
    return value


def _emit_stream_gauge(age_seconds: float) -> None:
    """Best-effort Prometheus liveness gauge (seconds since the last streamed
    message). A textfile-dir hiccup must never crash the loop — the poll backstop
    covers protection regardless of observability."""
    from alphalens_pipeline.observability.textfile import emit_domain_metrics

    try:
        emit_domain_metrics(_STREAM_GAUGE_DOMAIN_JOB, {STREAM_LAST_MESSAGE_METRIC: age_seconds})
    except OSError:
        logger.warning("broker-manager stream-liveness gauge emit failed", exc_info=True)


def _make_stream_tick(
    trigger: StreamTrigger,
    *,
    get_bearer: Callable[[], str],
    alert_throttled: Callable[[str, str], bool],
    stale_s: float,
    emit_gauge: Callable[[float], None] = _emit_stream_gauge,
) -> Callable[[], None]:
    """Build the per-tick streaming hook run by ``run_daemon`` on the MAIN thread.

    If the reader's circuit breaker has tripped (``trigger.is_streaming`` False) it
    pages a THROTTLED ``stream-breaker`` alert and returns — the daemon is on the
    poll backstop, and this is the only place a permanent trip surfaces to the
    operator on a thread where the alert sink is safe. Otherwise each tick it
    (1) pushes the current bearer into the reader so a mid-stream
    token rotation is re-authorized in place (never pulled by the reader — it
    can never stall on the token flock), and (2) reads the reader's
    ``seconds_since_last_message`` for the liveness gauge + a THROTTLED
    ``stream-dead`` alert once silence exceeds ``stale_s``. Both the alert and the
    gauge run only on the main thread (the shared ``_AlertThrottle`` is not
    thread-safe). Fully best-effort: a bearer-read error (chain loss) degrades to
    poll-only silently rather than crashing the protective loop."""

    def _tick() -> None:
        if not trigger.is_streaming:
            # The circuit breaker tripped PERMANENTLY -> the reader thread is dead and
            # the daemon is on the poll backstop. Page once (throttled) on the MAIN
            # thread — the breaker's own alert runs on the reader thread where the
            # _AlertThrottle/Telegram sink is not thread-safe, and the 'stream-dead'
            # silence alert never fires when the stream never delivered a message
            # (seconds_since_last_message stays None). zen MEDIUM, PR #900.
            alert_throttled(
                "saxo stream circuit breaker tripped — running on poll backstop",
                "stream-breaker",
            )
            return
        try:
            bearer = get_bearer()
        except Exception:  # a token/chain error must never crash the protective loop
            logger.warning("streaming: bearer read failed — skipping push this tick", exc_info=True)
            bearer = None
        if bearer:
            trigger.push_token(bearer)
        silence = trigger.seconds_since_last_message()
        if silence is None:
            return  # no message yet — the poll backstop covers protection
        emit_gauge(silence)
        if silence > stale_s:
            alert_throttled(
                f"saxo stream silent >{stale_s:.0f}s ({silence:.0f}s) — running on poll backstop",
                "stream-dead",
            )

    return _tick


def build_default_deps(
    *, notify: NotificationPort, chain_loss_notify: NotificationPort
) -> LoopDeps:
    """Wire the real Task 1-10 seams. Imported lazily so the alphalens binary's
    startup budget stays off this path (lazy-CLI doctrine); covered by the
    SAXO_LIVE_TEST=1 SIM probe, not the hermetic unit tests. The factory helpers
    (_default_oauth_provider, _make_place_pick, _make_position_view_builder,
    build_protection_view + _make_protection_executor) compose the seams; they
    are validated only by the SIM probe. The pluggable fill-source
    (fill_source.PollingFillSource) stays a tested seam for the phase-B streaming
    drop-in; the MVP loop detects fills through reconcile_bridge.verdicts
    (reconcile classifies FILLED), so no PollingFillSource instance is wired
    into LoopDeps here.

    ``notify`` and ``chain_loss_notify`` are the concrete alert sinks (PR-4,
    NotificationPort) — injected by the CLI composition root
    (``alphalens_cli.commands.broker``), which is the only site allowed to
    import telegram. ``notify`` is the raw daemon alert sink (wrapped here in
    ``_journaled_alert`` so journald always gets the line first);
    ``chain_loss_notify`` is threaded into the OAuth provider for the
    refresh-chain-lost alert. This module never imports telegram itself."""
    from alphalens_pipeline.brokers.automanager import (  # noqa: F401 (planner/safety used by _make_place_pick)
        orphan_sweeper,
        picks,
        placement_planner,
        reconcile_bridge,
        safety,
        session_keeper,
    )
    from alphalens_pipeline.brokers.registry import get_default_broker
    from alphalens_pipeline.brokers.submission_log import (
        DEFAULT_SUBMISSIONS_PATH,
        iter_submission_records,
    )

    # One-shot bounded-growth maintenance: fold the append-only standalone-stop
    # journal down to its minimal fold-equivalent set (issue #895). Runs here —
    # at startup, before the tick loop — so no concurrent tick races the rewrite.
    _compact_standalone_stop_journal()

    broker = get_default_broker()
    # Resolve the behavioral exit policy ONCE, at startup — fail fast on a bad env
    # name here (a ValueError inside the per-tick protection pass would starve every
    # position that tick). The resolved instance is cached on LoopDeps + threaded
    # into build_protection_view so no hot path ever re-resolves.
    exit_policy = resolve_exit_policy(_exit_policy())
    if not isinstance(broker, SupportsStandaloneStop):
        raise BrokerCapabilityError(
            f"broker {broker.name!r} does not implement place_standalone_stop "
            "(SupportsStandaloneStop) — the auto-manager's disaster-stop flow "
            "requires it; wire a different broker or add the capability."
        )
    # OCO (rung 1 -> 2) is OPTIONAL, unlike the hard standalone-stop gate above: a
    # broker lacking SupportsOcoExit (or with the env flag off) runs stop-only,
    # unchanged. Detect it once here and inject into the executor closure.
    oco_placer: OcoPlacer | None = (
        broker.place_oco_exit if isinstance(broker, SupportsOcoExit) else None
    )
    # Stage-3 amend capability. FAIL-FAST when the amend flag is on but the wired
    # broker cannot amend — so the pure layer may emit AmendStop freely, knowing a
    # capable broker is guaranteed at runtime (saxo Stage-3 memo, §Env gates). When
    # the flag is off, an incapable broker simply gets amend_placer=None and the
    # pure arm never emits AmendStop (additive-stop fallback, unchanged).
    if _amend_enabled() and not isinstance(broker, SupportsAmendStop):
        raise BrokerCapabilityError(
            f"broker {broker.name!r} does not implement amend_stop_amount "
            "(SupportsAmendStop) but ALPHALENS_BROKER_AMEND_ENABLED=1 — the Stage-3 "
            "AmendStop resize requires it; wire a capable broker or unset the flag."
        )
    amend_placer: AmendStopPlacer | None = (
        broker.amend_stop_amount if isinstance(broker, SupportsAmendStop) else None
    )
    # Exit-geometry (PR-6a/6b) CAPABILITY gate — the principled replacement for
    # PR-6a's removed blanket fail-fast. Flipping ALPHALENS_BROKER_EXIT_POLICY off
    # "setup_static" places an ATR-bracket stop anchored to the PLANNED blend, which
    # the PR-6b fill-complete reanchor (position_manager._maybe_reanchor) MUST PATCH
    # onto the realized avg_price via the AmendStop rail. A broker without
    # SupportsAmendStop cannot run that reanchor, so geometry would go live leaving a
    # wrong-distance stop (the memo §4.3 P0). Gate on the CAPABILITY, NOT on
    # _amend_enabled(): the reanchor is part of the geometry feature, not the Stage-3
    # grow/downsize amend that ALPHALENS_BROKER_AMEND_ENABLED gates — requiring that
    # flag too would let geometry go live WITHOUT the reanchor, the exact unsafe combo.
    if exit_policy.requires_amend_stop and not isinstance(broker, SupportsAmendStop):
        raise BrokerCapabilityError(
            f"exit policy {exit_policy.name!r} needs the AmendStop rail for "
            f"the PR-6b fill-complete reanchor, but broker {broker.name!r} does not implement "
            "amend_stop_amount (SupportsAmendStop) — geometry-live would leave a wrong-distance "
            "stop. Wire an amend-capable broker or unset the flag (setup_static)."
        )
    # ONE OAuth provider instance is shared by the SessionKeeper AND the streaming
    # reader, so there is a single OAuth chain / one flock owner and the reader can
    # re-authorize in place off the same bearer the main loop pushes.
    provider = _default_oauth_provider(alert=chain_loss_notify)
    keeper = session_keeper.SessionKeeper(provider)

    def _read_records() -> list[Mapping[str, Any]]:
        return list(iter_submission_records(DEFAULT_SUBMISSIONS_PATH))

    # One throttle instance lives for the daemon's lifetime so the re-alert
    # interval + per-uic failure escalation persist across ticks; it wraps the
    # same base sink the generic (un-throttled) tick alerts use.
    base_alert = _journaled_alert(notify)
    throttle = _AlertThrottle(base_alert)

    def _throttled(message: str, reason: str) -> bool:
        return throttle.emit(message, reason=reason)

    # Streaming early-wake handles (dark, SIM-only). All None unless the flag is on
    # AND the broker is Saxo AND the provider is OAuth AND the reader started — in
    # which case the daemon degrades to poll-only, byte-identical to today. The
    # _compact_standalone_stop_journal() above already ran, so the reader thread
    # never races the compaction rewrite (thread-model §Startup ordering).
    wake_event, stream_tick, stream_trigger = _build_stream_handles(broker, provider, _throttled)

    return LoopDeps(
        broker=broker,
        kill_file=KILL_FILE_PATH,
        ensure_alive=keeper.ensure_alive,
        iter_picks=picks.iter_picks,
        place_pick=_make_place_pick(broker, exit_policy),
        read_records=_read_records,
        verdicts_fn=reconcile_bridge.verdicts,
        build_position_view=_make_position_view_builder(broker),
        build_protection_view=functools.partial(build_protection_view, exit_policy=exit_policy),
        execute_protection=_make_protection_executor(
            broker, throttle, place_oco_exit=oco_placer, amend_stop=amend_placer
        ),
        sweep_orphans_fn=lambda b: orphan_sweeper.sweep(b, _read_records()),
        alert=base_alert,
        alert_throttled=_throttled,
        place_oco_exit=oco_placer,
        amend_stop=amend_placer,
        wake_event=wake_event,
        stream_tick=stream_tick,
        stream_trigger=stream_trigger,
        exit_policy=exit_policy,
        live_exits_feed_factory=_default_live_exits_feed_factory,
    )


def _build_streaming_subscriber(provider: Any) -> Any:
    """The subscription-REST client for the streaming reader thread.

    A DEDICATED SaxoClient with its OWN ``requests.Session`` — never the shared
    ``get_default_saxo_client()`` singleton. ``requests.Session`` is not
    thread-safe, so sharing it between the reader thread (subscription POST/DELETE
    on connect / reconnect) and the main protective thread (``get_positions`` /
    ``place_standalone_stop``) could corrupt the urllib3 connection pool and skip a
    protection pass — leaving a position naked longer than ``poll_seconds`` (worse
    than poll-only). The thread-safe OAuth ``provider`` IS shared so both clients
    see the same rotated bearer; only the HTTP session is isolated. Streaming REST
    is bounded by the reader's circuit breaker, so an independent throttle budget is
    acceptable (zen HIGH, PR #900)."""
    from alphalens_pipeline.brokers.saxo.client import SaxoClient

    return SaxoClient(provider)


def _build_stream_handles(
    broker: Broker,
    provider: Any,
    alert_throttled: Callable[[str, str], bool],
) -> tuple[threading.Event | None, Callable[[], None] | None, StreamTrigger | None]:
    """Construct + start the dark streaming reader when every structural
    precondition holds, else return the poll-only ``(None, None, None)``.

    Preconditions (each a fail-safe-to-poll gate, design memo §Env gates):
      1. ``ALPHALENS_BROKER_STREAMING_ENABLED=1`` (master dark gate);
      2. the broker is Saxo (the streaming REST + SIM rail live on ``SaxoClient``);
      3. the provider is OAuth — a static 24h token cannot be PUT-reauthorized in
         place, so :meth:`SaxoStreamingClient.start` would refuse anyway;
      4. the reader thread actually started (``start()`` returns True).

    SIM-probe-only (no hermetic cycle — the run_daemon wait + the per-tick hook are
    unit-tested against stubs). A construction / start failure logs once and falls
    back to poll-only rather than raising — streaming is a pure latency win and its
    absence must never block the protective loop."""
    if not _streaming_enabled():
        return None, None, None

    from alphalens_pipeline.brokers.automanager.streaming_trigger import StreamTrigger
    from alphalens_pipeline.brokers.saxo.broker import SaxoBroker
    from alphalens_pipeline.brokers.saxo.tokens import OAuthTokenProvider

    if not isinstance(broker, SaxoBroker):
        logger.warning(
            "streaming enabled but broker %r is not Saxo — running poll-only", broker.name
        )
        return None, None, None
    if not isinstance(provider, OAuthTokenProvider):
        logger.warning(
            "streaming enabled but the token provider is not OAuth (a static token "
            "cannot be re-authorized in place) — running poll-only"
        )
        return None, None, None

    stale_s = _stream_stale_s()
    context_id = f"almgr-{os.getpid()}-{int(time.time())}"  # <=50 chars, [a-zA-Z0-9-]
    try:
        trigger = StreamTrigger(
            token_provider=provider,
            subscriber=_build_streaming_subscriber(
                provider
            ),  # dedicated session, never the singleton
            context_id=context_id,
            client_stale_after_s=stale_s,
        )
        started = trigger.start()
    except Exception:  # any streaming-setup failure degrades to poll-only, never raises
        logger.warning(
            "streaming client construction/start failed — running poll-only", exc_info=True
        )
        return None, None, None
    if not started:
        logger.warning("streaming client refused to start — running poll-only")
        return None, None, None

    logger.info("saxo streaming reader started (context_id=%s, stale_s=%.0f)", context_id, stale_s)
    stream_tick = _make_stream_tick(
        trigger,
        get_bearer=provider.get_access_token,
        alert_throttled=alert_throttled,
        stale_s=stale_s,
    )
    return trigger.wake_event, stream_tick, trigger


# --- SIM-probe-only factory helpers (Component 6 "placer" home) --------------
# Thin composers over the Task 1-10 seams. They carry NO hermetic unit-test
# cycle (test_control_loop.py injects LoopDeps as stubs; build_default_deps and
# everything it wires is exercised end-to-end only by the deferred
# SAXO_LIVE_TEST=1 SIM live probe). _make_place_pick writes the append-only
# STANDALONE_STOP_JOURNAL_PATH `planned` lines — the plan PRICES the broker
# cannot know (disaster stop + in-band TP), keyed to the entry client_request_id
# and tier_index. NO journal line confers protection (saxo-oco memo §7): the
# protection pass (build_protection_view + reconcile_protection) derives it from
# live broker state. `_fold_planned_exits` folds the `planned` lines per-uic.

STANDALONE_STOP_JOURNAL_PATH = _BROKER_ORDERS_DIR / "standalone_stops.jsonl"

_ENTRY_SIDE = "BUY"  # MVP scope: long entries only (design memo, single-name equities)
_DISASTER_STOP_SIDE = "SELL"  # protective exit of a long entry


@dataclass(frozen=True)
class _AlreadyGatedSessionState:
    """safety.check's SessionState — place_pick only ever runs after run_once's
    own (no KILL) AND (chain alive) placement gate, so alive=True here restates
    a fact already established by the caller; the rails that actually gate this
    call are safety.check's own KILL-file / ALLOW_ORDERS / cap checks."""

    alive: bool = True


def _append_standalone_stop_journal(record: Mapping[str, Any]) -> None:
    """Append one line to the out-of-band standalone-stop journal (never rewrites).

    Flush + fsync after the append so a plan price / capability marker is durable
    the instant it is written — a buffered write lost to a crash (or systemd
    SIGKILL) would silently drop a disaster-stop plan, and the protection pass
    can never re-derive a price the broker does not know."""
    import json
    import os

    STANDALONE_STOP_JOURNAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    with STANDALONE_STOP_JOURNAL_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True, default=str) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


_INITIAL_GEN = 0  # entry-placement plan is generation 0; resizes bump it via next_gen() (Task 4)


def _build_planned_line(
    *,
    entry_crid: str,
    uic: int,
    side: str,
    stop_price: float,
    take_profit: float | None,
    tier_index: int,
    gen: int = _INITIAL_GEN,
    geometry_stamp: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One append-only `planned` journal line — the plan PRICES the broker cannot
    know (disaster stop + in-band TP), keyed to the entry client_request_id and
    its ORIGINAL tier_index, plus the resize `gen`. `_fold_planned_exits` (Task 4)
    reads these back per-uic into PlannedExit; NO line here confers protection —
    protection is derived from live broker state only (design memo §7).

    ``geometry_stamp`` (PR-6a dark shadow, exit-geometry memo §4.1/§4.3) is
    TELEMETRY ONLY — namespaced under a single ``"geometry"`` key so it can
    never collide with a field `_fold_planned_exits` reads, and it is never
    read by the fold (measures anchor divergence; confers no protection).
    ``None`` (the default) omits the key entirely, so a caller that never
    passes it keeps a byte-identical record to pre-PR-6a."""
    record: dict[str, Any] = {
        "kind": "planned",
        "client_request_id": entry_crid,
        "uic": int(uic),
        "side": side,
        "stop_price": float(stop_price),
        "take_profit": None if take_profit is None else float(take_profit),
        "tier_index": int(tier_index),
        "gen": int(gen),
    }
    if geometry_stamp is not None:
        record["geometry"] = geometry_stamp
    return record


def _iter_standalone_stop_journal() -> Iterator[dict[str, Any]]:
    """Yield parsed lines from the standalone-stop journal; malformed lines skipped."""
    import json

    if not STANDALONE_STOP_JOURNAL_PATH.exists():
        return
    with STANDALONE_STOP_JOURNAL_PATH.open("r", encoding="utf-8") as fh:
        for raw_line in fh:
            line = raw_line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                yield record


def _read_persisted_gen(uic: int) -> tuple[int, float | None]:
    """Latest ``(gen, qty)`` recorded for a uic in the append-only gen journal;
    ``(_INITIAL_GEN, None)`` when the uic has never been sized (append-only, so
    the last matching line wins)."""
    gen = _INITIAL_GEN
    last_qty: float | None = None
    for line in _iter_standalone_stop_journal():
        if line.get("kind") != "gen":
            continue
        try:
            if int(line["uic"]) != uic:
                continue
            gen = int(line["gen"])
            last_qty = float(line["qty"])
        except (KeyError, TypeError, ValueError):
            continue
    return gen, last_qty


def _make_next_gen(uic: int) -> Callable[[float], int]:
    """A per-uic resize counter bound to the persisted gen journal (memo §4.5).

    Returns the SAME generation for a same-size retry — Saxo's 15 s request-id
    dedup then catches the re-POST — and a DISTINCT, incremented generation when
    the intended sell qty changes by more than ``_QTY_EPS`` (a resize is a
    distinct order, never falsely deduped to the stale, smaller one). The bump is
    appended, never rewritten, so the counter survives a systemd restart. The
    size compare uses ``_QTY_EPS`` — never a bare float ``>=`` (A-S6/B-S2)."""

    def _next_gen(qty: float) -> int:
        gen, last_qty = _read_persisted_gen(uic)
        if last_qty is not None and abs(qty - last_qty) <= _QTY_EPS:
            return gen  # same-size retry -> stable ref (dedup-safe)
        if last_qty is not None:
            gen += 1  # resize -> distinct ref (never deduped to the stale order)
        _append_standalone_stop_journal(
            {"kind": "gen", "uic": int(uic), "gen": int(gen), "qty": float(qty)}
        )
        return gen

    return _next_gen


def _latest_planned_by_crid(
    lines: Iterable[Mapping[str, Any]],
) -> dict[str, tuple[int, Mapping[str, Any]]]:
    """The newest well-formed ``planned`` line per entry client_request_id
    (append-only: highest ``gen`` wins). Non-``planned``, keyless, or malformed
    (bad uic / stop_price) lines are skipped."""
    latest: dict[str, tuple[int, Mapping[str, Any]]] = {}
    for line in lines:
        if line.get("kind") != "planned":
            continue
        crid = line.get("client_request_id")
        raw_uic = line.get("uic")
        if not crid or raw_uic is None:
            continue
        try:
            gen = int(line.get("gen", _INITIAL_GEN))
            int(raw_uic)
            float(line["stop_price"])
        except (KeyError, TypeError, ValueError):
            continue
        prev = latest.get(str(crid))
        if prev is None or gen >= prev[0]:
            latest[str(crid)] = (gen, line)
    return latest


def _fold_planned_exits(lines: Iterable[Mapping[str, Any]]) -> dict[int, PlannedExit]:
    """Fold the append-only ``planned`` journal lines into ONE PlannedExit per
    NETTED uic (saxo-oco memo §7) — PLAN PRICES only, NEVER a protected set.

    Protection is derived from live broker state every tick (Tasks 5/6); no
    journal line confers it, so ``intent`` / ``placed`` lines contribute nothing
    here. Keying is per-uic (the unit Saxo nets to), never per-client_request_id.

    Governing rules (memo §8):
      - disaster stop = the MAX stop for a long (tightest) — defensive if
        journaled tiers disagree;
      - TP + entry_crid = the SHALLOWEST tier (min ``tier_index``), so the
        deterministic ref is fill-order-independent;
      - a repeated ``tier_index`` on one uic reveals >1 distinct plan (each plan
        owns exactly one tier per index) -> ``conflicting`` so Task 5 refuses to
        merge. Malformed lines are skipped."""
    # Latest planned line per entry tier (append-only: highest gen wins per crid).
    latest_by_crid = _latest_planned_by_crid(lines)

    tiers_by_uic: dict[int, list[Mapping[str, Any]]] = {}
    for _gen, line in latest_by_crid.values():
        tiers_by_uic.setdefault(int(line["uic"]), []).append(line)

    result: dict[int, PlannedExit] = {}
    for uic, tiers in tiers_by_uic.items():
        index_counts: dict[int, int] = {}
        for line in tiers:
            idx = int(line.get("tier_index", 0))
            index_counts[idx] = index_counts.get(idx, 0) + 1
        n_plans = max(index_counts.values())
        stop_price = max(float(line["stop_price"]) for line in tiers)
        governing = min(tiers, key=lambda line: int(line.get("tier_index", 0)))
        tp_raw = governing.get("take_profit")
        result[uic] = PlannedExit(
            uic=uic,
            entry_crid=str(governing["client_request_id"]),
            side=str(governing.get("side", _DISASTER_STOP_SIDE)),
            stop_price=stop_price,
            tp_price=None if tp_raw is None else float(tp_raw),
            conflicting=n_plans > 1,
            n_plans=n_plans,
            next_gen=_make_next_gen(uic),
            next_amend_seq=_make_next_amend_seq(uic),
            reanchor=_reanchor_facts_from_governing(governing),
        )
    return result


def _reanchor_facts_from_governing(governing: Mapping[str, Any]) -> ReanchorFacts | None:
    """PR-6b: fold the governing planned line's ``"geometry"`` shadow stamp
    (PR-6a's ``_geometry_shadow_stamp``) into ``ReanchorFacts(k_atr, atr)``, or
    ``None`` when the blob is absent / malformed. ``None`` for every
    pre-PR-6a journal line (no ``"geometry"`` key) — so ``_fold_planned_exits``
    stays BYTE-IDENTICAL for the whole pre-PR-6a journal history."""
    geo = governing.get("geometry")
    if not isinstance(geo, dict):
        return None
    try:
        k_atr = float(geo["k_atr"])
        atr = float(geo["atr"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (math.isfinite(k_atr) and math.isfinite(atr)):
        return None
    return ReanchorFacts(k_atr=k_atr, atr=atr)


# --- Live-exit TP-tranche ladder persistence (INC-5 Task 1) ------------------
# The `planned` journal line above carries only a scalar `take_profit` -- not
# enough for the live-exit engine (`live_exit_engine.ManagedExit`), which needs
# the FULL tp_tranches tuple + the tranche-sizing base (`reference_qty`) to
# rebuild a managed position from the journal alone. `_build_tranche_plan_line`
# / `fold_tranche_plans` persist that ladder per uic, append-only, mirroring the
# `_build_planned_line` / `_fold_planned_exits` pattern. INERT here: nothing
# reads the fold until the live-exits tick phase (Task 2) is wired.


def _build_tranche_plan_line(
    *,
    uic: int,
    tp_tranches: tuple[TpTranchePlan, ...],
    reference_qty: float,
    stop_price: float,
) -> dict[str, Any]:
    """One append-only ``tranche_plan`` journal line -- the per-uic TP ladder the
    live-exit engine needs (INC-5) but the ``planned`` line does not carry.
    JSON-serializable (each ``TpTranchePlan`` is decomposed to a plain dict).
    Confers no protection by itself -- only the tranche reference prices/pcts,
    the sizing base, and the stop price the engine amends the standalone SL
    around. Written ONCE per placement (never per tier); a same-uic re-arm
    simply appends a newer line (append-only fold, last well-formed line wins,
    exactly like ``_build_planned_line``)."""
    return {
        "kind": "tranche_plan",
        "uic": int(uic),
        "tp_tranches": [
            {
                "tranche_index": int(t.tranche_index),
                "target_price": float(t.target_price),
                "tranche_pct": float(t.tranche_pct),
                "r_multiple": float(t.r_multiple),
                "tag": str(t.tag),
            }
            for t in tp_tranches
        ],
        "reference_qty": float(reference_qty),
        "stop_price": float(stop_price),
    }


def fold_tranche_plans(
    lines: Iterable[Mapping[str, Any]],
) -> dict[int, tuple[tuple[TpTranchePlan, ...], float, float]]:
    """Fold the append-only ``tranche_plan`` journal lines into the newest ladder
    per uic -- ``{uic: (tp_tranches, reference_qty, stop_price)}``.

    Append-only: the LAST well-formed line for a uic wins (mirrors
    ``_latest_planned_by_crid``'s "last wins" semantics, keyed per-uic since a
    live position nets to one uic). Non-``tranche_plan`` lines and malformed
    lines (missing/unparsable uic, non-list ``tp_tranches``, or a tranche
    missing/mistyping any of its five fields) are skipped ENTIRELY -- a
    malformed line contributes nothing, never a partial fold."""
    from broker_contract.sizing import TpTranchePlan

    out: dict[int, tuple[tuple[TpTranchePlan, ...], float, float]] = {}
    for line in lines:
        if line.get("kind") != "tranche_plan":
            continue
        raw_uic = line.get("uic")
        raw_tranches = line.get("tp_tranches")
        if raw_uic is None or not isinstance(raw_tranches, list):
            continue
        try:
            uic = int(raw_uic)
            reference_qty = float(line["reference_qty"])
            stop_price = float(line["stop_price"])
            tranches = tuple(
                TpTranchePlan(
                    tranche_index=int(t["tranche_index"]),
                    target_price=float(t["target_price"]),
                    tranche_pct=float(t["tranche_pct"]),
                    r_multiple=float(t["r_multiple"]),
                    tag=str(t["tag"]),
                )
                for t in raw_tranches
            )
        except (KeyError, TypeError, ValueError):
            continue
        out[uic] = (tranches, reference_qty, stop_price)
    return out


def _mark_oco_unsupported(uic: int) -> None:
    """Persist the per-instrument OCO-unsupported capability flag (saxo-oco memo §7).

    Append one out-of-band ``oco_unsupported`` line keyed by int uic. Written by
    the Stage-2 executor when ``place_oco_exit`` fails (any BrokerError — a
    structural ``SellOrdersAlreadyExist`` / ``TooFarFromEntry`` reject, a rate
    limit, or a 202) so the rung 1 -> 2 upgrade is never re-attempted on that uic,
    even after a systemd restart — the rung-1 stop stays the proven terminal rung.
    ``_fold_oco_unsupported`` reads these lines back into
    ``build_protection_view``'s ``ProtectionView.oco_unsupported``."""
    _append_standalone_stop_journal({"kind": "oco_unsupported", "uic": int(uic)})


def _fold_oco_unsupported(lines: Iterable[Mapping[str, Any]]) -> frozenset[int]:
    """Fold the append-only ``oco_unsupported`` journal lines into the set of uics
    whose OCO rung-2 upgrade is permanently disabled (saxo-oco memo §7).

    A uic marked once stays marked (append-only, so a rebuilt view after a restart
    still carries the flag and ``_reconcile_long`` degrades the covered branch to
    ``NoOp`` -> no re-attempt churn). Non-``oco_unsupported`` and malformed lines
    (missing / unparsable uic) are skipped.

    Clearing a permanent marker is a MANUAL operator action (delete its journal
    line while the daemon is stopped) — there is no un-mark fold. Markers written
    before the transient ``oco_too_far`` split (e.g. VRNS's from the 2026-07-29
    TooFarFromMarket open) keep their permanent stop-only meaning."""
    disabled: set[int] = set()
    for line in lines:
        if line.get("kind") != "oco_unsupported":
            continue
        try:
            disabled.add(int(line["uic"]))
        except (KeyError, TypeError, ValueError):
            continue
    return frozenset(disabled)


# Stage-3 TTL folds (saxo Stage-3 memo). Both start at 120s (~2-3 poll intervals),
# a value BETWEEN Saxo's 15s request-id dedup and the 45s poll so the JOURNAL — not
# request-id dedup — suppresses a B0 re-fire / an amend retry across the window.
# Tune after observing real SIM list-orders propagation lag + amend-retry cadence.
_OCO_PLACED_TTL_S = 120.0
_AMEND_FAILED_TTL_S = 120.0

# A TooFarFromMarket OCO reject is PRICE-dependent, not an instrument
# incapability: Saxo bounds the exit's distance from the CURRENT market, so a
# volatile open (VRNS incident 2026-07-29) rejects a bracket that succeeds once
# prices settle. 15 min — deliberately longer than the 120s folds above — rides
# out the opening dislocation instead of thrashing B0 retries against a
# still-moving price, while re-qualifying the uic for OCO the same session.
# Only FRESH naked fills consult it (B0), and the rejected fill is already
# covered by the fallback stop, so the longer TTL costs nothing in churn.
_OCO_TOO_FAR_TTL_S = 900.0


def _journal_oco_too_far(uic: int, *, clock: Callable[[], float] = time.time) -> None:
    """Persist a timestamped ``oco_too_far`` marker (overnight-drift memo, action 5).

    Written by the B0 executor on a clean ``TooFarFromMarket`` OCO reject
    INSTEAD of the permanent ``oco_unsupported`` flag. ``build_protection_view``
    unions markers newer than ``_OCO_TOO_FAR_TTL_S`` into the EXISTING
    ``ProtectionView.oco_unsupported`` set, so downstream B0 logic is untouched
    and the uic automatically becomes OCO-eligible again for fresh fills once
    the TTL expires."""
    _append_standalone_stop_journal({"kind": "oco_too_far", "uic": int(uic), "ts": float(clock())})


def _journal_oco_placed(uic: int, *, clock: Callable[[], float] = time.time) -> None:
    """Persist a timestamped ``oco_placed`` marker (saxo Stage-3 memo, H1b/A1).

    Written by the executor ONLY on a CONFIRMED 2xx B0 OCO placement.
    ``build_protection_view`` folds markers newer than ``_OCO_PLACED_TTL_S`` into
    ``ProtectionView.oco_recently_placed`` so a second B0 cannot double-commit atop
    a resting OCO pair that live list-orders has not yet surfaced. The ``clock``
    seam keeps the marker's ``ts`` testable (default wall clock)."""
    _append_standalone_stop_journal({"kind": "oco_placed", "uic": int(uic), "ts": float(clock())})


def _journal_amend_failed(uic: int, *, clock: Callable[[], float] = time.time) -> None:
    """Persist a timestamped ``amend_failed`` marker (saxo Stage-3 memo, A4).

    Written by the executor on ANY AmendStop failure. Folded (within
    ``_AMEND_FAILED_TTL_S``) into ``ProtectionView.amend_recently_failed`` so the
    NEXT tick's grow/downsize arm SKIPS amend and falls to the proven B1 additive /
    place-residual-first primitive. NOT a permanent latch — a benign fill-race 400
    self-clears after the TTL and amend is retried."""
    _append_standalone_stop_journal({"kind": "amend_failed", "uic": int(uic), "ts": float(clock())})


def _journal_stop_placed(uic: int, qty: float, *, clock: Callable[[], float] = time.time) -> None:
    """Persist a timestamped ``stop_placed`` outcome record (observability-only).

    Written by the executor ONLY on a confirmed standalone-stop placement, with the
    qty ACTUALLY placed (post execute-time clamp). Read by nothing in the protection
    logic — no fold consumes it — it exists so fill-to-protection latency is
    measurable for the non-OCO path too (``oco_placed`` covers the OCO path). The
    ``clock`` seam keeps the record's ``ts`` testable (default wall clock)."""
    _append_standalone_stop_journal(
        {"kind": "stop_placed", "uic": int(uic), "qty": float(qty), "ts": float(clock())}
    )


def _journal_amend_ok(uic: int, qty: float, *, clock: Callable[[], float] = time.time) -> None:
    """Persist a timestamped ``amend_ok`` outcome record (observability-only).

    Written by the executor ONLY on a confirmed AmendStop PATCH, with the qty the
    stop was amended to (the live-clamped absolute target). Read by nothing in the
    protection logic — no fold consumes it — it exists so fill-to-protection latency
    is measurable on the amend path (``amend_failed`` already covers failures). The
    ``clock`` seam keeps the record's ``ts`` testable (default wall clock)."""
    _append_standalone_stop_journal(
        {"kind": "amend_ok", "uic": int(uic), "qty": float(qty), "ts": float(clock())}
    )


def _journal_reanchored(
    uic: int, avg_price: float, *, clock: Callable[[], float] = time.time
) -> None:
    """Persist a timestamped ``reanchored`` marker (PR-6b, broker-manager
    extraction memo §4.3). Written by the executor ONLY on a CONFIRMED
    reanchor AmendStop PATCH success (never on a failed attempt — a failed
    amend journals ``amend_failed`` like any other amend and simply retries).
    ``_fold_reanchored_markers`` folds these into
    ``ProtectionView.reanchored_by_uic``, the PERMANENT per-blend idempotence
    latch (no TTL — unlike ``oco_placed`` / ``amend_failed``, a confirmed
    reanchor for a given avg_price never needs to re-fire for that same
    blend)."""
    _append_standalone_stop_journal(
        {"kind": "reanchored", "uic": int(uic), "avg_price": float(avg_price), "ts": float(clock())}
    )


def _fold_reanchored_markers(lines: Iterable[Mapping[str, Any]]) -> dict[int, float]:
    """Fold the append-only ``reanchored`` journal markers into the LATEST
    (by ``ts``) ``avg_price`` per uic (PR-6b). A DICT, not a TTL frozenset —
    the reanchor latch is PERMANENT per blend (see ``_journal_reanchored``),
    so this has no ``now`` / ``ttl_s`` parameters, unlike ``_fold_ttl_markers``.
    Malformed (missing / unparsable uic, avg_price, or ts) lines are skipped."""
    latest_ts: dict[int, float] = {}
    latest_avg_price: dict[int, float] = {}
    for line in lines:
        if line.get("kind") != "reanchored":
            continue
        try:
            uic = int(line["uic"])
            avg_price = float(line["avg_price"])
            ts = float(line["ts"])
        except (KeyError, TypeError, ValueError):
            continue
        if uic not in latest_ts or ts >= latest_ts[uic]:
            latest_ts[uic] = ts
            latest_avg_price[uic] = avg_price
    return latest_avg_price


def _fold_trailed_markers(lines: Iterable[Mapping[str, Any]]) -> dict[int, float]:
    """Fold the append-only ``trailed`` journal markers into the LATEST (by ``ts``)
    trailed ``level`` per uic (Task 2). Mirrors ``_fold_reanchored_markers`` — a
    DICT, not a TTL frozenset — but reads ``line["level"]`` (the price the stop was
    confirmed trailed to) instead of the reanchor avg_price. Feeds
    ``ProtectionView.trailed_stop_by_uic``, the never-DOWN ratchet floor
    ``_maybe_trail`` requires a new proposal to clear by ``_TRAIL_STEP_EPS``.
    Malformed (missing / unparsable uic, level, or ts) lines are skipped."""
    latest_ts: dict[int, float] = {}
    latest_level: dict[int, float] = {}
    for line in lines:
        if line.get("kind") != "trailed":
            continue
        try:
            uic = int(line["uic"])
            level = float(line["level"])
            ts = float(line["ts"])
        except (KeyError, TypeError, ValueError):
            continue
        if uic not in latest_ts or ts >= latest_ts[uic]:
            latest_ts[uic] = ts
            latest_level[uic] = level
    return latest_level


def _fold_ttl_markers(
    lines: Iterable[Mapping[str, Any]], kind: str, now: float, ttl_s: float
) -> frozenset[int]:
    """Fold timestamped ``kind`` markers into the set of uics whose newest marker is
    within ``ttl_s`` of ``now`` (saxo Stage-3 memo). Append-only, so a uic with BOTH
    a stale and a fresh marker still counts (the fresh one adds it; the stale one is
    simply skipped). Malformed (missing / unparsable uic or ts) lines are skipped."""
    fresh: set[int] = set()
    for line in lines:
        if line.get("kind") != kind:
            continue
        try:
            uic = int(line["uic"])
            ts = float(line["ts"])
        except (KeyError, TypeError, ValueError):
            continue
        if now - ts <= ttl_s:
            fresh.add(uic)
    return frozenset(fresh)


def _read_persisted_amend_seq(uic: int) -> int:
    """The highest ``amend_seq`` recorded for ``uic`` in the append-only journal, or
    ``-1`` when the uic has never been amend-sequenced (so the first seq is 0)."""
    seq = -1
    for line in _iter_standalone_stop_journal():
        if line.get("kind") != "amend_seq":
            continue
        try:
            if int(line["uic"]) != uic:
                continue
            seq = max(seq, int(line["seq"]))
        except (KeyError, TypeError, ValueError):
            continue
    return seq


def _make_next_amend_seq(uic: int) -> Callable[[], int]:
    """A per-uic MONOTONIC amend-sequence bound to the journal (saxo Stage-3 memo).

    Returns ``max+1`` ALWAYS (never qty-keyed), so a genuine re-resize to a
    previously-seen target qty gets a FRESH ``-amend-<seq>`` ref and is never
    dedup-swallowed by Saxo's 15s request-id window (mitigation A3/H3). Absolute-
    target semantics make a cross-tick re-emit safe (two sets of Amount=owned =
    owned, never 2x), so monotonic-not-qty-keyed never double-commits. The bump is
    appended, never rewritten, so the counter survives a systemd restart."""

    def _next_seq() -> int:
        seq = _read_persisted_amend_seq(uic) + 1
        _append_standalone_stop_journal({"kind": "amend_seq", "uic": int(uic), "seq": int(seq)})
        return seq

    return _next_seq


def _coerce(line: Mapping[str, Any], key: str, caster: Callable[[Any], Any]) -> Any:
    """Cast ``line[key]`` via ``caster``, or return None if the key is missing or
    the value is uncastable — the "skip this malformed field" primitive for the
    journal compactor."""
    try:
        return caster(line[key])
    except (KeyError, TypeError, ValueError):
        return None


def _keep_latest_marker(
    dest: dict[int, tuple[float, dict[str, Any]]],
    uic: Any,
    sort_key: Any,
    line: Mapping[str, Any],
) -> None:
    """Record ``line`` as ``dest[uic] = (sort_key, dict(line))`` keeping the MAX
    ``sort_key`` per uic (a later line breaks a tie via ``>=``). No-op when ``uic``
    or ``sort_key`` is None (a malformed line contributes nothing)."""
    if uic is None or sort_key is None:
        return
    prev = dest.get(uic)
    if prev is None or sort_key >= prev[0]:
        dest[uic] = (sort_key, dict(line))


def _compact_standalone_stop_journal_lines(
    lines: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Return the MINIMAL set of journal lines that folds IDENTICALLY to ``lines``
    (issue #895 — bound the append-only journal's unbounded growth).

    Keeps exactly what the readers need and nothing else:
      - the NEWEST ``planned`` per client_request_id (mirroring
        ``_latest_planned_by_crid`` — highest ``gen`` wins, later line breaks a
        tie), so ``_fold_planned_exits`` is unchanged;
      - ONE ``oco_unsupported`` per uic (``_fold_oco_unsupported`` only needs the
        uic present);
      - the NEWEST (max ``ts``) ``oco_placed`` / ``amend_failed`` / ``oco_too_far``
        per uic — the TTL fold's membership for ANY ``now`` is decided by the
        newest marker, so older ones are redundant — plus the NEWEST ``stop_placed``
        / ``amend_ok`` outcome record per uic (observability-only, no fold reads
        them; the latest outcome is what latency inspection needs);
      - the ``amend_seq`` carrying the MAX seq per uic (``_read_persisted_amend_seq``
        returns that max).

    Every other line — ``gen`` markers (read only by ``_read_persisted_gen``, whose
    reset to the initial gen is harmless: post-restart re-emits are past Saxo's 15s
    request-id dedup window, and protection is broker-state-truth not journal-derived),
    unknown kinds, and malformed lines — is dropped; none contributes to the four
    folds above. Pure: no I/O, input never mutated (kept lines are shallow-copied)."""
    materialized = list(lines)

    # Newest planned per crid — reuse the fold's own selection so the compacted
    # set contains EXACTLY the line _fold_planned_exits would elect. Sorted by
    # crid for a deterministic, stable file order.
    planned_by_crid = _latest_planned_by_crid(materialized)
    planned: list[dict[str, Any]] = [
        dict(planned_by_crid[crid][1]) for crid in sorted(planned_by_crid)
    ]

    oco_unsupported: dict[int, dict[str, Any]] = {}
    ttl_latest: dict[str, dict[int, tuple[float, dict[str, Any]]]] = {
        "oco_placed": {},
        "amend_failed": {},
        "oco_too_far": {},
        "stop_placed": {},
        "amend_ok": {},
    }
    amend_seq: dict[int, tuple[float, dict[str, Any]]] = {}

    for line in materialized:
        kind = line.get("kind")
        if kind == "oco_unsupported":
            uic = _coerce(line, "uic", int)
            if uic is not None:
                oco_unsupported.setdefault(uic, dict(line))
        elif kind in ttl_latest:
            _keep_latest_marker(
                ttl_latest[kind], _coerce(line, "uic", int), _coerce(line, "ts", float), line
            )
        elif kind == "amend_seq":
            _keep_latest_marker(
                amend_seq, _coerce(line, "uic", int), _coerce(line, "seq", int), line
            )

    compacted: list[dict[str, Any]] = list(planned)
    compacted.extend(oco_unsupported[uic] for uic in sorted(oco_unsupported))
    compacted.extend(ttl_latest["oco_placed"][uic][1] for uic in sorted(ttl_latest["oco_placed"]))
    compacted.extend(
        ttl_latest["amend_failed"][uic][1] for uic in sorted(ttl_latest["amend_failed"])
    )
    compacted.extend(ttl_latest["oco_too_far"][uic][1] for uic in sorted(ttl_latest["oco_too_far"]))
    compacted.extend(ttl_latest["stop_placed"][uic][1] for uic in sorted(ttl_latest["stop_placed"]))
    compacted.extend(ttl_latest["amend_ok"][uic][1] for uic in sorted(ttl_latest["amend_ok"]))
    compacted.extend(amend_seq[uic][1] for uic in sorted(amend_seq))
    return compacted


def _compact_standalone_stop_journal() -> None:
    """Atomically rewrite the standalone-stop journal with its compacted form.

    Read the current file, compute the minimal fold-equivalent line set, and
    replace the file in place (temp file in the SAME dir + ``os.replace`` — an
    atomic rename on POSIX, so a crash mid-rewrite leaves the old journal intact).
    A NO-OP when the journal is absent or holds no parseable records — never
    creates or truncates a file that has nothing to compact.

    Call ONCE at daemon startup (``build_default_deps``), BEFORE the tick loop, so
    no concurrent tick can race the rewrite against an append."""
    import contextlib
    import json
    import os
    import tempfile

    path = STANDALONE_STOP_JOURNAL_PATH
    if not path.exists():
        return
    lines = list(_iter_standalone_stop_journal())
    if not lines:
        return
    compacted = _compact_standalone_stop_journal_lines(lines)

    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=".standalone_stops.compact-", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            for record in compacted:
                fh.write(json.dumps(record, sort_keys=True, default=str) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, str(path))
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def _default_oauth_provider(*, alert: NotificationPort | None = None) -> Any:
    """Return the shipped OAuthTokenProvider wired from the Saxo env vars.

    ``alert`` is the chain-loss ``NotificationPort``, injected by
    ``build_default_deps`` from the CLI composition root (PR-4) — when
    omitted, ``OAuthTokenProvider`` falls back to its own journald-only
    default (``tokens._log_chain_loss``)."""
    from alphalens_pipeline.brokers.saxo.tokens import OAuthTokenProvider

    return OAuthTokenProvider.from_env(alert=alert)


def _journaled_alert(send: Callable[[str], None]) -> Callable[[str], None]:
    """Wrap an alert delivery callable so every emitted alert ALSO lands in
    journald via logger.warning BEFORE the delivery attempt.

    Sink-level seam (one place, not the ~30 call sites): the VRNS naked-delta
    incident (2026-07-29) was undiagnosable from the box because "OCO rejected",
    "stop deferred" and orphan alerts went to Telegram ONLY — journalctl had no
    trace while the daemon was actively deferring every tick. Logging first means
    Telegram success/failure cannot affect the journald line. Throttle semantics
    are preserved for free: _AlertThrottle calls its base sink only when an alert
    is actually emitted, so a suppressed repeat never spams journald."""

    def _alert(message: str) -> None:
        logger.warning("alert: %s", message)
        send(message)

    return _alert


def _make_place_pick(
    broker: Broker, exit_policy: ExitPolicy | None = None
) -> Callable[[Any], bool]:
    """Compose safety.check -> placement_planner.classify -> placer loop over
    place_bracket_order + the submissions journal for one armed pick, plus the
    "planned" half of the out-of-band standalone-stop journal (the entry's
    plan-level disaster stop, correlated by client_request_id for
    _make_position_view_builder to fold back later). A safety refusal or a
    resolve/size/placement failure logs and returns False rather than raising —
    one bad pick must never crash a tick.

    ``exit_policy`` is the resolved-once cached ExitPolicy (Task 4): it decides
    WHETHER the journaled planned stop/TP use the ``atr_bracket_1p5`` geometry
    (``applies_geometry``) instead of the brief's static levels. It is threaded
    down to ``_place_tiers`` because that gate lives in the nested
    ``_journal_tier``, which has no LoopDeps in scope. Defaults to the inert
    ``SetupStaticPolicy`` (dark) so non-geometry call sites/tests keep the
    pre-Task-4 behavior."""

    def _place(pick: Any) -> bool:
        return _place_pick(broker, pick, exit_policy)

    return _place


def _index_entries_by_request_id(
    records: Iterable[Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    """Map each journaled bracket's client_request_id -> the bracket dict."""
    return {
        str(bracket.get("client_request_id")): bracket
        for record in records
        for bracket in record.get("brackets") or []
    }


def _summarize_open_verdicts(
    open_verdicts: Iterable[Any], records: Iterable[Mapping[str, Any]], today_iso: str
) -> tuple[int, float, float]:
    """Fold open verdicts into the safety.JournalView inputs
    ``(open_bracket_count, gross_committed, realized_r_today)``. ``gross_committed``
    joins each still-working verdict back to its journaled entry bracket for the
    committed-capital figure; ``realized_r_today`` sums today's closed R."""
    entry_by_request_id = _index_entries_by_request_id(records)
    open_bracket_count = 0
    gross_committed = 0.0
    realized_r_today = 0.0
    for verdict in open_verdicts:
        realized_r = verdict.details.get("realized_r")
        realized_date = (verdict.activity_time or "")[:10] or verdict.brief_date
        if realized_r is not None and realized_date == today_iso:
            realized_r_today += float(realized_r)
        if verdict.status in {"WORKING", "PARTIALLY_FILLED"}:
            open_bracket_count += 1
            bracket = entry_by_request_id.get(str(verdict.details.get("client_request_id") or ""))
            if bracket and bracket.get("entry") is not None and bracket.get("qty") is not None:
                gross_committed += float(bracket["entry"]) * float(bracket["qty"])
    return open_bracket_count, gross_committed, realized_r_today


def _resolve_and_size(
    broker: Broker,
    ticker: str,
    account: Any,
    spec: Any,
) -> tuple[Any, Any, Any] | None:
    """Resolve the US instrument, build any needed FX conversion, and size the
    already-parsed :class:`~broker_contract.trade_intent.schema.TradeSpec`.
    Returns ``(instrument, fx, plan)`` or ``None`` on any resolve/size failure
    (logged) — one bad pick must never crash a tick.

    PR-7 (broker-manager extraction memo §5): the brief-side parse
    (``parse_brief_to_spec``) and the exit-geometry build
    (``build_exit_geometry_spec``) moved to arm-time (``arm_command``) — this
    helper now only runs the money half (``compute_setup_plan``) on the
    already-parsed ``spec`` the daemon received on the drained
    ``TradeIntent``. The caller reads ``intent.exit`` directly for the
    (possibly ``None``) exit-geometry spec; this helper never touches a
    brief."""
    from broker_contract.contract import BrokerError
    from broker_contract.sizing import TradeSetupNotPlannableError, compute_setup_plan

    from alphalens_pipeline.brokers.execution import build_fx_conversion
    from alphalens_pipeline.brokers.routing import resolve_us_instrument

    try:
        instrument = resolve_us_instrument(broker, ticker)
        if not instrument.currency:
            logger.warning("place_pick %s: resolved with no instrument currency", ticker)
            return None
        fx = None
        if instrument.currency != account.currency:
            get_fx_rate = getattr(broker, "get_fx_rate", None)
            if get_fx_rate is None:
                logger.warning(
                    "place_pick %s: %s vs account %s but broker has no get_fx_rate",
                    ticker,
                    instrument.currency,
                    account.currency,
                )
                return None
            fx = build_fx_conversion(get_fx_rate(account.currency, instrument.currency))
        plan = compute_setup_plan(
            spec,
            paper_equity=account.total_value,
            scale_factor=1.0,
            fx=fx,
        )
    except (BrokerError, TradeSetupNotPlannableError) as exc:
        logger.warning("place_pick %s: resolve/size failed: %s", ticker, exc)
        return None

    return instrument, fx, plan


def _is_journalable_price(value: float | None) -> bool:
    """A price the journal may carry verbatim: present, finite and strictly
    positive. ``_build_tranche_plan_line`` writes ``float(...)`` straight through,
    so a None/NaN/zero level from a future geometry policy must be caught HERE
    rather than poisoning the ladder the live-exit engine folds back."""
    return value is not None and math.isfinite(value) and value > 0


def _geometry_shadow_stamp(
    exit_spec: Any, spec: Any, *, use_geometry: bool
) -> dict[str, Any] | None:
    """The ``"geometry"`` stamp journaled alongside a ``planned`` line (memo §4.3).

    Telemetry only — it rides along whenever an ``exit_spec`` is buildable,
    whatever the active policy, so the dark shadow can measure anchor divergence
    before any flip; ``use_geometry`` only records whether the stamped levels were
    the ones actually placed. ``None`` when no ``exit_spec`` exists, which keeps
    the journaled line byte-identical to pre-PR-6a.

    PR-7 opened a decode boundary (iter_picks -> codec): the schema permits a
    non-None exit whose reaction_plan is empty (reserved kind="levels", or a
    future policy-only client) or whose first primitive is NOT a reanchor
    (TrailingStop / ModelPush). Pre-PR-7 the exit was always built in-process with
    a single ReanchorOnFill, so reaction_plan[0] was safe; now resolve the
    reanchor BY TYPE and leave the reanchor-specific k_atr/atr/ceiling facts None
    when absent — never index [0] / attribute-access blindly, or a
    valid-but-reanchor-less intent would crash the unattended drain every tick."""
    from broker_contract.trade_intent.schema import ReanchorOnFill

    from alphalens_pipeline.paper.sizing import planned_blended_entry_from_spec

    if exit_spec is None:
        return None
    reanchor = next((p for p in exit_spec.reaction_plan if isinstance(p, ReanchorOnFill)), None)
    blend = planned_blended_entry_from_spec(spec) if spec is not None else None
    return {
        "policy_name": "atr_bracket_1p5",
        "policy_version": 1,
        "planned_blend": blend,
        "geometry_stop": exit_spec.initial_levels.stop,
        "geometry_tp": exit_spec.initial_levels.tp,
        "k_atr": reanchor.k_atr if reanchor is not None else None,
        "atr": reanchor.atr if reanchor is not None else None,
        "ceiling_price": reanchor.ceiling_price if reanchor is not None else None,
        "applied": use_geometry,
    }


def _geometry_tranche_ladder(exit_spec: Any) -> tuple[tuple[TpTranchePlan, ...], float] | None:
    """The (ladder, stop) pair the geometry policy implies: its ONE (stop, tp)
    level becomes a single tranche that exits 100% of the position at that
    take-profit. ``None`` when either level is not journalable — the caller then
    journals NOTHING rather than falling back to the static ladder, which the
    geometry policy never placed."""
    from broker_contract.sizing import TpTranchePlan

    geo_stop = exit_spec.initial_levels.stop
    geo_tp = exit_spec.initial_levels.tp
    if not (_is_journalable_price(geo_stop) and _is_journalable_price(geo_tp)):
        return None
    ladder = (
        TpTranchePlan(
            tranche_index=0,
            target_price=float(geo_tp),
            tranche_pct=1.0,
            r_multiple=0.0,
            tag="geometry",
        ),
    )
    return ladder, geo_stop


def _journal_tranche_plan(
    *,
    plan: Any,
    exit_spec: Any,
    placement: Any,
    instrument: Any,
    use_geometry: bool,
) -> None:
    """INC-5: journal ONE ``tranche_plan`` line per uic so the live-exit engine can
    rebuild the TP ladder from the journal alone. Source it from whatever the
    ACTIVE exit policy actually places the TP from: under the geometry policy
    (atr_bracket_1p5) that is the single ``exit_spec.initial_levels.tp`` level and
    ``plan.tp_tranches`` is EMPTY (the brief expresses its exit as geometry, not
    static tranches) — so gating on ``plan.tp_tranches`` alone silently dropped
    every geometry pick. Under the static policy the ladder IS
    ``plan.tp_tranches``. ``getattr`` keeps a bare-stub plan (unrelated
    failure-path unit doubles with no ``entry_tiers``/``tp_tranches``) from
    crashing — it simply journals nothing."""
    entry_tiers = getattr(plan, "entry_tiers", None) if plan is not None else None
    if not entry_tiers:
        return
    stop_price = placement.disaster_stop_price
    if use_geometry and exit_spec is not None:
        geometry = _geometry_tranche_ladder(exit_spec)
        if geometry is None:
            # Otherwise this skip is invisible: the live-exit engine finds no
            # ladder for the uic and the position sits stop-only, which reads in
            # the journal exactly like a pre-INC-5 pick.
            logger.warning(
                "tranche_plan uic %d: geometry levels unusable (stop=%r, tp=%r) — "
                "no TP ladder journaled, the position stays stop-only",
                int(instrument.broker_instrument_id),
                exit_spec.initial_levels.stop,
                exit_spec.initial_levels.tp,
            )
            return
        ladder, stop_price = geometry
    else:
        ladder = getattr(plan, "tp_tranches", None) or ()
    if not ladder:
        return
    _append_standalone_stop_journal(
        _build_tranche_plan_line(
            uic=int(instrument.broker_instrument_id),
            tp_tranches=ladder,
            reference_qty=sum(t.qty for t in entry_tiers),
            stop_price=stop_price,
        )
    )


def _place_tiers(
    broker: Broker,
    intent: Any,
    ticker: str,
    instrument: Any,
    account: Any,
    fx: Any,
    placement: Any,
    spec: Any = None,
    exit_spec: Any = None,
    exit_policy: ExitPolicy | None = None,
    plan: Any = None,
) -> int:
    """Place each entry tier's bracket, journaling IMMEDIATELY after each fill so a
    mid-loop crash leaves at most a partial ladder joined to submissions.jsonl (the
    drain then never re-places the full set on restart). Returns the count actually
    placed; a BrokerError stops the loop and writes a note-only trace record so the
    failure is auditable and an all-fail pick is not retried forever.

    ``exit_spec`` (PR-6a; PR-7: read off ``intent.exit``) is the
    ``atr_bracket_1p5`` geometry. ``exit_policy`` (Task 4) is the resolved-once
    cached :class:`~broker_contract.exit_geometry.ExitPolicy` — the geometry
    override gate reads ``exit_policy.applies_geometry`` (NOT the old
    ``ALPHALENS_BROKER_EXIT_POLICY`` env sentinel). With the inert
    ``SetupStaticPolicy`` (``applies_geometry=False``, the default), the
    journaled ``planned`` line's stop/TP stay the brief's static
    ``placement.disaster_stop_price`` / ``tier.tp`` — BYTE IDENTICAL to
    pre-PR-6a. A geometry policy (``atr_bracket_1p5``) overrides the journaled
    stop/TP with ``exit_spec.initial_levels`` instead (safe now that PR-6b's
    fill-complete avg_price reanchor — ``position_manager._maybe_reanchor`` —
    ships; ``build_default_deps`` no longer fail-fasts on the flag).
    Either way, whenever ``exit_spec`` is buildable a ``"geometry"`` shadow
    stamp is journaled alongside the plan prices (telemetry only, memo §4.3) —
    this is unconditional on the policy so the dark shadow can measure
    anchor divergence before any flip.

    ``spec`` (PR-7) is the already-parsed
    :class:`~broker_contract.trade_intent.schema.TradeSpec` off the drained
    ``TradeIntent`` — the geometry shadow stamp's ``planned_blend`` reads it
    via :func:`~alphalens_pipeline.paper.sizing.planned_blended_entry_from_spec`
    (the daemon no longer has the raw brief dict at drain time).

    ``plan`` (INC-5 Task 1) is the raw sized
    :class:`~broker_contract.sizing.SetupPlan` off ``_resolve_and_size`` —
    consulted ONLY to journal ONE ``tranche_plan`` line per uic (the per-uic TP
    ladder the live-exit engine reads later, INC-5's persistence gap) — see
    :func:`_journal_tranche_plan` for which ladder the ACTIVE policy sources it
    from. ``None`` (a caller with no sized plan in scope, e.g. the direct-unit
    tests) journals nothing extra — INERT, byte-identical to a caller that never
    passes it."""
    from broker_contract.contract import BrokerError

    from alphalens_pipeline.brokers.submission_log import (
        append_submission_record,
        build_submission_record,
    )

    # Normalize the resolved-once cached policy (Task 4): the geometry-override
    # gate below reads ``exit_policy.applies_geometry`` — the retired env-string
    # sentinel is gone. Default inert (dark) when no policy was threaded in.
    resolved_exit_policy: ExitPolicy = (
        exit_policy if exit_policy is not None else SetupStaticPolicy()
    )

    def _journal_tier(tier: Any, placed: Any) -> None:
        bracket = tier.bracket
        append_submission_record(
            build_submission_record(
                brief_date=intent.meta.brief_date,
                ticker=ticker,
                mic=instrument.exchange_mic,
                uic=instrument.broker_instrument_id,
                brackets=[
                    {
                        "client_request_id": bracket.client_request_id,
                        "entry_order_id": placed.entry_order_id,
                        "exit_order_ids": list(placed.exit_order_ids),
                        "qty": bracket.quantity,
                        "entry": bracket.entry_limit,
                        "stop": bracket.stop_loss,
                        "tp": bracket.take_profit,
                        "ttl": bracket.entry_ttl_days,
                    }
                ],
                note=None,
                sizing_currency=account.currency,
                instrument_currency=instrument.currency,
                sizing_equity=account.total_value,
                fx=fx,
            )
        )
        use_geometry = resolved_exit_policy.applies_geometry and exit_spec is not None
        if use_geometry and exit_spec is not None:  # 2nd clause restated to narrow exit_spec
            stop_price = exit_spec.initial_levels.stop
            take_profit = exit_spec.initial_levels.tp
        else:
            stop_price = placement.disaster_stop_price
            take_profit = tier.tp
        _append_standalone_stop_journal(
            _build_planned_line(
                entry_crid=bracket.client_request_id,
                uic=int(instrument.broker_instrument_id),
                side=_DISASTER_STOP_SIDE,
                stop_price=stop_price,
                take_profit=take_profit,
                tier_index=tier.tier_index,
                geometry_stamp=_geometry_shadow_stamp(exit_spec, spec, use_geometry=use_geometry),
            )
        )

    _journal_tranche_plan(
        plan=plan,
        exit_spec=exit_spec,
        placement=placement,
        instrument=instrument,
        use_geometry=resolved_exit_policy.applies_geometry,
    )

    placed_count = 0
    failure_note: str | None = None
    try:
        for tier in placement.tiers:
            placed = broker.place_bracket_order(tier.bracket)
            _journal_tier(tier, placed)
            placed_count += 1
    except BrokerError as exc:
        failure_note = (
            f"placement stopped after {placed_count}/{len(placement.tiers)} bracket(s): {exc}"
        )
        # Journal a note-only record so the failure is traced (and, when nothing
        # placed, the pick is not silently retried forever).
        append_submission_record(
            build_submission_record(
                brief_date=intent.meta.brief_date,
                ticker=ticker,
                mic=instrument.exchange_mic,
                uic=instrument.broker_instrument_id,
                brackets=[],
                note=failure_note,
                sizing_currency=account.currency,
                instrument_currency=instrument.currency,
                sizing_equity=account.total_value,
                fx=fx,
            )
        )

    if failure_note:
        logger.warning("place_pick %s: %s", ticker, failure_note)
    return placed_count


def _place_pick(broker: Broker, intent: Any, exit_policy: ExitPolicy | None = None) -> bool:
    """Place one armed :class:`~broker_contract.trade_intent.schema.TradeIntent`
    end-to-end (see _make_place_pick). Module-level so the per-phase helpers
    keep the tick logic flat; every failure path logs and returns False
    rather than raising.

    ``exit_policy`` (Task 4) is the resolved-once cached policy passed straight
    through to ``_place_tiers`` (whose nested ``_journal_tier`` owns the
    geometry-override gate).

    PR-7 (broker-manager extraction memo §5): the daemon never touches a
    brief any more — ``ticker``/``brief_date``/``spec``/``exit_spec`` are all
    read directly off the drained ``intent`` (the client already parsed +
    validated the brief at arm time, in ``arm_command``)."""
    import datetime as _dt

    from broker_contract.contract import BrokerError

    from alphalens_pipeline.brokers.automanager import safety
    from alphalens_pipeline.brokers.automanager.placement_planner import classify
    from alphalens_pipeline.brokers.automanager.reconcile_bridge import (
        verdicts as reconcile_verdicts,
    )
    from alphalens_pipeline.brokers.submission_log import (
        DEFAULT_SUBMISSIONS_PATH,
        iter_submission_records,
    )

    ticker = intent.instrument.ticker.upper()
    brief_date = _dt.date.fromisoformat(intent.meta.brief_date)
    spec = intent.spec
    exit_spec = intent.exit

    try:
        account = broker.get_account()
        positions = broker.get_positions()
        records = list(iter_submission_records(DEFAULT_SUBMISSIONS_PATH))
        open_verdicts = reconcile_verdicts(records, broker)
    except BrokerError as exc:
        logger.warning("place_pick %s: broker read failed: %s", ticker, exc)
        return False

    open_bracket_count, gross_committed, realized_r_today = _summarize_open_verdicts(
        open_verdicts, records, _dt.date.today().isoformat()
    )
    decision = safety.check(
        intent,
        safety.JournalView(
            open_bracket_count=open_bracket_count,
            gross_committed=gross_committed,
            realized_r_today=realized_r_today,
        ),
        safety.BrokerView(open_position_count=len(positions), equity=account.total_value),
        _AlreadyGatedSessionState(),
    )
    if isinstance(decision, safety.Refuse):
        logger.warning("place_pick %s: refused — %s", ticker, decision.reason)
        # Terminal refusal (queue-semantics fix 2026-07-30): ONLY a capacity
        # refusal (decision.terminal — MAX_OPEN / portfolio gross cap) journals
        # a refused line so the pick never retries — left armed it would retry
        # every tick for days and then self-place a stale brief signal once
        # capacity frees. Re-arming via `alphalens broker arm` is the explicit
        # human path back. The transient rails (KILL file, dead chain,
        # ALLOW_ORDERS master arm, daily-loss lockout) keep the pick armed —
        # an inert/paused daemon must never destroy the armed queue. The
        # append is fallible I/O and must never crash the drain: on OSError
        # the pick stays armed and the refusal re-fires next tick
        # (re-attempting the append).
        if decision.terminal:
            from alphalens_pipeline.brokers.automanager import picks

            try:
                picks.mark_refused(ticker, brief_date, decision.reason)
            except OSError as exc:
                logger.warning(
                    "place_pick %s: refused-line append failed (pick stays armed): %s", ticker, exc
                )
        return False

    resolved = _resolve_and_size(broker, ticker, account, spec)
    if resolved is None:
        return False
    instrument, fx, plan = resolved

    placement = classify(plan, instrument, side=_ENTRY_SIDE)
    if not placement.tiers:
        logger.warning("place_pick %s: every entry tier sized to zero shares", ticker)
        return False

    return (
        _place_tiers(
            broker,
            intent,
            ticker,
            instrument,
            account,
            fx,
            placement,
            spec,
            exit_spec,
            exit_policy=exit_policy,
            plan=plan,
        )
        > 0
    )


def _make_position_view_builder(
    broker: Broker,
) -> Callable[[Broker, list[Mapping[str, Any]]], BrokerView]:
    """Fold the submissions journal into a position_manager.BrokerView carrying
    ONLY ``working_children`` — the still-WORKING exit order ids per entry, used
    by the terminal / round-trip ``CancelRemaining`` sweep.

    No journal line confers protection any more (saxo-oco memo §7): the
    disaster-stop / protected BrokerView halves are gone (Bug A). Protection is
    derived purely from live broker state by the protection pass
    (``build_protection_view`` + ``reconcile_protection``)."""

    def _build(_broker: Broker, records: list[Mapping[str, Any]]) -> BrokerView:
        from broker_contract.contract import OrderStatus

        working_ids = {
            str(state.order_id)
            for state in _broker.list_open_orders()
            if state.status == OrderStatus.WORKING
        }
        working_children: dict[str, tuple[str, ...]] = {}
        for record in records:
            for bracket in record.get("brackets") or []:
                request_id = bracket.get("client_request_id")
                if not request_id:
                    continue
                exits = tuple(
                    str(order_id)
                    for order_id in (bracket.get("exit_order_ids") or [])
                    if str(order_id) in working_ids
                )
                if exits:
                    working_children[str(request_id)] = exits

        return BrokerView(working_children=working_children)

    return _build


# --- Broker-state-truth protection (saxo-oco memo §5/§6) ---------------------


def _position_uic(pos: Position) -> int | None:
    """The uic a Position belongs to (``broker_instrument_id`` is ``str(Uic)``)."""
    try:
        return int(pos.instrument.broker_instrument_id)
    except (TypeError, ValueError, AttributeError):
        return None


def build_protection_view(
    broker: Broker,
    _records: list[Mapping[str, Any]],
    *,
    exit_policy: ExitPolicy | None = None,
    clock: Callable[[], float] = time.time,
) -> ProtectionView:
    """Assemble the ONE per-tick protection snapshot (saxo-oco memo §6): live
    netted positions + live working SELL legs (correlated by uic) + the plan
    PRICES folded from the append-only ``planned`` journal. Protection status is
    then a pure function of this view — no journal line asserts it (kills Bug A).

    ``oco_unsupported`` (Stage 2) folds the persisted per-instrument capability
    flag from the SAME append-only journal that carries the plan prices — read
    ONCE here so both folds see the same lines (a second pass over the generator
    would be empty). Stage 3 additionally folds the timestamped ``oco_placed`` /
    ``amend_failed`` markers against ``clock`` (default wall clock; injected in
    tests) into the TTL sets ``oco_recently_placed`` / ``amend_recently_failed``.
    Unexpired ``oco_too_far`` markers (transient TooFarFromMarket rejects) are
    unioned into ``oco_unsupported`` itself, so the degrade self-clears after
    ``_OCO_TOO_FAR_TTL_S`` with no downstream branching change."""
    all_positions: dict[int, Position] = {}
    for pos in broker.get_positions():
        uic = _position_uic(pos)
        if uic is not None:
            all_positions[uic] = pos

    long_positions: dict[int, Position] = {}
    get_long = getattr(broker, "get_long_positions", None)
    longs = get_long() if get_long is not None else list(all_positions.values())
    # get_long_positions returns ONE netted Position per uic (it sums same-uic
    # lots); this assignment therefore never overwrites a live uic. If a source
    # ever returned multiple lots per uic here, the stop would size to one lot
    # and leave the rest naked — that summing is the broker's responsibility.
    for pos in longs:
        uic = _position_uic(pos)
        if uic is not None and pos.quantity > _QTY_EPS:
            long_positions[uic] = pos

    sell_legs: dict[int, list[Any]] = {}
    list_sells = getattr(broker, "list_working_sell_orders", None)
    orders = list_sells() if list_sells is not None else []
    for order in orders:
        if order.uic is not None:
            sell_legs.setdefault(int(order.uic), []).append(order)

    # Materialize the append-only journal ONCE so every fold reads the same lines
    # (a second pass over the generator would be empty). ``now`` is sampled ONCE
    # so both TTL folds classify against a single instant.
    journal_lines = list(_iter_standalone_stop_journal())
    now = clock()
    return ProtectionView(
        long_positions=long_positions,
        all_positions=all_positions,
        sell_legs_by_uic={uic: tuple(legs) for uic, legs in sell_legs.items()},
        planned_by_uic=_fold_planned_exits(journal_lines),
        # Permanent capability markers UNION unexpired transient oco_too_far
        # markers — a TooFarFromMarket reject degrades the uic only for
        # _OCO_TOO_FAR_TTL_S, after which fresh fills re-qualify for OCO.
        oco_unsupported=_fold_oco_unsupported(journal_lines)
        | _fold_ttl_markers(journal_lines, "oco_too_far", now, _OCO_TOO_FAR_TTL_S),
        oco_recently_placed=_fold_ttl_markers(journal_lines, "oco_placed", now, _OCO_PLACED_TTL_S),
        amend_recently_failed=_fold_ttl_markers(
            journal_lines, "amend_failed", now, _AMEND_FAILED_TTL_S
        ),
        reanchored_by_uic=_fold_reanchored_markers(journal_lines),
        # Task 2 trailing ratchet floor: uic -> the last CONFIRMED trailed level.
        # ``peak_by_uic`` / ``last_price_by_uic`` default empty here (a LATER task
        # injects real feed values through a new param) so the trailing arm stays
        # dark until the peak feed is wired.
        trailed_stop_by_uic=_fold_trailed_markers(journal_lines),
        # The startup wiring (build_default_deps) binds the real cached policy via
        # functools.partial; the None default only guards direct test calls, where
        # the inert setup_static policy keeps the view byte-identical to today.
        exit_policy=exit_policy if exit_policy is not None else SetupStaticPolicy(),
    )


# Alert-throttle tuning: a re-alert interval so a stuck position does not page
# every tick, and a per-uic consecutive-failure escalation so N repeated
# stop-place failures raise ONE CRITICAL then back off (never a Telegram 429
# storm that drowns the next genuine naked alert). saxo-oco memo §5.
_ALERT_REPEAT_INTERVAL_S = 1800.0  # 30 min
_MAX_CONSECUTIVE_PLACE_FAILURES = 3


class _AlertThrottle:
    """Dedup protection alerts by ``(uic, reason)`` within a re-alert interval and
    escalate then back off a uic whose stop keeps failing to place (saxo-oco memo §5)."""

    def __init__(
        self,
        base_alert: Callable[[str], None],
        *,
        clock: Callable[[], float] = time.time,
        interval_s: float = _ALERT_REPEAT_INTERVAL_S,
    ) -> None:
        self._base = base_alert
        self._clock = clock
        self._interval = interval_s
        self._last_sent: dict[tuple[int | None, str], float] = {}
        self._fail_counts: dict[int, int] = {}
        self._escalated: set[int] = set()

    def emit(self, message: str, *, uic: int | None = None, reason: str | None = None) -> bool:
        """Send ``message`` unless an identical ``(uic, reason)`` alert fired
        within the interval. ``reason`` defaults to the message text. Returns
        True iff it was actually sent."""
        key = (uic, reason if reason is not None else message)
        now = self._clock()
        last = self._last_sent.get(key)
        if last is not None and (now - last) < self._interval:
            return False
        self._last_sent[key] = now
        self._base(message)
        return True

    def record_place_failure(self, uic: int, message: str) -> bool:
        """Count one consecutive stop-place failure on ``uic``; below the
        threshold emit a throttled routine alert, AT the threshold emit ONE
        CRITICAL escalation, above it back off silently. Returns True iff an
        alert was sent."""
        count = self._fail_counts.get(uic, 0) + 1
        self._fail_counts[uic] = count
        if count >= _MAX_CONSECUTIVE_PLACE_FAILURES:
            if uic in self._escalated:
                return False  # already escalated -> back off
            self._escalated.add(uic)
            self._base(
                f"CRITICAL uic {uic}: NAKED — {count} consecutive stop-place "
                "failures, manual action required"
            )
            return True
        return self.emit(message, uic=uic, reason="place-failure")

    def record_place_success(self, uic: int) -> None:
        """Clear the consecutive-failure state once a stop places on ``uic``."""
        self._fail_counts.pop(uic, None)
        self._escalated.discard(uic)


def _emit_alert(
    throttle: _AlertThrottle,
    report: TickReport,
    message: str,
    *,
    uic: int | None = None,
    reason: str | None = None,
) -> None:
    """Emit a throttled protection alert and count it in ``report`` iff it was
    actually sent (a dedup-suppressed repeat is not counted). Folds the ubiquitous
    ``if throttle.emit(...): report.alerts += 1`` idiom into one call."""
    if throttle.emit(message, uic=uic, reason=reason):
        report.alerts += 1


def _journal_outcome_best_effort(
    append: Callable[[], None],
    throttle: _AlertThrottle,
    report: TickReport,
    *,
    uic: int,
    kind: str,
) -> None:
    """Best-effort append of an observability-only outcome record (``stop_placed``
    / ``amend_ok``). The journal write is fallible I/O (``mkdir``/``open``/``fsync``
    raise ``OSError`` on disk full, ENOSPC, permission) and an ``OSError`` is NOT a
    ``BrokerError``, so unhandled it would blow through the per-action boundary in
    ``_run_protection_pass`` and abort the tick mid-protection. An outcome record
    is read by nothing in the protection logic, so its failure must never change
    protection behavior — swallow to a throttled alert. The catch is deliberately
    ``Exception``, not just ``OSError``: the containment intent is "the journal can
    NEVER abort protection", and a future append bug (a non-JSON-serializable field
    -> ``TypeError``) is exactly as non-Broker as ENOSPC. ``BaseException``
    (KeyboardInterrupt / SystemExit) still propagates."""
    try:
        append()
    except Exception as exc:
        _emit_alert(
            throttle,
            report,
            f"uic {uic}: {kind} outcome journal write failed — {exc}",
            uic=uic,
            reason="outcome-journal-io",
        )


# Message tokens that mean "the order is already gone" — an idempotent cancel of
# an already-cancelled / cascade-removed sibling must be a success, not a raise
# (saxo-oco memo §5). Cancel carries no structured code, so classify on the
# message (the one place string-matching is accepted, per the memo).
_ALREADY_GONE_TOKENS = (
    "404",
    "not found",
    "ordernotfound",
    "unknownorder",
    "already cancelled",
    "already canceled",
    "does not exist",
    "no open order",
    "no such order",
)


def _is_already_gone(exc: BrokerError) -> bool:
    text = str(exc).lower()
    return any(token in text for token in _ALREADY_GONE_TOKENS)


def _idempotent_cancel(broker: Broker, order_id: str) -> None:
    """Cancel ``order_id``, treating an already-gone order as success so a
    cascade-cancelled OCO sibling (or a manual pre-cancel) never thrashes."""
    try:
        broker.cancel_order(order_id)
    except BrokerError as exc:
        if _is_already_gone(exc):
            return
        raise


def _execute_cancel_sell_legs(
    broker: Broker, throttle: _AlertThrottle, action: CancelSellLegs, report: TickReport
) -> None:
    """Idempotently cancel a ``CancelSellLegs`` action's order ids (orphan sweep /
    over-hedge repair). A genuine transient failure on ONE leg must not strand the
    rest uncancelled — isolate it, alert, and continue the loop; a summary alert
    fires at the end."""
    for order_id in action.order_ids:
        try:
            _idempotent_cancel(broker, order_id)
            report.cancels += 1
        except BrokerError as exc:
            if throttle.emit(
                f"uic {action.uic}: failed to cancel {order_id}: {exc}",
                uic=action.uic,
                reason=f"cancel-fail:{action.uic}",
            ):
                report.alerts += 1
    if throttle.emit(action.reason, uic=action.uic, reason=f"cancel:{action.uic}"):
        report.alerts += 1


def _make_protection_executor(
    broker: Broker,
    throttle: _AlertThrottle,
    *,
    place_oco_exit: OcoPlacer | None = None,
    amend_stop: AmendStopPlacer | None = None,
) -> Callable[[Action, bool, TickReport], None]:
    """The protection-pass executor (saxo-oco memo §6 + Stage 3). Per Action:

    - ``NoOp`` — nothing.
    - ``AlertOnly`` — a throttled alert.
    - ``CancelSellLegs`` — idempotent cancels (orphan sweep / over-hedge repair).
    - ``PlaceStop`` — cancel any ``cancel_conflicting`` lone TP FIRST, re-read
      owned at execute time (never oversell, never plant on a flat uic), place
      the guaranteed standalone stop (ALLOWED under KILL — it only reduces
      exposure), then cancel ``supersede_ids`` AFTER the place confirms. A
      ``SellOrdersAlreadyExist`` rejection defers to next tick; any other place
      failure is counted for escalation and retried next tick (protection is
      broker-state truth, so nothing is recorded on failure -> Bug A cannot recur).
    - ``UpgradeToOco`` — B0 OCO-direct-on-fill (saxo Stage-3 memo). A truly naked
      fresh fill goes straight to a resting OCO pair. Under KILL / no OCO
      capability / OCO disabled it instead covers the naked fill with a plain
      standalone stop (never left naked). A three-way FAILURE TAXONOMY: a benign
      ``SellOrdersAlreadyExist`` defers (an OCO already rests); a CLEAN structural
      reject covers the fill with a fallback stop + marks ``oco_unsupported``; an
      AMBIGUOUS write places NO inline fallback (it may have landed -> would
      double-commit) and reconciles next tick.
    - ``AmendStop`` — a Stage-3 in-place PATCH resize of a single clean standalone
      stop to LIVE owned (both directions). NO cancel; ALLOWED under KILL. On any
      failure it journals ``amend_failed`` (TTL fold) so the next tick falls to the
      proven B1 additive / place-first primitive — no permanent latch.

    ``place_oco_exit`` / ``amend_stop`` are the SupportsOcoExit / SupportsAmendStop
    capabilities (or None when the broker lacks them), injected here so the
    pre-built executor closure can reach them."""

    def _execute(action: Action, kill: bool, report: TickReport) -> None:
        if isinstance(action, NoOp):
            return
        if isinstance(action, AlertOnly):
            if throttle.emit(action.reason):
                report.alerts += 1
            return
        if isinstance(action, CancelSellLegs):
            _execute_cancel_sell_legs(broker, throttle, action, report)
            return
        if isinstance(action, PlaceStop):
            _execute_place_stop(broker, throttle, action, report)
            return
        if isinstance(action, UpgradeToOco):
            _execute_upgrade_to_oco(broker, throttle, place_oco_exit, action, kill, report)
            return
        if isinstance(action, AmendStop):
            _execute_amend_stop(broker, throttle, amend_stop, action, report)

    return _execute


def _execute_place_fallback_stop(
    broker: Broker, throttle: _AlertThrottle, action: UpgradeToOco, report: TickReport
) -> None:
    """Cover a B0 naked fill with a PLAIN standalone stop (no TP), reusing the full
    ``PlaceStop`` executor path (execute-time owned re-read + clamp + flat-skip +
    SellOrdersAlreadyExist defer + escalation). Used when OCO is off / KILL / no
    capability, and after a CLEAN OCO reject — never a naked window. The stop ref is
    the standalone ``-stop-`` namespace derived from the same entry_crid + gen."""
    _execute_place_stop(
        broker,
        throttle,
        PlaceStop(
            action.uic,
            action.side,
            action.qty,
            action.stop_price,
            _exit_stop_ref(action.entry_crid, action.gen),
        ),
        report,
    )


def _execute_upgrade_to_oco(
    broker: Broker,
    throttle: _AlertThrottle,
    place_oco_exit: OcoPlacer | None,
    action: UpgradeToOco,
    kill: bool,
    report: TickReport,
) -> None:
    """Execute a B0 OCO-direct-on-fill (saxo Stage-3 memo). The action is a TRULY
    NAKED fresh fill (the pure arm emits it only on ``not legs``), so the fill MUST
    end this tick either behind a resting OCO pair or a plain standalone stop —
    never left naked.

    When OCO is disabled / the broker has no OCO capability / under KILL, cover the
    naked fill with a plain standalone stop (no TP churn, KILL-safe). Otherwise
    place the OCO pair with a three-way FAILURE TAXONOMY (mitigation H1/A2/H4):
      - benign ``SellOrdersAlreadyExist`` -> an OCO already rests from a prior
        tick's landed write; NO fallback (would double-commit), NO degrade, defer;
      - a CLEAN structural reject (provably NOT landed) -> mark ``oco_unsupported``
        and cover the naked fill NOW with a plain standalone stop;
      - an AMBIGUOUS write (5xx / network-after-send / rate-limit) -> it MAY have
        landed; NO inline fallback (would double-commit), NO ``oco_placed`` marker,
        CRITICAL alert, reconcile against live broker state next tick.
    On success: count the exit, journal an ``oco_placed`` marker (suppresses a B0
    re-fire while list-orders lags), then run the (empty for B0) supersede loop."""
    if not _oco_enabled() or place_oco_exit is None or kill:
        # OCO off / no capability / KILL: the fill is naked, so cover it NOW with a
        # plain standalone stop (a new OCO would be order churn under KILL).
        _execute_place_fallback_stop(broker, throttle, action, report)
        return

    # Execute-time owned re-check (mirror _execute_place_stop): never place on a
    # uic that shrank / closed between the snapshot and now.
    qty = action.qty
    get_by_uic = getattr(broker, "get_positions_by_uic", None)
    if get_by_uic is not None:
        live = get_by_uic(action.uic)
        if live.quantity + _QTY_EPS < qty:
            qty = max(live.quantity, 0.0)
    if qty <= _QTY_EPS:
        _emit_alert(
            throttle,
            report,
            f"uic {action.uic}: position gone before OCO placement — skipped",
            uic=action.uic,
            reason="flat-skip",
        )
        return

    request_id = _exit_oco_ref(action.entry_crid, action.gen)
    try:
        place_oco_exit(
            action.uic,
            action.side,
            qty,
            action.stop_price,
            action.tp_price,
            request_id,
            None,  # position_id: reduce-only linkage refuted (Stage 3, Q3); unused
        )
    except BrokerCapabilityError as exc:
        # PROVABLY UNSENT: placement is structurally disabled (ALLOW_ORDERS off or a
        # missing capability) — nothing reached Saxo. This is NEITHER an ambiguous
        # write (no CRITICAL, and a fallback stop is equally gated so it would fail
        # too) NOR a clean structural reject (do NOT mark oco_unsupported — a
        # transient env gate is not an instrument incapability). Throttled alert;
        # reconcile against live broker state next tick (the gate self-clears).
        _emit_alert(
            throttle,
            report,
            f"uic {action.uic}: order placement disabled — OCO not sent ({exc})",
            uic=action.uic,
            reason="orders-disabled",
        )
        return
    except OrderRejectedError as exc:
        if _is_sell_orders_already_exist(exc):
            # BENIGN: an OCO already rests from a prior tick's landed write that
            # live list-orders had not yet surfaced. NO fallback (a stop atop the
            # resting OCO pair = 2x owned), NO degrade, NO marker — just defer.
            _emit_alert(
                throttle,
                report,
                f"uic {action.uic}: OCO already rests (sell-commit held) — deferring",
                uic=action.uic,
                reason="oco-already",
            )
            return
        if _is_too_far_from_market(exc):
            # TRANSIENT price-dependent reject (VRNS incident 2026-07-29): the
            # exit distance vs the CURRENT market failed, not the instrument's
            # OCO capability. Journal the TTL marker, NOT the permanent flag,
            # so fresh fills re-qualify for OCO once the open settles.
            _journal_oco_too_far(action.uic)
            degrade_note = f"degraded stop-only for {_OCO_TOO_FAR_TTL_S:.0f}s (transient)"
        else:
            # CLEAN structural reject (provably NOT landed): degrade the uic
            # permanently so B0 is not re-attempted on it.
            _mark_oco_unsupported(action.uic)
            degrade_note = "degraded stop-only"
        # Either way, cover the naked fill NOW with a plain stop (never-naked first).
        _execute_place_fallback_stop(broker, throttle, action, report)
        _emit_alert(
            throttle,
            report,
            f"uic {action.uic}: OCO rejected ({exc}); placed fallback stop, {degrade_note}",
            uic=action.uic,
            reason="oco-degrade",
        )
        return
    except BrokerError as exc:
        # AMBIGUOUS/maybe-sent: the OCO MAY have landed. NO inline fallback (would
        # double-commit if it did), NO oco_placed marker (so next tick re-evaluates
        # against live broker state). Escalate loudly; the residual naked window is
        # bounded to <=1 poll interval and self-heals on reconcile.
        _emit_alert(
            throttle,
            report,
            f"CRITICAL uic {action.uic}: OCO placement ambiguous ({exc}) — "
            "no fallback, reconciling next tick",
            uic=action.uic,
            reason="oco-ambiguous",
        )
        return

    # SUCCESS: a resting OCO pair now covers the position. Journal the marker so a
    # list-orders-lagged next tick does not re-fire B0 and double-commit.
    report.exits_placed += 1
    _journal_oco_placed(action.uic)
    for order_id in action.supersede_ids:  # always () for B0 — no-op
        _idempotent_cancel(broker, order_id)
        report.cancels += 1


def _execute_amend_stop(
    broker: Broker,
    throttle: _AlertThrottle,
    amend_stop: AmendStopPlacer | None,
    action: AmendStop,
    report: TickReport,
) -> None:
    """Execute a Stage-3 ``AmendStop`` PATCH resize (saxo Stage-3 memo). NO cancel
    anywhere; ALLOWED under KILL (an in-place resize of a protective stop only
    reduces exposure or enlarges cover — it never adds a TP or market exposure, so
    no kill gate).

    ABSOLUTE-target (mitigation verdict-2 clamp): re-read LIVE owned and amend to
    it in BOTH directions (a position that grew between snapshot and execute is
    covered up to live owned, never stranded naked; one that shrank is never
    oversold). On ANY amend failure, journal ``amend_failed`` (folded into
    ``amend_recently_failed`` for one TTL) so the NEXT tick's grow/downsize arm
    SKIPS amend and the delta is covered by the proven B1 additive / place-first
    primitive, and escalate via ``record_place_failure`` — NO permanent capability
    latch (a benign fill-race 400 self-clears after the TTL and amend retries)."""
    if amend_stop is None:
        return  # broker lacks SupportsAmendStop -> the pure arm never emits this

    target = action.target_qty
    get_by_uic = getattr(broker, "get_positions_by_uic", None)
    if get_by_uic is not None:
        target = max(get_by_uic(action.uic).quantity, 0.0)
    if target <= _QTY_EPS:
        _emit_alert(
            throttle,
            report,
            f"uic {action.uic}: position gone before amend — skipped",
            uic=action.uic,
            reason="flat-skip",
        )
        return

    # Execute-time OCO-leg / standalone fill re-check (Q10 mid-fill TOCTOU): the
    # SPECIFIC resting stop being amended may have partially filled OR vanished
    # (gone / fully filled) between the decision snapshot and this PATCH landing.
    # Saxo's partial-fill amend semantics are UNPROVEN (Q10), so amending a leg
    # that already began filling is unsafe. Bail leg-shape-agnostically (covers a
    # standalone stop AND an OCO child stop): journal ``amend_failed`` (TTL fold)
    # and a throttled alert so the NEXT tick falls to the proven B1 additive /
    # place-residual-first primitive (never naked). Defensive getattr: a broker
    # without ``list_working_sell_orders`` keeps the prior behavior (the amend
    # capability implies Saxo, which has it).
    list_sells = getattr(broker, "list_working_sell_orders", None)
    if list_sells is not None:
        resting = next((o for o in list_sells() if str(o.order_id) == str(action.order_id)), None)
        if resting is None or resting.filled_quantity > _QTY_EPS:
            _journal_amend_failed(action.uic)
            _emit_alert(
                throttle,
                report,
                f"uic {action.uic}: stop {action.order_id} gone/partially-filled "
                "before amend — skipped, residual covered next tick",
                uic=action.uic,
                reason="amend-skip-filled",
            )
            return

    try:
        amend_stop(
            action.uic,
            action.order_id,
            action.side,
            action.order_type,
            target,
            action.stop_price,
            action.request_id,
        )
    except BrokerCapabilityError as exc:
        # PROVABLY UNSENT (orders disabled / no capability): NOT an amend rejection.
        # Do NOT journal amend_failed (it would needlessly skip amend next tick) and
        # do NOT escalate as a place-failure — a throttled alert; the env gate
        # self-clears and the amend retries next tick.
        _emit_alert(
            throttle,
            report,
            f"uic {action.uic}: order placement disabled — amend not sent ({exc})",
            uic=action.uic,
            reason="orders-disabled",
        )
        return
    except BrokerError as exc:
        # ANY OTHER failure (clean reject, ambiguous 5xx/network): journal
        # amend_failed (TTL fold -> next tick skips amend, B1 additive / place-first
        # covers the delta) and escalate. record_place_failure gives the naked-
        # position escalation without a permanent latch.
        _journal_amend_failed(action.uic)
        throttle.record_place_failure(action.uic, f"uic {action.uic}: stop amend failed — {exc}")
        return

    report.exits_placed += 1
    throttle.record_place_success(action.uic)
    # Outcome record with the qty the stop was amended to (live-clamped target).
    _journal_outcome_best_effort(
        lambda: _journal_amend_ok(action.uic, target),
        throttle,
        report,
        uic=action.uic,
        kind="amend_ok",
    )
    # PR-6b: latch the reanchor ONLY on this confirmed success — a failed
    # amend never reaches here (it returned above on the BrokerError branch,
    # having journaled amend_failed like any other amend arm). The marker write
    # is best-effort (like amend_ok): if it is dropped, the NEXT tick simply
    # re-emits the SAME reanchor (absolute target price + qty, idempotent-in-
    # effect) and re-journals — at worst one redundant, harmless PATCH, never a
    # wrong or naked stop. No in-memory secondary latch is warranted for that.
    if action.reanchor_avg_price is not None:
        reanchor_avg_price = action.reanchor_avg_price
        _journal_outcome_best_effort(
            lambda: _journal_reanchored(action.uic, reanchor_avg_price),
            throttle,
            report,
            uic=action.uic,
            kind="reanchored",
        )


def _execute_place_stop(
    broker: Broker, throttle: _AlertThrottle, action: PlaceStop, report: TickReport
) -> None:
    # A lone TP holds the conflicting sell commitment (Bug B) -> clear it BEFORE
    # the place so the standalone stop is the only sell on the uic.
    for order_id in action.cancel_conflicting:
        _idempotent_cancel(broker, order_id)
        report.cancels += 1

    # Execute-time owned re-check: never oversell, never plant a stop on a uic
    # that closed between the snapshot and now (it could later fire into a short).
    qty = action.qty
    get_by_uic = getattr(broker, "get_positions_by_uic", None)
    if get_by_uic is not None:
        live = get_by_uic(action.uic)
        if live.quantity + _QTY_EPS < qty:
            qty = max(live.quantity, 0.0)
    if qty <= _QTY_EPS:
        if throttle.emit(
            f"uic {action.uic}: position gone before stop place — skipped",
            uic=action.uic,
            reason="flat-skip",
        ):
            report.alerts += 1
        return

    # KILL allows a protective stop (it only REDUCES exposure) — no kill gate here.
    # build_default_deps gates isinstance(broker, SupportsStandaloneStop), so the
    # standalone-stop capability is guaranteed present at runtime.
    stop_broker = cast(SupportsStandaloneStop, broker)
    try:
        stop_broker.place_standalone_stop(
            action.uic, action.side, qty, action.stop_price, action.request_id
        )
    except OrderRejectedError as exc:
        if _is_sell_orders_already_exist(exc):
            if throttle.emit(
                f"uic {action.uic}: stop deferred — sell-commit not yet released",
                uic=action.uic,
                reason="defer",
            ):
                report.alerts += 1
            return  # retry next tick; broker-state truth means no false "protected"
        throttle.record_place_failure(
            action.uic, f"uic {action.uic}: stop placement rejected — {exc}"
        )
        return
    except BrokerError as exc:
        throttle.record_place_failure(
            action.uic, f"uic {action.uic}: stop placement failed — {exc}"
        )
        return

    report.exits_placed += 1
    throttle.record_place_success(action.uic)
    # Cancel the OLD / stale / smaller stop only AFTER the new one is confirmed —
    # never a naked window on the shares that were already covered. The
    # confirmed-place -> supersede-cancel adjacency is safety-critical (a break
    # between them leaves TWO live sell stops on the same shares, the double-sell
    # class killed in #878), so nothing fallible may sit between them.
    for order_id in action.supersede_ids:
        _idempotent_cancel(broker, order_id)
        report.cancels += 1
    # Outcome record with the qty ACTUALLY placed (post-clamp), never action.qty.
    # AFTER the supersede cancels: the record is observability-only (read by
    # nothing), so its ordering is irrelevant — its failure mode is not. Flip
    # side: a cancel that raises skips the record, so a MISSING stop_placed
    # never implies a naked position — the place above already succeeded.
    _journal_outcome_best_effort(
        lambda: _journal_stop_placed(action.uic, qty),
        throttle,
        report,
        uic=action.uic,
        kind="stop_placed",
    )


__all__ = [
    "HEARTBEAT_METRIC",
    "KILL_ACTIVE_METRIC",
    "KILL_FILE_PATH",
    "LoopDeps",
    "TickReport",
    "build_default_deps",
    "run_daemon",
    "run_once",
]
