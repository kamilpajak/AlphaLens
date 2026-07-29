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

import logging
import math
import os
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

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
    UpgradeToOco,
    _amend_enabled,
    _exit_oco_ref,
    _exit_stop_ref,
    _oco_enabled,
    advance,
    reconcile_protection,
)
from alphalens_pipeline.brokers.contract import (
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

if TYPE_CHECKING:
    import threading

    from alphalens_pipeline.brokers.automanager.streaming_trigger import StreamTrigger
    from alphalens_pipeline.brokers.contract import Broker
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


def _pick_key(pick: Any) -> tuple[str, str]:
    """The (ticker, brief_date) join key for one armed pick."""
    return (str(pick.ticker).upper(), pick.date.isoformat())


def _default_emit_heartbeat() -> None:
    """Write the per-tick Prometheus heartbeat gauge. A Type=simple daemon rarely
    triggers ExecStopPost, so the emit-job-metrics last_success clock is the
    wrong health signal — this gauge (watched by AlphalensBrokerManagerHeartbeatStale)
    is. Best-effort: a textfile-dir hiccup must never crash the loop."""
    from alphalens_pipeline.observability.textfile import emit_domain_metrics

    try:
        emit_domain_metrics("broker-manager", {HEARTBEAT_METRIC: int(time.time())})
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
    _run_protection_pass(deps, records, kill, report)
    return report


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
    heartbeat_fn: Callable[[], None] = _default_emit_heartbeat,
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
        heartbeat_fn()  # Task 13: writes the Prometheus heartbeat gauge
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


def build_default_deps() -> LoopDeps:
    """Wire the real Task 1-10 seams. Imported lazily so the alphalens binary's
    startup budget stays off this path (lazy-CLI doctrine); covered by the
    SAXO_LIVE_TEST=1 SIM probe, not the hermetic unit tests. The factory helpers
    (_default_oauth_provider, _make_place_pick, _make_position_view_builder,
    build_protection_view + _make_protection_executor) compose the seams; they
    are validated only by the SIM probe. The pluggable fill-source
    (fill_source.PollingFillSource) stays a tested seam for the phase-B streaming
    drop-in; the MVP loop detects fills through reconcile_bridge.verdicts
    (reconcile classifies FILLED), so no PollingFillSource instance is wired
    into LoopDeps here."""
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
    # ONE OAuth provider instance is shared by the SessionKeeper AND the streaming
    # reader, so there is a single OAuth chain / one flock owner and the reader can
    # re-authorize in place off the same bearer the main loop pushes.
    provider = _default_oauth_provider()
    keeper = session_keeper.SessionKeeper(provider)

    def _read_records() -> list[Mapping[str, Any]]:
        return list(iter_submission_records(DEFAULT_SUBMISSIONS_PATH))

    # One throttle instance lives for the daemon's lifetime so the re-alert
    # interval + per-uic failure escalation persist across ticks; it wraps the
    # same base sink the generic (un-throttled) tick alerts use.
    base_alert = _default_alert()
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
        place_pick=_make_place_pick(broker),
        read_records=_read_records,
        verdicts_fn=reconcile_bridge.verdicts,
        build_position_view=_make_position_view_builder(broker),
        build_protection_view=build_protection_view,
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

_DEFAULT_BRIEFS_DIR = _ALPHALENS_HOME / "thematic_briefs"
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
) -> dict[str, Any]:
    """One append-only `planned` journal line — the plan PRICES the broker cannot
    know (disaster stop + in-band TP), keyed to the entry client_request_id and
    its ORIGINAL tier_index, plus the resize `gen`. `_fold_planned_exits` (Task 4)
    reads these back per-uic into PlannedExit; NO line here confers protection —
    protection is derived from live broker state only (design memo §7)."""
    return {
        "kind": "planned",
        "client_request_id": entry_crid,
        "uic": int(uic),
        "side": side,
        "stop_price": float(stop_price),
        "take_profit": None if take_profit is None else float(take_profit),
        "tier_index": int(tier_index),
        "gen": int(gen),
    }


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
        )
    return result


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


def _default_oauth_provider() -> Any:
    """Return the shipped OAuthTokenProvider wired from the Saxo env vars."""
    from alphalens_pipeline.brokers.saxo.tokens import OAuthTokenProvider

    return OAuthTokenProvider.from_env()


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


def _default_alert() -> Callable[[str], None]:
    """Env-driven Telegram alert sink over the canonical TelegramClient
    (TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID), journald-mirrored via
    _journaled_alert. send_message never raises, so a delivery blip cannot crash
    a tick. SIM-probe-only.

    Operational alert bodies carry raw request-id reprs / reasons with `_`, `*`,
    `[` — under the client's default parse_mode="Markdown" those trip a Telegram
    400 and the alert is SILENTLY dropped (defeating the safety-alert path). Send
    plain: parse_mode="" disables entity parsing so the body goes through
    verbatim."""
    import os

    from alphalens_pipeline.data.alt_data.telegram_client import TelegramClient

    client = TelegramClient(os.environ["TELEGRAM_BOT_TOKEN"])
    chat_id = os.environ["TELEGRAM_CHAT_ID"]

    def _send(message: str) -> None:
        client.send_message(chat_id, message, parse_mode="")

    return _journaled_alert(_send)


def _make_place_pick(broker: Broker) -> Callable[[Any], bool]:
    """Compose safety.check -> placement_planner.classify -> placer loop over
    place_bracket_order + the submissions journal for one armed pick, plus the
    "planned" half of the out-of-band standalone-stop journal (the entry's
    plan-level disaster stop, correlated by client_request_id for
    _make_position_view_builder to fold back later). A safety refusal or a
    resolve/size/placement failure logs and returns False rather than raising —
    one bad pick must never crash a tick."""

    def _place(pick: Any) -> bool:
        return _place_pick(broker, pick)

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
    broker: Broker, ticker: str, account: Any, trade_setup: Any
) -> tuple[Any, Any, Any] | None:
    """Resolve the US instrument, build any needed FX conversion, and size the
    setup plan. Returns ``(instrument, fx, plan)`` or ``None`` on any resolve/size
    failure (logged) — one bad pick must never crash a tick."""
    from alphalens_pipeline.brokers.contract import BrokerError
    from alphalens_pipeline.brokers.execution import build_fx_conversion
    from alphalens_pipeline.brokers.routing import resolve_us_instrument
    from alphalens_pipeline.paper.sizing import TradeSetupNotPlannableError, compute_setup_plan

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
            brief_trade_setup=trade_setup,
            paper_equity=account.total_value,
            scale_factor=1.0,
            fx=fx,
        )
    except (BrokerError, TradeSetupNotPlannableError) as exc:
        logger.warning("place_pick %s: resolve/size failed: %s", ticker, exc)
        return None
    return instrument, fx, plan


def _place_tiers(
    broker: Broker, pick: Any, ticker: str, instrument: Any, account: Any, fx: Any, placement: Any
) -> int:
    """Place each entry tier's bracket, journaling IMMEDIATELY after each fill so a
    mid-loop crash leaves at most a partial ladder joined to submissions.jsonl (the
    drain then never re-places the full set on restart). Returns the count actually
    placed; a BrokerError stops the loop and writes a note-only trace record so the
    failure is auditable and an all-fail pick is not retried forever."""
    from alphalens_pipeline.brokers.contract import BrokerError
    from alphalens_pipeline.brokers.submission_log import (
        append_submission_record,
        build_submission_record,
    )

    def _journal_tier(tier: Any, placed: Any) -> None:
        bracket = tier.bracket
        append_submission_record(
            build_submission_record(
                brief_date=pick.date.isoformat(),
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
        _append_standalone_stop_journal(
            _build_planned_line(
                entry_crid=bracket.client_request_id,
                uic=int(instrument.broker_instrument_id),
                side=_DISASTER_STOP_SIDE,
                stop_price=placement.disaster_stop_price,
                take_profit=tier.tp,
                tier_index=tier.tier_index,
            )
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
                brief_date=pick.date.isoformat(),
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


def _place_pick(broker: Broker, pick: Any) -> bool:
    """Place one armed pick end-to-end (see _make_place_pick). Module-level so the
    per-phase helpers keep the tick logic flat; every failure path logs and returns
    False rather than raising."""
    import datetime as _dt

    from alphalens_pipeline.brokers.automanager import safety
    from alphalens_pipeline.brokers.automanager.placement_planner import classify
    from alphalens_pipeline.brokers.automanager.reconcile_bridge import (
        verdicts as reconcile_verdicts,
    )
    from alphalens_pipeline.brokers.contract import BrokerError
    from alphalens_pipeline.brokers.submission_log import (
        DEFAULT_SUBMISSIONS_PATH,
        iter_submission_records,
    )
    from alphalens_pipeline.paper.brief_loader import load_brief

    ticker = pick.ticker.upper()
    try:
        candidates = load_brief(pick.date, _DEFAULT_BRIEFS_DIR)
    except (FileNotFoundError, ValueError) as exc:
        logger.warning("place_pick %s: brief unavailable for %s: %s", ticker, pick.date, exc)
        return False
    candidate = next((c for c in candidates if c.ticker.upper() == ticker), None)
    if candidate is None or candidate.trade_setup is None:
        logger.warning("place_pick %s: no plannable trade_setup in the %s brief", ticker, pick.date)
        return False

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
        pick,
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
        return False

    # Earnings-window gate (overnight-drift memo 2026-07-29, exposure J): never
    # rest a NEW entry across a known earnings date. Runs after the safety rails
    # (cheap, local) and before resolve/size (network). Fail-open inside the
    # gate; the armed pick stays queued, so it self-heals once the date passes.
    from alphalens_pipeline.brokers.automanager.earnings_gate import earnings_window_refusal
    from alphalens_pipeline.paper.constants import DEFAULT_ORDER_TTL_DAYS

    setup = candidate.trade_setup
    raw_ttl = setup.get("order_ttl_days") if hasattr(setup, "get") else None
    ttl_days = int(raw_ttl or 0) or DEFAULT_ORDER_TTL_DAYS
    earnings_reason = earnings_window_refusal(ticker, ttl_days=ttl_days)
    if earnings_reason:
        logger.warning("place_pick %s: refused — %s", ticker, earnings_reason)
        return False

    resolved = _resolve_and_size(broker, ticker, account, candidate.trade_setup)
    if resolved is None:
        return False
    instrument, fx, plan = resolved

    placement = classify(plan, instrument, side=_ENTRY_SIDE)
    if not placement.tiers:
        logger.warning("place_pick %s: every entry tier sized to zero shares", ticker)
        return False

    return _place_tiers(broker, pick, ticker, instrument, account, fx, placement) > 0


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
        from alphalens_pipeline.brokers.contract import OrderStatus

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
    _journal_amend_ok(action.uic, target)


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
    # Outcome record with the qty ACTUALLY placed (post-clamp), never action.qty.
    _journal_stop_placed(action.uic, qty)
    # Cancel the OLD / stale / smaller stop only AFTER the new one is confirmed —
    # never a naked window on the shares that were already covered.
    for order_id in action.supersede_ids:
        _idempotent_cancel(broker, order_id)
        report.cancels += 1


__all__ = [
    "HEARTBEAT_METRIC",
    "KILL_FILE_PATH",
    "LoopDeps",
    "TickReport",
    "build_default_deps",
    "run_daemon",
    "run_once",
]
