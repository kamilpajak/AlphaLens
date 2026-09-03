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

import datetime as dt
import enum
import functools
import logging
import math
import os
import time
from collections import deque
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, is_dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, NamedTuple, cast

from broker_contract.constants import DEFAULT_ORDER_TTL_DAYS
from broker_contract.contract import (
    _QTY_EPS,
    BrokerCapabilityError,
    BrokerError,
    OrderRejectedError,
    PlacedOrder,
    Position,
    SupportsAmendStop,
    SupportsNettedPositionReads,
    SupportsOcoExit,
    SupportsPriceTickFloor,
    SupportsStandaloneStop,
    SupportsTrailingStop,
    _is_insufficient_funds,
    _is_price_tolerance_reject,
    _is_sell_orders_already_exist,
    _is_too_far_from_market,
)
from broker_contract.exit_geometry import (
    ExitPolicy,
    SetupStaticPolicy,
    resolve_exit_policy,
)
from broker_contract.exit_geometry.registry import resolve_policy
from broker_contract.price_feed import SupportsSessionLow

from alphalens_pipeline.brokers.automanager import (
    entry_trail_geometry,
    entry_trail_watcher,
    entry_trails,
    picks,
    state_paths,
)
from alphalens_pipeline.brokers.automanager.costs import (
    COMMISSION_RATE,
    EXIT_EDGE_MIN_BPS,
    FX_ROUND_TRIP_RATE,
    MIN_COMMISSION_USD,
    US_FEE_CARD,
    apportioned_coverage_violation,
    cost_gate_facts,
    fee_card_for,
    round_trip_fee_bps,
    single_full_position_tranche_violation,
)
from alphalens_pipeline.brokers.automanager.labels import (
    entry_label_from_crid,
    human_label_from_external_reference,
    tp_label_from_tag,
)
from alphalens_pipeline.brokers.automanager.live_exit_engine import (
    LiveExitBroker,
    ManagedExit,
    apportion_tranche_quantities,
    run_live_exits,
)
from alphalens_pipeline.brokers.automanager.position_manager import (
    _OCO_LAG_HOLD_REASON,
    _TERMINAL_NON_FILLED,
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
from alphalens_pipeline.brokers.reconcile import (
    VERDICT_AUDIT_DEFERRED,
    OutcomeAuditBudget,
    SupportsOrderResolution,
    SupportsOutcomeCachePeek,
)
from alphalens_pipeline.data.alt_data.saxo_exchanges import US_MIC_PROBE_ORDER

if TYPE_CHECKING:
    import threading

    from broker_contract.contract import Broker, OrderState
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

# The runtime data root, the broker-orders subtree, and the KILL/journal paths
# below it are NOT defined here any more — every broker-state path funnels
# through the ONE seam (state_paths.py, ADR 0016 / design memo D2) and is
# resolved at call/deps-build time, never as an import-time Path constant.

# Prometheus heartbeat + KILL-active gauge NAMES (Task 13 wires
# _default_emit_heartbeat as the run_daemon default; the metric name has one
# home here). The gauge NAMES are fixed; only the ``{job=...}`` label varies
# per broker instance (ADR 0016 D5), so these are builder functions taking
# the resolved job label (``state_paths.metrics_job()``) rather than
# import-time string constants — a second SIM/LIVE instance on the same host
# must never share a job label.
_HEARTBEAT_METRIC_NAME = "alphalens_broker_manager_last_tick_timestamp_seconds"
_KILL_ACTIVE_METRIC_NAME = "alphalens_broker_manager_kill_active"


def heartbeat_metric(job: str) -> str:
    """The per-tick heartbeat gauge, labeled with the resolved instance job."""
    return f'{_HEARTBEAT_METRIC_NAME}{{job="{job}"}}'


def kill_active_metric(job: str) -> str:
    """The KILL-active gauge (level, 0/1): 1 while the KILL file is present, 0 when
    absent, so Prometheus can alert on an active emergency stop (KILL was journald-only
    before, invisible to monitoring — the heartbeat kept ticking under KILL). It is
    CO-EMITTED with ``heartbeat_metric`` in the SAME ``emit_domain_metrics(job, {...})``
    call: that write atomically OVERWRITES the whole per-instance textfile, so a
    separate call to this domain would clobber the heartbeat gauge and vice-versa."""
    return f'{_KILL_ACTIVE_METRIC_NAME}{{job="{job}"}}'


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

# --- Stream breaker re-arm episode constants (rearm design memo §5) -----------
# All named, all sited here beside _DEFAULT_STREAM_STALE_S; deliberately NO env
# knob (ALPHALENS_BROKER_STREAM_DEBOUNCE_S is a documented knob on this exact
# surface that already rotted dead) — tuning here is a code change with a test.
#
# Cooldown-ladder floor between re-arm trials. Must exceed the probed ~31s
# wall-clock cost of one full in-breaker backoff budget (1+2+4+8+16s sleeps +
# 6 connects — matching the incident's 08:43:26 -> 08:46:21 gap), so a re-arm
# cycle can never spend connects faster than the failing state it replaces. It
# also exceeds both stale_after_s (45s) and the 45s poll grid, guaranteeing at
# most one trial per protective pass.
_STREAM_REARM_FLOOR_S = 60.0
# Ladder ceiling: bounds a long outage at 4 connect attempts/hour. A dark
# stream costs at most one poll period of extra wake latency, never protection,
# so 15 min is the worst-case dark-after-vendor-recovery window (vs the 14h
# observed on 2026-08-22).
_STREAM_REARM_CEILING_S = 900.0
# Delivery-confirmed dwell before (a) the recovery CLOSE page and (b) the
# ladder resets to the floor. 10x recv_timeout_s (30s) ~= 10-15 consecutive SIM
# heartbeats at the documented 20-30s cadence — a deliver-once-then-die flapper
# climbs the ladder instead of looping at the floor. A normal reconnect never
# reaches this code (it never trips), so the dwell cannot affect healthy
# operation.
_STREAM_HEALTHY_DWELL_S = 300.0
# Rolling window for the flap escalation: 4x the ceiling, so a window that saw
# the threshold has necessarily seen the ladder fail to converge.
_STREAM_FLAP_WINDOW_S = 3600.0
# Trips inside the window before ONE CRITICAL page + the OPEN-page latch (the
# CLOSE page is never suppressed). Mirrors _MAX_CONSECUTIVE_PLACE_FAILURES —
# the repo's existing escalate-once threshold on the alert throttle.
_STREAM_FLAP_ESCALATE_AT = 3

# Prometheus liveness gauge: seconds since the last streamed message (age). Watched
# by the AlphalensBrokerStreamStale rule shipped in
# deploy/monitoring/prometheus/rules/alphalens.yaml (repo SoT; the live copy is
# hand-synced — see deploy/systemd/README.md §8.5), distinct from the per-poll
# heartbeat gauge (a dead stream still emits heartbeats — the poll backstop keeps
# running).
_STREAM_LAST_MESSAGE_METRIC_NAME = "alphalens_broker_manager_stream_last_message_age_seconds"

# Stream-state gauge base names (rearm design memo §4.6). All co-emitted with the
# age gauge in ONE atomic emit per tick (_emit_stream_gauge) — the write
# OVERWRITES the whole stream domain textfile, so an omitted key deletes its
# series and a second call to the domain clobbers the first.
_STREAM_READER_UP_METRIC_NAME = "alphalens_broker_manager_stream_reader_up"
# EPISODE-scoped: 1 from the down edge until the delivery-confirmed close. It
# deliberately does NOT flicker per re-arm trial — a per-trial gauge resets to 0
# on every ladder rung, so no Prometheus `for:` longer than one rung could fire.
_STREAM_BREAKER_OPEN_METRIC_NAME = "alphalens_broker_manager_stream_breaker_open"
# A LEVEL for eyeballing streak composition — rate()/increase() on it are
# nonsense. The number the 2026-08-22 incident journal could not recover.
_STREAM_CONSECUTIVE_FAILURES_METRIC_NAME = "alphalens_broker_manager_stream_consecutive_failures"
# Monotonic counter: survives a tick gap; feeds the flapping rule.
_STREAM_TRIPS_TOTAL_METRIC_NAME = "alphalens_broker_manager_stream_trips_total"
# 0/1 trading-window gauge from _make_stream_session_window (memo §3 Q5):
# emitted but referenced by NO shipped rule — making a rule session-aware
# later is a one-line YAML change, not a code change. The trip page itself
# stays unconditional (weekend quiet comes from the episode latch).
_STREAM_IN_SESSION_METRIC_NAME = "alphalens_broker_manager_stream_in_session"


def stream_last_message_metric(job: str) -> str:
    """The stream-liveness gauge, labeled with the SAME job as the heartbeat
    (``state_paths.metrics_job()``) — it is the same daemon instance, only
    written to a distinct domain textfile (see ``_emit_stream_gauge``)."""
    return f'{_STREAM_LAST_MESSAGE_METRIC_NAME}{{job="{job}"}}'


# The stream gauge writes to its OWN domain textfile, NOT the heartbeat's domain
# (``state_paths.stream_metrics_job()``, e.g. "broker-manager-sim-stream"). Both
# emit_domain_metrics(...) writes atomically OVERWRITE alphalens_domain_<domain>.prom,
# and _emit_stream_gauge runs AFTER heartbeat_fn every tick — sharing the heartbeat's
# domain would clobber the heartbeat gauge and break the liveness alert while
# streaming is on. node_exporter merges every *.prom in the dir, so a distinct file
# keeps BOTH series scraped (the metric name + {job} label are the SAME job as the
# heartbeat's — only the containing file differs).

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
    # ``Callable[..., ProtectionView]`` (not a fixed 2-arg signature): the wired
    # partial binds ``exit_policy`` and the trailing path passes ``peak_by_uic`` /
    # ``last_price_by_uic`` keyword args through (Task 4). Non-trailing callers /
    # tests still call it with just ``(broker, records)``.
    build_protection_view: Callable[..., ProtectionView]
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
    # GLOBAL kill (D3, ADR 0016): the legacy parent-level
    # broker_orders/KILL (state_paths.global_kill_file_path()), honored by
    # EVERY instance IN ADDITION to this instance's own kill_file — defense
    # in depth, so the operator's memorized `touch
    # ~/.alphalens/broker_orders/KILL` keeps meaning "stop everything" once a
    # second (LIVE) instance exists. Optional (default None) so a LoopDeps
    # built without it — every pre-ADR-0016 test/harness — behaves exactly as
    # before (the global check is simply skipped); build_default_deps always
    # supplies the real seam path.
    global_kill_file: Path | None = None
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
    # ON). Called as factory(uic_to_instrument, scope=...) where the mapping is
    # uic -> (ticker, exchange_mic) (the venue is load-bearing, see
    # _default_live_exits_feed_factory's docstring) and scope names the
    # caller's slice of the shared price-stream subscription
    # (_FEED_SCOPE_EXITS / _FEED_SCOPE_ENTRY_WATCH) so the tick's multiple
    # feed builds replace only their own uics instead of fighting over one
    # replace-the-whole-set call (the 2026-08-18 subscription-churn incident).
    live_exits_feed_factory: Callable[..., PriceFeed] | None = None
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
    # Day-1 gap gate (execution-quality placement discipline — see the
    # "Day-1 gap gate" section below): ``(ticker, exchange_mic) -> an
    # indicative CURRENT price (bid or last), or None when unavailable``.
    # Baked into the ``place_pick`` closure at build time
    # (``_make_place_pick``'s ``day1_gap_price_probe`` kwarg) — kept here for
    # symmetry / introspection, mirroring ``place_oco_exit`` / ``amend_stop``
    # above. ``None`` (default) is what every pre-day1-gap LoopDeps
    # test/harness gets; the gate itself only runs when
    # ``ALPHALENS_BROKER_DAY1_GAP_GATE=1``, so a ``None`` probe with the flag
    # off is completely inert.
    day1_gap_price_probe: Callable[[str, str], float | None] | None = None
    # #1223 two-consecutive-tick confirmation latch for the FIRED-terminal
    # tranche_plan retraction (``_retract_round_tripped_tranche_plans``):
    # ``(pick_key, uic)`` candidates that passed every gate on the PREVIOUS
    # sweep. A MUTABLE set on the (frozen) deps — built once, carried across
    # ticks, mirroring oco_lag_counts above; a daemon restart starts empty
    # (one extra clean tick, fail-safe). Frozen forbids REBINDING the field,
    # not mutating the set it points at.
    pending_plan_retractions: set[tuple[str, int]] = field(default_factory=set)
    # Entry-trailing watcher runtimes (PR-T1, DRY-RUN): crid -> the daemon-
    # lifetime state for ONE open entry-tier watch (the stateful engine watcher
    # + its measurement marks). A MUTABLE dict on the (frozen-field) deps — built
    # once, carried across ticks — mirroring peak_tracker/oco_lag_counts above.
    # The engine watcher's transient staleness-gap / open-check fields must
    # persist across ticks within a lifetime; on first sight (fresh watch OR
    # post-restart) the runtime is reconstructed from the journal fold so the
    # trough never reseeds upward (memo §5). Empty until a watch opens; with the
    # ENTRY_TRAIL_BPS flag off it stays empty, byte-identical to today.
    entry_watchers: dict[str, _EntryWatchRuntime] = field(default_factory=dict)
    # Shared per-tick cap on audit-log resolution reads (audit-429 memo §3 +
    # Amendment 1): BOTH resolve consumers — the entry-trail reconcile pass and
    # the verdict pass (reconcile_bridge, bound via functools.partial in
    # build_default_deps) — draw from this ONE instance, so their combined
    # cold-start fan-out never exceeds the cap in a tick. A MUTABLE object on
    # the (frozen) deps — built once, carried across ticks, reset by run_once
    # at tick start — mirroring oco_lag_counts / kill_state above. Memoized
    # terminals (SupportsOutcomeCachePeek) resolve budget-free, so steady
    # state is byte-identical to the un-budgeted tick.
    audit_budget: OutcomeAuditBudget = field(default_factory=OutcomeAuditBudget)


@dataclass
class TickReport:
    picks_placed: int = 0
    exits_placed: int = 0  # protective exits placed this tick (rung 0 -> 1 stop, or 1 -> 2 OCO)
    cancels: int = 0
    alerts: int = 0
    orphans: int = 0
    verdict_count: int = 0
    # Brackets whose audit-log resolution was NOT attempted this tick (shared
    # audit budget exhausted — audit-429 memo): retried next tick, no alert.
    audits_deferred: int = 0
    actions: list[tuple[str, str]] = field(default_factory=list)  # (ticker, Action class)


def _always() -> bool:
    return True


def _default_emit_heartbeat(kill: bool = False) -> None:
    """Write the per-tick Prometheus heartbeat + KILL-active gauges. A Type=simple
    daemon rarely triggers ExecStopPost, so the emit-job-metrics last_success clock is
    the wrong health signal — the heartbeat gauge (watched by
    AlphalensBrokerManagerHeartbeatStale) is. The KILL-active gauge co-emits here (1
    when the KILL file is present, 0 when absent) so an emergency stop is visible to
    Prometheus. BOTH gauges MUST go in ONE emit call: the write atomically overwrites
    the whole per-instance textfile, so a separate call would clobber the other
    gauge. The job label (``state_paths.metrics_job()``) is resolved HERE, at call
    time, not at import — so the textfile domain and the ``{job=...}`` label always
    reflect the CURRENT ``$ALPHALENS_BROKER_ENVIRONMENT`` rather than a value frozen
    at process start (ADR 0016 D5). Best-effort: a textfile-dir hiccup must never
    crash the loop."""
    from alphalens_pipeline.observability.textfile import emit_domain_metrics

    job = state_paths.metrics_job()
    try:
        emit_domain_metrics(
            job,
            {heartbeat_metric(job): int(time.time()), kill_active_metric(job): int(kill)},
        )
    except OSError:
        logger.warning("broker-manager heartbeat emit failed", exc_info=True)


def price_reader_client_metrics_job(env: str | None = None) -> str:
    """``"price-reader-client-<env>"`` — this daemon's view of the shared reader.

    A job of its OWN, distinct from the reader's (`price-reader`) and from the
    per-env price-stream jobs: ``emit_domain_metrics`` rewrites a whole per-job
    file, so two emitters sharing a job silently erase each other's series."""
    return f"price-reader-client-{state_paths._resolve_env(env)}"


def _emit_price_reader_client_gauges(remote: Any | None) -> None:
    """Publish whether THIS daemon is actually getting prices from the reader.

    The reader publishes its own liveness, which answers a different question:
    a reader that is up while a daemon cannot reach its socket (a wrong path in
    a drop-in, permissions, a restart race) is invisible from the reader's side
    alone. A daemon on the in-process path has no client to describe and emits
    NOTHING — writing up=0 there would page for a reader it never meant to use.

    Best-effort, like the heartbeat: a textfile-dir hiccup must never reach the
    tick."""
    if remote is None:
        return
    from alphalens_pipeline.observability.textfile import emit_domain_metrics

    job = price_reader_client_metrics_job()
    label = f'{{job="{job}"}}'
    try:
        emit_domain_metrics(
            job,
            {
                f"alphalens_price_reader_client_up{label}": int(bool(remote.is_connected)),
                f"alphalens_price_reader_client_connect_attempts_total{label}": (
                    remote.connect_attempts
                ),
                f"alphalens_price_reader_client_failures_total{label}": remote.failures,
            },
        )
    except OSError:
        logger.warning("price-reader client gauge emit failed", exc_info=True)


# --- Frame gauge: the capital frame this daemon is actually sizing with (#1203)
#
# Under `declared` sizing mode the pin IS the frame, so position size does not
# follow the account. The daily-loss breaker is denominated in that frame, which
# makes one "1R" cost `frame / balance` times what it looks like in real money.
# The direction is the hazard: a loss lowers the balance, raises the ratio, and
# lets the daily stop tolerate a LARGER share of what is left — the rail loosens
# exactly when it should tighten. Specified as critic finding B9 in
# `broker_sizing_declared_frame_design_2026_08_12.md` section 4.6.
#
# OWNERSHIP SPLIT — the daemon publishes the CHEAP half only. The frame is a
# local config read; the balance needs a broker round trip, and `get_account()`
# is THREE HTTP requests, each retrying up to
# ``SaxoClient._MAX_REQUEST_ATTEMPTS`` times on ``_SERVER_ERROR_BACKOFFS`` behind
# the client ``timeout`` (4 / 5-15-30s / 30s today), so one read can block for
# minutes. `run_daemon` calls
# `run_once` BARE, so a blocking observability read would stall the protective
# tick — no reconcile, no exit management, no stop placement — for exactly as
# long, and precisely during a broker outage. Telemetry may observe the control
# path; it must not become a participant in whether that path runs.
#
# The expensive half is therefore collected out-of-process by
# `alphalens broker capital-reader`, on its own timer, into its own domain. The
# alert joins the two (node_exporter merges every *.prom in the directory). This
# keeps the consistency guarantee that motivated in-process emission — the frame
# published is the frame in use, it cannot drift from a second config source —
# without putting its acquisition on the critical path.
_SIZING_PIN_METRIC_NAME = "alphalens_broker_manager_sizing_pin_acct"
_SIZING_MODE_DECLARED_METRIC_NAME = "alphalens_broker_manager_sizing_mode_declared"


def _sizing_pin_and_mode() -> tuple[float | None, bool]:
    """The configured pin and whether the mode is ``declared``, read from env.

    Deliberately NOT via :func:`_resolve_sizing_equity`: that function needs the
    account equity to resolve ``clamped`` mode (``min(pin, snapshot)``), and the
    balance is exactly what this daemon no longer reads. So it reports the raw
    pin plus the mode, and the consumer decides — in ``declared`` mode the pin IS
    the effective frame, which is why the alert conditions on the mode gauge
    instead of assuming it.

    Returns ``(None, ...)`` when the pin is unset or unparseable; the caller then
    emits nothing rather than publishing a frame that does not exist."""
    from alphalens_pipeline.brokers.automanager.live_rails import (
        SIZING_EQUITY_ENV,
        SIZING_EQUITY_MODE_ENV,
        SIZING_MODE_DECLARED,
    )

    declared = (
        os.environ.get(SIZING_EQUITY_MODE_ENV) or ""
    ).strip().lower() == SIZING_MODE_DECLARED
    raw = os.environ.get(SIZING_EQUITY_ENV)
    if raw is None or not raw.strip():
        return None, declared
    try:
        pin = float(raw)
    except ValueError:
        return None, declared
    if not math.isfinite(pin) or pin <= 0.0:
        return None, declared
    return pin, declared


def _emit_frame_gauges() -> None:
    """Publish the sizing pin and the mode. Pure local work — no broker call.

    Its own domain (``state_paths.capital_metrics_job``): ``emit_domain_metrics``
    overwrites a whole per-job file, so sharing the heartbeat's domain would
    erase the liveness gauge every tick. The ``{job=...}`` LABEL stays the
    heartbeat's — same daemon instance, different file.

    Best-effort, like the emitters above: a textfile-dir hiccup must never reach
    the tick."""
    pin, declared = _sizing_pin_and_mode()
    if pin is None:
        return
    from alphalens_pipeline.observability.textfile import emit_domain_metrics

    domain = state_paths.capital_metrics_job()
    label = f'{{job="{state_paths.metrics_job()}"}}'
    try:
        emit_domain_metrics(
            domain,
            {
                f"{_SIZING_PIN_METRIC_NAME}{label}": pin,
                f"{_SIZING_MODE_DECLARED_METRIC_NAME}{label}": int(declared),
            },
        )
    except OSError:
        logger.warning("sizing frame gauge emit failed", exc_info=True)


_BALANCE_METRIC_NAME = "alphalens_broker_manager_account_total_value_acct"
_BALANCE_READ_TS_METRIC_NAME = "alphalens_broker_manager_account_read_timestamp_seconds"


def emit_capital_reader_gauges(broker: Broker, *, now_wall: float | None = None) -> float:
    """Read the account balance ONCE and publish it. The expensive half of #1203.

    Called out-of-process by ``alphalens broker capital-reader`` on its own
    timer, never from the daemon tick — see the ownership-split note above.

    ``total_value`` is ACCOUNT currency, the same denomination as the sizing pin
    (it comes from the account-scoped balances payload, while the pin is
    ``paper_equity``, which ``broker_contract.sizing`` documents as account
    currency). That is what lets the alert divide the two directly; fx enters
    only later, converting a notional to the instrument currency for the share
    count.

    ON FAILURE THIS WRITES NOTHING, deliberately. ``emit_domain_metrics``
    replaces a whole file atomically, so declining to write leaves the previous
    snapshot — balance AND its read timestamp — exactly as it was. The reading
    goes STALE rather than ABSENT, which is what the alert's freshness guard
    detects; writing a zero or dropping the keys would either lie or silently
    disarm the rule. Raises so the unit exits non-zero and systemd records it."""
    account = broker.get_account()
    balance = float(account.total_value)
    if not math.isfinite(balance) or balance <= 0.0:
        raise ValueError(f"account total_value is not a usable balance: {balance!r}")
    from alphalens_pipeline.observability.textfile import emit_domain_metrics

    label = f'{{job="{state_paths.metrics_job()}"}}'
    emit_domain_metrics(
        state_paths.capital_reader_metrics_job(),
        {
            f"{_BALANCE_METRIC_NAME}{label}": balance,
            f"{_BALANCE_READ_TS_METRIC_NAME}{label}": time.time() if now_wall is None else now_wall,
        },
    )
    return balance


def _kill_active(deps: LoopDeps) -> bool:
    """D3 (ADR 0016): True when EITHER the per-instance ``kill_file`` OR the
    GLOBAL kill (when wired) is present — defense in depth. ``global_kill_file``
    is None for any LoopDeps built without it (pre-ADR-0016 tests/harnesses),
    so the OR degrades to exactly the instance-only check.

    This is the SINGLE source of truth for kill-state observability
    (``run_daemon``'s heartbeat gauge, ``InProcessManagerService``'s
    ``LivenessEvent``) as well as placement gating (``run_once``) — reading
    ``deps.kill_file.exists()`` directly at any of those sites would silently
    make a GLOBAL-only KILL invisible there."""
    return deps.kill_file.exists() or (
        deps.global_kill_file is not None and deps.global_kill_file.exists()
    )


def run_once(deps: LoopDeps, *, sweep_orphans: bool = False) -> TickReport:
    """One control-loop tick. Placement is gated on (no KILL) AND (chain alive);
    reconcile + Action execution ALWAYS run so a KILL still cancels and a dead
    chain still surfaces terminal state. The tick is a sequence of independent
    phases (each with its OWN BrokerError boundary in its helper) so one phase
    failing never starves the safety-critical protection pass."""
    report = TickReport()
    # Fresh shared audit-read budget every tick (audit-429 memo §3): both
    # resolve consumers below draw from it; without the reset a cold-start
    # backlog would permanently starve later ticks.
    deps.audit_budget.start_tick()
    kill = _kill_active(deps)
    _alert_kill_transition(deps, kill)
    chain = deps.ensure_alive()
    alive = bool(getattr(chain, "alive", False))
    if not alive:
        deps.alert(
            f"session-keeper: chain dead — {getattr(chain, 'reason', None)}; placement halted"
        )

    if sweep_orphans:
        _run_orphan_sweep(deps, report)

    # PR-T2b fill-reconcile (Finding 1) MUST run BEFORE the placement drain: a
    # native trail that filled appears in the broker positions immediately, but its
    # tier stays NON-terminal in the fold until this pass writes the terminal
    # `fired` line that releases the virtual gross reservation + un-jams watch
    # capacity. If the drain's gross-cap / cash-floor check ran FIRST, the filled
    # tier would be counted TWICE (once as a filled position, once as its still-live
    # virtual reservation) — spuriously breaching the cap and PERMANENTLY refusing
    # (`mark_refused`) another valid pick drained the same tick. Reconciling first
    # releases the reservation so the drain counts it once. UNGATED by KILL (a fill
    # during an emergency stop must still release + is covered by the fire-arm
    # planned disaster line) and a no-op when the flag is unset/0; it writes ONLY
    # terminals — never places — so this call site is unconditional (ahead of the
    # placement gate). Running before the watch pass is safe: a tier armed THIS tick
    # just placed a WORKING order (reconcile no-ops on it), so it only reconciles
    # prior-tick resting orders.
    _run_entry_trail_reconcile_pass(deps, report)

    # #1219 stop-fill reconcile: announce a standalone stop that FILLED at the
    # broker (terminal `stop_filled` line + ONE throttled alert). Placement-order
    # independent (no money-gate fold reads it) but grouped with the entry
    # reconcile on purpose: both are journal-vs-book terminal writers, UNGATED
    # by KILL, and both draw on the same per-tick audit budget started above.
    _run_stop_fill_reconcile_pass(deps, report)
    # #1198 restart-safe backstop: re-derive owed sibling retires from the
    # durable round-trip records (crash between a terminal write and the
    # inline retire self-heals here; idempotent, quiet in steady state).
    _sweep_owed_sibling_retires(deps, report)

    if not kill and alive:
        _run_placement_drain(deps, report)

    # Entry-trailing watcher pass (PR-T1, DRY-RUN): advance each open watch's
    # state machine off the shared INC-2 price stream. KILL-GATED internally
    # (memo §3 G2 — no open-watch/touch/would-fire/journal under KILL) and a
    # no-op when ALPHALENS_BROKER_ENTRY_TRAIL_BPS is unset/0, so this call site
    # is unconditional and the flag alone controls behaviour. Runs every tick
    # regardless of chain-alive (like the exit/protection passes) so time-based
    # expiry + measurement never stall on a dead session. STILL DRY-RUN: the
    # fire path only alerts "would fire" — no broker order is ever placed.
    _run_entry_watch_pass(deps, kill, report)

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
    report.audits_deferred = deps.audit_budget.deferred
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
        # Human line renders the E{n}/TP{n} label off the machine ref (falls back
        # to the order id for an order orphan with no ref); the raw machine repr
        # is kept OUT of the operator line and carried only on a debug logger.
        human = human_label_from_external_reference(orphan.external_reference) or orphan.order_id
        deps.alert(f"orphan (placed but never journaled): {human} [{orphan.kind}]")
        logger.debug("orphan detail: %r", orphan)
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
    already_submitted = picks.submitted_pick_keys(deps.read_records())
    placed_this_tick: set[tuple[str, str]] = set()
    for pick in deps.iter_picks():
        key = picks.pick_key(pick)
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
    _page_now_residuals(verdicts, records, deps.alert_throttled)
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
    # #1249 class (c): a bracket whose verdict proves the entry terminally
    # never filled retracts its ``planned`` line. Runs AFTER the advance loop
    # (advance() stays pure) and BEFORE the protection pass in run_once, so a
    # healed fold governs the same tick.
    _retract_planned_for_verdicts(verdicts)


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
    wrapper around the same append-only journal, not an engine change.

    Identity-keyed reset (2026-08-19 adjudication finding 4): a ``tranche_plan``
    line carrying the SAME ``pick_key`` as the uic's governing plan is an
    idempotent re-append (the ``already_watching`` crash-recovery re-drive
    re-journals the pick's plan every tick until its retirement record lands)
    and must NOT reset — resetting would re-arm already-fired tranches and
    re-sell the remainder at the tranche-0 target. A DIFFERENT ``pick_key``, a
    keyless line (the bracket path — today's always-reset semantics), or a
    ``tranche_plan_retracted`` line (finding 3) still clears the accumulator."""
    fired: dict[int, set[str]] = {}
    governing_key: dict[int, str] = {}
    for line in lines:
        uic = _coerce(line, "uic", int)
        if uic is None:
            continue
        kind = line.get("kind")
        if _apply_generation_reset(kind, line, uic, governing_key, (fired,)):
            continue
        if kind == "tranche_fired":
            tag = line.get("tag")
            if tag:
                fired.setdefault(uic, set()).add(str(tag))
    return {u: frozenset(t) for u, t in fired.items()}


def _apply_generation_reset(
    kind: Any,
    line: Mapping[str, Any],
    uic: int,
    governing_key: dict[int, str],
    accumulators: tuple[dict[int, Any], ...],
) -> bool:
    """The identity-keyed generation reset shared by the fired/trailed folds
    (see ``_fold_fired_since_latest_plan`` for the incident history): a keyless
    ``tranche_plan`` or one with a DIFFERENT ``pick_key`` clears the uic's
    accumulators; a SAME-key re-append does not; ``tranche_plan_retracted``
    always clears. Returns True when the line was a plan/retraction line (the
    caller consumes it and moves on)."""
    if kind == _TRANCHE_PLAN_KIND:
        key = line.get("pick_key")
        if key is None or str(key) != governing_key.get(uic):
            for acc in accumulators:
                acc.pop(uic, None)
        if key is None:
            governing_key.pop(uic, None)
        else:
            governing_key[uic] = str(key)
        return True
    if kind == _TRANCHE_PLAN_RETRACTED_KIND:
        for acc in accumulators:
            acc.pop(uic, None)
        governing_key.pop(uic, None)
        return True
    return False


def _fold_round_trip_closures_since_latest_plan(
    lines: Iterable[Mapping[str, Any]],
) -> dict[int, frozenset[str | None]]:
    """Durable round-trip closure evidence per uic, RESET on each new plan
    generation (#1223).

    The fired-terminal retraction sweep needs POSITIVE journal evidence that a
    fired tier's position lifecycle CONCLUDED before it may consult the
    positions endpoint at all: a positions read alone can report flat while a
    fresh fill has not materialized yet (retracting then would strip a LIVE
    position's exit management — fail-deadly), and a held position whose
    sibling watches merely expired looks terminal without ever having closed.

    Evidence setters, counted only while the uic's plan generation is OPEN (a
    ``tranche_plan`` line was seen and not retracted — a closure can never
    predate the plan it closes, and requiring this keeps the boot compactor's
    reordered output folding identically):
      - a full ``stop_filled`` (falsy ``partial``); its element is the stop
        ref's parsed pick key, or ``None`` when the ref has no entry-trail
        shape (a classic bracket stop round-tripping the uic still closed it);
      - a ``tranche_fired`` carrying ``position_closed`` (element ``None`` —
        the line has no ref to attribute).
    Reset shares :func:`_apply_generation_reset` verbatim: a keyless or
    different-key plan line and a retraction clear the uic; the
    ``already_watching`` same-key re-append does not."""
    closures: dict[int, set[str | None]] = {}
    governing_key: dict[int, str] = {}
    generation_open: set[int] = set()
    for line in lines:
        uic = _coerce(line, "uic", int)
        if uic is None:
            continue
        kind = line.get("kind")
        if _apply_generation_reset(kind, line, uic, governing_key, (closures,)):
            if kind == _TRANCHE_PLAN_KIND:
                generation_open.add(uic)
            else:
                generation_open.discard(uic)
            continue
        if uic not in generation_open:
            continue
        if kind == "stop_filled" and not line.get("partial"):
            ref = line.get("ref")
            closures.setdefault(uic, set()).add(
                _pick_key_from_stop_ref(ref if isinstance(ref, str) else None)
            )
        elif kind == _TRANCHE_FIRED_KIND and line.get("position_closed"):
            closures.setdefault(uic, set()).add(None)
    return {u: frozenset(s) for u, s in closures.items()}


def _build_managed_exits(
    *,
    long_positions: Iterable[Position],
    tranche_plans: Mapping[int, tuple[tuple[TpTranchePlan, ...], float, float]],
    fired: Mapping[int, frozenset[str]],
    trailed: Mapping[int, float],
    plan_currencies: Mapping[int, tuple[str | None, str | None]] | None = None,
) -> list[ManagedExit]:
    """Build this tick's managed-position list. Pure — no broker/journal I/O.

    A live long position whose uic has a folded ``tranche_plan`` (Task 1)
    becomes ONE ``ManagedExit``; a live long with NO ``tranche_plan`` on record
    is SKIPPED — positions placed before this deploys carry no ladder and stay
    stop-only forever (the deliberate gradual-rollout boundary).

    ``trailed`` is the generation-gated ``_fold_trailed_since_latest_plan`` map: a
    tranche fire amends the SL to ``ManagedExit.stop_price``
    (``execute_tranche_exit``), so under a trailing policy the placement-time
    plan stop must be RAISED to the last confirmed trailed level — otherwise
    TP1 firing would PATCH a ratcheted stop back down to the disaster level,
    and ``_maybe_trail``'s own ratchet floor would then refuse to restore it
    until the peak advanced past the old level again."""
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
        trailed_level = trailed.get(uic)
        if trailed_level is not None:
            stop_price = max(stop_price, trailed_level)
        instrument_ccy, sizing_ccy = (plan_currencies or {}).get(uic, (None, None))
        managed.append(
            ManagedExit(
                uic=uic,
                tp_tranches=tp_tranches,
                reference_qty=reference_qty,
                stop_price=stop_price,
                already_fired=fired.get(uic, frozenset()),
                cost_facts=cost_gate_facts(
                    instrument_currency=instrument_ccy, sizing_currency=sizing_ccy
                ),
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


# Session gate for the shared price stream: outside market hours no frames
# flow, so a 24/7 WebSocket recv-times-out every ~3min into a reconnect +
# subscription-recreate cycle all night. Behind its own flag (default OFF ->
# None -> today's behavior); the stream side is fail-open by contract, so a
# raising predicate can never silence the stream during trading hours.
_STREAM_SESSION_GATE_ENV = "ALPHALENS_SAXO_STREAM_SESSION_GATE"

# The window is [session_open - WARMUP, session_close + GRACE]. WARMUP exists
# because the connection must be up and the create-subscription snapshot
# applied BEFORE the open — the DelayedByMinutes flag arrives ONLY in that
# snapshot (2026-08-18 probe), so connecting at the bell would veto the first
# minutes of quotes. GRACE keeps the closing auction's last prints flowing.
_STREAM_SESSION_WARMUP = dt.timedelta(minutes=15)
_STREAM_SESSION_GRACE = dt.timedelta(minutes=10)

# The venue set the window is computed over (#1238 PR 5). Comma-separated
# MICs; unset -> ("XNYS",), byte-identical to the pre-#1238 single-venue gate
# (XNYS and XNAS share the regular session, so one US calendar covers both).
# CONFIG-DRIVEN on purpose, never derived from open watches/positions: the
# stream must be up (warmup included) BEFORE the first watch on a new venue
# can tick — a derived window would hold the socket closed exactly when the
# venue's first pick needs quotes. INVARIANT: every configured venue's hull
# (open - warmup .. close + grace) must stay inside one UTC day — the per-day
# window memo keys on UTC now.date(); an Asian venue opening near 00:00 UTC
# needs that memo redesigned first.
_STREAM_SESSION_VENUES_ENV = "ALPHALENS_SAXO_STREAM_SESSION_VENUES"
_STREAM_SESSION_DEFAULT_VENUES: tuple[str, ...] = ("XNYS",)


def _stream_session_venues() -> tuple[str, ...]:
    raw = os.environ.get(_STREAM_SESSION_VENUES_ENV, "")
    venues = tuple(
        dict.fromkeys(token.strip().upper() for token in raw.split(",") if token.strip())
    )
    return venues or _STREAM_SESSION_DEFAULT_VENUES


def _stream_session_gate_enabled() -> bool:
    return os.environ.get(_STREAM_SESSION_GATE_ENV) == "1"


def _make_stream_session_window(
    clock: Callable[[], dt.datetime] | None = None,
) -> Callable[[], bool]:
    """Build the "is now inside the trading window" predicate for the price
    stream's session gate.

    The window comes from the exchange-parametrized calendar helpers
    (``paper.calendar`` on ``exchange_calendars`` — real holidays, early
    closes, DST), NEVER hand-rolled hours: half-days resolve to the actual
    per-session close, a non-trading day is False all day.

    A calendar exception PROPAGATES by design — the stream side fails OPEN on
    a raise (connects, warns once). Swallowing it into False here would let a
    calendar bug silence the stream during trading hours, the one failure the
    gate's safety contract forbids.

    Per-day session bounds are memoized (the reader polls the predicate every
    second while asleep); only successful lookups are cached, so a transient
    raise is retried on the next poll.

    The window is the per-day HULL over ``_stream_session_venues()`` — from
    the earliest trading venue's open − WARMUP to the latest close + GRACE,
    skipping each venue on its own holidays (#1238 PR 5); no venue trading
    means False all day. A hull, not a union: the socket stays up between a
    European close and the US open — reconnect churn in that gap is exactly
    what the gate exists to avoid overnight, and the gap is bounded.

    UTC-date note: every supported hull (XWAR 06:45 UTC at the earliest to
    XNYS 21:10 UTC at the latest) never crosses UTC midnight, so
    ``now.date()`` in UTC is always the session date being asked about.
    """
    venues = _stream_session_venues()
    read_clock = clock or (lambda: dt.datetime.now(dt.UTC))
    # Single-writer by construction: the predicate is called ONLY from the
    # stream's reader thread (_supervise), so this memo needs no lock — do not
    # share the predicate across threads without adding one.
    bounds_by_day: dict[dt.date, tuple[dt.datetime, dt.datetime] | None] = {}

    def _bounds(day: dt.date) -> tuple[dt.datetime, dt.datetime] | None:
        from alphalens_pipeline.paper.calendar import (
            is_trading_day,
            session_close_utc,
            session_open_utc,
        )

        opens: list[dt.datetime] = []
        closes: list[dt.datetime] = []
        for venue in venues:
            if not is_trading_day(day, venue):
                continue
            opens.append(session_open_utc(day, venue))
            closes.append(session_close_utc(day, venue))
        if not opens:
            return None
        return (min(opens) - _STREAM_SESSION_WARMUP, max(closes) + _STREAM_SESSION_GRACE)

    def _in_window() -> bool:
        now = read_clock()
        day = now.date()
        if day not in bounds_by_day:
            # One live entry is enough (the daemon runs for months): drop
            # yesterday's bounds before caching today's.
            bounds_by_day.clear()
            bounds_by_day[day] = _bounds(day)
        bounds = bounds_by_day[day]
        if bounds is None:
            return False
        window_start, window_end = bounds
        return window_start <= now <= window_end

    return _in_window


def _stream_session_window_if_enabled() -> Callable[[], bool] | None:
    """The predicate ``get_shared_price_stream`` should construct the stream
    with: None (today's behavior, byte-identical) unless
    ``ALPHALENS_SAXO_STREAM_SESSION_GATE`` is exactly ``"1"``."""
    if not _stream_session_gate_enabled():
        return None
    return _make_stream_session_window()


class _NullPriceFeed:
    """Vetoes everything. The OFF state of the Saxo feed is 'no prices', never a
    quiet downgrade to a weaker source (see the INC-2 design memo)."""

    # The parameter name is pinned by the structural ``PriceFeed`` Protocol
    # (pyright matches names on non-positional-only params), so it cannot be
    # underscored away even though this null feed ignores it.
    def latest(self, uic: int) -> None:  # NOSONAR
        return None


# Scopes of the shared price-stream subscription, one per feed-building call
# site (SaxoPriceStream.ensure_subscribed keys its per-caller desired sets by
# these). The exits pass and the peak update watch the SAME open-position uics,
# so they share one scope; the entry-watch pass owns its own.
_FEED_SCOPE_EXITS = "exits"
_FEED_SCOPE_ENTRY_WATCH = "entry-watch"


def _release_feed_scope(deps: LoopDeps, scope: str) -> None:
    """Hand an EMPTY desired set to one pass's slice of the shared
    price-stream subscription.

    Called on a pass's quiet early-returns (no active watches, KILL, feature
    off) which would otherwise never reach the pass's feed build — the only
    writer of its scope. Skipping the write leaves the scope holding its
    LAST uics for the daemon's lifetime: the wire-level union never shrinks,
    the reader keeps a WebSocket plus a server-side subscription streaming
    uics nobody reads (the zero-desired-uics idle protection never engages),
    and a stale delayed quote for a dead uic can pin ``any_delayed`` and
    drive the session-reclaim retry loop with zero price consumers.

    Quiet by design (no alert, no raise): releasing has no reader this tick,
    a failure here is retried next tick, and a stale scope is only ever
    over-subscription — never a safety hazard worth paging over."""
    import contextlib

    feed_factory = deps.live_exits_feed_factory or _default_live_exits_feed_factory
    with contextlib.suppress(Exception):
        feed_factory({}, scope=scope)


# The cross-process price reader's socket (#1172). When set, this daemon reads
# the ONE elevated Saxo session that reader process holds instead of opening its
# own stream — Saxo grants a single elevated session per LIVE login, so two
# in-process streams demote BOTH daemons to 15-minute quotes, silently. Unset
# keeps today's in-process behaviour, which is also the rollback lever: remove
# the drop-in, restart, done.
_PRICE_READER_SOCKET_ENV = "ALPHALENS_SAXO_PRICE_READER_SOCKET"

# The remote source is held for the process, not per tick: the reader keys each
# client's touch-latch accumulator by CONNECTION, so a per-tick client would
# reconnect every ~45s and always drain an empty window.
_REMOTE_QUOTE_SOURCE: Any | None = None


def _reset_remote_quote_source_for_tests() -> None:
    """Drop the cached remote source. Test-only seam, mirroring
    ``polygon_client._reset_default_client_for_tests``."""
    import contextlib

    global _REMOTE_QUOTE_SOURCE  # noqa: PLW0603 — lazy singleton is the documented pattern
    if _REMOTE_QUOTE_SOURCE is not None:
        with contextlib.suppress(Exception):
            _REMOTE_QUOTE_SOURCE.close()
    _REMOTE_QUOTE_SOURCE = None


def _quote_source() -> Any:
    """The quote source this tick reads from: the shared reader when a socket
    is configured, otherwise this process's own stream.

    Both satisfy ``QuoteSource``, so everything downstream — subscription
    scoping, the price-feed adapter, the touch-latch drain — is identical
    either way."""
    global _REMOTE_QUOTE_SOURCE  # noqa: PLW0603 — lazy singleton is the documented pattern
    socket_path = os.environ.get(_PRICE_READER_SOCKET_ENV)
    if socket_path:
        from alphalens_pipeline.data.alt_data.price_reader_client import RemoteQuoteSource

        if _REMOTE_QUOTE_SOURCE is None:
            _REMOTE_QUOTE_SOURCE = RemoteQuoteSource(socket_path)
            logger.info("live prices: reading the shared price reader at %s", socket_path)
        return _REMOTE_QUOTE_SOURCE

    from alphalens_pipeline.data.alt_data.saxo_price_stream import get_shared_price_stream

    # ADR 0016 D5: the stream's gauges must carry a per-instance job label so a
    # future LIVE daemon's price stream never shares a Prometheus job (and thus
    # textfile) with the SIM instance's. Like metrics_job, session_window only
    # takes effect on the FIRST call that actually constructs the singleton —
    # see get_shared_price_stream.
    return get_shared_price_stream(
        metrics_job=state_paths.price_stream_metrics_job(),
        session_window=_stream_session_window_if_enabled(),
    )


def _default_live_exits_feed_factory(
    uic_to_instrument: Mapping[int, tuple[str, str]],
    *,
    scope: str,
) -> PriceFeed:
    """The production price feed: Saxo LIVE streaming, or nothing.

    yfinance is NOT a fallback here. It remains in the tree, unwired, and its
    PricePoint carries no event time so the freshness gate would veto it
    anyway. Behind ``ALPHALENS_SAXO_LIVE_PRICES`` (default OFF); when off this
    returns a feed that vetoes every uic rather than quietly downgrading.

    ``scope`` is forwarded to ``stream.ensure_subscribed`` so each of the
    tick's feed builds (exits/peaks vs entry-watch) replaces only its own
    slice of the shared subscription — passing the whole set from every call
    site made the builds fight and churn the single server-side subscription
    every tick (2026-08-18 incident)."""
    if not _saxo_live_prices_enabled():
        return _NullPriceFeed()
    from alphalens_pipeline.brokers.automanager.saxo_live_price_feed import SaxoLivePriceFeed

    stream = _quote_source()
    live_uics = {
        sim_uic: stream.live_uic_for(ticker, exchange_mic=mic)
        for sim_uic, (ticker, mic) in uic_to_instrument.items()
    }
    stream.ensure_subscribed([u for u in live_uics.values() if u is not None], scope=scope)
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
        return feed_factory(uic_to_instrument, scope=_FEED_SCOPE_EXITS)
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
    health veto), a ``None`` bid (``PricePoint.bid`` is typed ``float`` but
    nothing at runtime stops a feed from constructing one with ``bid=None`` —
    the same "must not trust the caller" doubt ``is_fresh`` already guards
    against), or a non-finite/non-positive price leaves ``deps.peak_tracker``
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
    stale peak if the uic is re-picked later.

    Transactional + per-uic fault-isolated: the loop accumulates into a LOCAL
    ``new_peaks`` copy of ``deps.peak_tracker`` and only commits it back once
    the loop has finished, so a ``feed.latest(uic)`` that raises mid-loop
    cannot leave ``deps.peak_tracker`` half-updated relative to the raised
    exception. Each uic's ``feed.latest(uic)`` call is wrapped in its own
    ``try/except Exception: continue`` — one bad uic is skipped exactly like a
    ``None`` point (a doubt becomes a veto), and does not abort the other
    uics still to be processed this tick."""
    uic_to_instrument = {
        uic: (pos.instrument.ticker, pos.instrument.exchange_mic)
        for pos in long_positions
        if (uic := _position_uic(pos)) is not None
    }
    feed_factory = deps.live_exits_feed_factory or _default_live_exits_feed_factory
    # Same scope as the exits pass: both watch the SAME open-position uics, so
    # this build must replace (not duplicate) the exits slice of the shared
    # price-stream subscription.
    feed = feed_factory(uic_to_instrument, scope=_FEED_SCOPE_EXITS)
    peak_by_uic: dict[int, float] = {}
    last_price_by_uic: dict[int, float] = {}
    new_peaks = dict(deps.peak_tracker)
    for uic in uic_to_instrument:
        try:
            point = feed.latest(uic)
        except Exception:  # broad on purpose: one bad uic must not abort the others
            continue  # per-uic feed fault — leave that uic's peak untouched
        if point is None:
            continue  # stream-health veto — leave peak_tracker untouched
        price = point.bid
        if price is None or not math.isfinite(price) or price <= 0.0:
            continue  # a doubt about the price becomes a veto, never a crash
        existing_peak = new_peaks.get(uic)
        new_peaks[uic] = price if existing_peak is None else max(existing_peak, price)
        peak_by_uic[uic] = new_peaks[uic]
        last_price_by_uic[uic] = price
    for stale_uic in set(new_peaks) - set(uic_to_instrument):
        del new_peaks[stale_uic]
    deps.peak_tracker.clear()
    deps.peak_tracker.update(new_peaks)
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
        # While disabled the gate is the only code in this pass that runs, so
        # it owns releasing the "exits" scope — otherwise toggling the feature
        # off freezes the scope on its last uics (under a non-trailing policy
        # nothing else writes it) and the shared subscription streams them
        # forever.
        _release_feed_scope(deps, _FEED_SCOPE_EXITS)
        return
    broker = deps.broker
    if not isinstance(broker, LiveExitBroker):
        # Structurally impossible after build_default_deps (its boot gate refuses
        # such a broker when the flag is on) — reachable only for directly
        # composed deps, i.e. tests. Skip + alert, NEVER an AttributeError: the
        # try below catches only BrokerError, so an AttributeError would escape
        # and starve the protection pass that follows in the tick (#1141).
        if deps.alert_throttled(
            f"live-exits: broker {getattr(broker, 'name', '?')!r} lacks the "
            "live-exit capability set (LiveExitBroker) — pass skipped",
            "live-exits-capability",
        ):
            report.alerts += 1
        _release_feed_scope(deps, _FEED_SCOPE_EXITS)
        return
    try:
        long_positions = broker.get_long_positions()
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
        trailed=_fold_trailed_since_latest_plan(journal_lines),
        plan_currencies=fold_tranche_plan_currencies(journal_lines),
    )
    # uic -> (ticker, venue) off the live positions just read. The venue must
    # survive: resolving a LIVE instrument by bare ticker is ambiguous for
    # cross-listed names.
    uic_to_instrument = {
        uic: (pos.instrument.ticker, pos.instrument.exchange_mic)
        for pos in long_positions
        if (uic := _position_uic(pos)) is not None
    }
    # Built BEFORE the managed check on purpose: the build writes this pass's
    # "exits" slice of the shared price-stream subscription off the live long
    # positions, and that write must happen on quiet ticks too — when the last
    # managed position closes, the scope must shrink with it (a skipped write
    # would stream the closed positions' uics forever), and it must hold the
    # SAME long-position set the trailing peak updater writes (an empty write
    # here would flip-flop the shared subscription against ``_update_peaks``
    # every tick while an unmanaged long position is open).
    feed = _build_live_exits_feed(deps, uic_to_instrument, report)
    if not managed:
        return
    # Lazy, per the module's convention (see the CLI import note): the rail
    # lattice is policy, and policy lives in `execution`.
    from alphalens_pipeline.brokers.execution import RAIL_LATTICE

    try:
        fired = run_live_exits(broker, feed, managed, lattice=RAIL_LATTICE)
    except BrokerError as exc:
        if deps.alert_throttled(
            f"live-exits: pass failed (broker error) — skipped: {exc}",
            "live-exits-run-fail",
        ):
            report.alerts += 1
        return
    if fired:
        report.exits_placed += len(fired)
        report.actions.append(("live-exits", f"fired={len(fired)}"))
        # #1219: exits announce themselves — ONE throttled alert per fired
        # tranche, rendered HERE (not in the engine, which stays port-free and
        # knows no tickers). The (uic, tag) key lets a repeat within the
        # throttle window dedup while a distinct tranche still notifies.
        for tranche in fired:
            ticker = uic_to_instrument.get(tranche.uic, (f"uic {tranche.uic}", None))[0]
            order_text = f" (order {tranche.sell_order_id})" if tranche.sell_order_id else ""
            if deps.alert_throttled(
                f"exit: {ticker} tranche {tp_label_from_tag(tranche.tag)} sold "
                f"{tranche.qty:g} shares{order_text}",
                f"tranche-fired:{tranche.uic}:{tranche.tag}",
            ):
                report.alerts += 1
            if tranche.position_closed:
                # #1198 option B: a TP exit that closed the position produces
                # no stop fill (the SL is CANCELLED), so the sibling-watch
                # retire hooks HERE. journal_lines is this tick's snapshot —
                # the plan for a just-closed position cannot have been written
                # after it.
                _retire_sibling_watches(
                    deps,
                    _fold_governing_plan_pick_keys(journal_lines).get(tranche.uic),
                    report,
                    trigger=f"tranche {tp_label_from_tag(tranche.tag)} closed the position",
                )


def _fetch_protection_peaks(
    deps: LoopDeps, report: TickReport
) -> tuple[dict[int, float], dict[int, float]]:
    """Task 4: fetch this tick's high-water peaks for the trailing arm, behind a
    boundary whose ENTIRE job is that NOTHING here can starve the never-naked
    protection pass that runs immediately after.

    Called ONLY when ``deps.exit_policy.trails``. Reads the live long positions
    (an EXTRA ``get_long_positions`` beyond the one ``build_protection_view`` does
    internally — a known minor inefficiency on the trailing path only, acceptable
    for this cut; fold into a shared per-tick read later) and hands them to
    ``_update_peaks``, which builds its own price feed via the injected/default
    factory (CARRYOVER-2: that helper has no boundary of its own).

    Deliberately catches ``Exception``, not just ``BrokerError``: the feed factory
    reaches real Saxo LIVE auth/REST/streaming machinery whose failures are not
    all ``BrokerError``, and the whole point of this boundary is that a doubt
    becomes trailing-dark-this-tick (empty peak maps), never a crash. On failure
    the caller still runs ``build_protection_view`` + ``reconcile_protection`` with
    empty maps, so trailing simply goes dark this tick while never-naked holds. The
    failure is surfaced via the shared throttled-alert sink the pass already uses."""
    broker = deps.broker
    if not isinstance(broker, SupportsNettedPositionReads):
        # Boot-unreachable (build_default_deps refuses such a broker
        # unconditionally, #1141) — reachable only for directly composed deps.
        # An EXPLICIT refusal, not the except-Exception path below: an absent
        # capability is a composition defect that holds on EVERY tick, and the
        # generic "peak fetch failed" message reads as a transient feed error —
        # the operator would wait out an outage that is not one. logger.error,
        # not logger.exception: there is no active exception here, and a
        # fabricated "NoneType: None" traceback would only mislead.
        logger.error(
            "trailing: broker %r lacks netted position reads — trailing dark",
            getattr(broker, "name", "?"),
        )
        if deps.alert_throttled(
            f"trailing: broker {getattr(broker, 'name', '?')!r} lacks netted "
            "position reads (SupportsNettedPositionReads) — trailing dark",
            "trail-peak-capability",
        ):
            report.alerts += 1
        return {}, {}
    try:
        long_positions = broker.get_long_positions()
        return _update_peaks(deps, long_positions)
    # Broad on purpose (mirrors _build_live_exits_feed): a feed/network/auth error
    # must not propagate into the protection pass. Trailing goes dark, protection
    # runs unchanged.
    except Exception as exc:
        logger.exception("trailing: peak fetch failed — trailing dark this tick")
        if deps.alert_throttled(
            f"trailing: peak fetch failed — trailing dark this tick: {exc}",
            "trail-peak-fetch-fail",
        ):
            report.alerts += 1
        return {}, {}


def _run_protection_pass(
    deps: LoopDeps, records: list[Mapping[str, Any]], kill: bool, report: TickReport
) -> None:
    """Broker-state-truth protection pass (saxo-oco memo §6): ONE snapshot, then a
    pure desired-vs-actual diff over live positions + live SELL legs, each action
    executed inside its OWN per-action BrokerError boundary so one uic's failure
    never aborts the tick or the other uics. This is the ONLY path that places /
    resizes protective stops now (advance no longer does).

    On the trailing path only (``deps.exit_policy.trails`` — ``trailing_atr``) this
    first fetches the per-uic high-water peaks and threads them into the view so the
    pure ``_maybe_trail`` arm can ratchet the stop UP. The peak fetch is behind its
    OWN boundary (``_fetch_protection_peaks``): a fetch failure degrades trailing to
    dark (empty maps) but the view build + reconcile ALWAYS run, so the never-naked
    backstop can never be starved. Every non-trailing policy takes the exact call
    ``deps.build_protection_view(deps.broker, records)`` with no peak fetch — zero
    new behaviour, byte-identical to today."""
    try:
        if deps.exit_policy.trails:
            peak_by_uic, last_price_by_uic = _fetch_protection_peaks(deps, report)
            protection_view = deps.build_protection_view(
                deps.broker,
                records,
                peak_by_uic=peak_by_uic,
                last_price_by_uic=last_price_by_uic,
            )
        else:
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


# --- Entry-trailing watcher pass (PR-T1, DRY-RUN) ----------------------------
#
# The WIRE half of ALPHALENS_BROKER_ENTRY_TRAIL_BPS. When the flag is armed,
# _place_pick routes an eligible pick into a WATCH (per-tier watch_open lines on
# entry_trails.jsonl) INSTEAD of resting the three server-side limit-entry
# orders; this per-tick pass then drives each open watch's WATCHING -> TOUCHED
# -> (WOULD_FIRE | SUSPENDED | EXPIRED | CANCELLED) state machine off the shared
# INC-2 price stream. STRICTLY DRY-RUN: the pass NEVER places, amends, or
# cancels a broker order — the "fire" is an alert-only "would fire @ trigger X"
# plus a journal marker (memo §7 PR-T1). Flag unset/0 => nothing here runs and
# the daemon is byte-identical to today (PR-T0 inertness).

# The watch-capacity rail is OWNED by entry_trails (module-ownership doctrine,
# #1189): live_rails pins it as the 9th LIVE boot-assert rail and this module
# reads it every tick, so both must resolve the same name and bounds. Re-exported
# under the historical private names so the call sites below stay unchanged.
_ENTRY_WATCH_MAX_PICKS_ENV = entry_trails.ENTRY_WATCH_MAX_PICKS_ENV
_ENTRY_WATCH_MAX_PICKS_DEFAULT = entry_trails.ENTRY_WATCH_MAX_PICKS_DEFAULT
_ENTRY_WATCH_MAX_PICKS_MIN = entry_trails.ENTRY_WATCH_MAX_PICKS_MIN
_ENTRY_WATCH_MAX_PICKS_MAX = entry_trails.ENTRY_WATCH_MAX_PICKS_MAX

_entry_watch_max_picks_warned = False
"""One logger.warning per process for an invalid/out-of-range env value — the
env is re-read every tick and would otherwise warn every ~45s all day."""

_entry_watch_capacity_deferred: set[str] = set()
"""Process-lifetime observability only (no behaviour): the pick_keys whose
capacity deferral was already logged at INFO, so an armed pick queued behind a
full watch book is visible exactly once per daemon lifetime (later ticks stay
DEBUG)."""


def _entry_watch_max_picks() -> int:
    """Watch capacity (memo decision #4 / G5 CRITICAL-1): at most this many
    DISTINCT picks may hold open watches at once — a PICK-denominated limit,
    deliberately NOT folded into MAX_OPEN (which counts per tier and would make
    a 3-tier trailing pick un-armable at MAX_OPEN=1). The account is protected
    by the virtual gross/cash reservation fold
    (entry_trails.watching_virtual_gross_acct), not by this capacity number.

    Sourced from :data:`_ENTRY_WATCH_MAX_PICKS_ENV`; unset falls back to the
    default silently, an invalid or out-of-range value falls back too but pages
    the journal with ONE warning per process."""
    global _entry_watch_max_picks_warned  # noqa: PLW0603 — once-per-process warn latch
    raw = os.environ.get(_ENTRY_WATCH_MAX_PICKS_ENV)
    if raw is None:
        return _ENTRY_WATCH_MAX_PICKS_DEFAULT
    try:
        value = int(raw)
    except ValueError:
        value = None
    if value is not None and _ENTRY_WATCH_MAX_PICKS_MIN <= value <= _ENTRY_WATCH_MAX_PICKS_MAX:
        return value
    if not _entry_watch_max_picks_warned:
        _entry_watch_max_picks_warned = True
        logger.warning(
            "%s=%r is invalid (expected an integer in [%d, %d]) — using the default %d",
            _ENTRY_WATCH_MAX_PICKS_ENV,
            raw,
            _ENTRY_WATCH_MAX_PICKS_MIN,
            _ENTRY_WATCH_MAX_PICKS_MAX,
            _ENTRY_WATCH_MAX_PICKS_DEFAULT,
        )
    return _ENTRY_WATCH_MAX_PICKS_DEFAULT


_ENTRY_BPS_DENOMINATOR = 10_000
"""``d = d_bps / 10_000`` — the would-be-trigger basis-point divisor for the
measurement stamp (mirrors ``entry_trail_watcher._BPS_DENOMINATOR``; a local
copy keeps this module from importing a private engine constant)."""

_ENTRY_REARM_MARKER = "awaiting_fresh_low"
"""The truthy flag the reconcile pass stamps on a RE-ARM ``watch_open`` line
(memo §5 CRITICAL-2): the reconstructed watcher seeds ``awaiting_fresh_low`` from
it so the open-check blocks the fresh arm until a NEW post-open low forms. Absent
on a first-time watch_open (a fresh watch tracks its trough in-session, no stale
carried trigger to guard)."""


@dataclass
class _EntryWatchRuntime:
    """Daemon-lifetime scratch for ONE open entry-tier watch (one ``crid``).

    Holds the stateful engine watcher (whose transient staleness-gap /
    open-check fields must persist across ticks within a lifetime) plus the
    measurement marks the terminal journal line stamps (memo §5 Measurement,
    filled by T1d): the running ``trough`` (mirrored from each ``trough`` intent,
    seeded from the fold on reconstruct so a restart never forgets the low
    upward) and the touch price/timestamp captured when the tier is first
    touched."""

    watcher: entry_trail_watcher.EntryTierWatcher
    trough: float | None = None
    touch_price: float | None = None
    touch_ts: str | None = None


def _entry_watch_crid(ticker: str, brief_date: str, tier_index: int) -> str:
    """Deterministic per-tier watch id in the ``-entry-`` request-id family
    (memo §5 — parallel to the exit ids so entry/exit ids can never collide on
    one uic). DETERMINISTIC, not a uuid: a crash between the journal-first
    watch_open and the note-only pick retirement re-opens the SAME crid on the
    next drain, and the fold's latest-watch_open-wins semantics make that
    re-open idempotent (no double reservation) where a fresh uuid would leak a
    second watch."""
    return f"{ticker}-{brief_date}-entry-t{tier_index}"


def _entry_trail_mode_tag(d_bps: int) -> str:
    """The measurement ``entry_mode`` cohort tag (memo §5 / T8): the native
    trailing mode + configured distance + the execution-config version, so fills
    measured under different execution policies never pool in the offline
    analysis join. PR-T2b drops the ``dryrun`` token — a real native order rests
    now — and the local trailing wire spellings join
    ``execution_config_version()`` (broker.py comment §100-107)."""
    from alphalens_pipeline.brokers.execution import execution_config_version

    return f"entry-trail-native-d{d_bps}-{execution_config_version()}"


def _entry_trail_eligible(plan: Any) -> bool:
    """Whether a sized pick can be routed into an entry-trail watch: it has at
    least one positive-quantity entry tier (an all-zero-tier plan is handled by
    the normal zero-tiers refusal downstream). MVP scope is long single-name
    equities, which every drained pick already is."""
    return any(getattr(tier, "qty", 0) > 0 for tier in getattr(plan, "entry_tiers", ()) or ())


def _open_watch_pick_keys(fold: entry_trails.EntryTrailFold) -> set[str]:
    """The distinct ``pick_key`` of every NON-terminal watch_open tier in the
    fold (falling back to the crid when a record predates the pick_key field)
    — the set of picks that currently hold an open watch."""
    pick_keys: set[str] = set()
    for state in fold.tiers.values():
        if state.terminal_kind is not None or state.watch_open is None:
            continue
        pick_keys.add(str(state.watch_open.get("pick_key") or state.crid))
    return pick_keys


_entry_watch_live_uic_deferred: set[str] = set()
"""``pick_key``s already logged as live-uic-deferred (2026-08-19 adjudication
finding 2) — first deferral WARNs, later ticks DEBUG. Process-lifetime
observability only, no behaviour rides on membership."""


def _has_live_long_on_uic(positions: Iterable[Position], uic: int) -> bool:
    """Whether any live broker position row is LONG on ``uic`` (a zero/short
    row never blocks — only an actual long can be clobbered by a re-picked
    ticker's fresh ``tranche_plan``)."""
    return any(
        _position_uic(pos) == uic and float(getattr(pos, "quantity", 0.0) or 0.0) > 0.0
        for pos in positions
    )


def _log_live_uic_deferral(ticker: str, pick_key: str, uic: int) -> None:
    """Log a live-uic routing deferral: WARNING the FIRST time this pick_key is
    deferred in this process (an abnormal state the operator should see — a
    re-picked ticker is queuing behind its own live position), DEBUG on every
    later tick. Process-lifetime observability only — no behaviour rides on the
    set (mirrors :func:`_log_watch_capacity_deferral`)."""
    log = logger.debug
    if pick_key not in _entry_watch_live_uic_deferred:
        _entry_watch_live_uic_deferred.add(pick_key)
        log = logger.warning
    log(
        "place_pick %s: a live long already holds uic %d — %s stays armed until the "
        "uic is flat (routing a watch now would clobber the live position's ladder)",
        ticker,
        uic,
        ticker,
    )


def _open_watch_picks_for_max_open(
    fold: entry_trails.EntryTrailFold,
    *,
    own_pick_key: str,
    position_uics: Collection[int],
) -> set[str]:
    """The DISTINCT ``pick_key`` of every open entry watch that occupies a
    prospective-position slot in the MAX_OPEN admission check (2026-08-19
    adjudication finding 1).

    An open watch — or its armed unfilled native trail, which is equally
    non-terminal in the fold — is a committed risk unit ``safety.check`` cannot
    see: the note-only watch submission record carries no brackets and no
    position exists until the trail fires, so with N watches open the classic
    sum (journal brackets + live positions) under-counts by N and a raised
    watch capacity could over-commit up to N extra concurrent positions.

    Two exclusions keep the count one-slot-per-risk-unit:

    - ``own_pick_key`` — the candidate pick's own watch (the crash-recovery
      re-drive must not self-block on its own reservation, mirroring the
      intercept's ``already_watching`` exemption);
    - any watch whose uic is in ``position_uics`` — a pick whose tier already
      FIRED shows up as a live position while a deeper tier still watches; that
      unit is already counted in ``BrokerView.open_position_count``. The caller
      passes NET-open uics (``_net_open_position_uics``) — a net-flat uic
      (an EOD-netting round-trip's two ledger rows) is NOT in the position
      count, so its watch must keep occupying a slot here.

    A watch record with no parseable uic still counts (conservative: an
    over-reserved slot refuses one pick too early; an under-count re-opens the
    over-commit)."""
    picks: set[str] = set()
    for state in fold.tiers.values():
        if state.terminal_kind is not None or state.watch_open is None:
            continue
        record = state.watch_open
        pick_key = str(record.get("pick_key") or state.crid)
        if pick_key == own_pick_key or _watch_uic_in(record, position_uics):
            continue
        picks.add(pick_key)
    return picks


def _watch_uic_in(record: Mapping[str, Any], uics: Collection[int]) -> bool:
    """Whether the watch record's uic parses AND is in ``uics``; False on a
    missing/unparseable uic (the caller then counts the watch conservatively)."""
    try:
        return int(record["uic"]) in uics
    except (KeyError, TypeError, ValueError):
        return False


def _entry_watch_capacity_reached(fold: entry_trails.EntryTrailFold) -> bool:
    """True iff opening another watch would exceed :func:`_entry_watch_max_picks`
    DISTINCT watching picks."""
    return len(_open_watch_pick_keys(fold)) >= _entry_watch_max_picks()


def _log_watch_capacity_deferral(ticker: str, pick_key: str) -> None:
    """Log a capacity deferral: INFO the FIRST time this pick_key is deferred in
    this process, DEBUG on every later tick. Process-lifetime observability
    only — no behaviour rides on the set (2026-08-19 incident: ETSY sat
    capacity-deferred for a day with only DEBUG lines to show for it)."""
    log = logger.debug
    if pick_key not in _entry_watch_capacity_deferred:
        _entry_watch_capacity_deferred.add(pick_key)
        log = logger.info
    log(
        "place_pick %s: entry-trail watch capacity reached (cap=%d) — %s stays armed",
        ticker,
        _entry_watch_max_picks(),
        ticker,
    )


def _sizing_currency_of(fx: Any, instrument: Any) -> str:
    """The account/sizing currency for a journal currency stamp (#1238 PR 3).

    ``fx`` is None on the same-currency path, where the sizing currency IS the
    instrument currency by construction; a stub without the attribute yields
    "" (no stamp -> the gates keep the conservative legacy facts)."""
    if fx is not None:
        return str(getattr(fx, "account_currency", "") or "")
    return str(getattr(instrument, "currency", "") or "")


def _open_entry_watches(
    intent: Any,
    ticker: str,
    instrument: Any,
    plan: Any,
    fx: Any,
    *,
    d_bps: int,
    geometry_stamp: dict[str, Any] | None = None,
) -> int:
    """Journal one ``watch_open`` line per positive-quantity entry tier (memo
    §5, G3 journal-FIRST) and return the count opened.

    The shared TTL ``window_end`` is resolved ONCE (memo §5 "one rule":
    ``advance_trading_sessions(brief_date, DEFAULT_ORDER_TTL_DAYS)`` -> that
    session's close in UTC, never "+7d from each order"). Each watch_open
    carries BOTH the reservation-critical fields the gross/cash fold values
    (``limit``/``qty``/``fx_rate``) AND the WIRE context the per-tick pass needs
    to reconstruct the watcher and resolve the price feed
    (``uic``/``ticker``/``exchange_mic``/``next_tier_limit``/``d_bps``/
    ``window_end``/``pick_key``/``entry_mode``). Tiers are strictly descending,
    so ``next_tier_limit`` is the deeper tier's limit for the G9 depth suspend;
    ``None`` on the deepest tier.

    ``geometry_stamp`` (2026-08-19 incident fix) is the exact
    :func:`_geometry_shadow_stamp` blob the bracket path journals on its
    ``planned`` lines — stamped on every watch_open here so the fire-arm
    ``planned`` writer can pass it through and the trailing-SL pass has its
    (k_atr, atr) reanchor facts. ``None`` omits the key entirely, keeping the
    line byte-identical to a pre-stamp watch_open."""
    from alphalens_pipeline.paper.calendar import advance_trading_sessions, session_close_utc

    brief_date = intent.meta.brief_date
    mic = instrument.exchange_mic
    uic = int(instrument.broker_instrument_id)
    ttl_date = advance_trading_sessions(
        dt.date.fromisoformat(brief_date), DEFAULT_ORDER_TTL_DAYS, exchange=mic
    )
    window_end = session_close_utc(ttl_date, exchange=mic).isoformat()
    fx_rate = float(fx.rate) if fx is not None else None
    mode_tag = _entry_trail_mode_tag(d_bps)
    pick_key = f"{ticker}:{brief_date}"

    tiers = tuple(plan.entry_tiers)
    opened = 0
    for index, tier in enumerate(tiers):
        if tier.qty <= 0:
            continue  # a zero-sized tier has nothing to watch (mirrors classify)
        next_limit = tiers[index + 1].limit_price if index + 1 < len(tiers) else None
        line: dict[str, Any] = {
            "kind": entry_trails.KIND_WATCH_OPEN,
            "crid": _entry_watch_crid(ticker, brief_date, tier.tier_index),
            "limit": float(tier.limit_price),
            "qty": float(tier.qty),
            "d_bps": int(d_bps),
            "window_end": window_end,
            "fx_rate": fx_rate,
            "uic": uic,
            "ticker": ticker,
            "exchange_mic": mic,
            "next_tier_limit": None if next_limit is None else float(next_limit),
            "pick_key": pick_key,
            "entry_mode": mode_tag,
            # PR-T2b never-naked (memo §5): the brief disaster-stop floor +
            # the original tier_index, carried so the fire-arm executor can
            # journal the `planned` disaster-SL line at placement (the plan
            # PRICE the broker cannot know) WITHOUT re-running classify.
            "disaster_stop": float(plan.disaster_stop),
            "tier_index": int(tier.tier_index),
            # #1238 PR 3: the currency pair the #1112 arm gate prices the
            # round trip with. fx is None on the same-currency path, where
            # the sizing currency IS the instrument currency.
            "instrument_currency": str(instrument.currency or ""),
            "sizing_currency": _sizing_currency_of(fx, instrument),
        }
        if geometry_stamp is not None:
            line["geometry"] = geometry_stamp
        entry_trails.append_entry_trail_line(line)
        opened += 1
    return opened


def _route_pick_to_entry_watch(
    broker: Broker,
    intent: Any,
    ticker: str,
    instrument: Any,
    account: Any,
    plan: Any,
    fx: Any,
    *,
    d_bps: int,
    exit_policy: ExitPolicy | None = None,
    reference_qty_override: float | None = None,
) -> bool:
    """The flag-ON drain tail: journal the pick's managed-exit state (ONE
    ``tranche_plan`` line per uic, exactly what the bracket path journals in
    ``_place_tiers`` — 2026-08-19 live incident: without it the live-exit
    engine skips the filled position forever), then the per-tier watches (G3
    journal-FIRST; the tranche_plan goes to disk BEFORE the watch_open lines so
    a crash between the two never yields a watching tier without its ladder),
    then RETIRE the pick from the drain with the SAME note-only submission
    record ``_place_tiers`` uses (``picks.submitted_pick_keys`` treats a note-only
    record as submitted, so the drain never re-drives this pick). Returns True
    when at least one watch opened. No broker order is placed here — the
    native trail rests later, at TOUCH.

    ``exit_policy`` is the same resolved-once cached policy ``_place_tiers``
    receives; the geometry gate below mirrors its ``use_geometry`` decision
    (``applies_geometry`` + a buildable ``exit_spec``) so the two paths can
    never disagree on which ladder is journaled.

    A calendar/journal failure inside the journal writes must never crash the
    drain: it is contained to a logged False (the pick stays armed and is
    re-attempted next tick; the deterministic crid + the tranche_plan fold's
    last-wins semantics make any partial write idempotent on retry)."""
    from alphalens_pipeline.brokers.submission_log import (
        append_submission_record,
        build_submission_record,
    )

    resolved_exit_policy: ExitPolicy = (
        exit_policy if exit_policy is not None else SetupStaticPolicy()
    )
    exit_spec = intent.exit
    try:
        _journal_tranche_plan_core(
            plan=plan,
            exit_spec=exit_spec,
            stop_price=float(plan.disaster_stop),
            # #1247 split pick: the override carries the FULL ladder qty
            # (now + pullback) so the TP ladder covers both halves' fills;
            # default = today's pullback-only sum, byte-identical.
            reference_qty=(
                reference_qty_override
                if reference_qty_override is not None
                else sum(t.qty for t in plan.entry_tiers if t.qty > 0)
            ),
            uic=int(instrument.broker_instrument_id),
            use_geometry=resolved_exit_policy.applies_geometry,
            # Trade identity (adjudication finding 4): a crash-recovery
            # re-drive re-appends this line — the SAME pick_key keeps the
            # fired-tranche fold from resetting on the re-append.
            pick_key=f"{ticker}:{intent.meta.brief_date}",
            instrument_currency=str(getattr(instrument, "currency", "") or ""),
            sizing_currency=_sizing_currency_of(fx, instrument),
        )
        # Same use_geometry decision _place_tiers makes for its planned lines:
        # the stamp rides every watch_open so the fire-arm planned writer can
        # hand the (k_atr, atr) reanchor facts to the trailing-SL pass.
        use_geometry = resolved_exit_policy.applies_geometry and exit_spec is not None
        opened = _open_entry_watches(
            intent,
            ticker,
            instrument,
            plan,
            fx,
            d_bps=d_bps,
            geometry_stamp=_geometry_shadow_stamp(
                exit_spec,
                intent.spec,
                use_geometry=use_geometry,
                exit_policy=resolved_exit_policy,
            ),
        )
    # Broad on purpose: an unrecognised MIC (calendar ValueError) or a journal
    # I/O error must degrade to "pick stays armed", never abort the tick before
    # the protection pass (_place_pick's own try only catches BrokerError).
    except Exception:
        logger.warning(
            "place_pick %s: entry-trail watch-open failed — pick stays armed", ticker, exc_info=True
        )
        return False
    if opened == 0:
        logger.warning(
            "place_pick %s: every entry tier sized to zero shares — no watch opened", ticker
        )
        return False
    append_submission_record(
        build_submission_record(
            brief_date=intent.meta.brief_date,
            ticker=ticker,
            mic=instrument.exchange_mic,
            uic=instrument.broker_instrument_id,
            brackets=[],
            note="entry-trail watch opened",
            sizing_currency=account.currency,
            instrument_currency=instrument.currency,
            sizing_equity=_resolve_sizing_equity(account.total_value),
            fx=fx,
            est_round_trip_fee_bps=_estimate_round_trip_fee_bps(
                plan, fx, instrument_currency=instrument.currency
            ),
        )
    )
    logger.info(
        "place_pick %s: routed into entry-trail watch (%d tier(s), d=%dbps)", ticker, opened, d_bps
    )
    return True


def _run_entry_watch_pass(deps: LoopDeps, kill: bool, report: TickReport) -> None:
    """Advance every open entry-trail watch by one decision tick (memo §5).

    KILL-GATED (memo §3 G2, CRITICAL): under KILL the pass opens no watch,
    advances no state, writes no journal line and sends no alert — mirroring the
    drain's ``if not kill`` gate (:func:`run_once`), NOT the ungated live-exits
    pass (copying that would let entries progress under an emergency stop).
    Under KILL the pass ALSO cancels every working ``-entry-`` family order
    (memo §3 G2): cancelling is risk-reducing so it is UNGATED by ALLOW_ORDERS
    (only PLACEMENT is gated), and it runs BEFORE the no-op return so an
    emergency stop takes the resting native trails off the book.

    Unlike the protection pass this takes NO ``records`` parameter: the watch
    state lives in the SEPARATE ``entry_trails.jsonl`` journal, never the
    submissions journal (the same reason :func:`_run_live_exits_pass` omits it).
    A no-op when the flag is unset/0.

    Every early return RELEASES the pass's "entry-watch" slice of the shared
    price-stream subscription (:func:`_release_feed_scope`): the feed build
    below is the scope's only writer, so skipping it after the last watch went
    terminal (or under KILL) would leave that watch's uic in the wire-level
    union forever — a live server-side subscription with zero consumers."""
    if kill:
        _cancel_working_entry_orders(deps, report)
        _release_feed_scope(deps, _FEED_SCOPE_ENTRY_WATCH)
        return
    d_bps = entry_trails.entry_trail_bps()
    if d_bps <= 0:
        _release_feed_scope(deps, _FEED_SCOPE_ENTRY_WATCH)
        return
    fold = entry_trails.read_entry_trail_fold()
    # Housekeeping BEFORE the empty-active early return: an all-terminal fold
    # is exactly the state whose stale routed ladders need retracting
    # (2026-08-19 adjudication finding 3). Self-contained error boundary; under
    # KILL the pass returned above, so a KILL-cancelled watch is swept on the
    # first non-KILL tick.
    _retract_stale_tranche_plans(fold, deps)
    active = _active_entry_watches(fold)
    if not active:
        deps.entry_watchers.clear()  # every watch went terminal — drop stale runtimes
        _release_feed_scope(deps, _FEED_SCOPE_ENTRY_WATCH)
        return
    # Prune runtimes whose crid terminated last tick (no longer in the fold's
    # active set) so a re-picked crid can never resurrect a stale watcher.
    for stale_crid in set(deps.entry_watchers) - set(active):
        deps.entry_watchers.pop(stale_crid, None)

    uic_to_instrument = {
        int(record["uic"]): (str(record["ticker"]), str(record["exchange_mic"]))
        for record in active.values()
        if _has_feed_context(record)
    }
    feed = _build_entry_watch_feed(deps, uic_to_instrument, report)
    uic_lows = _drain_session_lows(feed, uic_to_instrument)
    uic_points = _point_sample_bids(feed, uic_to_instrument)
    _reseed_vetoed_point_lows(feed, uic_points, uic_lows)
    now = dt.datetime.now(dt.UTC)
    for crid, record in active.items():
        _advance_one_entry_watch(
            deps, crid, record, fold.tiers.get(crid), uic_points, uic_lows, now, d_bps, report
        )


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
    A market discontinuity cannot hide inside that window for US equities: an
    LULD pause lasts >= 5 min (== ``STALE_FIRE_GAP``) and starts AFTER the last
    fresh sample, so every halt-spanning recovery arrives beyond the gate; the
    pre-open quiet spell is the overnight gap, far beyond it. Deliberately NO
    tick-count cap on the reseed chain: with the 3s point freshness bound vs
    the 45s tick, a thin change-driven stream (the OLN incident profile)
    point-vetoes MOST drain instants, so a multi-tick veto chain is the normal
    path a real touch survives — capping it would reintroduce the incident."""
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


def _build_entry_watch_feed(
    deps: LoopDeps, uic_to_instrument: Mapping[int, tuple[str, str]], report: TickReport
) -> PriceFeed:
    """Build this tick's price feed for the watching uics via the injected/
    default factory (the SAME source the exit pass uses, so entry and exit read
    identical prices), behind a boundary whose entire job is that NOTHING here
    can reach the tick. A construction failure degrades to a feed that vetoes
    every uic (no watch progress), exactly like an OFF flag or a stale quote —
    mirrors :func:`_build_live_exits_feed`."""
    feed_factory = deps.live_exits_feed_factory or _default_live_exits_feed_factory
    try:
        return feed_factory(uic_to_instrument, scope=_FEED_SCOPE_ENTRY_WATCH)
    # Broad on purpose (mirrors _build_live_exits_feed): a feed/network/auth
    # error becomes a veto, never a crash — the watches simply make no progress.
    except Exception as exc:
        if deps.alert_throttled(
            f"entry-watch: price feed construction failed — degrading to no-prices: {exc}",
            "entry-watch-feed-build-fail",
        ):
            report.alerts += 1
        return _NullPriceFeed()


def _advance_one_entry_watch(
    deps: LoopDeps,
    crid: str,
    record: Mapping[str, Any],
    tier_state: entry_trails.EntryTrailTierState | None,
    uic_points: Mapping[int, float | None],
    uic_lows: Mapping[int, float | None],
    now: dt.datetime,
    d_bps: int,
    report: TickReport,
) -> None:
    """Advance ONE watch: reconstruct-or-fetch its runtime, read the fresh
    reference price (folding in the tick's drained sub-tick running low),
    ``process`` one tick, persist the journal intents + terminal measurement,
    route the alerts, and drop the runtime once terminal. Per-watch fault
    isolation: an unreconstructable record is skipped."""
    runtime = _get_or_create_entry_runtime(deps, crid, record, tier_state)
    if runtime is None:
        return
    was_awaiting_fresh_low = runtime.watcher.awaiting_fresh_low
    price = _entry_watch_reference_price(uic_points, record)
    price = _combine_with_session_low(price, uic_lows, record, runtime, now)
    result = runtime.watcher.process(entry_trail_watcher.TickInput(now=now, price=price))
    # G6/G9 (memo §3): a SUSPENDED/EXPIRED terminal must never leave a resting
    # -entry- order alive on the book. Cancel-then-verify it BEFORE the terminal
    # is persisted — a fill that raced the cancel becomes `fired`, not the clock/
    # depth terminal (the fill is already covered by the fire-arm planned line).
    if _terminal_leaves_a_resting_order(result):
        result = _finalize_entry_terminal_vs_broker(deps, crid, result, report)
    _persist_entry_watch_result(crid, record, runtime, result, now, price, d_bps)
    # Persist the open-check clearance ON THE TRANSITION tick, BEFORE the arm
    # attempt below — the arm can fail transiently for many ticks, and only the
    # journal survives a restart (see _persist_open_check_clearance).
    if was_awaiting_fresh_low and not runtime.watcher.awaiting_fresh_low:
        _persist_open_check_clearance(record)
    # PR-T2b native arm: once TOUCHED (with a trustworthy price) PLACE the resting
    # Saxo trailing-LIMIT order out-of-band — the server ratchets + fires from
    # there. Re-attempted every TOUCHED tick until it arms (idempotent, dedup on
    # the -entry- family per G3) or is terminal-refused (insufficient funds, G7).
    if result.state is entry_trail_watcher.WatchState.TOUCHED and price is not None:
        _arm_native_trail(deps, crid, record, runtime, price, d_bps, report)
    for alert in result.alerts:
        if deps.alert_throttled(alert.message, alert.throttle_key):
            report.alerts += 1
    if runtime.watcher.is_terminal:
        deps.entry_watchers.pop(crid, None)


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


def _combine_with_session_low(
    price: float | None,
    uic_lows: Mapping[int, float | None],
    record: Mapping[str, Any],
    runtime: _EntryWatchRuntime,
    now: dt.datetime,
) -> float | None:
    """Fold this tick's drained 1 Hz sub-tick running low into the point-sampled
    reference so a wick BETWEEN the coarse 45s samples still registers a touch
    (touch-latch, entry_trailing_design §5). The drained low was popped once per
    uic by :func:`_drain_session_lows`; here it is only READ.

    Latch-then-gate-at-drain — the low is DISCARDED (point-sample alone drives
    the tick) in three cases, and only otherwise pulls the reference DOWN:

    - point-sample vetoed (``None``): never act on a latched low when the
      concurrent point-sample is itself untrusted/stale. This discard is NOT a
      destruction: :func:`_reseed_vetoed_point_lows` already handed the drained
      low back to the feed's accumulator (min-merge) before this per-tier
      combine ran, so it survives for a later fresh tick (2026-08-18 incident);
    - ``awaiting_fresh_low`` (a re-armed tier, memo G1): the open-check clear
      must be driven by the FRESH point-sampled bid only, so a stale overnight
      wick in the latch can never re-anchor the trigger and arm into a gap;
    - the latch is not trusted this tick (first fresh tick of the watch, or a
      ``> STALE_FIRE_GAP`` recovery that may span a session boundary): the
      running low could predate the watch or the session — see
      :meth:`EntryTierWatcher.latch_low_trusted`.
    """
    if price is None:
        return None
    if runtime.watcher.awaiting_fresh_low:
        return price
    if not runtime.watcher.latch_low_trusted(now):
        return price
    low = uic_lows.get(int(record["uic"]))
    if low is None:
        return price
    return min(price, low)


def _get_or_create_entry_runtime(
    deps: LoopDeps,
    crid: str,
    record: Mapping[str, Any],
    tier_state: entry_trails.EntryTrailTierState | None,
) -> _EntryWatchRuntime | None:
    """The daemon-lifetime runtime for ``crid``, reconstructing it from the
    journal fold on first sight (fresh watch OR post-restart). The trough is
    seeded from the fold's ``min_trough`` and the state from the latest
    non-terminal kind, so a restart never reseeds the trough upward (memo §5
    restart rule). ``None`` when the watch_open record is unreconstructable
    (logged once)."""
    existing = deps.entry_watchers.get(crid)
    if existing is not None:
        return existing
    config = _entry_watch_config_from_record(record)
    if config is None:
        return None
    seeded_trough = tier_state.min_trough if tier_state is not None else None
    initial_state = _entry_watch_initial_state(tier_state)
    runtime = _EntryWatchRuntime(
        watcher=entry_trail_watcher.EntryTierWatcher(
            config,
            seeded_trough=seeded_trough,
            initial_state=initial_state,
            # PR-T2b: the server ratchets + fires; the engine must not self-fire.
            native_trail=True,
            # memo §5 CRITICAL-2: a re-armed tier carries the open-check marker on
            # its re-appended watch_open, so it resumes with the arm BLOCKED until
            # a fresh post-open low re-anchors the stale carried trigger.
            awaiting_fresh_low=bool(record.get(_ENTRY_REARM_MARKER)),
        ),
        trough=seeded_trough,
    )
    deps.entry_watchers[crid] = runtime
    return runtime


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


def _persist_entry_watch_result(
    crid: str,
    record: Mapping[str, Any],
    runtime: _EntryWatchRuntime,
    result: entry_trail_watcher.TickResult,
    now: dt.datetime,
    price: float | None,
    d_bps: int,
) -> None:
    """Persist one tick's journal intents (memo §5 journals) plus, at a terminal,
    the measurement blob (memo §5 Measurement / T1d). The running trough + touch
    marks are mirrored into the runtime as they pass, so a later-tick terminal
    can stamp them; the ``touched`` line ALSO carries the touch price/ts inline
    (offline-join durability).

    PR-T2b: the engine never reaches WOULD_FIRE (native mode — the SERVER fires),
    so there is no fabricated ``fired`` line here. The real ``trail_armed`` (with
    the order id) and, on a fill, ``fired`` lines are written by the executor /
    reconcile out of band; this function only persists the engine's own
    touch/trough/suspend/expire/cancel intents."""
    for intent in result.journal_intents:
        payload = dict(intent.payload)
        if intent.kind == entry_trails.KIND_TROUGH:
            trough_value = payload.get("trough")
            if isinstance(trough_value, (int, float)):
                runtime.trough = float(trough_value)
        elif intent.kind == entry_trails.KIND_TOUCHED:
            runtime.touch_price = price
            runtime.touch_ts = now.isoformat()
            payload["touch_price"] = price
            payload["touch_ts"] = runtime.touch_ts
        line: dict[str, Any] = {"kind": intent.kind, "crid": intent.crid, **payload}
        if intent.kind in entry_trails.ENTRY_TRAIL_TERMINAL_KINDS:
            # A `fired` line (the G6 cancel-then-verify rewrite) carries the real
            # order id; every other terminal leaves it null (the offline reconcile
            # join fills it — memo §5 / T1d "join by order id").
            line["measurement"] = _entry_measurement_blob(
                record, runtime, d_bps, order_id=payload.get("order_id")
            )
        entry_trails.append_entry_trail_line(line)


def _persist_open_check_clearance(record: Mapping[str, Any]) -> None:
    """Make the open-check clear DURABLE (memo §5 CRITICAL-2 hardening).

    The engine clears ``awaiting_fresh_low`` in memory the tick a fresh
    post-open low forms, but the latest ``watch_open`` line still carries the
    re-arm marker — so a daemon restart between the clear and a SUCCESSFUL arm
    (the POST can keep failing transiently, or the geometry can stay degenerate
    for ticks) would re-seed the block from the fold and freeze the tier until
    a SECOND fresh low forms, silently dropping a session's only dip.

    Re-append the watch_open record WITHOUT the marker: the fold's
    latest-watch_open-wins semantics adopt it (no new state store, no fold
    change), so reconstruction reads the marker as absent. Every other field
    rides through verbatim; the fold's watch_open handler also resets
    ``armed_order_id``, which is correct here — the tier has no confirmed
    order yet (the arm attempt runs AFTER this persist on the same tick, and
    its ``trail_armed`` lines land later in the journal)."""
    if not record.get(_ENTRY_REARM_MARKER):
        return  # the latest watch_open already carries no marker
    cleared = {key: value for key, value in record.items() if key != _ENTRY_REARM_MARKER}
    entry_trails.append_entry_trail_line(cleared)


def _entry_measurement_blob(
    record: Mapping[str, Any],
    runtime: _EntryWatchRuntime,
    d_bps: int,
    *,
    order_id: str | None = None,
) -> dict[str, Any]:
    """The per-tier terminal measurement stamp (memo §5 / T1d): the variant-A
    entry (``tier_limit``), the touch price/ts, the final trough, the would-be
    trigger ``trough*(1+d)``, the order id (the REAL id on a ``fired`` line the
    G6 cancel-then-verify wrote; NULL on the other terminals — the offline
    reconcile join fills those by order id), and the ``entry_mode`` cohort tag
    (T8 poolability). Follows the ``tranche_fired`` telemetry-blob shape so the
    offline exec_quality join can compute concession / implied ΔR / fill-rate
    loss later."""
    trough = runtime.trough
    trigger = None if trough is None else trough * (1.0 + d_bps / _ENTRY_BPS_DENOMINATOR)
    limit = record.get("limit")
    return {
        "tier_limit": None if limit is None else float(limit),
        "touch_price": runtime.touch_price,
        "touch_ts": runtime.touch_ts,
        "final_trough": trough,
        "would_be_trigger": trigger,
        "order_id": order_id,
        "entry_mode": record.get("entry_mode") or _entry_trail_mode_tag(d_bps),
    }


# --- PR-T2b native trailing-limit executor -----------------------------------


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


_ENTRY_ORDER_REF_MARKER = "-entry-t"
"""The substring every entry-trail order ``ExternalReference`` carries (the crid
is ``<ticker>-<briefdate>-entry-t<i>`` and the fire id appends ``-fire``). The
KILL cancel + orphan sweep recognise a resting native ENTRY order by it (memo §3
G2 / §5 -entry- family) — distinct from the exit-leg ``-stop`` / ``-tp`` refs."""


def _entry_fire_request_id(crid: str) -> str:
    """The deterministic ``ExternalReference`` for a tier's native trailing order
    (memo §5 ``-entry-fire`` family). Deterministic so a crash-window re-POST hits
    Saxo's 15 s x-request-id dedup instead of resting a second trail; the ``crid``
    already encodes ``ticker-briefdate-entry-t<i>``, so the suffix keeps the whole
    id inside the ``-entry-`` family the KILL cancel + orphan sweep recognise."""
    return f"{crid}-fire"


def _cancel_working_entry_orders(deps: LoopDeps, report: TickReport) -> None:
    """KILL cleanup (memo §3 G2): cancel every working ``-entry-`` family order.

    Cancelling is risk-reducing, so it runs UNGATED by ALLOW_ORDERS (only
    PLACEMENT is gated) and independent of the flag. Best-effort + BROADLY
    guarded: a broker that cannot list its open orders — or the bare ``object()``
    broker some tests wire — is a silent no-op, never a crash that would starve
    the rest of the KILL tick. Only entry-side (BUY / unknown) orders qualify;
    the protective SELL disaster stop is left in place under KILL."""
    broker = deps.broker
    try:
        working = [
            state
            for state in broker.list_open_orders()
            if state.external_reference
            and _ENTRY_ORDER_REF_MARKER in state.external_reference
            and state.side != _DISASTER_STOP_SIDE
        ]
    # Broad on purpose (mirrors the orphan sweep / feed-build boundaries): a
    # list failure or a broker without the method must degrade to no-op.
    except Exception as exc:
        logger.debug("KILL entry-trail cancel: could not list open orders (%s)", exc)
        return
    if not working:
        return
    cancelled = _cancel_orders_best_effort(
        broker, [state.order_id for state in working], ticker="KILL:entry-trail"
    )
    if cancelled and deps.alert_throttled(
        f"KILL — cancelled {cancelled} working entry-trail order(s)",
        "entry-trail:kill-cancel",
    ):
        report.alerts += 1


def _journal_trail_armed(crid: str, *, order_id: str | None, trigger: float) -> None:
    """Append one ``trail_armed`` line (the G3 write-ahead uses ``order_id=None``
    before the POST; the post-POST line fills the real id in — the fold's
    latest-wins semantics adopt it)."""
    entry_trails.append_entry_trail_line(
        {
            "kind": entry_trails.KIND_TRAIL_ARMED,
            "crid": crid,
            "order_id": order_id,
            "trigger": float(trigger),
        }
    )


def _journal_entry_planned_disaster(record: Mapping[str, Any], uic: int, entry_crid: str) -> None:
    """Journal the ``planned`` disaster-SL line at FIRE-ARM (memo §5 never-naked):
    the brief disaster-stop PRICE the broker cannot know, keyed to the resting
    order's ``ExternalReference`` so that when the trail fills into a Position the
    UNCHANGED protection pass (``build_protection_view`` + ``reconcile_protection``)
    places the covering SELL disaster stop with ZERO new protection code — a fill
    during daemon downtime is naked for at most one tick. ``take_profit`` is left
    ``None`` (stop-only disaster protection; the in-band OCO upgrade is a later
    increment) — a null TP still confers the disaster stop, which is the
    never-naked guarantee."""
    disaster_stop = record.get("disaster_stop")
    if disaster_stop is None:
        logger.warning(
            "entry-trail arm: %s watch_open carries no disaster_stop — "
            "SKIPPING the trailing order so a fill can never go uncovered",
            entry_label_from_crid(entry_crid),
        )
        raise _EntryArmAbortError  # never place a trail we cannot cover (never-naked)
    tier_index = record.get("tier_index")
    _append_standalone_stop_journal(
        _build_planned_line(
            entry_crid=entry_crid,
            uic=int(uic),
            side=_DISASTER_STOP_SIDE,
            stop_price=float(disaster_stop),
            take_profit=None,
            tier_index=int(tier_index) if tier_index is not None else 0,
            # 2026-08-19 incident fix: the geometry blob stamped on the
            # watch_open at routing time rides through to the planned line so
            # _reanchor_facts_from_governing can recover (k_atr, atr) and the
            # position actually trails. Absent on old lines -> None -> the
            # planned line stays byte-identical to today.
            geometry_stamp=record.get("geometry"),
        )
    )


class _EntryArmAbortError(Exception):
    """Internal control-flow signal: abort arming this tick WITHOUT placing an
    order (a missing never-naked plan price). Caught inside :func:`_arm_native_trail`
    — never escapes to the tick loop."""


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
    :func:`_geometry_shadow_stamp` wrote at routing time, whose ``geometry_tp``
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
    uic = _coerce(record, "uic", int)
    if uic is None:
        return None, _ArmRefusal(
            "entry watch carries no uic — the exit plan cannot be resolved",
            _ARM_REFUSAL_EXIT_PLAN_SHAPE,
            terminal=True,
        )
    try:
        plan = fold_tranche_plans(_iter_standalone_stop_journal()).get(uic)
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

    Since #1183 both daemons run a no-geometry exit policy
    (``applies_geometry=False``): the placed exit is the brief's multi-tranche
    ladder, ``_stamped_exit_target`` returns ``None``, and the two geometry-
    scoped gates above price nothing. This gate closes that hole with the same
    issue-#1112 condition, evaluated against the plan that will ACTUALLY govern
    the position:

        refuse unless  tp1 > fill_estimate + round_trip_cost + E_min

    where tp1 is the shallowest ACTIVE tranche of the journaled ``tranche_plan``
    and the cost is priced at THAT tranche's apportioned share count — the exact
    quantity ``live_exit_engine.plan_tranche_exits`` will sell there, so the arm
    bar and the exit bar coincide for the tranche this gate prices.

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


def _terminal_refuse_arm(
    deps: LoopDeps,
    crid: str,
    record: Mapping[str, Any],
    runtime: _EntryWatchRuntime,
    d_bps: int,
    note: str,
    report: TickReport,
    *,
    reason: str,
) -> None:
    """Terminal-refuse this tier (``KIND_CANCELLED`` + ``watcher.cancel()``),
    mirroring the G7 insufficient-funds refuse in :func:`_handle_arm_failure` —
    both refusal conditions are properties of the pick's own journaled state, so
    retrying them every 45 s tick would only spam. ``reason`` keys the alert
    throttle so the two refusals never suppress each other.

    The caller must have a SUCCESSFUL open-order read in hand: this terminal is
    outside ``_RESTING_BEARING_TERMINALS``, so nothing would cancel-then-verify
    an order that turned out to be resting after all.
    """
    entry_trails.append_entry_trail_line(
        {
            "kind": entry_trails.KIND_CANCELLED,
            "crid": crid,
            "note": note,
            "measurement": _entry_measurement_blob(record, runtime, d_bps),
        }
    )
    runtime.watcher.cancel()
    if deps.alert_throttled(
        f"entry-trail {entry_label_from_crid(crid)}: {note} — tier refused",
        f"entry-trail:{reason}:{crid}",
    ):
        report.alerts += 1


def _arm_native_trail(
    deps: LoopDeps,
    crid: str,
    record: Mapping[str, Any],
    runtime: _EntryWatchRuntime,
    reference: float,
    d_bps: int,
    report: TickReport,
) -> None:
    """Place ONE native Saxo trailing-LIMIT order at the TOUCH (memo §2 V1, §5).

    Sequence (money-critical ORDER):
      1. capability + ALLOW_ORDERS gates -> place nothing when disabled;
      2. compute the tick-agnostic geometry (the broker tick-aligns at placement);
      3. idempotent re-arm: adopt an already-resting ``-entry-`` order (G3);
      4. G3 write-ahead: ``trail_armed`` (null id) BEFORE the POST;
      5. never-naked: the ``planned`` disaster-SL line at FIRE-ARM;
      6. the POST (ALLOW_ORDERS-gated at the broker too);
      7. fill the real order id into a second ``trail_armed`` line, mark armed.

    A rejected POST leaves the tier TOUCHED (retry next tick) unless it is
    insufficient funds (G7 -> terminal-refuse so it never spams retries)."""
    broker = deps.broker
    if not isinstance(broker, SupportsTrailingStop) or not _entry_trail_orders_allowed():
        return
    # memo §5 CRITICAL-2 open-check: a re-armed tier (a DayOrder cancelled at the
    # prior session close) carries a STALE overnight trough — block the fresh arm
    # until a NEW post-open low re-anchors the trigger, so the stale trigger is
    # never handed to the broker into a gap. The tier stays TOUCHED and re-attempts
    # every tick; the engine clears the flag the tick a fresh low forms.
    if runtime.watcher.awaiting_fresh_low:
        return
    trough = runtime.trough if runtime.trough is not None else reference
    geo = entry_trail_geometry.compute_trailing_order_geometry(
        reference=reference, trough=trough, d_bps=d_bps
    )
    if geo is None:
        return  # degenerate geometry — retry next tick
    try:
        uic = int(record["uic"])
        qty = float(record["qty"])
    except (KeyError, TypeError, ValueError):
        logger.warning(
            "entry-trail arm: %s record missing uic/qty — skipped", entry_label_from_crid(crid)
        )
        return
    fire_rid = _entry_fire_request_id(crid)

    lookup = _find_working_entry_order(broker, fire_rid)
    if lookup.order_id is not None:
        _journal_trail_armed(crid, order_id=lookup.order_id, trigger=geo.order_price)
        runtime.watcher.mark_armed()
        return

    # AFTER the adopt (issue #1112 step 1): refusing before it would terminate
    # the watch while a real buy order still rests at the broker, and
    # KIND_CANCELLED is outside _RESTING_BEARING_TERMINALS so nothing would
    # cancel-then-verify it. Only a FRESH arm is refused.
    region_note = _inside_exit_region_note(record, d_bps, reference, trough, qty)
    refusal = (
        _ArmRefusal(region_note, _ARM_REFUSAL_INSIDE_EXIT_REGION, terminal=True)
        if region_note is not None
        # The whole-position pricing the gate above just did is only
        # conservative while the exit side sells the whole position in one
        # tranche. Checked, never assumed (issue #1112 round 2, point 2).
        else _exit_plan_shape_refusal(record, qty)
    )
    if refusal is None:
        # Both gates above are scoped to an APPLIED geometry target. Under a
        # no-geometry exit policy (breakeven_trail, since #1183) the placed
        # exit is the brief's own ladder — priced by its own gate instead.
        refusal = _brief_plan_arm_refusal(record, d_bps, reference, trough)
    if refusal is not None:
        if lookup.read_ok and refusal.terminal:
            _terminal_refuse_arm(
                deps, crid, record, runtime, d_bps, refusal.note, report, reason=refusal.reason
            )
        else:
            # Either the book read FAILED — so "no order rests" is a fact we do
            # not have, a prior tick's POST could be resting, and this terminal
            # does no cancel-then-verify — or the refusal itself is not a
            # verdict yet. Neither terminate nor arm a tier we cannot clear:
            # stay TOUCHED and settle on a tick that can read what it needs.
            logger.warning(
                "entry-trail %s: %s — deferring the refusal",
                entry_label_from_crid(crid),
                refusal.note,
            )
        return

    try:
        # NEVER-NAKED first: journal the planned disaster-SL line BEFORE the
        # write-ahead + POST, so a malformed record (no disaster price -> abort)
        # never strands the tier as arm-in-progress, and the covering plan is on
        # disk before any order can exist.
        _journal_entry_planned_disaster(record, uic, fire_rid)
    except _EntryArmAbortError:
        return
    _journal_trail_armed(crid, order_id=None, trigger=geo.order_price)  # G3 write-ahead, pre-POST
    try:
        placed = broker.place_trailing_stop(
            uic,
            _ENTRY_SIDE,
            qty,
            order_price=geo.order_price,
            trailing_distance=geo.trailing_distance,
            trailing_step=geo.trailing_step,
            ceiling_price=geo.ceiling_price,
            request_id=fire_rid,
        )
    except BrokerError as exc:
        _handle_arm_failure(deps, crid, record, runtime, d_bps, exc, report)
        return
    _journal_trail_armed(crid, order_id=placed.entry_order_id, trigger=geo.order_price)
    runtime.watcher.mark_armed()
    logger.info(
        "entry-trail %s: armed native trailing order %s @ trigger %.4f (ceiling %.4f)",
        entry_label_from_crid(crid),
        placed.entry_order_id,
        geo.order_price,
        geo.ceiling_price,
    )


def _handle_arm_failure(
    deps: LoopDeps,
    crid: str,
    record: Mapping[str, Any],
    runtime: _EntryWatchRuntime,
    d_bps: int,
    exc: BrokerError,
    report: TickReport,
) -> None:
    """A rejected trailing-order POST: insufficient funds (memo §3 G7) is
    TERMINAL — refuse the tier so it never re-arms to spam retries, releasing its
    virtual reservation; any other error keeps the tier TOUCHED (arm-in-progress
    write-ahead already journaled) to retry next tick."""
    if _is_insufficient_funds(exc):
        entry_trails.append_entry_trail_line(
            {
                "kind": entry_trails.KIND_CANCELLED,
                "crid": crid,
                "note": f"insufficient funds at fire-arm: {exc}",
                "measurement": _entry_measurement_blob(record, runtime, d_bps),
            }
        )
        runtime.watcher.cancel()
        if deps.alert_throttled(
            f"entry-trail {entry_label_from_crid(crid)}: insufficient funds at fire-arm — tier refused",
            f"entry-trail:nofunds:{crid}",
        ):
            report.alerts += 1
        return
    if deps.alert_throttled(
        f"entry-trail {entry_label_from_crid(crid)}: trailing-order POST failed — will retry: {exc}",
        f"entry-trail:arm-fail:{crid}",
    ):
        report.alerts += 1


# The engine terminals that can strand a resting native order at the broker: a
# tier that hit a G9 deep-decline (SUSPENDED) or its TTL window (EXPIRED) while
# still arm-in-progress (a G3 null-id write-ahead whose POST rested at the broker
# but whose id-journal was lost). WOULD_FIRE never occurs in native mode.
#
# CANCELLED has three producers, none of which needs the cancel-then-verify here:
# the KILL and insufficient-funds paths do their own broker cancel, and the
# #1112 inside-the-exit-region refuse runs only AFTER _find_working_entry_order
# came back empty, so there is no order of ours to cancel. That third path rests
# on the open-order read being complete: a broker read that fails SOFT (returns
# an empty book instead of raising) while a crashed prior tick's POST is resting
# would terminal-refuse and leave that order untracked. _find_working_entry_order
# raises on a BrokerError rather than returning None, which is what keeps the
# assumption true today.
_RESTING_BEARING_TERMINALS = frozenset(
    {entry_trail_watcher.WatchState.SUSPENDED, entry_trail_watcher.WatchState.EXPIRED}
)


def _terminal_leaves_a_resting_order(result: entry_trail_watcher.TickResult) -> bool:
    """Whether this tick's engine terminal (SUSPENDED/EXPIRED) could have left a
    native ``-entry-`` order resting at the broker (memo §3 G6/G9)."""
    return result.state in _RESTING_BEARING_TERMINALS


def _read_entry_order(broker: Broker, order_id: str) -> OrderState | None:
    """Re-read one order for the cancel-then-verify (memo §3 G6). ``None`` on any
    doubt (a ``BrokerError`` reading the book) — a verify we cannot complete must
    not manufacture a phantom ``fired``; the cancel already reduced exposure."""
    try:
        return broker.get_order(order_id)
    except BrokerError as exc:
        logger.warning(
            "entry-trail terminal: get_order(%s) failed for the fill re-read (%s)", order_id, exc
        )
        return None


def _entry_order_filled_qty(state: OrderState | None) -> float | None:
    """The FILLED quantity of a re-read order, or ``None`` when it is not (yet)
    filled. A ``FILLED``/``PARTIALLY_FILLED`` status means the cancel raced a
    fill (memo §3 G6/G8): the tier is a ``fired`` fill, not the clock/depth
    terminal it became in the engine."""
    from broker_contract.contract import OrderStatus

    if state is None:
        return None
    if state.status not in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED):
        return None
    filled = state.filled_quantity
    return float(filled) if isinstance(filled, (int, float)) and filled > 0 else None


def _finalize_entry_terminal_vs_broker(
    deps: LoopDeps,
    crid: str,
    result: entry_trail_watcher.TickResult,
    report: TickReport,
) -> entry_trail_watcher.TickResult:
    """Cancel-then-verify a resting ``-entry-`` order before its SUSPENDED/EXPIRED
    terminal is journaled (memo §3 G6/G9).

    Returns the (possibly rewritten) tick result: unchanged when no order rests
    (the common path), or with the terminal intent REWRITTEN to ``fired`` when the
    re-read shows the cancel raced a fill (the fill is already covered by the
    fire-arm planned disaster line — journalling ``suspended``/``expired`` against
    a live fill would be the G6 violation). The cancel itself is best-effort +
    risk-reducing (ungated by ALLOW_ORDERS, like the KILL cancel)."""
    broker = deps.broker
    if not isinstance(broker, SupportsTrailingStop):
        return result  # a non-trailing broker never armed — nothing can rest
    fire_rid = _entry_fire_request_id(crid)
    existing = _find_working_entry_order(broker, fire_rid).order_id
    if existing is None:
        return result  # nothing resting — the terminal stands as the engine set it
    # Cancel FIRST, then re-read (memo §3 G6 cancel-then-verify).
    _cancel_orders_best_effort(broker, [existing], ticker=f"entry-trail:{result.state.value}")
    filled_qty = _entry_order_filled_qty(_read_entry_order(broker, existing))
    if filled_qty is not None:
        return _entry_terminal_rewritten_as_fired(crid, result, existing, filled_qty)
    if deps.alert_throttled(
        f"entry-trail {entry_label_from_crid(crid)}: cancelled resting trail {existing} "
        f"on {result.state.value}",
        f"entry-trail:terminal-cancel:{crid}",
    ):
        report.alerts += 1
    return result


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


# --- PR-T2b fill-reconcile of a resting armed trail (Finding 1) --------------
#
# Once a tier reaches TRAIL_ARMED with a real order id the watch pass drops it
# (_active_entry_watches :1261 — the broker owns the resting native order), so
# nothing else ever observes its fill / DayOrder-expiry. Without a terminal
# `entry_trails` line watching_virtual_gross_acct keeps reserving limit*qty
# FOREVER (it skips only terminal_kind) AND _open_watch_pick_keys keeps the tier
# occupying capacity forever — the feature arms one pick then jams. This sibling
# pass writes the terminal `fired` line when the order fills, releasing both in
# ONE write. It NEVER places / amends / arms (safe under KILL); a GONE-but-
# UNFILLED order (DayOrder expiry / raced cancel) is LEFT for the Rearm phase.


def _resting_armed_tiers(
    fold: entry_trails.EntryTrailFold,
) -> dict[str, entry_trails.EntryTrailTierState]:
    """The tiers this reconcile pass owns: NON-terminal, latest kind
    ``trail_armed``, with a REAL ``armed_order_id`` — the exact complement of the
    resting-order exclusion in :func:`_active_entry_watches` (the watch pass drops
    these because the broker owns the resting order, so nothing else observes their
    fill / DayOrder-expiry). The Rearm phase (Finding 2) consumes the SAME set to
    find a gone-but-unfilled (DayOrder-cancelled) tier to re-admit."""
    return {
        crid: state
        for crid, state in fold.tiers.items()
        if state.terminal_kind is None
        and state.latest_kind == entry_trails.KIND_TRAIL_ARMED
        and state.armed_order_id is not None
    }


def _run_entry_trail_reconcile_pass(deps: LoopDeps, report: TickReport) -> None:
    """Reconcile every resting armed ``-entry-`` trailing order against the broker
    (memo §5 Finding 1 — the gross-reservation leak fix).

    Runs UNGATED by KILL (like the live-exits pass): a fill that lands during an
    emergency stop must still release its virtual reservation + be covered by the
    fire-arm planned disaster line, not freeze until KILL clears. The pass writes
    ONLY terminals — it NEVER places, amends, or arms an order — so it is safe
    under KILL. A no-op when the flag is unset/0 (byte-identical to today) or when
    the broker cannot classify a disappeared order (no ``SupportsOrderResolution``
    — the audit-log read is the ONLY fill-vs-expiry disambiguator, memo trap #4).

    THIS phase handles ONLY the FILLED -> ``fired`` transition and the
    still-working no-op; a GONE-but-UNFILLED order (a DayOrder expiry or a raced
    cancel) is LEFT for the Rearm phase (Finding 2) — never terminated here, so no
    terminal is ever written against a re-armable tier (memo §5 CRITICAL-2)."""
    d_bps = entry_trails.entry_trail_bps()
    if d_bps <= 0:
        return
    broker = deps.broker
    if not isinstance(broker, SupportsOrderResolution):
        return
    fold = entry_trails.read_entry_trail_fold()
    now = dt.datetime.now(dt.UTC)
    for crid, tier_state in _resting_armed_tiers(fold).items():
        _reconcile_one_armed_tier(deps, crid, tier_state, d_bps, now, report)


@dataclass(frozen=True)
class _StandingStop:
    """One journaled standalone stop the fill-reconcile pass can still act on:
    the broker ``order_id`` to compare against the open-orders book and the
    deterministic ``ref`` that renders the operator label (may be ``None`` only
    for records written before the ``ref`` field existed)."""

    order_id: str
    ref: str | None


def _fold_standing_stop_ids(lines: Iterable[Mapping[str, Any]]) -> dict[int, _StandingStop]:
    """Fold ``stop_placed`` / ``stop_filled`` lines into the per-uic standing
    stop the fill-reconcile pass should watch (#1219).

    Per uic the LATEST (by ``ts``, later line breaks a tie) ``stop_placed`` wins
    OUTRIGHT; if that elected record carries no ``order_id`` (written before
    #1219) the uic yields NO candidate — its id was never journaled, so it can
    never be reconciled. Electing latest-overall (not latest-WITH-id) keeps this
    fold exactly equivalent under the boot compactor, which keeps only the
    newest ``stop_placed`` per uic — a latest-with-id election would survive
    compaction differently whenever an id-less record is newer. A
    ``stop_filled`` line whose ``order_id`` matches the elected candidate removes
    the uic: the terminal already exists, so the pass must not re-resolve or
    re-alert it (restart idempotence, mirroring the entry-side ``fired`` fold)."""
    latest_ts: dict[int, float] = {}
    latest: dict[int, _StandingStop | None] = {}
    filled_ids: set[str] = set()
    for line in lines:
        kind = line.get("kind")
        if kind == "stop_placed":
            try:
                uic = int(line["uic"])
                ts = float(line["ts"])
            except (KeyError, TypeError, ValueError):
                continue
            if uic not in latest_ts or ts >= latest_ts[uic]:
                latest_ts[uic] = ts
                order_id = line.get("order_id")
                if isinstance(order_id, str) and order_id:
                    ref = line.get("ref")
                    latest[uic] = _StandingStop(
                        order_id=order_id, ref=ref if isinstance(ref, str) and ref else None
                    )
                else:
                    latest[uic] = None
        elif kind == "stop_filled":
            order_id = line.get("order_id")
            if isinstance(order_id, str) and order_id:
                filled_ids.add(order_id)
    return {
        uic: stop
        for uic, stop in latest.items()
        if stop is not None and stop.order_id not in filled_ids
    }


def _run_stop_fill_reconcile_pass(deps: LoopDeps, report: TickReport) -> None:
    """Reconcile every journaled standing standalone stop against the broker and
    announce a FILL (#1219 — the silent-exit gap).

    Mirrors ``_run_entry_trail_reconcile_pass`` exactly: journaled order id ->
    gone from the open-orders book -> ONE budgeted audit-log resolution -> on a
    FILLED outcome the terminal ``stop_filled`` line FIRST (the idempotence
    latch), then ONE throttled operator alert. Runs UNGATED by KILL (a stop that
    fills during an emergency stop must still be announced; the pass writes only
    terminals — it never places, amends or cancels) and needs no feature flag:
    it is a no-op whenever no journaled stop id is outstanding.

    Rotation-race safety (the false "chain lost" class): an id gone from the
    book is NOT evidence of a fill — daily order rotation, an OCO-upgrade
    supersede-cancel and a manual cancel all look identical there. Only a
    resolved FILLED outcome acts; every other outcome is left untouched (the
    protection pass owns re-covering a still-open position, and the superseding
    ``stop_placed`` line re-points the fold at the replacement id).

    The reused ``_read_entry_order`` / ``_resolve_entry_order_outcome`` /
    ``_entry_order_filled_qty`` helpers are named for their entry-trail origin
    but operate on ANY order id — deliberate reuse of the battle-tested logic
    over a renamed duplicate."""
    from broker_contract.contract import OrderStatus

    broker = deps.broker
    if not isinstance(broker, SupportsOrderResolution):
        return
    lines = list(_iter_standalone_stop_journal())
    standing = _fold_standing_stop_ids(lines)
    if not standing:
        return
    plan_pick_keys = _fold_governing_plan_pick_keys(lines)
    for uic in sorted(standing):
        stop = standing[uic]
        if not _armed_order_is_gone(_read_entry_order(broker, stop.order_id)):
            continue
        if not _acquire_outcome_audit_budget(
            deps, broker, stop.order_id, context="stop-fill reconcile"
        ):
            continue
        outcome = _resolve_entry_order_outcome(broker, stop.order_id)
        filled_qty = _entry_order_filled_qty(outcome)
        if filled_qty is None:
            continue
        avg_price = outcome.avg_fill_price if outcome is not None else None
        # PARTIALLY_FILLED terminal = the remainder was cancelled with residual
        # exposure still on the book — announce the fill, never treat it as a
        # round trip (zen MEDIUM on #1222). FILLED = the whole resting stop
        # (protection keeps SL == owned) executed: the position is closed.
        partial = outcome is not None and outcome.status is not OrderStatus.FILLED
        _journal_stop_filled(
            uic,
            order_id=stop.order_id,
            qty=filled_qty,
            avg_price=avg_price,
            ref=stop.ref,
            partial=partial,
        )
        # The stop ref is `<entry_crid>-stop-<gen>` (position_manager._exit_stop_ref)
        # — no E{n}/TP{n} shape to render, so the operator label is the ticker
        # prefix (labels doctrine: never a raw machine ref in message text).
        label = stop.ref.split("-", 1)[0] if stop.ref else f"uic {uic}"
        price_text = f" @ {avg_price:g}" if avg_price is not None else ""
        if deps.alert_throttled(
            f"exit: {label} stop {stop.order_id} filled {filled_qty:g} shares{price_text}",
            f"stop-fill:{stop.order_id}",
        ):
            report.alerts += 1
        if partial:
            continue
        # #1198 option B: the round trip is over — retire the pick's still-open
        # sibling entry watches so their virtual gross reservation and watch
        # slot free NOW instead of at the entry TTL. The restart-safe sweep
        # (_sweep_owed_sibling_retires) re-derives this from the journal, so a
        # crash between the stop_filled write and this call self-heals.
        # Ref first: the stop ref is stamped from the exact PlannedExit that
        # owned THIS stop (generation-exact); the tranche_plan fold is uic-keyed
        # last-wins and could in principle point at a newer pick on a reused
        # uic. The fold is the fallback for a ref-less legacy record.
        pick_key = _pick_key_from_stop_ref(stop.ref) or plan_pick_keys.get(uic)
        _retire_sibling_watches(deps, pick_key, report, trigger=f"stop {stop.order_id} filled")


def _sweep_owed_sibling_retires(deps: LoopDeps, report: TickReport) -> None:
    """Restart-safe backstop for the #1198 sibling retire (zen HIGH on #1222).

    The inline retires in the stop-fill and live-exits passes are side effects
    AFTER their durable trigger records (``stop_filled`` /
    ``tranche_fired[position_closed]``) — a crash in that window would
    otherwise leave a sibling watch open forever (the stop-fill latch removes
    the uic from the reconcile fold, so the inline call never retries). This
    sweep re-derives the owed picks from the journal every tick and calls the
    idempotent ``cancel_open_watches`` machinery; once a pick's tiers are all
    terminal the call matches nothing, writes nothing and alerts nothing, so
    the steady state is a cheap no-op.

    Attribution (#1230): a parsed stop ref is generation-exact — it names the
    pick that owned THAT stop, so the ref-first branch stays a whole-journal
    walk (retiring an old pick's siblings off its own ref is correct even
    after uic reuse; a fully-terminal pick matches nothing). KEYLESS evidence
    (``tranche_fired[position_closed]`` and a ref-less ``stop_filled``) has
    only the uic, and the governing fold is last-wins — so it is attributable
    ONLY within the plan generation that wrote it. The generation-scoped
    closures fold drops evidence a newer ``tranche_plan`` / retraction has
    reset, and within an open generation the current governing pick IS the
    pick that closed (both folds consume the same plan lines in the same
    order), so the last-wins map becomes correct once scoped. This is the
    same semantics the boot compactor already applies to these lines."""
    owed: dict[str, str] = {}
    lines = list(_iter_standalone_stop_journal())
    for line in lines:
        if line.get("kind") == "stop_filled" and not line.get("partial"):
            ref = line.get("ref")
            pick_key = _pick_key_from_stop_ref(ref if isinstance(ref, str) else None)
            if pick_key is not None:
                owed.setdefault(pick_key, "stop fill on record")
    plan_pick_keys = _fold_governing_plan_pick_keys(lines)
    for uic, closure_keys in _fold_round_trip_closures_since_latest_plan(lines).items():
        # str elements are parsed stop refs the ref-first walk above already
        # collected; only the keyless None element needs the governing fold.
        if None in closure_keys:
            pick_key = plan_pick_keys.get(uic)
            if pick_key is not None:
                owed.setdefault(pick_key, "position-closing tranche on record")
    for pick_key in sorted(owed):
        _retire_sibling_watches(deps, pick_key, report, trigger=owed[pick_key])


def _pick_key_from_stop_ref(ref: str | None) -> str | None:
    """Recover the colon-form pick key from a stop ref (fallback when the uic
    has no ``tranche_plan`` ``pick_key`` on record).

    The entry-trail stop ref is ``<ticker>-<brief_date>-entry-t<i>-stop-<gen>``
    (``position_manager._exit_stop_ref`` over the watch crid). A classic
    bracket ref has a different shape and returns ``None`` — the caller treats
    that as "no entry-trail siblings exist", which is true by construction."""
    if not ref or "-entry-t" not in ref:
        return None
    prefix = ref.split("-entry-t", 1)[0]  # "<ticker>-<YYYY-MM-DD>"
    # rpartition: the DATE is the fixed-shape tail; the ticker may itself carry
    # a hyphen (yfinance-style class shares, e.g. BRK-B) — zen LOW on #1222.
    head, sep, day = prefix.rpartition("-")
    head, sep2, month = head.rpartition("-")
    ticker, sep3, year = head.rpartition("-")
    if not (sep and sep2 and sep3 and ticker):
        return None
    brief_date = f"{year}-{month}-{day}"
    try:
        dt.date.fromisoformat(brief_date)
    except ValueError:
        return None
    return f"{ticker}:{brief_date}"


def _retire_sibling_watches(
    deps: LoopDeps, pick_key: str | None, report: TickReport, *, trigger: str
) -> None:
    """Cancel the pick's still-open sibling entry-watch tiers after a
    round-trip close (#1198, option B — backtest in the issue: exercised
    post-round-trip re-entries net R -0.03..-0.07 while a freed slot earns
    +0.365R under the same policy).

    Reuses ``entry_trails.cancel_open_watches`` (the disarm machinery):
    journal-terminal writes only, idempotent (already-terminal tiers never
    match), and refuse-first on a tier with a resting armed BUY — v1 SKIPS
    that pick with an alert rather than cancel-then-verifying the broker
    order. ``pick_key is None`` means a bracket-path position with no
    entry-trail siblings — a quiet no-op."""
    if pick_key is None:
        return
    try:
        cancelled = entry_trails.cancel_open_watches(
            pick_key, note=f"sibling retire: position round-tripped ({trigger}) — #1198"
        )
    except entry_trails.DisarmRestingOrderError as exc:
        if deps.alert_throttled(
            f"entry-watch: sibling retire of {pick_key} skipped — {exc}",
            f"sibling-retire-armed:{pick_key}",
        ):
            report.alerts += 1
        return
    except OSError as exc:
        # A journal write failure (ENOSPC, permissions) must never escape the
        # tick — it would starve the protection pass (zen MEDIUM on #1222).
        # The restart-safe sweep retries next tick off the durable record.
        if deps.alert_throttled(
            f"entry-watch: sibling retire of {pick_key} failed — journal write error: {exc}",
            f"sibling-retire-io:{pick_key}",
        ):
            report.alerts += 1
        return
    if cancelled and deps.alert_throttled(
        f"entry-watch: retired {len(cancelled)} sibling tier(s) of {pick_key} "
        f"after round-trip ({trigger})",
        f"sibling-retire:{pick_key}",
    ):
        report.alerts += 1


def _reconcile_one_armed_tier(
    deps: LoopDeps,
    crid: str,
    tier_state: entry_trails.EntryTrailTierState,
    d_bps: int,
    now: dt.datetime,
    report: TickReport,
) -> None:
    """Resolve ONE resting armed tier's order and act on its outcome:

    - a FILL -> the terminal ``fired`` line (releasing the reservation + un-jamming
      capacity, Finding 1);
    - a DayOrder gone UNFILLED (resolve -> EXPIRED / CANCELLED at the session
      close) -> RE-ARM within the ORIGINAL TTL, or terminal ``expired`` past it
      (Finding 2 / memo §5 CRITICAL-2, delegated to
      :func:`_rearm_or_expire_gone_tier`);
    - still resting, or an ambiguous / unreadable order -> a no-op this tick (retry
      next tick — never a fabricated terminal, memo §3 G6)."""
    order_id = tier_state.armed_order_id
    if order_id is None:  # defensive — _resting_armed_tiers already filtered
        return
    broker = deps.broker
    if not isinstance(broker, SupportsOrderResolution):
        # Defensive re-narrow (#1141): the ONLY caller
        # (_run_entry_trail_reconcile_pass) already gates on this, but a guard
        # that fires on one entry point only is a guard that can be walked past
        # — this helper's guarantee is now its own, and the narrowing survives
        # the re-read of deps.broker.
        return
    # Still resting on the open-orders book -> nothing to reconcile (the server is
    # ratcheting / waiting for the bounce). get_order answers WORKING while it
    # rests and UNKNOWN once it DISAPPEARS (Saxo drops filled/expired/cancelled
    # from the open-orders view) — the disappearance is the only trigger to
    # disambiguate via the audit log.
    if not _armed_order_is_gone(_read_entry_order(broker, order_id)):
        return
    # GONE from the book — one audit-log read disambiguates fill vs expiry/cancel
    # (memo trap #4: get_order alone reads UNKNOWN for ALL of them). The read
    # bills the SHARED per-tick audit budget (audit-429 memo §3): over budget it
    # defers to the next tick — the SAME no-op-retry contract as an unreadable
    # audit below, never a fabricated terminal.
    if not _acquire_outcome_audit_budget(deps, broker, order_id):
        return
    outcome = _resolve_entry_order_outcome(broker, order_id)
    filled_qty = _entry_order_filled_qty(outcome)
    if filled_qty is None:
        # A GONE-but-UNFILLED order: the DayOrder cancelled at the session close.
        # The Rearm path (Finding 2 / memo §5 CRITICAL-2) re-admits it within the
        # ORIGINAL TTL or expires it past window_end — NEVER a fabricated fill; a
        # still-unresolved UNKNOWN defers (verify-before-terminal, memo §3 G6).
        _rearm_or_expire_gone_tier(deps, crid, tier_state, outcome, d_bps, now, report)
        return
    _journal_entry_fired(
        crid,
        tier_state,
        d_bps,
        now,
        order_id=order_id,
        realized_qty=filled_qty,
        avg_price=outcome.avg_fill_price if outcome is not None else None,
    )
    if deps.alert_throttled(
        f"entry-trail {entry_label_from_crid(crid)}: native trail {order_id} "
        f"filled {filled_qty:g} shares -> fired",
        f"entry-trail:fired:{crid}",
    ):
        report.alerts += 1


def _armed_order_is_gone(state: OrderState | None) -> bool:
    """Whether a re-read armed order has LEFT the open-orders book (memo trap #4).

    ``None`` (a ``BrokerError`` re-reading) -> NOT gone: a read we cannot complete
    must not trigger the audit-log disambiguation (retry next tick). A
    ``WORKING``/``PARTIALLY_FILLED`` status means it still rests — the residual of
    a partial keeps its FULL virtual reservation this phase (G8 residual-cancel is
    a later increment). Any other status (``UNKNOWN`` once Saxo drops it, or a
    terminal a non-Saxo broker surfaces on ``get_order``) means gone -> resolve."""
    from broker_contract.contract import OrderStatus

    if state is None:
        return False
    return state.status not in (OrderStatus.WORKING, OrderStatus.PARTIALLY_FILLED)


def _acquire_outcome_audit_budget(
    deps: LoopDeps, broker: Broker, order_id: str, *, context: str = "entry-trail reconcile"
) -> bool:
    """One draw on the SHARED per-tick audit-read budget (audit-429 memo §3).

    A memoized terminal (``SupportsOutcomeCachePeek``) resolves budget-free —
    no audit HTTP read happens. Over budget: count + log the deferral (the
    non-alerting ``VERDICT_AUDIT_DEFERRED`` marker) and let the caller retry
    next tick. ``context`` names the CALLING pass in the deferral log line so a
    post-mortem can tell an entry-trail deferral from a stop-fill one (#1219
    zen finding) — both passes draw from this one budget, entry-trail first."""
    if isinstance(broker, SupportsOutcomeCachePeek) and broker.has_cached_order_outcome(order_id):
        # Contract (SupportsOutcomeCachePeek): a True peek MUST mean
        # resolve_order_outcome answers from the terminal memo with NO audit
        # HTTP read — a lazily-populated cache returning True here would
        # silently bypass the budget (broker bug, not a core bug).
        return True
    if deps.audit_budget.try_acquire():
        return True
    deps.audit_budget.note_deferred()
    logger.info(
        "%s: %s — audit budget exhausted, deferred resolve of %s to next tick",
        context,
        VERDICT_AUDIT_DEFERRED,
        order_id,
    )
    return False


def _resolve_entry_order_outcome(
    broker: SupportsOrderResolution, order_id: str
) -> OrderState | None:
    """One audit-log terminal resolution for a disappeared armed order (memo §5 /
    trap #4). ``None`` on a ``BrokerError`` (retry next tick — never a fabricated
    fill)."""
    try:
        return broker.resolve_order_outcome(order_id)
    except BrokerError as exc:
        logger.warning(
            "entry-trail reconcile: resolve_order_outcome(%s) failed (%s)", order_id, exc
        )
        return None


def _journal_entry_fired(
    crid: str,
    tier_state: entry_trails.EntryTrailTierState,
    d_bps: int,
    now: dt.datetime,
    *,
    order_id: str,
    realized_qty: float,
    avg_price: float | None,
) -> None:
    """Append the terminal ``fired`` line for a reconciled fill (memo §5
    ``fired{realized_qty}`` + measurement).

    Top-level ``order_id`` + ``realized_qty`` release the virtual reservation (the
    fold's ``terminal_kind`` -> ``watching_virtual_gross_acct`` skips the tier) and
    un-jam capacity (``_open_watch_pick_keys`` skips it) in ONE write; ``avg_price``
    + ``ts`` carry the realized entry-side fill the offline exec_quality join needs.
    Idempotent by construction: once written the tier is terminal in the fold, so
    the next reconcile pass excludes it (``_resting_armed_tiers``)."""
    entry_trails.append_entry_trail_line(
        {
            "kind": entry_trails.KIND_FIRED,
            "crid": crid,
            "order_id": order_id,
            "realized_qty": realized_qty,
            "avg_price": avg_price,
            "ts": now.isoformat(),
            "measurement": _entry_reconcile_measurement(
                tier_state, d_bps, order_id=order_id, realized_qty=realized_qty, avg_price=avg_price
            ),
        }
    )


def _entry_reconcile_measurement(
    tier_state: entry_trails.EntryTrailTierState,
    d_bps: int,
    *,
    order_id: str | None,
    realized_qty: float | None,
    avg_price: float | None,
) -> dict[str, Any]:
    """The terminal measurement stamp for a RECONCILED fill (memo §5 / T1d),
    mirroring :func:`_entry_measurement_blob` but sourced from the FOLD (the
    watcher runtime is gone once a tier is resting) + the resolved fill: the
    variant-A entry ``tier_limit``, the final trough + its ``would_be_trigger``,
    the join ``order_id``, the realized ``avg_price``/``realized_qty`` (the
    entry-side concession the offline join computes vs the limit), and the
    ``entry_mode`` cohort tag (T8). Touch marks are the ``touched`` line's job (the
    offline join reads them there), so they are ``None`` here."""
    record = tier_state.watch_open or {}
    trough = tier_state.min_trough
    trigger = None if trough is None else trough * (1.0 + d_bps / _ENTRY_BPS_DENOMINATOR)
    limit = record.get("limit")
    return {
        "tier_limit": None if limit is None else float(limit),
        "touch_price": None,
        "touch_ts": None,
        "final_trough": trough,
        "would_be_trigger": trigger,
        "order_id": order_id,
        "avg_price": avg_price,
        "realized_qty": realized_qty,
        "entry_mode": record.get("entry_mode") or _entry_trail_mode_tag(d_bps),
    }


# --- PR-T2b overnight DayOrder-cancel -> next-session re-arm (Finding 2) ------
#
# A native trailing entry is a DayOrder — it cancels at the session close. Left
# frozen, a 7-day-TTL trailing entry silently degrades to a single-session order.
# When the fill-reconcile above sees a resting armed order GONE-but-UNFILLED it
# hands the tier here: within the ORIGINAL TTL window re-arm it (re-admit to the
# watch pass with the trough carried + the open-check armed), past window_end
# expire it (release the reservation). NEVER a fabricated fill, never a window
# extension (memo §5 CRITICAL-2 + TTL "one rule").


def _rearm_or_expire_gone_tier(
    deps: LoopDeps,
    crid: str,
    tier_state: entry_trails.EntryTrailTierState,
    outcome: OrderState | None,
    d_bps: int,
    now: dt.datetime,
    report: TickReport,
) -> None:
    """A resting armed DayOrder that DISAPPEARED unfilled (memo §5 CRITICAL-2).

    Only a DEFINITIVE non-fill terminal (resolve -> EXPIRED / CANCELLED = the
    DayOrder cancelled at the session close) drives the transition; an UNKNOWN /
    unresolved outcome is a no-op this tick (retry) — never presume expiry over a
    still-materialising fill from an ambiguous audit read (memo §3 G6 / trap #5).

    Within the ORIGINAL TTL window the tier RE-ARMS (re-append watch_open — arm
    state reset, trough carried, open-check armed — so the next session re-admits
    it); PAST ``window_end`` it is terminal ``expired`` (the re-arm NEVER extends
    the window, memo §5 TTL "one rule"), which releases the virtual reservation."""
    if not _gone_order_is_definitely_unfilled(outcome):
        return  # UNKNOWN / unresolved -> defer, never fabricate a re-arm or expiry
    window_end = _entry_window_end(tier_state)
    if window_end is None:
        return  # corrupt/absent TTL -> cannot re-arm or expire safely (alarm state)
    if now >= window_end:
        _journal_entry_expired(crid, tier_state, d_bps, now)
        if deps.alert_throttled(
            f"entry-trail {entry_label_from_crid(crid)}: TTL window closed with no fill -> expired",
            f"entry-trail:expired:{crid}",
        ):
            report.alerts += 1
        return
    _journal_entry_rearm(crid, tier_state)
    if deps.alert_throttled(
        f"entry-trail {entry_label_from_crid(crid)}: DayOrder cancelled at close "
        f"-> re-armed (trough carried)",
        f"entry-trail:rearm:{crid}",
    ):
        report.alerts += 1


def _gone_order_is_definitely_unfilled(outcome: OrderState | None) -> bool:
    """A DEFINITIVE non-fill terminal for a DISAPPEARED armed order (memo §5): the
    DayOrder cancelled/expired at the session close. ``UNKNOWN`` / ``None`` (an
    audit row not-in-retention, or a ``BrokerError`` on resolve) is NOT definitive
    — the caller defers rather than presume expiry over a still-materialising fill
    (memo §3 G6 / trap #5). A ``FILLED``/``PARTIALLY_FILLED`` outcome never reaches
    here (the fill path handled it)."""
    from broker_contract.contract import OrderStatus

    return outcome is not None and outcome.status in (OrderStatus.EXPIRED, OrderStatus.CANCELLED)


def _entry_window_end(tier_state: entry_trails.EntryTrailTierState) -> dt.datetime | None:
    """The ORIGINAL TTL ``window_end`` from the tier's carried watch_open (memo §5
    "one rule"), or ``None`` when it is missing / unparseable — the tier is then
    unreconstructable, so the caller neither re-arms nor expires it (an alarm state
    surfaced by the watch pass / monitoring, never a fabricated terminal)."""
    raw = (tier_state.watch_open or {}).get("window_end")
    if raw is None:
        return None
    try:
        return dt.datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        logger.warning(
            "entry-trail rearm: %s has an unparseable window_end %r — deferring",
            entry_label_from_crid(tier_state.crid),
            raw,
        )
        return None


def _journal_entry_rearm(crid: str, tier_state: entry_trails.EntryTrailTierState) -> None:
    """Re-admit a DayOrder-cancelled tier to the watch pass (memo §5 CRITICAL-2):
    re-append its carried ``watch_open`` (the SAME reservation + wire fields) with
    the open-check marker set.

    The fold then resets ``latest_kind`` -> ``watch_open`` (re-admitted to
    :func:`_active_entry_watches`, dropped from :func:`_resting_armed_tiers`) AND
    ``armed_order_id`` -> ``None`` (a re-opened watch owns no resting order);
    ``min_trough`` is preserved automatically (it is the historical minimum over
    the whole crid). NON-terminal by construction — the virtual reservation keeps
    counting (the tier is watching again) and it re-occupies watch capacity. The
    deterministic crid + the fold's latest-watch_open-wins make the re-append
    idempotent (a repeated re-arm never double-reserves)."""
    record = dict(tier_state.watch_open or {})
    record["kind"] = entry_trails.KIND_WATCH_OPEN
    record["crid"] = crid
    record[_ENTRY_REARM_MARKER] = True
    entry_trails.append_entry_trail_line(record)


def _journal_entry_expired(
    crid: str, tier_state: entry_trails.EntryTrailTierState, d_bps: int, now: dt.datetime
) -> None:
    """Terminal ``expired`` for a DayOrder gone unfilled PAST the ORIGINAL
    ``window_end`` (memo §5 TTL "one rule" — the re-arm never extends the window).

    Releases the virtual reservation (the fold's ``terminal_kind`` ->
    ``watching_virtual_gross_acct`` skips the tier) and un-jams capacity in ONE
    write; carries the reconcile measurement with a null fill (no order id, no
    realized qty). Idempotent: once terminal the tier is excluded from
    :func:`_resting_armed_tiers` next pass."""
    entry_trails.append_entry_trail_line(
        {
            "kind": entry_trails.KIND_EXPIRED,
            "crid": crid,
            "ts": now.isoformat(),
            "measurement": _entry_reconcile_measurement(
                tier_state, d_bps, order_id=None, realized_qty=None, avg_price=None
            ),
        }
    )


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
        heartbeat_fn(_kill_active(deps))
        # #1172: and this instance's view of the shared price reader. A no-op on
        # the in-process path. Separate emit because it is a separate job — one
        # emit call writes one whole per-job textfile.
        _emit_price_reader_client_gauges(_REMOTE_QUOTE_SOURCE)
        # #1203: the frame this daemon is sizing with. Config read + one local
        # file write, no broker call — see the ownership-split note on
        # _emit_frame_gauges. The balance half is collected out-of-process.
        _emit_frame_gauges()
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


def _emit_stream_gauge(values: Mapping[str, float]) -> None:
    """Best-effort Prometheus stream-state gauges, in ONE atomic write per tick
    (rearm design memo §4.6). ``values`` maps gauge BASE names to values; the
    per-instance ``{job=...}`` label is applied here (the SAME job as the
    heartbeat's, ``state_paths.metrics_job()`` — it is the same daemon instance).
    The write atomically OVERWRITES the stream's OWN domain textfile
    (``state_paths.stream_metrics_job()``) — never the heartbeat's, which a
    shared domain would clobber — so every stream gauge MUST land in this single
    call: an omitted key deletes its series. A textfile-dir hiccup must never
    crash the loop — the poll backstop covers protection regardless of
    observability."""
    from alphalens_pipeline.observability.textfile import emit_domain_metrics

    job = state_paths.metrics_job()
    try:
        emit_domain_metrics(
            state_paths.stream_metrics_job(),
            {f'{name}{{job="{job}"}}': value for name, value in values.items()},
        )
    except OSError:
        logger.warning("broker-manager stream gauges emit failed", exc_info=True)


@dataclass
class _StreamEpisodeState:
    """Daemon-lifetime breaker-episode state threaded through the stream-tick
    helpers (memo §4.5) — one instance per ``_make_stream_tick`` closure."""

    started_mono: float
    last_trips_total: int
    stale_s: float
    episode_open: bool = False
    episode_rearms: int = 0
    cooldown_s: float = _STREAM_REARM_FLOOR_S
    next_trial_mono: float = 0.0
    delivered_at_rearm: int = 0
    up_since: float | None = None
    trip_times: deque[float] = field(default_factory=deque)
    flap_latched: bool = False


@dataclass(frozen=True)
class _StreamHealthSample:
    """One tick's delivery-backed health sample (memo §4.4 step 2). ``up``
    requires a frame delivered on THIS trial (``frames > delivered_at_rearm``)
    — never ``is_streaming``, which ``rearm()`` sets True before any evidence;
    ``reader_dark`` includes ``not is_running()`` so a reader thread that
    crashed WITHOUT tripping is recovered by the same path."""

    running: bool
    streaming: bool
    frames: int
    silence: float | None
    reader_dark: bool
    up: bool


def _sample_stream_health(
    trigger: StreamTrigger, state: _StreamEpisodeState
) -> _StreamHealthSample:
    running = trigger.is_running()
    streaming = trigger.is_streaming
    frames = trigger.frames_delivered
    silence = trigger.seconds_since_last_message()
    return _StreamHealthSample(
        running=running,
        streaming=streaming,
        frames=frames,
        silence=silence,
        reader_dark=(not running) or (not streaming),
        up=(
            running
            and streaming
            and frames > state.delivered_at_rearm
            and silence is not None
            and silence <= state.stale_s
        ),
    )


def _account_stream_flaps(
    state: _StreamEpisodeState, trips: int, now: float, alert: Callable[[str], None]
) -> None:
    """Track breaker trips inside the flap window; latch ONE CRITICAL page when
    the escalation threshold is crossed (memo §7.1)."""
    for _ in range(max(0, trips - state.last_trips_total)):
        state.trip_times.append(now)
    state.last_trips_total = trips
    while state.trip_times and now - state.trip_times[0] > _STREAM_FLAP_WINDOW_S:
        state.trip_times.popleft()
    flap_active = len(state.trip_times) >= _STREAM_FLAP_ESCALATE_AT
    if flap_active and not state.flap_latched:
        alert(
            f"CRITICAL: saxo stream flapping — {len(state.trip_times)} breaker trips "
            f"inside {_STREAM_FLAP_WINDOW_S / 60:.0f} min; episode-OPEN pages "
            "suppressed until the window clears (recovery pages never are)"
        )
    state.flap_latched = flap_active


def _drive_stream_state_machine(
    state: _StreamEpisodeState,
    trigger: StreamTrigger,
    alert: Callable[[str], None],
    *,
    now: float,
    health: _StreamHealthSample,
) -> float | None:
    """CLOSED -> OPEN -> TRIAL -> CLOSED episode machine (memo §4.5). Returns
    the silence sample, refreshed after a TRIAL's ``rearm()``."""
    silence = health.silence
    if not state.episode_open:
        if health.reader_dark:
            # CLOSED -> OPEN: arm the ladder; NO trial on the opening tick.
            state.episode_open = True
            state.episode_rearms = 0
            state.cooldown_s = _STREAM_REARM_FLOOR_S
            state.next_trial_mono = now + state.cooldown_s
            state.up_since = None
            if not state.flap_latched:
                alert(
                    "saxo stream DOWN — reader dark, re-arm ladder engaged; "
                    "running on poll backstop"
                )
    elif health.up:
        if state.up_since is None:
            state.up_since = now
        if now - state.up_since >= _STREAM_HEALTHY_DWELL_S:
            # OPEN -> CLOSED: delivery-confirmed recovery held for the full
            # dwell. NEVER suppressed by the flap latch — the operator must
            # always see an episode end.
            state.episode_open = False
            state.cooldown_s = _STREAM_REARM_FLOOR_S
            state.up_since = None
            alert(
                f"saxo stream RECOVERED — delivery-confirmed after "
                f"{state.episode_rearms} re-arm trial(s); ladder reset"
            )
    else:
        state.up_since = None  # the dwell must be CONTINUOUS delivery-backed health
        if health.reader_dark and now >= state.next_trial_mono:
            # OPEN -> TRIAL: at most ONE trial per tick, no page. The ladder
            # advances BEFORE the rearm so a raising spawn cannot burn
            # trials at the floor rate.
            state.delivered_at_rearm = health.frames
            trigger.reset_liveness()  # an hours-old epoch must never page stream-dead
            state.cooldown_s = min(state.cooldown_s * 2.0, _STREAM_REARM_CEILING_S)
            state.next_trial_mono = now + state.cooldown_s
            state.episode_rearms += 1
            trigger.rearm()
            silence = trigger.seconds_since_last_message()
    return silence


def _emit_stream_tick_gauges(
    state: _StreamEpisodeState,
    trigger: StreamTrigger,
    emit_gauge: Callable[[Mapping[str, float]], None],
    session_predicate: Callable[[], bool],
    *,
    now: float,
    health: _StreamHealthSample,
    silence: float | None,
    trips: int,
) -> None:
    """ONE atomic multi-key gauge write (all SIX keys: an omitted key deletes
    its series). The age key is never omitted: epoch None -> seconds since
    closure build. Fallback diverges from memo §4.6's "seconds since reader
    start": started_mono is the CLOSURE build time (daemon start). Harmless —
    no alert keys on the absolute value while the breaker is open."""
    age = silence if silence is not None else now - state.started_mono
    try:
        session = 1.0 if session_predicate() else 0.0
    except Exception:  # calendar fail-open: report in-session, keep gauges
        logger.warning("streaming: session predicate raised — reporting in-session")
        session = 1.0
    emit_gauge(
        {
            _STREAM_READER_UP_METRIC_NAME: 1.0 if (health.running and health.streaming) else 0.0,
            _STREAM_BREAKER_OPEN_METRIC_NAME: 1.0 if state.episode_open else 0.0,
            _STREAM_LAST_MESSAGE_METRIC_NAME: age,
            _STREAM_CONSECUTIVE_FAILURES_METRIC_NAME: float(trigger.consecutive_failures),
            _STREAM_TRIPS_TOTAL_METRIC_NAME: float(trips),
            _STREAM_IN_SESSION_METRIC_NAME: session,
        }
    )


def _make_stream_tick(
    trigger: StreamTrigger,
    *,
    get_bearer: Callable[[], str],
    alert: Callable[[str], None],
    alert_throttled: Callable[[str, str], bool],
    stale_s: float,
    emit_gauge: Callable[[Mapping[str, float]], None] = _emit_stream_gauge,
    monotonic: Callable[[], float] = time.monotonic,
    in_session: Callable[[], bool] | None = None,
) -> Callable[[], None]:
    """Build the per-tick streaming hook run by ``run_daemon`` on the MAIN thread.

    Rearm design memo ``saxo_stream_breaker_rearm_design_2026_08_22.md``
    §4.4-§4.6. Every tick, unconditionally, in this order:

    1. **Push the bearer FIRST** — before any dark-branch return. The pre-rearm
       tick returned from the breaker branch before ``push_token``, freezing the
       reader's token at the trip instant while ``alphalens-saxo-refresh``
       rotated the real one every ~20 min — a re-armed reader would have burned
       its single half-open trial on a 401.
    2. **Sample delivery-backed health.** ``up`` requires a frame delivered on
       THIS trial (``frames_delivered > delivered_at_rearm``) — never
       ``is_streaming``, which ``rearm()`` sets True before any evidence; and
       ``reader_dark`` includes ``not is_running()`` so a reader thread that
       crashed WITHOUT tripping is recovered by the same path.
    3. **Drive the episode state machine**: CLOSED -> OPEN on ``reader_dark``
       (ONE guaranteed-send page); OPEN -> TRIAL at each cooldown-ladder rung
       (60s doubling to 900s, at most one ``trigger.rearm()`` per tick, no
       page); OPEN -> CLOSED once ``up`` has held ``_STREAM_HEALTHY_DWELL_S``
       (ONE guaranteed-send page, ladder back to the floor). Telegram gets
       EDGES once per EPISODE; Prometheus owns every level — a sustained dark
       stream NEVER pages on an interval (the 2026-08-22 metronome). Flapping
       (``_STREAM_FLAP_ESCALATE_AT`` trips inside ``_STREAM_FLAP_WINDOW_S``)
       escalates ONE CRITICAL and then suppresses further OPEN pages only; the
       CLOSE page is never suppressed (an unpaired page is worse than none).
       The throttled ``stream-dead`` alert covers only the dark-but-CONNECTED
       case (``not episode_open``) — an open episode already reports the dark
       stream via its own page and gauge.
    4. **Emit the stream gauges** — always, including while dark, in ONE atomic
       multi-key write; the age key is never omitted (epoch ``None`` reports
       seconds since this closure was built).

    Both ``alert`` (guaranteed-send, edges only — mirrors
    ``_alert_kill_transition``: edges are rare and each transition must deliver)
    and ``alert_throttled`` are MAIN-THREAD-ONLY sinks; neither may ever be
    handed to the reader thread's client (memo §7.14 / PR #900). Everything
    after the bearer push is best-effort: ``run_daemon`` calls ``on_tick()``
    bare and the CLI catches only ``BrokerError``, so a raising ``rearm()``
    (e.g. ``Thread.start()`` under thread exhaustion) must never unwind the
    protective daemon."""

    # Episode state lives in a per-closure _StreamEpisodeState, constructed once
    # per daemon by _build_stream_handles — the same daemon-lifetime one-slot
    # shape as deps.kill_state, without a new LoopDeps field (memo §4.5).
    #
    # The session predicate feeds the in_session GAUGE only (memo §3 Q5) —
    # nothing here gates on it. Built per-tick-closure (main-thread-only, so
    # _make_stream_session_window's single-writer memo holds), injectable for
    # tests. It FAILS OPEN: the calendar contract says a raising predicate is
    # treated as in-session, and a calendar bug must never take the other five
    # gauges down with it.
    session_predicate = in_session if in_session is not None else _make_stream_session_window()
    state = _StreamEpisodeState(
        started_mono=monotonic(), last_trips_total=trigger.trips_total, stale_s=stale_s
    )

    def _drive_episode() -> None:
        now = monotonic()
        # (2) Delivery-backed health sample (memo §4.4 step 2).
        health = _sample_stream_health(trigger, state)
        # Flap accounting off the monotonic trips_total counter — a trip whose
        # whole lifetime falls between two ticks is still counted (memo §7.1).
        trips = trigger.trips_total
        _account_stream_flaps(state, trips, now, alert)
        # (3) Episode state machine (memo §4.5).
        silence = _drive_stream_state_machine(state, trigger, alert, now=now, health=health)
        # stream-dead is for the dark-but-CONNECTED case only (memo §7.2): an
        # open episode already reports the dark stream via its own page + gauge.
        if not state.episode_open and silence is not None and silence > stale_s:
            alert_throttled(
                f"saxo stream silent >{stale_s:.0f}s ({silence:.0f}s) — running on poll backstop",
                "stream-dead",
            )
        # (4) Gauges — every tick, including while dark.
        _emit_stream_tick_gauges(
            state,
            trigger,
            emit_gauge,
            session_predicate,
            now=now,
            health=health,
            silence=silence,
            trips=trips,
        )

    def _tick() -> None:
        # (1) Bearer FIRST — before any dark-branch logic (memo §4.4 step 1).
        try:
            bearer = get_bearer()
        except Exception:  # a token/chain error must never crash the protective loop
            logger.warning("streaming: bearer read failed — skipping push this tick", exc_info=True)
            bearer = None
        if bearer:
            trigger.push_token(bearer)
        try:
            _drive_episode()
        except Exception:
            # run_daemon calls on_tick() bare and the CLI catches only
            # BrokerError — a raising rearm()/read must degrade to poll-only,
            # never unwind the protective daemon (memo §7.5).
            logger.warning(
                "streaming: episode tick failed — poll backstop covers protection",
                exc_info=True,
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
    refresh-chain-lost alert. This module never imports telegram itself.

    Two state-safety guards (ADR 0016) run FIRST, before any broker/journal
    I/O: D7 used to hard-block a LIVE boot outright (the only client path was
    unconditionally SIM — ADR 0015 lock — so a "live" instance would trade
    SIM while journaling/alerting under LIVE labels, a mislabeled-state
    hazard); it is now the ``env == live`` branch below that routes into
    :func:`~alphalens_pipeline.brokers.saxo.broker.create_saxo_broker_live_from_env`
    (ADR 0017) instead of the SIM registry — that factory itself refuses to
    construct anything until
    :func:`~alphalens_pipeline.brokers.automanager.live_rails.assert_live_rails`
    and the §1 account-bound grant both pass, so a mis-pinned LIVE unit still
    fails loud at boot, before any network call. D4 refuses to start against
    a pre-migration flat state layout (an empty per-env root while the broker
    still holds positions would reconcile against nothing and silently
    degrade protection)."""
    broker_environment = state_paths.broker_environment()
    state_paths.assert_no_legacy_flat_state()

    from alphalens_pipeline.brokers.automanager import (  # noqa: F401 (planner/safety used by _make_place_pick)
        orphan_sweeper,
        picks,
        placement_planner,
        reconcile_bridge,
        safety,
        session_keeper,
    )
    from alphalens_pipeline.brokers.submission_log import iter_submission_records

    # One-shot bounded-growth maintenance: fold the append-only standalone-stop
    # journal down to its minimal fold-equivalent set (issue #895). Runs here —
    # at startup, before the tick loop — so no concurrent tick races the rewrite.
    _compact_standalone_stop_journal()
    # Same maintenance for the entry-trails journal (entry-trailing PR-T0):
    # startup, before the tick loop, no concurrent tick — a missing/empty
    # journal is a no-op, so this is inert until a watcher writes records.
    entry_trails.compact_entry_trail_journal()

    # ADR 0017 composition root: env == live routes into the LIVE factory (which
    # itself refuses to construct anything until assert_live_rails + the §1
    # account-bound grant both pass — a mis-pinned LIVE unit still fails loud at
    # boot, before any network call) instead of the SIM registry path, so
    # env == live can NEVER reach get_default_broker. Both imports stay local so
    # the common SIM-only path never pulls in the LIVE factory's import graph.
    live_token_provider: Any = None
    if broker_environment == state_paths.ENV_LIVE:
        from alphalens_pipeline.brokers.saxo.broker import create_saxo_broker_live_from_env

        broker, live_token_provider = create_saxo_broker_live_from_env(alert=chain_loss_notify)
    else:
        from alphalens_pipeline.brokers.registry import get_default_broker

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
    # UNCONDITIONAL, like the standalone-stop gate above (#1141): the protection
    # view's per-uic netting and the execute-time owned re-checks read
    # get_long_positions / get_positions_by_uic on every tick, and their
    # historical getattr fallback substituted the UN-NETTED get_positions()
    # result — a stop sized to one lot with the rest of the position naked. A
    # broker that cannot read netted positions must not boot the manager.
    # ORDERING is deliberate: this gate sits BEFORE the amend / exit-policy
    # gates so their capability errors stay attributable — the amend-gate tests'
    # _StopOnlyBroker stubs carry the reads for exactly that reason.
    if not isinstance(broker, SupportsNettedPositionReads):
        raise BrokerCapabilityError(
            f"broker {broker.name!r} does not implement get_long_positions / "
            "get_positions_by_uic (SupportsNettedPositionReads) — the protection "
            "pass sizes stops off the NETTED per-uic reads; wire a capable broker "
            "or add the capability."
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
    # Live-exits capability gate (#1141), mirroring the _amend_enabled() gate
    # above: when the flag is on, the pass amends the SL and market-sells
    # tranches, so the FULL engine requirement set (LiveExitBroker — amend +
    # market orders + netted reads + cancel + working-sell listing) must be
    # guaranteed at boot; with the flag off, an incapable broker is fine — the
    # pass never runs and its own isinstance guard backstops a post-boot flip.
    if _live_market_exits_enabled() and not isinstance(broker, LiveExitBroker):
        raise BrokerCapabilityError(
            f"broker {broker.name!r} does not satisfy the live-exit capability set "
            "(LiveExitBroker: amend_stop_amount, place_market_order, netted position "
            "reads, cancel_order, list_working_sell_orders) but "
            "ALPHALENS_LIVE_MARKET_EXITS=1 — wire a capable broker or unset the flag."
        )
    # ONE token-provider instance is shared by the SessionKeeper AND the (SIM-only)
    # streaming reader, so there is a single OAuth chain / one flock owner and the
    # reader can re-authorize in place off the same bearer the main loop pushes.
    # LIVE reuses the SAME LiveOrderTokenProvider the factory built above —
    # constructing a second adapter over the same underlying LiveTokenProvider
    # would be two independent dead-latches that could disagree about whether the
    # chain is alive (design memo §2). Streaming is structurally skipped for LIVE
    # regardless of the flag (_build_stream_handles), so only SIM ever shares this
    # instance with a reader thread.
    provider = (
        live_token_provider
        if broker_environment == state_paths.ENV_LIVE
        else _default_oauth_provider(alert=chain_loss_notify)
    )
    keeper = session_keeper.SessionKeeper(provider)

    def _read_records() -> list[Mapping[str, Any]]:
        return list(iter_submission_records(state_paths.submissions_path()))

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
    wake_event, stream_tick, stream_trigger = _build_stream_handles(
        broker, provider, base_alert, _throttled
    )

    # Day-1 gap gate price probe (built once, shared by both LoopDeps sites it
    # is wired into below — the closure captured by ``place_pick`` and the
    # bare field kept for symmetry/introspection, mirroring place_oco_exit /
    # amend_stop). Constructing it does no I/O; the Saxo LIVE marketdata
    # chain is only ever reached lazily, per call, inside the probe itself —
    # this is the SAME factory for both the SIM and LIVE daemon instances
    # (both call build_default_deps), and it self-degrades to always
    # returning None when the LIVE chain/env is absent (SIM).
    day1_gap_probe = _build_day1_gap_price_probe()

    # ONE shared per-tick audit-read budget (audit-429 memo §3 + Amendment 1)
    # for BOTH resolve consumers: bound into the verdict pass via the partial
    # below (mirroring build_protection_view) and carried on LoopDeps for the
    # entry-trail reconcile pass; run_once resets it at every tick start.
    audit_budget = OutcomeAuditBudget()

    return LoopDeps(
        broker=broker,
        kill_file=state_paths.kill_file_path(),
        global_kill_file=state_paths.global_kill_file_path(),
        ensure_alive=keeper.ensure_alive,
        iter_picks=picks.iter_picks,
        place_pick=_make_place_pick(
            broker,
            exit_policy,
            alert_throttled=_throttled,
            day1_gap_price_probe=day1_gap_probe,
            audit_budget=audit_budget,
            # #1247: the now tranche's marketability gate reads the SAME live
            # feed the exit/entry-watch passes use (per-uic now-entry scope).
            now_entry_feed_factory=_default_live_exits_feed_factory,
        ),
        read_records=_read_records,
        verdicts_fn=functools.partial(reconcile_bridge.verdicts, audit_budget=audit_budget),
        build_position_view=_make_position_view_builder(broker),
        build_protection_view=functools.partial(build_protection_view, exit_policy=exit_policy),
        execute_protection=_make_protection_executor(
            broker, throttle, place_oco_exit=oco_placer, amend_stop=amend_placer
        ),
        sweep_orphans_fn=lambda b: orphan_sweeper.sweep(
            b, _read_records(), entry_trail_ref_marker=_ENTRY_ORDER_REF_MARKER
        ),
        alert=base_alert,
        alert_throttled=_throttled,
        place_oco_exit=oco_placer,
        amend_stop=amend_placer,
        wake_event=wake_event,
        stream_tick=stream_tick,
        stream_trigger=stream_trigger,
        exit_policy=exit_policy,
        live_exits_feed_factory=_default_live_exits_feed_factory,
        day1_gap_price_probe=day1_gap_probe,
        audit_budget=audit_budget,
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
    base_alert: Callable[[str], None],
    alert_throttled: Callable[[str, str], bool],
) -> tuple[threading.Event | None, Callable[[], None] | None, StreamTrigger | None]:
    """Construct + start the dark streaming reader when every structural
    precondition holds, else return the poll-only ``(None, None, None)``.

    ``base_alert`` (guaranteed-send, episode edges) and ``alert_throttled`` are
    both MAIN-THREAD-ONLY sinks consumed by the tick closure. NEITHER may ever
    be passed into the StreamTrigger / streaming-client construction below —
    the client's optional ``alert=`` kwarg must stay unset so ``_trip_breaker``
    stays journald-only on the READER thread (rearm design memo §7.14; pinned
    by ``test_client_factory_is_never_given_an_alert_sink``).

    Preconditions (each a fail-safe-to-poll gate, design memo §Env gates):
      0. ``env != live`` — the order-WS subscriber
         (:func:`_build_streaming_subscriber`) is a SIM-rail ``SaxoClient``
         (no ``standing_live_authorized``); a LIVE instance is structurally
         refused this reader regardless of the flag below (design memo §3:
         "order-WS early-wake needs its own LIVE re-validation" — a separate,
         not-yet-built follow-up), so the pin recommending
         ``STREAMING_ENABLED=0`` for the LIVE unit is defense-in-depth, not
         the only guard;
      1. ``ALPHALENS_BROKER_STREAMING_ENABLED=1`` (master dark gate);
      2. the broker is Saxo (the streaming REST + SIM rail live on ``SaxoClient``);
      3. the provider is OAuth — a static 24h token cannot be PUT-reauthorized in
         place, so :meth:`SaxoStreamingClient.start` would refuse anyway;
      4. the reader thread actually started (``start()`` returns True).

    SIM-probe-only (no hermetic cycle — the run_daemon wait + the per-tick hook are
    unit-tested against stubs). A construction / start failure logs once and falls
    back to poll-only rather than raising — streaming is a pure latency win and its
    absence must never block the protective loop."""
    if state_paths.broker_environment() == state_paths.ENV_LIVE:
        logger.info(
            "streaming early-wake reader structurally skipped for the LIVE broker "
            "instance regardless of %s (design memo §3 — the order-WS subscriber "
            "is a SIM-rail SaxoClient; LIVE re-validation of the reader is a "
            "separate, not-yet-built follow-up)",
            _STREAMING_ENABLED_ENV,
        )
        return None, None, None
    if not _streaming_enabled():
        return None, None, None

    from alphalens_pipeline.brokers.automanager.streaming_trigger import (
        StreamTrigger,
        default_context_id_factory,
    )
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
    # The contextId format has ONE home (streaming_trigger.default_context_id_factory,
    # <=50 chars, [a-zA-Z0-9-]) so the initial context and every rearm() rotation
    # stay consistent (rearm design memo §4.3).
    context_id = default_context_id_factory()
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
        alert=base_alert,
        alert_throttled=alert_throttled,
        stale_s=stale_s,
    )
    return trigger.wake_event, stream_tick, trigger


# --- SIM-probe-only factory helpers (Component 6 "placer" home) --------------
# Thin composers over the Task 1-10 seams. They carry NO hermetic unit-test
# cycle (test_control_loop.py injects LoopDeps as stubs; build_default_deps and
# everything it wires is exercised end-to-end only by the deferred
# SAXO_LIVE_TEST=1 SIM live probe). _make_place_pick writes the append-only
# standalone-stop journal (_standalone_stop_journal_path()) `planned` lines —
# the plan PRICES the broker cannot know (disaster stop + in-band TP), keyed to
# the entry client_request_id and tier_index. NO journal line confers
# protection (saxo-oco memo §7): the protection pass (build_protection_view +
# reconcile_protection) derives it from live broker state. `_fold_planned_exits`
# folds the `planned` lines per-uic.


def _standalone_stop_journal_path() -> Path:
    """The out-of-band standalone-stop journal path — funnels through the ONE
    broker-state path seam (state_paths.standalone_stops_path(), ADR 0016 /
    design memo D2), resolved fresh on EVERY call, never cached at import
    time. A thin named wrapper (rather than calling the seam directly at each
    of the three call sites below) so tests monkeypatch ONE attribute."""
    return state_paths.standalone_stops_path()


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

    path = _standalone_stop_journal_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
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

    path = _standalone_stop_journal_path()
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as fh:
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
    (bad uic / stop_price) lines are skipped.

    A ``planned_retracted`` marker (#1249) REMOVES the crid in write order — a
    ``planned`` line appended AFTER the marker is a genuinely new plan and
    governs again (entry-trail crids are sticky-terminal and bracket crids are
    UUIDs, so a retracted crid cannot resurrect by accident). This is the ONE
    choke point both the fold and the compaction read, so retraction semantics
    cannot drift between them."""
    latest: dict[str, tuple[int, Mapping[str, Any]]] = {}
    for line in lines:
        kind = line.get("kind")
        if kind == _PLANNED_RETRACTED_KIND:
            retracted_crid = line.get("client_request_id")
            if retracted_crid:
                latest.pop(str(retracted_crid), None)
            continue
        if kind != "planned":
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


_TRANCHE_PLAN_KIND = "tranche_plan"
_TRANCHE_PLAN_RETRACTED_KIND = "tranche_plan_retracted"
_TRANCHE_FIRED_KIND = "tranche_fired"
# #1249: the per-crid retraction marker for ``planned`` disaster-stop lines —
# the tranche marker's sibling, keyed by client_request_id instead of uic.
# Consumed inside ``_latest_planned_by_crid`` (write-order pop), so the fold
# and the compaction inherit retraction at the same choke point.
_PLANNED_RETRACTED_KIND = "planned_retracted"


def _build_tranche_plan_line(
    *,
    uic: int,
    tp_tranches: tuple[TpTranchePlan, ...],
    reference_qty: float,
    stop_price: float,
    pick_key: str | None = None,
    instrument_currency: str | None = None,
    sizing_currency: str | None = None,
) -> dict[str, Any]:
    """One append-only ``tranche_plan`` journal line -- the per-uic TP ladder the
    live-exit engine needs (INC-5) but the ``planned`` line does not carry.
    JSON-serializable (each ``TpTranchePlan`` is decomposed to a plain dict).
    Confers no protection by itself -- only the tranche reference prices/pcts,
    the sizing base, and the stop price the engine amends the standalone SL
    around. Written ONCE per placement (never per tier); a same-uic re-arm
    simply appends a newer line (append-only fold, last well-formed line wins,
    exactly like ``_build_planned_line``).

    ``pick_key`` (2026-08-19 adjudication finding 4) is the plan's trade
    identity: the entry-trail watch routing stamps ``ticker:brief_date`` so
    :func:`_fold_fired_since_latest_plan` treats a crash-recovery re-drive's
    re-append as the SAME trade (no fired-set reset). ``None`` (the bracket
    path) omits the key -- a keyless line keeps today's always-reset
    semantics."""
    line: dict[str, Any] = {
        "kind": _TRANCHE_PLAN_KIND,
        "uic": int(uic),
        "tp_tranches": [
            {
                "tranche_index": int(t.tranche_index),
                "target_price": float(t.target_price),
                "tranche_frac": float(t.tranche_frac),
                "r_multiple": float(t.r_multiple),
                "tag": str(t.tag),
            }
            for t in tp_tranches
        ],
        "reference_qty": float(reference_qty),
        "stop_price": float(stop_price),
    }
    if pick_key is not None:
        line["pick_key"] = str(pick_key)
    # #1238 PR 3: the currency pair the #1112 exit gate prices the round trip
    # with. Written only when known -- an absent stamp folds to the
    # conservative legacy facts, exactly like a pre-#1238 line.
    if instrument_currency:
        line["instrument_currency"] = str(instrument_currency)
    if sizing_currency:
        line["sizing_currency"] = str(sizing_currency)
    return line


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
    malformed line contributes nothing, never a partial fold.

    A ``tranche_plan_retracted`` line (2026-08-19 adjudication finding 3 -- a
    watch that ended with no fill) REMOVES the uic's governing ladder; a later
    plan line for the uic governs again (still last-wins, in write order)."""
    out: dict[int, tuple[tuple[TpTranchePlan, ...], float, float]] = {}
    for line in lines:
        kind = line.get("kind")
        if kind == _TRANCHE_PLAN_RETRACTED_KIND:
            retracted_uic = _coerce(line, "uic", int)
            if retracted_uic is not None:
                out.pop(retracted_uic, None)
            continue
        if kind != _TRANCHE_PLAN_KIND:
            continue
        parsed = _parse_tranche_plan_line(line)
        if parsed is None:
            continue
        uic, tranches, reference_qty, stop_price = parsed
        out[uic] = (tranches, reference_qty, stop_price)
    return out


def fold_tranche_plan_currencies(
    lines: Iterable[Mapping[str, Any]],
) -> dict[int, tuple[str | None, str | None]]:
    """Fold the ``tranche_plan`` lines into the newest journal-stamped
    ``(instrument_currency, sizing_currency)`` per uic (#1238 PR 3).

    A SEPARATE fold from :func:`fold_tranche_plans` on purpose: the ladder
    fold's ``(tranches, reference_qty, stop_price)`` tuple is a shape many
    consumers key on, and the currency pair is consumed by exactly one
    (:func:`_build_managed_exits`). Same walk, same semantics: last
    well-formed line per uic wins, ``tranche_plan_retracted`` pops the uic,
    and a legacy line without the stamps folds to ``(None, None)`` -- which
    :func:`~alphalens_pipeline.brokers.automanager.costs.cost_gate_facts`
    turns into the conservative legacy facts."""
    out: dict[int, tuple[str | None, str | None]] = {}
    for line in lines:
        kind = line.get("kind")
        if kind == _TRANCHE_PLAN_RETRACTED_KIND:
            retracted_uic = _coerce(line, "uic", int)
            if retracted_uic is not None:
                out.pop(retracted_uic, None)
            continue
        if kind != _TRANCHE_PLAN_KIND:
            continue
        if _parse_tranche_plan_line(line) is None:
            continue
        uic = _coerce(line, "uic", int)
        if uic is None:
            continue
        instrument_ccy = line.get("instrument_currency")
        sizing_ccy = line.get("sizing_currency")
        out[uic] = (
            str(instrument_ccy) if instrument_ccy else None,
            str(sizing_ccy) if sizing_ccy else None,
        )
    return out


def _parse_tranche_plan_line(
    line: Mapping[str, Any],
) -> tuple[int, tuple[TpTranchePlan, ...], float, float] | None:
    """Parse one ``tranche_plan`` line; None for ANY malformation (a bad line
    contributes nothing, never a partial fold)."""
    from broker_contract.sizing import TpTranchePlan

    raw_uic = line.get("uic")
    raw_tranches = line.get("tp_tranches")
    if raw_uic is None or not isinstance(raw_tranches, list):
        return None
    try:
        uic = int(raw_uic)
        reference_qty = float(line["reference_qty"])
        stop_price = float(line["stop_price"])
        # `float()` happily parses JSON's `NaN` / `Infinity`, so a malformed
        # or hand-edited line could otherwise become a GOVERNING ladder
        # carrying a non-finite size. Refused at the SOURCE as well as at
        # the sizer, because a bad line should contribute nothing rather
        # than be caught later by whichever consumer happens to look first.
        if not math.isfinite(reference_qty) or not math.isfinite(stop_price):
            raise ValueError(
                f"non-finite tranche_plan scalars for uic {uic}: "
                f"reference_qty={reference_qty!r} stop_price={stop_price!r}"
            )
        tranches = tuple(
            TpTranchePlan(
                tranche_index=int(t["tranche_index"]),
                target_price=float(t["target_price"]),
                # Legacy lines carry "tranche_pct". Read them as a FRACTION,
                # because that is what the writer meant: every tranche_plan
                # record on the LIVE rail was written by the geometry
                # producer with the literal 1.0 for "the whole position"
                # (verified 2026-08-25 against all three live journal
                # lines). Converting them as percentages would resize an
                # in-flight position's exit to 1% and leave the rest naked.
                tranche_frac=float(t["tranche_frac"] if "tranche_frac" in t else t["tranche_pct"]),
                r_multiple=float(t["r_multiple"]),
                tag=str(t["tag"]),
            )
            for t in raw_tranches
        )
        # Same source-refusal as the scalars above: tranche_frac is covered by
        # TpTranchePlan's [0, 1] guard (NaN fails the range check), but
        # target_price and r_multiple have no construction guard — a
        # hand-edited line carrying a non-finite take-profit must contribute
        # nothing, never become a governing ladder whose TP limit goes to the
        # broker.
        for tranche in tranches:
            if not math.isfinite(tranche.target_price) or not math.isfinite(tranche.r_multiple):
                raise ValueError(
                    f"non-finite tranche field for uic {uic}: "
                    f"target_price={tranche.target_price!r} r_multiple={tranche.r_multiple!r}"
                )
    except (KeyError, TypeError, ValueError):
        return None
    return uic, tranches, reference_qty, stop_price


def _fold_governing_plan_pick_keys(lines: Iterable[Mapping[str, Any]]) -> dict[int, str | None]:
    """The governing ``tranche_plan``'s ``pick_key`` per uic, in write order
    (last wins; ``None`` = a keyless bracket-path plan). A
    ``tranche_plan_retracted`` line removes the uic — an already-retracted plan
    can never be matched (and so never re-retracted) by the sweep below."""
    governing: dict[int, str | None] = {}
    for line in lines:
        kind = line.get("kind")
        if kind not in (_TRANCHE_PLAN_KIND, _TRANCHE_PLAN_RETRACTED_KIND):
            continue
        uic = _coerce(line, "uic", int)
        if uic is None:
            continue
        if kind == _TRANCHE_PLAN_KIND:
            key = line.get("pick_key")
            governing[uic] = None if key is None else str(key)
        else:
            governing.pop(uic, None)
    return governing


def _terminal_watch_picks(
    fold: entry_trails.EntryTrailFold,
) -> dict[str, tuple[int, bool, tuple[str, ...]]]:
    """``{pick_key: (uic, any_tier_fired, tier_crids)}`` for every pick whose
    entry watch is FULLY terminal (2026-08-19 adjudication finding 3 / #1223).
    A pick with any still-open or ARMED tier is live (``terminal_kind is None``
    — an armed sibling skipped by the #1198 retire still owns a resting native
    BUY, so the first skip also encodes #1223's "never retract while any tier
    is open or armed"). ``tier_crids`` carries the pick's tier watch crids so
    the planned-line retraction (#1249) can derive the fire-crids without a
    second fold walk. Records whose pick_key or uic cannot be reconstructed are
    skipped — never retract on doubt."""
    states_by_pick: dict[str, list[tuple[str, entry_trails.EntryTrailTierState]]] = {}
    for crid, state in fold.tiers.items():
        record = state.watch_open
        if record is None:
            continue
        key = record.get("pick_key")
        if key is None:
            continue
        states_by_pick.setdefault(str(key), []).append((str(crid), state))
    out: dict[str, tuple[int, bool, tuple[str, ...]]] = {}
    for pick_key, crid_states in states_by_pick.items():
        if any(s.terminal_kind is None for _crid, s in crid_states):
            continue  # a tier still watches / arms — the pick is live
        uics = {_coerce(s.watch_open or {}, "uic", int) for _crid, s in crid_states}
        if len(uics) != 1 or None in uics:
            continue  # unmappable / inconsistent — never retract on doubt
        fired = any(s.terminal_kind == entry_trails.KIND_FIRED for _crid, s in crid_states)
        crids = tuple(sorted(crid for crid, _s in crid_states))
        out[pick_key] = (cast(int, next(iter(uics))), fired, crids)
    return out


def _retract_planned_lines(
    crids: Iterable[str],
    *,
    note: str,
    journal_lines: Sequence[Mapping[str, Any]] | None = None,
) -> int:
    """Append one ``planned_retracted`` marker per STILL-GOVERNING crid (#1249).

    Idempotence lives here so every retraction class gets it for free: a crid
    already retracted (or never journaled — a tier that expired before its
    fire-arm) is skipped, never marker-stacked. ``journal_lines`` lets a sweep
    that already read the journal skip the re-read; omitted, the journal is
    read fresh. Broad exception boundary on purpose — retraction is journal
    housekeeping, a read/write failure degrades to a WARN and a retry on the
    next tick, never an aborted caller. Returns the number of markers written."""
    try:
        lines = (
            journal_lines if journal_lines is not None else list(_iter_standalone_stop_journal())
        )
        latest = _latest_planned_by_crid(lines)
        count = 0
        for crid in crids:
            entry = latest.get(str(crid))
            if entry is None:
                continue  # absent or already retracted — nothing to do
            _gen, line = entry
            _append_standalone_stop_journal(
                {
                    "kind": _PLANNED_RETRACTED_KIND,
                    "client_request_id": str(crid),
                    "uic": _coerce(line, "uic", int),
                    "note": note,
                }
            )
            logger.info("planned line %s retracted — %s", crid, note)
            count += 1
        return count
    except Exception:
        logger.warning("planned-line retraction failed — will retry next tick", exc_info=True)
        return 0


def _page_now_residuals(
    verdicts: Iterable[Any],
    records: Iterable[Mapping[str, Any]],
    alert_throttled: Callable[[str, str], bool] | None,
) -> None:
    """#1247 memo §3.2.6: page once when a now order died TERMINAL with a
    PARTIAL fill — the position is smaller than planned; exits size to the
    filled quantity (the standard exit management needs no action here). A
    full cancel with no fill stays quiet (the existing verdict flow covers
    it); the throttle bounds repeats per ticker."""
    if alert_throttled is None:
        return
    now_brackets: dict[str, tuple[str, float | None]] = {}
    for record in records:
        if record.get("tranche") != "now":
            continue
        for bracket in record.get("brackets") or []:
            crid = str(bracket.get("client_request_id") or "")
            if crid:
                ticker = str(record.get("ticker") or "?")
                qty = bracket.get("qty")
                now_brackets[crid] = (ticker, float(qty) if qty is not None else None)
    if not now_brackets:
        return
    for verdict in verdicts:
        if verdict.status not in _TERMINAL_NON_FILLED:
            continue
        filled = verdict.details.get("filled_quantity")
        if not filled:
            continue
        joined = now_brackets.get(str(verdict.details.get("client_request_id") or ""))
        if joined is None:
            continue
        ticker, qty = joined
        planned = f"{qty:g}" if qty is not None else "?"
        alert_throttled(
            f"now tranche {ticker}: order died at close partially filled "
            f"({float(filled):g} of {planned}) — exits size to the filled quantity",
            f"now-residual:{ticker}",
        )


def _retract_planned_for_verdicts(verdicts: Iterable[ReconcileVerdict]) -> None:
    """#1249 class (c): retract the ``planned`` line of every bracket whose
    verdict proves the entry TERMINALLY NEVER FILLED (CANCELLED / REJECTED /
    EXPIRED with no fill evidence). Per-order proof, not per-uic state: an
    entry that provably never filled has a planned line that covers nothing
    regardless of what else lives on the uic — exactly the stale-UUID-crid
    class the entry-trail sweeps cannot reach. Any ``filled_quantity`` in the
    verdict details vetoes (a partial fill leaves a position); UNRESOLVED /
    UNKNOWN outcomes never retract; a missing crid never retracts."""
    crids = sorted(
        {
            str(v.details["client_request_id"])
            for v in verdicts
            if v.status in _TERMINAL_NON_FILLED
            and not v.details.get("filled_quantity")
            and v.details.get("client_request_id")
        }
    )
    if crids:
        _retract_planned_lines(crids, note="entry verdict: terminal without a fill")


def _retract_stale_tranche_plans(
    fold: entry_trails.EntryTrailFold, deps: LoopDeps | None = None
) -> None:
    """Retract every ``tranche_plan`` that no longer governs anything real.

    Two independent candidate classes, each ending the last-wins fold's
    "governs the uic FOREVER" default — without which any later long on the
    uic that ends up with a sole standalone SL (protection-pass covering of an
    out-of-band fill, a manual buy plus manual stop) would be silently sold
    down the stale pick's targets:

    - UNFIRED (2026-08-19 adjudication finding 3): the watch ended with no
      fill (expired / suspended / KILL-cancelled) — the ladder never matched
      an order. Journal-only, position-blind, retracted immediately.
    - FIRED-terminal (#1223): a fill happened but the position ROUND-TRIPPED
      (durable closure evidence + a net-flat book) and every sibling tier is
      terminal — delegated to :func:`_retract_round_tripped_tranche_plans`
      with its belt-and-braces gates; skipped entirely when ``deps`` is
      ``None`` (direct-call tests, pre-#1223 harnesses).

    Only a plan whose governing ``pick_key`` MATCHES the ended pick is
    retracted: a keyless bracket plan (coupled to a real placement) and a
    newer pick's plan on the same uic are never touched, and the retraction
    line itself un-governs the uic so the sweep is idempotent (append-only,
    one line per ended pick). Runs every watch-pass tick; any journal failure
    degrades to a WARN and a retry next tick — never an aborted pass."""
    try:
        candidates = _terminal_watch_picks(fold)
        if not candidates:
            if deps is not None:
                deps.pending_plan_retractions.clear()
            return
        journal_lines = list(_iter_standalone_stop_journal())
        governing = _fold_governing_plan_pick_keys(journal_lines)
        for pick_key, (uic, fired, crids) in candidates.items():
            if fired:
                continue  # the fired class runs below with its own gates
            # #1249: an ended-unfired pick's fire-arm ``planned`` write-aheads
            # cover nothing (no fill ever happened) — retract them so a stale
            # line can never conflict a later fill on the uic. Keyed per-crid,
            # so this is orthogonal to the tranche governance check below (a
            # newer pick's lines are structurally untouchable) and idempotent
            # inside the helper.
            _retract_planned_lines(
                (_entry_fire_request_id(crid) for crid in crids),
                note=f"entry-trail {pick_key}: watch ended with no fill",
                journal_lines=journal_lines,
            )
            if governing.get(uic) != pick_key:
                continue  # keyless/bracket plan, newer pick, or already retracted
            _append_standalone_stop_journal(
                {"kind": _TRANCHE_PLAN_RETRACTED_KIND, "uic": uic, "pick_key": pick_key}
            )
            logger.info(
                "entry-trail %s: watch ended with no fill — retracted the tranche_plan for uic %d",
                pick_key,
                uic,
            )
        if deps is not None:
            _retract_round_tripped_tranche_plans(candidates, governing, journal_lines, deps)
    # Broad on purpose: the sweep is housekeeping inside the watch pass — a
    # journal read/write failure must degrade to a warning + retry next tick,
    # never abort the pass that advances live watches. The latch clears too:
    # a failed tick observed nothing, and every fired-class retraction must
    # rest on two consecutive CLEAN observations (#1223 zen M2).
    except Exception:
        if deps is not None:
            deps.pending_plan_retractions.clear()
        logger.warning("entry-trail: stale tranche_plan retraction sweep failed", exc_info=True)


def _retract_round_tripped_tranche_plans(
    candidates: Mapping[str, tuple[int, bool, tuple[str, ...]]],
    governing: Mapping[int, str | None],
    journal_lines: Sequence[Mapping[str, Any]],
    deps: LoopDeps,
) -> None:
    """The FIRED-terminal retraction class (#1223): a round-tripped pick's
    ``tranche_plan`` stops governing the uic, so a later manual/out-of-band
    long is never adopted onto the closed trade's ladder.

    Wrongly retracting under a live position would strip its exit management —
    strictly worse than the stale-ladder hazard being fixed — so every
    candidate must clear ALL of, in order:
      1. every tier terminal + ≥1 FIRED (from ``candidates``);
      2. the governing plan is the candidate's own (idempotence / newer pick /
         keyless — same rule as the unfired class);
      3. closure evidence since the current plan generation
         (:func:`_fold_round_trip_closures_since_latest_plan`) whose element
         is keyless or matches the pick — WITHOUT this, a positions read that
         lags a fresh fill would look flat (fail-deadly), and a held position
         with expired siblings would never be distinguishable from a closed
         one. A manually-closed position leaves no record, so its plan is
         (accepted limitation) never retracted;
      4. a NET-flat book on a fresh positions read — lazy: the endpoint is
         only consulted when a candidate survives 1-3, so the steady state
         adds zero REST calls. EOD-netting rows (+q/−q) net to flat
         (:func:`_net_open_position_uics`); any unresolvable row or read
         failure skips the class this tick (fail-safe), as does a broker
         without ``get_positions`` (test harnesses);
      5. the two-consecutive-tick confirmation latch
         (``deps.pending_plan_retractions``): retract only a candidate that
         ALSO passed 1-4 on the previous sweep. A sub-tick positions lag (the
         armed-sibling second-fire scenario: the same plan generation fills
         again with no new plan line, so 1-3 all pass on stale evidence)
         cannot survive two ~45 s polls; any class skip clears the latch, so
         every retraction rests on two consecutive CLEAN observations. A
         daemon restart costs one extra clean tick — the sweep stays fully
         re-derivable from the journals plus one positions read."""
    pending = deps.pending_plan_retractions
    try:
        fired_candidates = {
            (pick_key, uic)
            for pick_key, (uic, fired, _crids) in candidates.items()
            if fired and governing.get(uic) == pick_key
        }
        if fired_candidates:
            closures = _fold_round_trip_closures_since_latest_plan(journal_lines)
            fired_candidates = {
                (pick_key, uic)
                for pick_key, uic in fired_candidates
                if any(key is None or key == pick_key for key in closures.get(uic, ()))
            }
        if not fired_candidates:
            pending.clear()
            return
        get_positions = getattr(deps.broker, "get_positions", None)
        if get_positions is None:
            pending.clear()
            return
        open_uics, unresolvable = _net_open_position_uics(get_positions())
        if unresolvable:
            pending.clear()
            return
        passing = {(pk, uic) for pk, uic in fired_candidates if uic not in open_uics}
        confirmed = passing & pending
        for pick_key, uic in sorted(confirmed):
            _append_standalone_stop_journal(
                {"kind": _TRANCHE_PLAN_RETRACTED_KIND, "uic": uic, "pick_key": pick_key}
            )
            logger.info(
                "entry-trail %s: round trip closed and the book is flat — "
                "retracted the tranche_plan for uic %d",
                pick_key,
                uic,
            )
            # #1249: the closed trade's fire-arm ``planned`` write-aheads ride
            # the SAME confirmed write (closure evidence + net-flat book + the
            # two-tick latch) — retract-with, never retract-before. Fresh
            # journal read inside the helper: this tick may already have
            # appended markers.
            _retract_planned_lines(
                (_entry_fire_request_id(crid) for crid in candidates[pick_key][2]),
                note=f"entry-trail {pick_key}: round trip closed, book flat",
            )
        pending.clear()
        pending.update(passing - confirmed)
    # Broad like the caller's sweep boundary: a positions/journal failure must
    # degrade to a warning + a cleared latch (never a first-sight retraction
    # on the next tick), and never abort the unfired class already done.
    except Exception:
        pending.clear()
        logger.warning(
            "entry-trail: round-tripped tranche_plan retraction sweep failed", exc_info=True
        )


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


def _journal_stop_placed(
    uic: int,
    qty: float,
    *,
    order_id: str,
    ref: str,
    clock: Callable[[], float] = time.time,
) -> None:
    """Persist a timestamped ``stop_placed`` outcome record.

    Written by the executor ONLY on a confirmed standalone-stop placement, with the
    qty ACTUALLY placed (post execute-time clamp). Since #1219 the record also
    carries the broker ``order_id`` (the ONLY durable handle the stop-fill
    reconcile pass can later compare against the open-orders book — no other
    journal line retains it) and the deterministic ``ref``
    (``PlaceStop.request_id``) that renders the operator label on the fill alert.
    ``_fold_standing_stop_ids`` consumes both; fill-to-protection latency stays
    measurable as before (``oco_placed`` covers the OCO path). The ``clock`` seam
    keeps the record's ``ts`` testable (default wall clock)."""
    _append_standalone_stop_journal(
        {
            "kind": "stop_placed",
            "uic": int(uic),
            "qty": float(qty),
            "order_id": str(order_id),
            "ref": str(ref),
            "ts": float(clock()),
        }
    )


def _journal_stop_filled(
    uic: int,
    *,
    order_id: str,
    qty: float,
    avg_price: float | None,
    ref: str | None = None,
    partial: bool = False,
    clock: Callable[[], float] = time.time,
) -> None:
    """Append the terminal ``stop_filled`` line for a reconciled stop fill (#1219).

    The top-level ``order_id`` is the idempotence latch: once written,
    ``_fold_standing_stop_ids`` excludes the uic, so the reconcile pass never
    re-resolves or re-alerts the same fill (restart-safe by construction, like
    the entry-side ``fired`` line). ``qty`` / ``avg_price`` carry the realized
    exit the offline exec-quality join needs; ``avg_price`` may be ``None`` when
    the audit row's price is unparseable — the fill still terminates."""
    _append_standalone_stop_journal(
        {
            "kind": "stop_filled",
            "uic": int(uic),
            "order_id": str(order_id),
            "qty": float(qty),
            "avg_price": avg_price if avg_price is None else float(avg_price),
            # Durable retire-trigger fields (#1198 crash window): `ref` carries
            # the generation-exact pick attribution into the restart-safe
            # sweep; `partial` (PARTIALLY_FILLED terminal — residual exposure
            # remains) marks a fill that must announce but never retire.
            "ref": ref,
            "partial": bool(partial),
            "ts": float(clock()),
        }
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


def _journal_trailed(
    uic: int,
    level: float,
    *,
    peak: float | None = None,
    last_price: float | None = None,
    clock: Callable[[], float] = time.time,
) -> None:
    """Persist a timestamped ``trailed`` marker (Task 4). Written by the executor
    ONLY on a CONFIRMED trail AmendStop PATCH success (never on a failed attempt —
    a failed amend journals ``amend_failed`` like any other amend and simply
    retries). Mirrors ``_journal_reanchored``: ``_fold_trailed_since_latest_plan`` folds
    these into ``ProtectionView.trailed_stop_by_uic``, the never-DOWN ratchet floor
    a new trail proposal must clear by ``_TRAIL_STEP_EPS``.

    ``level`` is the stop price actually placed (the ratchet floor, read by the
    fold). ``peak`` / ``last_price`` are the high-water mark and live price the
    trail was computed from — telemetry substrate for the future /edge trailing
    lens; the fold ignores them, so a missing one is harmless (omitted here)."""
    marker: dict[str, Any] = {
        "kind": "trailed",
        "uic": int(uic),
        "level": float(level),
        "ts": float(clock()),
    }
    if peak is not None:
        marker["peak"] = float(peak)
    if last_price is not None:
        marker["last_price"] = float(last_price)
    _append_standalone_stop_journal(marker)


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


def _fold_trailed_since_latest_plan(lines: Iterable[Mapping[str, Any]]) -> dict[int, float]:
    """Fold the append-only ``trailed`` journal markers into the LATEST (by ``ts``)
    trailed ``level`` per uic (Task 2), RESET on each new-generation
    ``tranche_plan`` line. Mirrors ``_fold_reanchored_markers`` — a DICT, not a
    TTL frozenset — but reads ``line["level"]`` (the price the stop was
    confirmed trailed to) instead of the reanchor avg_price.

    The generation reset uses the SAME identity rules as
    ``_fold_fired_since_latest_plan`` (see its docstring for the incident
    history): a keyless ``tranche_plan`` or one with a DIFFERENT ``pick_key``
    is a NEW trade in the uic and drops the accumulated level — the journal is
    append-only and uic-keyed, so without the reset a re-entered position
    would inherit the PRIOR trade's trailed level. That stale level used to be
    merely a too-high ratchet floor that silently blackholed trailing for the
    new position; once the level also feeds ``ManagedExit.stop_price`` it
    would be actively PLACED (an absurdly high SL on a fresh entry), so the
    reset is load-bearing for both consumers. A ``tranche_plan`` re-appended
    with the SAME ``pick_key`` (the already_watching crash-recovery re-drive)
    does NOT reset; ``tranche_plan_retracted`` clears. Feeds
    ``ProtectionView.trailed_stop_by_uic`` (the never-DOWN ratchet floor
    ``_maybe_trail`` requires a new proposal to clear by ``_TRAIL_STEP_EPS``)
    and the live-exit engine's SL-amend level via ``_build_managed_exits``.
    Malformed (missing / unparsable uic, level, or ts) lines are skipped."""
    latest_ts: dict[int, float] = {}
    latest_level: dict[int, float] = {}
    governing_key: dict[int, str] = {}
    for line in lines:
        uic = _coerce(line, "uic", int)
        if uic is None:
            continue
        kind = line.get("kind")
        if _apply_generation_reset(kind, line, uic, governing_key, (latest_ts, latest_level)):
            continue
        if kind == "trailed":
            try:
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


def _track_tranche_plan(
    line: Mapping[str, Any],
    uic: int,
    *,
    ladder_line: dict[int, Mapping[str, Any]],
    latest_plan: dict[int, Mapping[str, Any]],
    governing_key: dict[int, str],
    fired_lines: dict[int, list[Mapping[str, Any]]],
    closure_lines: dict[int, dict[str | None, Mapping[str, Any]]],
) -> None:
    """Advance the tranche-compaction state for one ``tranche_plan`` line,
    delegating the identity-keyed reset to ``_apply_generation_reset`` (the
    live folds' single implementation): a keyless line or a ``pick_key``
    differing from the uic's governing key resets the fired and closure
    accumulators; a same-key re-append (the crash-recovery re-drive) does
    not. The line becomes the uic's latest plan always, and its
    ladder-governing plan only when ``fold_tranche_plans`` accepts it as
    well-formed (a corrupt line still resets fired but never governs the
    ladder — the folds' own semantics)."""
    _apply_generation_reset(
        _TRANCHE_PLAN_KIND, line, uic, governing_key, (fired_lines, closure_lines)
    )
    latest_plan[uic] = line
    if fold_tranche_plans([line]):
        ladder_line[uic] = line


def _compact_tranche_lines(lines: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The kept tranche-ladder lines: per uic, the governing plan line(s)
    followed by the fired and closure-evidence lines still counting, so
    ``fold_tranche_plans``, ``_fold_fired_since_latest_plan``,
    ``_fold_governing_plan_pick_keys``, and
    ``_fold_round_trip_closures_since_latest_plan`` all return exactly what
    they return on the full journal.

    Per uic the election keeps, in this write order (the folds process lines
    in order — the governing plan MUST precede its fired/closure lines so a
    record written before a same-key re-append is never mistaken for a
    pre-reset leftover, and so the closure fold's open-generation gate sees
    the plan first despite the compactor reordering the file):
      1. the plan line whose values are the uic's final folded ladder (if any);
      2. the LATEST plan line, when distinct from (1) — it may be
         ladder-malformed yet still carry the fired fold's reset identity and
         the retraction sweep's governing ``pick_key``;
      3. every ``tranche_fired`` line inside the fired fold's END accumulator
         (fired tags reset away by a later plan/retraction are dropped) — a
         tag-less line survives here only when it carries ``position_closed``
         (worthless to the fired fold, closure evidence to #1223's);
      4. the NEWEST full ``stop_filled`` per (uic, parsed ref key) inside the
         closure fold's END accumulator (#1223) — the top-level compactor's
         own ``stop_filled`` keep is keyed to the newest ``stop_placed``, so a
         stop rotation AFTER the fill would otherwise drop the round-trip
         evidence at startup and the stale plan would silently survive.
    ``tranche_plan_retracted`` markers are consumed during the election — a
    retraction erases the uic's kept plan, fired and closure lines, so a fully
    retracted uic keeps NOTHING (the folds already treat it as absent).
    Pure; kept lines are shallow-copied."""
    ladder_line: dict[int, Mapping[str, Any]] = {}
    latest_plan: dict[int, Mapping[str, Any]] = {}
    governing_key: dict[int, str] = {}
    fired_lines: dict[int, list[Mapping[str, Any]]] = {}
    closure_lines: dict[int, dict[str | None, Mapping[str, Any]]] = {}
    for line in lines:
        kind = line.get("kind")
        if kind not in (
            _TRANCHE_PLAN_KIND,
            _TRANCHE_PLAN_RETRACTED_KIND,
            _TRANCHE_FIRED_KIND,
            "stop_filled",
        ):
            continue
        uic = _coerce(line, "uic", int)
        if uic is None:
            continue
        if kind == _TRANCHE_PLAN_KIND:
            _track_tranche_plan(
                line,
                uic,
                ladder_line=ladder_line,
                latest_plan=latest_plan,
                governing_key=governing_key,
                fired_lines=fired_lines,
                closure_lines=closure_lines,
            )
        elif kind == _TRANCHE_PLAN_RETRACTED_KIND:
            ladder_line.pop(uic, None)
            latest_plan.pop(uic, None)
            governing_key.pop(uic, None)
            fired_lines.pop(uic, None)
            closure_lines.pop(uic, None)
        elif kind == "stop_filled":
            # Closure evidence only while the generation is open — mirroring
            # the closure fold's own gate (a pre-plan fill never counts).
            if uic in latest_plan and not line.get("partial"):
                ref = line.get("ref")
                key = _pick_key_from_stop_ref(ref if isinstance(ref, str) else None)
                closure_lines.setdefault(uic, {})[key] = line
        elif line.get("tag") or line.get("position_closed"):
            fired_lines.setdefault(uic, []).append(line)
    kept: list[dict[str, Any]] = []
    for uic in sorted(set(latest_plan) | set(fired_lines) | set(closure_lines)):
        ladder = ladder_line.get(uic)
        latest = latest_plan.get(uic)
        if ladder is not None:
            kept.append(dict(ladder))
        if latest is not None and latest is not ladder:
            kept.append(dict(latest))
        kept.extend(dict(fired) for fired in fired_lines.get(uic, ()))
        kept.extend(dict(fill) for fill in closure_lines.get(uic, {}).values())
    return kept


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
        / ``amend_ok`` outcome record per uic (the latest outcome is what latency
        inspection needs; since #1219 ``stop_placed`` also feeds
        ``_fold_standing_stop_ids``), and for each KEPT ``stop_placed`` its
        matching ``stop_filled`` terminal (newest per order_id) — dropping it
        would resurrect the uic as a reconcile candidate and re-alert a fill
        already announced (a ``stop_filled`` for any OTHER order id cannot
        affect that fold, but #1223's closure fold may still need it — see the
        tranche election below; when both elections pick the same line the
        tranche copy is the one kept);
      - the ``amend_seq`` carrying the MAX seq per uic (``_read_persisted_amend_seq``
        returns that max);
      - the tranche-ladder lines ``_compact_tranche_lines`` elects — per uic the
        governing ``tranche_plan`` line(s) followed by the ``tranche_fired``
        lines still inside ``_fold_fired_since_latest_plan``'s accumulator and
        the ``stop_filled`` closure evidence still inside
        ``_fold_round_trip_closures_since_latest_plan``'s (#1223), so
        ``fold_tranche_plans`` / ``_fold_fired_since_latest_plan`` / the
        retraction sweep's ``_fold_governing_plan_pick_keys`` / the closure
        fold are all unchanged; ``tranche_plan_retracted`` markers are
        consumed during the election (a fully retracted uic keeps nothing).
        ``planned_retracted`` markers (#1249) are consumed the same way — the
        per-crid election above runs through ``_latest_planned_by_crid``, so a
        fully retracted crid keeps neither its planned line nor the marker.

    Every other line — ``gen`` markers (read only by ``_read_persisted_gen``, whose
    reset to the initial gen is harmless: post-restart re-emits are past Saxo's 15s
    request-id dedup window, and protection is broker-state-truth not journal-derived),
    unknown kinds, and malformed lines — is dropped; none contributes to the
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
    # Newest stop_filled per ORDER ID (not uic): only the one matching a kept
    # stop_placed matters for _fold_standing_stop_ids, elected below.
    stop_filled_by_id: dict[str, tuple[float, dict[str, Any]]] = {}

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
        elif kind == "stop_filled":
            order_id = line.get("order_id")
            ts = _coerce(line, "ts", float)
            if isinstance(order_id, str) and order_id and ts is not None:
                kept = stop_filled_by_id.get(order_id)
                if kept is None or ts >= kept[0]:
                    stop_filled_by_id[order_id] = (ts, dict(line))

    compacted: list[dict[str, Any]] = list(planned)
    compacted.extend(oco_unsupported[uic] for uic in sorted(oco_unsupported))
    compacted.extend(ttl_latest["oco_placed"][uic][1] for uic in sorted(ttl_latest["oco_placed"]))
    compacted.extend(
        ttl_latest["amend_failed"][uic][1] for uic in sorted(ttl_latest["amend_failed"])
    )
    compacted.extend(ttl_latest["oco_too_far"][uic][1] for uic in sorted(ttl_latest["oco_too_far"]))
    compacted.extend(ttl_latest["stop_placed"][uic][1] for uic in sorted(ttl_latest["stop_placed"]))
    # The tranche election may keep the SAME stop_filled line as round-trip
    # closure evidence (#1223) — positioned AFTER its plan line, where the
    # closure fold's open-generation gate can see it. _fold_standing_stop_ids
    # collects filled ids over the whole file (position-insensitive), so when
    # both elections pick one line the closure copy serves both folds and the
    # stop_placed-matched keep below skips it.
    tranche_kept = _compact_tranche_lines(materialized)
    closure_fill_keys = {
        (line.get("order_id"), line.get("ts"))
        for line in tranche_kept
        if line.get("kind") == "stop_filled"
    }
    for uic in sorted(ttl_latest["stop_placed"]):
        kept_order_id = ttl_latest["stop_placed"][uic][1].get("order_id")
        if isinstance(kept_order_id, str) and kept_order_id in stop_filled_by_id:
            matched = stop_filled_by_id[kept_order_id][1]
            if (matched.get("order_id"), matched.get("ts")) not in closure_fill_keys:
                compacted.append(matched)
    compacted.extend(ttl_latest["amend_ok"][uic][1] for uic in sorted(ttl_latest["amend_ok"]))
    compacted.extend(amend_seq[uic][1] for uic in sorted(amend_seq))
    compacted.extend(tranche_kept)
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

    path = _standalone_stop_journal_path()
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
    broker: Broker,
    exit_policy: ExitPolicy | None = None,
    *,
    alert_throttled: Callable[[str, str], bool] | None = None,
    day1_gap_price_probe: Callable[[str, str], float | None] | None = None,
    audit_budget: OutcomeAuditBudget | None = None,
    now_entry_feed_factory: Callable[..., Any] | None = None,
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
    pre-Task-4 behavior.

    ``alert_throttled`` (design memo §4 fee floor) is the same throttled-alert
    sink ``LoopDeps.alert_throttled`` wraps — the composition root threads its
    ONE daemon-lifetime ``_AlertThrottle`` in here so a fee-floor refusal pages
    the operator via the usual channel. ``None`` (every pre-existing call site
    /test) is tolerated — the fee floor still refuses + journals, it just does
    not page.

    ``day1_gap_price_probe`` (execution-quality placement discipline) is the
    same probe ``LoopDeps.day1_gap_price_probe`` carries — see the "Day-1 gap
    gate" section above. ``None`` (every pre-day1-gap call site/test) is
    tolerated: the gate only calls it when ``ALPHALENS_BROKER_DAY1_GAP_GATE``
    is armed, and a ``None`` probe there simply defers every day-1 pick."""

    def _place(pick: Any) -> bool:
        return _place_pick(
            broker,
            pick,
            exit_policy,
            alert_throttled=alert_throttled,
            day1_gap_price_probe=day1_gap_price_probe,
            audit_budget=audit_budget,
            now_entry_feed_factory=now_entry_feed_factory,
        )

    return _place


def _summarize_open_verdicts(open_verdicts: Iterable[Any], today_iso: str) -> tuple[int, float]:
    """Fold open verdicts into the safety.JournalView inputs
    ``(open_bracket_count, realized_r_today)``: still-working verdicts are
    counted for the MAX_OPEN rail, and ``realized_r_today`` sums today's
    closed R for the daily-loss rail.

    No committed-gross term, and so no ``records`` argument (#1192): the gross
    rail moved to :func:`_check_gross_cap`, which values exposure post-sizing
    in account currency and builds its own verdict-to-bracket join because it
    also needs each record's journaled ``fx_rate``. This function no longer
    reads the journal at all — it is a pure fold over verdicts."""
    open_bracket_count = 0
    realized_r_today = 0.0
    for verdict in open_verdicts:
        realized_r = verdict.details.get("realized_r")
        realized_date = (verdict.activity_time or "")[:10] or verdict.brief_date
        if realized_r is not None and realized_date == today_iso:
            realized_r_today += float(realized_r)
        if verdict.status in {"WORKING", "PARTIALLY_FILLED"}:
            open_bracket_count += 1
    return open_bracket_count, realized_r_today


def _resolve_sizing_equity(account_equity: float) -> float:
    """Effective sizing equity for ``compute_setup_plan`` (design memo §4 +
    the declared-frame memo §4.1). Two modes, selected by
    ``ALPHALENS_BROKER_SIZING_EQUITY_MODE`` (read at CALL TIME, like the pin,
    so an operator edit takes effect on the daemon's next restart):

    - ``clamped`` (or unset — today's behavior): ``min(pinned, snapshot)``
      when ``ALPHALENS_BROKER_SIZING_EQUITY`` is explicitly set, else
      ``account_equity`` unchanged — SIM never sets the pin, so this stays
      byte-identical to the raw account snapshot on the SIM path. ``min``
      survives BOTH failure directions: a pin set too high above the real
      balance stays capped at the snapshot, and a balance that has dropped
      below the frame stays capped at the snapshot too.
    - ``declared``: the pin IS the frame — no ``min()``; the cash floor
      (PR-2) guards the real balance. A declared mode with NO pin fails
      CLOSED to zero equity (critic B8) — never the raw snapshot.

    Every malformed/unknown value fails CLOSED to zero sizing equity:
    raw-snapshot sizing (scaling picks to the FULL real balance) is the one
    thing this must never fall back to."""
    from alphalens_pipeline.brokers.automanager.live_rails import (
        _VALID_SIZING_MODES,
        SIZING_EQUITY_ENV,
        SIZING_EQUITY_MODE_ENV,
        SIZING_MODE_CLAMPED,
        SIZING_MODE_DECLARED,
    )

    mode_raw = os.environ.get(SIZING_EQUITY_MODE_ENV)
    mode = (mode_raw or "").strip().lower() or SIZING_MODE_CLAMPED
    if mode not in _VALID_SIZING_MODES:
        logger.warning(
            "%s=%r is not a valid sizing mode (valid: %s) — failing CLOSED to zero sizing equity",
            SIZING_EQUITY_MODE_ENV,
            mode_raw,
            ", ".join(_VALID_SIZING_MODES),
        )
        return 0.0
    pinned_raw = os.environ.get(SIZING_EQUITY_ENV)
    if pinned_raw is None or not pinned_raw.strip():
        if mode == SIZING_MODE_DECLARED:
            # FAIL-CLOSED (critic B8): declared mode without a pin has no
            # frame to size against — the raw snapshot is exactly the
            # fallback the declared frame exists to prevent.
            logger.warning(
                "%s=declared but %s is unset/blank — failing CLOSED to zero sizing equity",
                SIZING_EQUITY_MODE_ENV,
                SIZING_EQUITY_ENV,
            )
            return 0.0
        return account_equity
    try:
        pinned = float(pinned_raw)
    except ValueError:
        # FAIL-CLOSED: a typo'd pin must never crash the tick, and it must
        # never fall back to the raw snapshot either (sizing off the FULL
        # real balance is the exact outcome the pin exists to prevent).
        # Zero equity sizes nothing; the unplannable/zero-tiers refusal
        # path downstream handles the pick, and the operator fixes the pin.
        logger.warning(
            "%s=%r is not a number — failing CLOSED to zero sizing equity",
            SIZING_EQUITY_ENV,
            pinned_raw,
        )
        return 0.0
    if mode == SIZING_MODE_DECLARED:
        return pinned
    return min(pinned, account_equity)


def _resolve_and_size(
    broker: Broker,
    ticker: str,
    account: Any,
    spec: Any,
    hint_mic: str | None = None,
) -> tuple[Any, Any, Any] | None:
    """Resolve the instrument, build any needed FX conversion, and size the
    already-parsed :class:`~broker_contract.trade_intent.schema.TradeSpec`.
    Returns ``(instrument, fx, plan)`` or ``None`` on any resolve/size failure
    (logged) — one bad pick must never crash a tick.

    ``hint_mic`` is the intent's ``InstrumentHint.mic`` (#1238):
    ``explicit_mic_from_hint`` keeps US hints on the probe path (a brief pick
    hints XNYS while its real venue may be XNAS) and turns a non-US hint
    (``arm-manual``'s operator venue, e.g. XWAR) into an explicit
    single-venue resolve.

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
    from alphalens_pipeline.brokers.routing import explicit_mic_from_hint, resolve_us_instrument

    try:
        instrument = resolve_us_instrument(
            broker, ticker, exchange_mic=explicit_mic_from_hint(hint_mic)
        )
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
            paper_equity=_resolve_sizing_equity(account.total_value),
            scale_factor=1.0,
            fx=fx,
        )
    except (BrokerError, TradeSetupNotPlannableError) as exc:
        logger.warning("place_pick %s: resolve/size failed: %s", ticker, exc)
        return None

    return instrument, fx, plan


# --- Fee floor (design memo §4 round-trip fee equation) ----------------------
#
# The model itself lives in ``alphalens_pipeline.brokers.automanager.costs`` (extracted for #1112 so
# the placement fee floor and the exit-time cost gate share ONE model):
#
#   fee_rt(N) = 2 x max(MIN_COMMISSION_USD, COMMISSION_RATE x N)
#               + (FX_ROUND_TRIP_RATE x N if an FX conversion applies else 0)


def _check_fee_floor(
    plan: Any, fx: Any, *, ticker: str, instrument_currency: str = "USD"
) -> str | None:
    """``None`` iff the pick clears the round-trip fee floor OR
    ``ALPHALENS_BROKER_MAX_FEE_BPS`` is unset (SIM — no fee floor, byte-
    identical to pre-fee-floor behavior). Else a refusal message naming the
    ticker, the estimated fee, and the cap (design memo §4) — never feeds
    back into selection (R2), just a fee fact reported to the operator.

    Prices the plan with ``_estimate_round_trip_fee_bps`` — the SAME per-tier
    model already journaled as ``est_round_trip_fee_bps`` on every placement,
    so the gate and the journal can never disagree about the cost of one plan
    (#1123). Every commission minimum below roughly $1,250 per order is a flat
    $1, so at our notionals the estimate is a COUNT of chargeable orders; the
    older aggregate model counted exactly two however deep the ladder was, and
    understated a real 3-tier SMG ladder by 120 bps (110.2 vs 230.7 journaled).

    Falls back to the aggregate ``round_trip_fee_bps`` over
    ``setup_plan_gross_notional`` when the per-tier model returns an honest
    ``None`` (no sized plan / no tiers / zero gross) — the floor must always
    answer, never crash the tick on a degenerate plan. The refusal message
    names which model produced the number so the operator is never guessing.

    NOTE (#1123): the per-tier model assumes ONE chargeable order per tier.
    Saxo charges the minimum per order per EXECUTION DAY, so a tier resting as
    GTD and filling across two days pays twice — neither model expresses that.
    The mirrored exit is likewise an assumption, not a derivation: a
    geometry-policy pick currently places a single 100% tranche."""
    from alphalens_pipeline.brokers.automanager.live_rails import MAX_FEE_BPS_ENV

    max_fee_bps_raw = os.environ.get(MAX_FEE_BPS_ENV)
    if max_fee_bps_raw is None or not max_fee_bps_raw.strip():
        return None
    try:
        max_fee_bps = float(max_fee_bps_raw)
    except ValueError:
        # FAIL-CLOSED: a typo'd cap must never crash the tick and must never
        # silently disable the floor (fail-open). Refuse the pick with a
        # message naming the env var — the operator fixes the unit.
        return (
            f"fee floor: {MAX_FEE_BPS_ENV}={max_fee_bps_raw!r} is not a number — "
            f"{ticker} refused (fail-closed until the cap is fixed)"
        )

    from broker_contract.sizing import setup_plan_gross_notional

    notional = setup_plan_gross_notional(plan)
    fee_bps = _estimate_round_trip_fee_bps(plan, fx, instrument_currency=instrument_currency)
    model = "per-tier"
    if fee_bps is None:
        # FAIL-OPEN, deliberately — and NOT the same class as the malformed-cap
        # branch above, which fails CLOSED. That one is an operator typo in
        # configuration: the floor is live but unreadable, so refusing is the
        # only safe answer. THIS one is a plan that honestly prices to nothing
        # — the only shape that reaches here is an all-zero-qty tier set (gross
        # 0), because `compute_setup_plan` raises TradeSetupNotPlannableError
        # for anything less. Such a plan places NO order: `classify` yields no
        # tiers and `_place_pick` returns before `_place_tiers` ("every entry
        # tier sized to zero shares"). So passing it here costs nothing, and
        # refusing it would attribute the refusal to the fee floor instead of
        # to the sizing that actually produced it. Logged so a LIVE rail never
        # takes this path silently.
        model = "aggregate"
        logger.warning(
            "fee floor: %s — per-tier model could not price the plan (gross %.2f), "
            "falling back to the aggregate model",
            ticker,
            notional,
        )
        fallback_card = fee_card_for(instrument_currency)
        fee_bps = round_trip_fee_bps(
            notional,
            fx_applies=fx is not None,
            min_commission_applies=fallback_card is not None or instrument_currency == "USD",
            card=fallback_card if fallback_card is not None else US_FEE_CARD,
        )
    if fee_bps <= max_fee_bps:
        return None
    return (
        f"fee floor: {ticker} round-trip {fee_bps:.1f} bps > cap {max_fee_bps:.1f} bps "
        f"({model} model, notional {notional:,.2f}) — pick refused"
    )


def _estimate_round_trip_fee_bps(
    plan: Any, fx: Any, *, instrument_currency: str = "USD"
) -> float | None:
    """The HONEST per-tier round-trip fee estimate in bps of the plan's gross
    (broker sizing memo §4.5, amended by operator decision §7.3) — journaled
    on every placement as the calibration series for path B's 150 bps target,
    and since #1123 also the number the fee FLOOR (``_check_fee_floor``) gates
    on, so the gate and the journal price one plan the same way. The aggregate
    ``round_trip_fee_bps`` survives only as the floor's fallback for when this
    returns ``None``.

    - ``entry_fees``: each non-zero tier pays its own commission
      ``max($1, 0.08% x qty x limit)`` — zero-qty tiers are never POSTed
      (``_ZERO_QTY_TIER_POLICY``), so they pay nothing. The $1 minimum is a
      USD figure, gated on ``instrument_currency`` exactly like
      ``round_trip_fee_bps``.
    - ``exit_fees``: the same shape over the TP tranches, with tranche qtys
      derived at placement as ``tranche_frac x total entry qty``
      (the brief-shaped ``TpTrancheSpec.tranche_pct`` is a PERCENTAGE 0-100;
      ``compute_setup_plan`` converts it ONCE into the plan's fraction, so
      nothing downstream divides by 100 again — this function used to, and
      priced the whole exit leg at 1% of the position). When the
      plan carries NO tranches (geometry-policy picks express the exit in
      ``exit_spec``, not static tranches) the estimate MIRRORS the entry fees
      — a symmetric single-exit assumption, deliberately simple over falsely
      precise.
    - ``fx_cost``: the 0.50% FX round trip on the gross when a conversion
      applies.

    ``None`` (an honest "not estimable", journaled as a real null) when there
    is no sized plan / no tiers / zero gross — mirrors the inert stance of
    ``round_trip_fee_bps`` on a non-positive notional."""
    # Two separate refusals, not one compound expression: a reader (and a type
    # checker) can then see that everything below this point has a real plan.
    # The compound form left ``plan`` optional for the whole body while the
    # ``gross`` call below requires one.
    if plan is None:
        return None
    entry_tiers = getattr(plan, "entry_tiers", None)
    if not entry_tiers:
        return None

    from broker_contract.sizing import setup_plan_gross_notional

    gross = setup_plan_gross_notional(plan)
    if gross <= 0:
        return None
    # #1238 PR 3: price the venue's own card when one exists (WSE 0.12% min
    # PLN 10); a currency with no verified card keeps the legacy shape (US
    # rate, minimum only for USD).
    card = fee_card_for(instrument_currency)
    commission_rate = card.commission_rate if card is not None else COMMISSION_RATE
    min_commission = card.min_commission if card is not None else MIN_COMMISSION_USD
    min_commission_applies = card is not None or instrument_currency == "USD"

    def _fill_fee(qty: float, price: float) -> float:
        ad_valorem = commission_rate * qty * price
        if min_commission_applies:
            return max(min_commission, ad_valorem)
        return ad_valorem

    entry_fees = sum(_fill_fee(t.qty, t.limit_price) for t in entry_tiers if t.qty > 0)
    tranches = getattr(plan, "tp_tranches", None) or ()
    if tranches:
        # Same `qty > 0` filter as entry_fees above. Zero-qty tiers add zero
        # either way; keeping the two sums written the same way stops a reader
        # hunting for a difference that is not there.
        total_qty = sum(t.qty for t in entry_tiers if t.qty > 0)
        exit_fees = sum(_fill_fee(total_qty * t.tranche_frac, t.target_price) for t in tranches)
    else:
        exit_fees = entry_fees
    fx_cost = FX_ROUND_TRIP_RATE * gross if fx is not None else 0.0
    return (entry_fees + exit_fees + fx_cost) / gross * 10000.0


# --- Post-sizing portfolio gross cap (broker sizing memo §3) -----------------
#
# THE gross rail — there is no longer a second one. A pre-sizing arm lived in
# safety.check until #1192 and was broken three ways: (1) currency mismatch —
# the journal's committed sum is entry x qty in INSTRUMENT currency (USD)
# against a limit in ACCOUNT currency (PLN), ~3.7x looser than it read;
# (2) candidate exclusion — it ran BEFORE _resolve_and_size, so the first pick
# of any size always passed; (3) filled-position blindness — only
# WORKING/PARTIALLY_FILLED verdicts counted, filled exposure dropped out.
#
# It was removed rather than repaired, and the distinction is per TERM: the
# committed-working term could have been valued correctly pre-sizing from each
# record's journaled fx_rate (exactly what _committed_working_gross_acct does
# below), but the candidate has no rate or size until _resolve_and_size runs
# AFTER safety.check, and filled exposure needs a rate too. Fixing only the
# term that was fixable leaves a candidate-blind, filled-blind rail — still
# unable to bound exposure, and still shadowed by this one.
#
# THIS check (a sibling of the fee floor, same inputs, zero new broker I/O):
#
#   committed_working_acct + candidate_gross_acct + filled_positions_acct
#       <= GROSS_FRAC x account.total_value


def _committed_working_gross_acct(
    open_verdicts: Iterable[Any], records: Iterable[Mapping[str, Any]]
) -> tuple[float, int]:
    """``(total, unjoined)`` — the still-working journaled entry gross folded
    into ACCOUNT currency, plus the count of working verdicts that could NOT
    be joined back to a journaled entry bracket.

    WORKING/PARTIALLY_FILLED verdicts joined back to their journaled entry
    bracket — the join the removed pre-sizing rail used to do untyped — but
    each bracket's ``entry x qty`` — INSTRUMENT currency — is
    converted through that record's OWN journaled ``fx_rate`` (submission_log
    schema 2: the account-ccy -> instrument-ccy Mid the sizing used), so
    mixed-vintage rates never revalue each other. ``fx_rate`` null
    (same-currency / schema-1 era) folds as-is.

    ``unjoined`` counts working verdicts whose ``client_request_id`` matches
    no journaled bracket (or a bracket missing entry/qty) — exposure that
    EXISTS at the broker but cannot be valued from the journal. The caller
    fails CLOSED on it (zen pre-merge finding): silently skipping would
    understate committed gross and let a pick through over the true cap.
    ``_summarize_open_verdicts`` no longer folds gross at all (#1192) — it
    counts slots and today's realized R; THIS fold is the only gross valuation."""
    entry_fx_by_request_id: dict[str, tuple[Mapping[str, Any], Any]] = {
        str(bracket.get("client_request_id")): (bracket, record.get("fx_rate"))
        for record in records
        for bracket in record.get("brackets") or []
    }
    total = 0.0
    unjoined = 0
    for verdict in open_verdicts:
        if verdict.status not in {"WORKING", "PARTIALLY_FILLED"}:
            continue
        joined = entry_fx_by_request_id.get(str(verdict.details.get("client_request_id") or ""))
        if joined is None:
            unjoined += 1
            continue
        bracket, fx_rate = joined
        if bracket.get("entry") is None or bracket.get("qty") is None:
            unjoined += 1
            continue
        notional = float(bracket["entry"]) * float(bracket["qty"])
        if fx_rate is not None:
            # rate is instrument-ccy per 1 account-ccy -> acct = instr / rate.
            notional /= float(fx_rate)
        total += notional
    return total, unjoined


def _filled_positions_gross_acct(
    positions: Iterable[Any],
    fx: Any,
    *,
    account_currency: str = "",
    rate_lookup: Callable[[str], float | None] | None = None,
) -> tuple[float, str | None]:
    """``(total, None)`` — the broker positions' mark-to-market gross in
    ACCOUNT currency — or ``(0.0, failure)`` when any position carries no
    usable mark or a currency that cannot be converted.

    Valuation choice: ``Position.market_value`` (Saxo
    ``PositionView.MarketValue`` — qty x current market price, INSTRUMENT
    currency) is the one current-price field the position row carries;
    ``avg_price`` is the stale open price and would mis-state exposure after
    any move. A ``None`` mark (SIM NoAccess) cannot be valued conservatively
    HIGH without a price, so it FAILS CLOSED — the caller refuses the pick
    with an alert rather than silently skipping the position. ``abs`` because
    gross exposure ignores position sign.

    Mixed-currency book (#1238 PR 4 — pre-#1238 this failed closed the moment
    ANY stamped currency differed from the candidate's fx, so the first GPW
    position alongside USD ones would have refused every placement). Per
    position, by its stamped ``instrument.currency``:

    - ``""`` (not stamped — best-effort reverse lookup rows,
      ``InstrumentRef`` docstring): today's path byte-identical — the
      candidate ``fx.rate`` when present, else raw. Absent is not wrong.
    - equal to the candidate fx's instrument currency: the candidate rate.
    - equal to ``account_currency``: already account currency, folds raw.
    - anything else: converted through ``rate_lookup`` (ONE lookup per
      distinct currency per attempt — it is broker I/O inside a money gate);
      no lookup available or no rate producible fails CLOSED, exactly like a
      missing mark."""
    expected_ccy = getattr(fx, "instrument_currency", "") if fx is not None else ""
    rates: dict[str, float] = {}
    total = 0.0
    for position in positions:
        position_ccy = getattr(position.instrument, "currency", "") or ""
        if position.market_value is None:
            return 0.0, (
                f"position {position.instrument.ticker} has no broker mark "
                "(market_value=None) — cannot value gross exposure, failing closed"
            )
        value_instr = abs(float(position.market_value))
        # Legacy path first, byte-identical: a candidate fx with no stamped
        # instrument currency (or an unstamped/matching position) converts
        # through the candidate rate exactly as before #1238; fx=None with an
        # unstamped position folds raw.
        if fx is not None and (not expected_ccy or position_ccy in ("", expected_ccy)):
            total += value_instr / float(fx.rate)
            continue
        if fx is None and not position_ccy:
            total += value_instr
            continue
        if account_currency and position_ccy == account_currency:
            total += value_instr
            continue
        rate = rates.get(position_ccy)
        if rate is None and rate_lookup is not None:
            looked_up = rate_lookup(position_ccy)
            if looked_up is not None and looked_up > 0.0:
                rate = float(looked_up)
                rates[position_ccy] = rate
        if rate is None:
            return 0.0, (
                f"position {position.instrument.ticker} trades in {position_ccy} and no "
                f"conversion into the account currency is available — cannot value gross "
                "exposure through a foreign rate, failing closed"
            )
        total += value_instr / rate
    return total, None


def _make_position_rate_lookup(broker: Any, account_currency: str) -> Callable[[str], float | None]:
    """A policy-checked ``instrument-per-account`` rate per foreign currency
    for :func:`_filled_positions_gross_acct` (#1238 PR 4). Reuses the SAME
    quote source and acceptance policy as candidate sizing
    (``broker.get_fx_rate`` -> ``build_fx_conversion``); any failure —
    missing capability, a broker error, a policy-rejected quote — returns
    ``None`` and the fold fails closed."""

    def _lookup(currency: str) -> float | None:
        get_fx_rate = getattr(broker, "get_fx_rate", None)
        if get_fx_rate is None or not account_currency:
            return None
        from broker_contract.contract import BrokerError
        from broker_contract.sizing import TradeSetupNotPlannableError

        from alphalens_pipeline.brokers.execution import build_fx_conversion

        try:
            return float(build_fx_conversion(get_fx_rate(account_currency, currency)).rate)
        except (BrokerError, TradeSetupNotPlannableError, TypeError, ValueError) as exc:
            logger.warning(
                "gross cap: FX lookup %s->%s failed (%s) — the fold fails closed",
                account_currency,
                currency,
                exc,
            )
            return None

    return _lookup


def _check_gross_cap(
    plan: Any,
    fx: Any,
    *,
    account: Any,
    open_verdicts: Iterable[Any],
    records: Iterable[Mapping[str, Any]],
    positions: Iterable[Any],
    ticker: str,
    entry_trail_fold: entry_trails.EntryTrailFold | None = None,
    broker: Any = None,
) -> str | None:
    """``None`` iff the pick keeps total gross exposure — still-working
    journaled entries + THIS candidate + filled positions + WATCHING trail
    tiers, all in ACCOUNT currency — within ``GROSS_FRAC x
    account.total_value``; else a terminal refusal message naming the total,
    its components, the limit, GROSS_FRAC and total_value.

    ``GROSS_FRAC`` is read THROUGH ``safety.PORTFOLIO_GROSS_FRAC_ENV`` and
    ``safety.DEFAULT_PORTFOLIO_GROSS_FRAC`` with the same ``_float_env``
    fallback, so this rail can never drift from the configured name or
    default even though ``safety.check`` no longer reads them itself.
    (``safety`` still OWNS the env contract; #1192 removed only its rail.) The candidate
    folds its RAW planned gross (``setup_plan_gross_notional``) — explicitly
    NO cash/fee buffer: the cap measures EXPOSURE, not funding.

    The watching term (entry-trailing memo G5) folds the limit-valued virtual
    reservation of NON-terminal entry-trail tiers from ``entry_trails.jsonl``
    — those tiers have NO broker order yet, so they are invisible to the
    committed-working fold. It applies in BOTH sizing modes (the cash floor
    is inert outside declared mode, so THIS rail must carry the virtual fold
    everywhere); no/empty journal folds to exactly 0.0, and the refusal text
    only names the component when it is non-zero (PR-T0 inertness)."""
    from broker_contract.sizing import setup_plan_gross_notional

    from alphalens_pipeline.brokers.automanager import safety

    gross_frac = safety._float_env(
        safety.PORTFOLIO_GROSS_FRAC_ENV, safety.DEFAULT_PORTFOLIO_GROSS_FRAC
    )

    candidate_acct = setup_plan_gross_notional(plan)
    if fx is not None:
        # rate is instrument-ccy per 1 account-ccy -> acct = instr / rate.
        candidate_acct /= float(fx.rate)

    committed_acct, unjoined = _committed_working_gross_acct(open_verdicts, records)
    if unjoined:
        # Fail CLOSED on journal join-skew (zen pre-merge finding): a working
        # verdict we cannot value means real broker exposure the cap cannot
        # see — refusing beats silently under-counting on a money rail.
        return (
            f"gross cap: {ticker} refused — {unjoined} working order(s) could not be "
            "joined to a journaled entry bracket; committed gross cannot be valued, "
            "failing closed"
        )
    account_ccy = str(getattr(account, "currency", "") or "")
    filled_acct, mark_failure = _filled_positions_gross_acct(
        positions,
        fx,
        account_currency=account_ccy,
        rate_lookup=_make_position_rate_lookup(broker, account_ccy) if broker is not None else None,
    )
    if mark_failure is not None:
        return f"gross cap: {ticker} refused — {mark_failure}"

    # PR-T1: read the fold ONCE in _place_pick and thread it into BOTH money
    # gates so a mid-attempt append (a watch opening on another pick this tick)
    # can never tear the read between them. A None fold (direct unit tests) reads
    # its own snapshot, as before.
    fold = (
        entry_trail_fold if entry_trail_fold is not None else entry_trails.read_entry_trail_fold()
    )
    watching_acct, unvaluable = entry_trails.watching_virtual_gross_acct(fold)
    if unvaluable:
        # Fail CLOSED exactly like the unjoined-working-orders path above: a
        # malformed/unvaluable entry-trail record may be a virtual reservation
        # the cap cannot see — refusing beats silently under-counting.
        return (
            f"gross cap: {ticker} refused — {unvaluable} entry-trail record(s) could not "
            "be valued (malformed or missing watch_open); the watching reservation "
            "cannot be valued, failing closed"
        )

    total_acct = committed_acct + candidate_acct + filled_acct + watching_acct
    limit_acct = gross_frac * account.total_value
    if total_acct <= limit_acct:
        return None
    # Named only when non-zero so the pre-trailing refusal text stays
    # byte-identical while no watch is open (PR-T0 inertness proof).
    watching_component = f" + watching {watching_acct:,.2f}" if watching_acct else ""
    return (
        f"gross cap: {ticker} total gross {total_acct:,.2f} {account.currency} "
        f"(working {committed_acct:,.2f} + candidate {candidate_acct:,.2f} "
        f"+ filled {filled_acct:,.2f}{watching_component}) exceeds limit {limit_acct:,.2f} "
        f"({gross_frac:g} x total_value {account.total_value:,.2f}) — pick refused"
    )


# --- Cash floor (broker sizing declared-frame memo §4.2) ---------------------
#
# Funding-friction buffer on the candidate's account-currency gross. Covers
# entry commissions (0.08% min $1 per tier), the one-way FX conversion markup
# (<= 0.25%), and USDPLN drift over the full GTD-7d entry window + T+2
# settlement. NOT a percentile-calibrated figure: the consequence of an
# underfunded fill (reject vs forced action) is un-probeable on SIM (P2), so
# the first LIVE weeks are the observation; the buffer is sized to make that
# event rare, not impossible (memo §4.2, zen finding applied).
_CASH_FLOOR_BUFFER_PCT = 4.0


def _check_cash_floor(
    plan: Any,
    fx: Any,
    *,
    account: Any,
    open_verdicts: Iterable[Any],
    records: Iterable[Mapping[str, Any]],
    ticker: str,
    entry_trail_fold: entry_trails.EntryTrailFold | None = None,
) -> str | None:
    """``None`` iff the pick's buffered funding need fits the account's real
    ``margin_available`` (or the sizing mode is not ``declared`` — clamped /
    unset stays byte-identical to pre-cash-floor behavior; the min-clamp
    already bounds sizing by the snapshot there). Else a terminal refusal
    message naming the buffered candidate, the resting reservation, the
    available figure and the account currency (memo §4.2/§4.3):

        candidate_buffered + reserved_resting > available -> refuse

    ``reserved_resting`` folds the committed-working entry gross from the
    journal via the PR-0 ``_committed_working_gross_acct`` fold because the
    broker reserves NOTHING for a resting buy limit (P1 probe, 2026-08-12
    SIM: CashBalance, MarginAvailableForTrading and TotalValue all UNCHANGED
    after placement and after cancel) — without this ledger two armed picks
    would double-spend the same cash. The watching virtual reservation
    (entry-trailing memo G5) joins the same sum: a watching trail tier has NO
    broker order at all, so its future fire is cash the floor must reserve;
    no/empty journal adds exactly 0.0 (PR-T0 inertness), and an unvaluable
    watching record fails CLOSED here too — independent of the gross cap
    running first, so no caller ordering can silently under-reserve. The
    committed fold's ``unjoined`` count is deliberately ignored HERE: the
    gross cap (which runs FIRST in ``_place_pick``, same verdicts+records)
    already fails closed on any unjoined working verdict, so this code path
    only ever sees ``unjoined`` when called outside that ordering (direct
    unit tests).

    ``available`` is ``margin_available`` — never ``cash``, which ignores
    margin impact and lags under EOD netting; ``None`` (SIM NoAccess or an
    account double without the field) fails CLOSED."""
    from alphalens_pipeline.brokers.automanager.live_rails import (
        SIZING_EQUITY_MODE_ENV,
        SIZING_MODE_DECLARED,
    )

    mode = (os.environ.get(SIZING_EQUITY_MODE_ENV) or "").strip().lower()
    if mode != SIZING_MODE_DECLARED:
        return None

    from broker_contract.sizing import setup_plan_gross_notional

    candidate_acct = setup_plan_gross_notional(plan)
    if candidate_acct <= 0:
        # An unplannable/zero-tier pick funds nothing — stay inert (before the
        # margin read); the zero-tiers refusal downstream owns such a pick.
        return None
    if fx is not None:
        # rate is instrument-ccy per 1 account-ccy -> acct = instr / rate.
        candidate_acct /= float(fx.rate)
    candidate_buffered = candidate_acct * (1.0 + _CASH_FLOOR_BUFFER_PCT / 100.0)

    reserved_resting, _unjoined = _committed_working_gross_acct(open_verdicts, records)
    # PR-T1 torn-read fix: _place_pick reads the fold ONCE and threads the SAME
    # snapshot into this gate and _check_gross_cap, so a mid-attempt watch_open
    # append on another pick this tick can never tear the read between the two
    # money gates. A None fold (direct unit tests) reads its own snapshot.
    fold = (
        entry_trail_fold if entry_trail_fold is not None else entry_trails.read_entry_trail_fold()
    )
    watching_acct, unvaluable = entry_trails.watching_virtual_gross_acct(fold)
    if unvaluable:
        # Fail CLOSED independent of the gross cap running first: a direct or
        # future caller outside the _place_pick ordering must never silently
        # under-reserve on a watching record it cannot value.
        return (
            f"cash floor: {ticker} refused — {unvaluable} entry-trail record(s) could not "
            "be valued (malformed or missing watch_open); the watching reservation "
            "cannot be valued, failing closed"
        )
    reserved_resting += watching_acct

    available = getattr(account, "margin_available", None)
    if available is None:
        return (
            f"cash floor: {ticker} refused — account margin_available is None, the "
            "real balance cannot be read; failing closed"
        )
    if candidate_buffered + reserved_resting <= available:
        return None
    return (
        f"cash floor: {ticker} needs {candidate_buffered:,.2f} {account.currency} "
        f"(incl. {_CASH_FLOOR_BUFFER_PCT:g}% buffer) + {reserved_resting:,.2f} already "
        f"reserved by resting entries, but only {available:,.2f} {account.currency} is "
        "available — deposit and re-arm"
    )


def _refuse_pick_terminal(
    ticker: str,
    brief_date: dt.date,
    violation: str,
    alert_key: str,
    alert_throttled: Callable[[str, str], bool] | None,
) -> None:
    """The shared terminal-refusal tail for the post-sizing ``_place_pick``
    gates (fee floor, gross cap): warn, page the operator (throttled, only
    when a sink exists), and retire the pick with a refused line so it never
    retries every tick. The ``mark_refused`` append is fallible I/O and must
    never crash the drain: on OSError the pick stays armed and the refusal
    re-fires next tick (re-attempting the append)."""
    logger.warning("place_pick %s: %s", ticker, violation)
    if alert_throttled is not None:
        alert_throttled(violation, alert_key)
    try:
        picks.mark_refused(ticker, brief_date, violation)
    except OSError as exc:
        logger.warning(
            "place_pick %s: refused-line append failed (pick stays armed): %s", ticker, exc
        )


def _is_journalable_price(value: float | None) -> bool:
    """A price the journal may carry verbatim: present, finite and strictly
    positive. ``_build_tranche_plan_line`` writes ``float(...)`` straight through,
    so a None/NaN/zero level from a future geometry policy must be caught HERE
    rather than poisoning the ladder the live-exit engine folds back."""
    return value is not None and math.isfinite(value) and value > 0


# The exit-geometry policy whose numbers `build_exit_geometry_spec` builds. NOTE:
# this is the GEOMETRY policy, not the behavioural exit policy the live rail runs
# (that is `trailing_atr` per #1008), so the stamped `policy_name` is still
# narrower than it reads. Fixing that means threading the resolved policy's name
# through the placement path, which is out of scope for #1114.
_GEOMETRY_STAMP_POLICY_NAME = "atr_bracket_1p5"

# The entry anchor `build_exit_geometry_spec` places against: the alloc-weighted
# blend over ALL intended tiers (`planned_blended_entry`). Mirrors
# `alphalens_pipeline.feedback.ladder_replay.ANCHOR_PLANNED` without importing
# the feedback tier into the broker daemon's hot path.
_GEOMETRY_STAMP_ANCHOR_MODE = "planned"

# The take-profit cost floor the geometry policy applies, resolved ONCE at import
# time. `resolve_policy` raises ValueError on an unknown name and the stamp runs
# on every watch_open inside the unattended drain, where nothing may raise — so
# an unknown name must fail the daemon at startup, not on a tick hours later.
_GEOMETRY_STAMP_TP_FLOOR_FRAC = resolve_policy(_GEOMETRY_STAMP_POLICY_NAME).tp_floor_frac


def _geometry_shadow_stamp(
    exit_spec: Any, spec: Any, *, use_geometry: bool, exit_policy: ExitPolicy
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
        "policy_name": _GEOMETRY_STAMP_POLICY_NAME,
        "policy_version": 1,
        "planned_blend": blend,
        "geometry_stop": exit_spec.initial_levels.stop,
        "geometry_tp": exit_spec.initial_levels.tp,
        "k_atr": reanchor.k_atr if reanchor is not None else None,
        "atr": reanchor.atr if reanchor is not None else None,
        "ceiling_price": reanchor.ceiling_price if reanchor is not None else None,
        "applied": use_geometry,
        # Issue #1114: the two facts that made the divergence unreadable. The
        # stamp already carried the planned blend as a VALUE, but nothing said
        # the levels came from the planned anchor, and the /edge lens sharing
        # this policy_name used the realised one. It also never named the
        # take-profit floor, which is why the floor looked one-sided when in
        # fact both sides reach it through the same atr_bracket_levels leaf.
        "anchor_mode": _GEOMETRY_STAMP_ANCHOR_MODE,
        "tp_floor_frac": _GEOMETRY_STAMP_TP_FLOOR_FRAC,
        # Issue #1138: which POLICY ran, as distinct from which geometry it
        # placed. ``policy_name`` above is the geometry and keeps that meaning
        # on every row already written; this is the behavioural policy the
        # daemon resolved from ALPHALENS_BROKER_EXIT_POLICY. Read off the
        # already-resolved instance -- no registry lookup on the drain path.
        "exit_policy_name": exit_policy.name,
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
    use_geometry: bool,
    pick_key: str | None = None,
    instrument_currency: str | None = None,
    sizing_currency: str | None = None,
) -> None:
    """The ladder-choice + line-build core shared by BOTH placement paths
    (bracket ``_journal_tranche_plan`` and the entry-trail watch routing).
    ``pick_key`` is the optional trade identity stamped into the line (watch
    path only — see :func:`_build_tranche_plan_line`).
    Source the ladder from whatever the ACTIVE exit policy actually places the
    TP from: under the geometry policy (atr_bracket_1p5) that is the single
    ``exit_spec.initial_levels.tp`` level (and the passed ``stop_price`` is
    REPLACED by the geometry stop); under the static policy the ladder IS
    ``plan.tp_tranches`` and ``stop_price`` is journaled verbatim. Takes
    explicit ``stop_price``/``reference_qty``/``uic`` so the caller decides the
    plan-vs-placement source of each — the bracket path reads
    ``placement.disaster_stop_price`` and sums ALL entry tiers, the watch path
    reads ``plan.disaster_stop`` and sums only the tiers that actually watch."""
    if use_geometry and exit_spec is not None:
        geometry = _geometry_tranche_ladder(exit_spec)
        if geometry is None:
            # Otherwise this skip is invisible: the live-exit engine finds no
            # ladder for the uic and the position sits stop-only, which reads in
            # the journal exactly like a pre-INC-5 pick.
            logger.warning(
                "tranche_plan uic %d: geometry levels unusable (stop=%r, tp=%r) — "
                "no TP ladder journaled, the position stays stop-only",
                uic,
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
            uic=uic,
            tp_tranches=ladder,
            reference_qty=reference_qty,
            stop_price=stop_price,
            pick_key=pick_key,
            instrument_currency=instrument_currency,
            sizing_currency=sizing_currency,
        )
    )


def _journal_tranche_plan(
    *,
    plan: Any,
    exit_spec: Any,
    placement: Any,
    instrument: Any,
    use_geometry: bool,
    fx: Any = None,
    override: tuple[str, float] | None = None,
) -> None:
    """INC-5: journal ONE ``tranche_plan`` line per uic so the live-exit engine can
    rebuild the TP ladder from the journal alone — see
    :func:`_journal_tranche_plan_core` for which ladder the ACTIVE policy
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
        use_geometry=use_geometry,
        pick_key=pick_key,
        instrument_currency=str(getattr(instrument, "currency", "") or ""),
        sizing_currency=_sizing_currency_of(fx, instrument),
    )


def _cancel_orders_best_effort(broker: Broker, order_ids: Iterable[str], *, ticker: str) -> int:
    """Cancel each order id, best-effort: one cancel failure (log + continue)
    must never abort the remaining cancels — every order we CAN take off the
    book during an insufficient-funds rollback reduces the partial-ladder
    exposure. Returns the count actually cancelled."""
    cancelled = 0
    for order_id in order_ids:
        try:
            broker.cancel_order(order_id)
            cancelled += 1
        except BrokerError as exc:
            logger.warning(
                "place_pick %s: rollback cancel of entry %s failed (continuing): %s",
                ticker,
                order_id,
                exc,
            )
    return cancelled


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
    *,
    entry_duration: str | None = None,
    record_tranche: str | None = None,
    record_meta: Mapping[str, Any] | None = None,
    write_ahead_note: str = "placement attempt",
    tranche_plan_override: tuple[str, float] | None = None,
    on_broker_error: Callable[[Any], str] | None = None,
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

    # Honest per-tier round-trip fee estimate (memo §4.5) — computed ONCE and
    # stamped on EVERY record this placement journals (write-ahead, per-tier,
    # failure note), so the calibration series survives whatever the ladder
    # outcome was. None (a real null) when no sized plan is in scope.
    est_fee_bps = _estimate_round_trip_fee_bps(plan, fx, instrument_currency=instrument.currency)

    def _tranche_kwargs(outcome: str) -> dict[str, Any]:
        # #1247: per-tranche markers on every record this placement journals.
        # Absent params -> empty dict -> legacy record shape byte-identical.
        if record_tranche is None:
            return {}
        meta = dict(record_meta or {})
        meta["outcome"] = outcome
        return {"tranche": record_tranche, "tranche_meta": meta}

    def _journal_tier(tier: Any, bracket: Any, placed: Any) -> None:
        bracket_row: dict[str, Any] = {
            "client_request_id": bracket.client_request_id,
            "entry_order_id": placed.entry_order_id,
            "exit_order_ids": list(placed.exit_order_ids),
            "qty": bracket.quantity,
            "entry": bracket.entry_limit,
            "stop": bracket.stop_loss,
            "tp": bracket.take_profit,
            "ttl": bracket.entry_ttl_days,
        }
        if entry_duration is not None:
            bracket_row["entry_duration"] = entry_duration
        append_submission_record(
            build_submission_record(
                brief_date=intent.meta.brief_date,
                ticker=ticker,
                mic=instrument.exchange_mic,
                uic=instrument.broker_instrument_id,
                brackets=[bracket_row],
                note=None,
                sizing_currency=account.currency,
                instrument_currency=instrument.currency,
                sizing_equity=_resolve_sizing_equity(account.total_value),
                fx=fx,
                est_round_trip_fee_bps=est_fee_bps,
                **_tranche_kwargs("placed"),
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
                geometry_stamp=_geometry_shadow_stamp(
                    exit_spec,
                    spec,
                    use_geometry=use_geometry,
                    exit_policy=resolved_exit_policy,
                ),
            )
        )

    _journal_tranche_plan(
        plan=plan,
        exit_spec=exit_spec,
        placement=placement,
        instrument=instrument,
        use_geometry=resolved_exit_policy.applies_geometry,
        fx=fx,
        override=tranche_plan_override,
    )

    # Write-ahead dedup line (memo §4.4 B2): register the (ticker, brief_date)
    # dedup key BEFORE the first broker POST — picks.submitted_pick_keys already
    # treats note-only records as submitted, so a crash between the POST and
    # the per-tier journal append strands an alertable non-retried attempt
    # instead of re-placing the whole frame-sized ladder on restart. The
    # record is INERT everywhere brackets are folded (reconcile,
    # _summarize_open_verdicts, _committed_working_gross_acct: brackets=[]
    # folds zero). The post-placement per-tier append below stays — it
    # carries the real brackets.
    append_submission_record(
        build_submission_record(
            brief_date=intent.meta.brief_date,
            ticker=ticker,
            mic=instrument.exchange_mic,
            uic=instrument.broker_instrument_id,
            brackets=[],
            note=write_ahead_note,
            sizing_currency=account.currency,
            instrument_currency=instrument.currency,
            sizing_equity=_resolve_sizing_equity(account.total_value),
            fx=fx,
            est_round_trip_fee_bps=est_fee_bps,
            **_tranche_kwargs("attempt"),
        )
    )

    placed_count = 0
    placed_entry_ids: list[str] = []
    failure_note: str | None = None
    failure_outcome = "failed"
    try:
        for tier in placement.tiers:
            bracket: Any = tier.bracket
            if (
                entry_duration is not None
                and is_dataclass(bracket)
                and not isinstance(bracket, type)
            ):
                # #1247 now tranche: dataclasses.replace keeps the request
                # frozen; PR-B's adapter dispatches the duration block on it.
                # (Duck-typed test brackets skip the replace; the record stamp
                # below still carries the duration.)
                bracket = cast(Any, replace(bracket, entry_duration=entry_duration))
            placed = broker.place_bracket_order(cast(Any, bracket))
            _journal_tier(tier, bracket, placed)
            placed_entry_ids.append(str(placed.entry_order_id))
            placed_count += 1
    except BrokerError as exc:
        failure_note = (
            f"placement stopped after {placed_count}/{len(placement.tiers)} bracket(s): {exc}"
        )
        if on_broker_error is not None:
            # #1247 now tranche: classify (price-tolerance reject vs generic
            # failure) + page; the returned string is the failure record's
            # tranche_meta outcome. The exception still never escapes.
            failure_outcome = on_broker_error(exc)
        # Memo §4.4 B1 — insufficient-funds rollback: a tier rejected for lack
        # of cash means the WHOLE pick is unaffordable; leaving the earlier
        # tiers resting would keep a partial frame-sized ladder live at a
        # near-boundary account. Cancel the just-placed unfilled entries
        # (cancel_order is deliberately ungated) BEFORE journaling the note.
        # Classified on the structured Saxo error code only — any other
        # BrokerError (including error_code=None) keeps today's behavior.
        if _is_insufficient_funds(exc) and placed_entry_ids:
            cancelled = _cancel_orders_best_effort(broker, placed_entry_ids, ticker=ticker)
            failure_note += (
                f"; insufficient funds — cancelled {cancelled}/{len(placed_entry_ids)} "
                "placed entry tier(s)"
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
                sizing_equity=_resolve_sizing_equity(account.total_value),
                fx=fx,
                est_round_trip_fee_bps=est_fee_bps,
                **_tranche_kwargs(failure_outcome),
            )
        )

    if failure_note:
        logger.warning("place_pick %s: %s", ticker, failure_note)
    return placed_count


# --- Day-1 gap gate (execution-quality placement discipline) -----------------
#
# Empirical finding (population-ladder analysis, N=30/588, 2026-08-11): picks
# whose FIRST post-brief session opens BELOW the tier-1 (E1) entry limit — an
# overnight gap through the limit — carry a median terminal outcome of -1R
# (64% losers) vs +0.21R baseline. Gaps on later sessions are benign (+0.23R)
# — classic limit-order adverse selection at the opening print. The gate only
# DEFERS a day-1 placement (never refuses it): a deferred pick stays armed and
# is re-evaluated next tick, so it never feeds back into WHICH ticker is
# selected (ADR 0013 R2) — only WHEN, on day 1, the entry is allowed to fill.
_DAY1_GAP_GATE_ENV = "ALPHALENS_BROKER_DAY1_GAP_GATE"

# The opening auction print needs a few minutes to settle before an
# indicative quote is trustworthy enough to gate a placement on.
_DAY1_GAP_GATE_OPEN_GRACE_S = 300

Day1GapGateVerdict = Literal["pass", "defer_preopen", "defer_no_price", "defer_below_e1"]


def _day1_gap_gate_enabled() -> bool:
    """Whether the day-1 gap gate is armed (read at call time, mirrors
    ``_live_market_exits_enabled`` / ``_saxo_live_prices_enabled`` above).
    Defaults OFF — unset means ``_place_pick`` never evaluates the gate,
    byte-identical to today."""
    return os.environ.get(_DAY1_GAP_GATE_ENV) == "1"


def _day1_gap_gate_session_info(
    brief_date: dt.date, exchange_mic: str, *, day1_includes_brief_date: bool = False
) -> tuple[dt.date, dt.datetime] | None:
    """``(day1 session date, day1 session open UTC)`` for ``brief_date`` on
    ``exchange_mic``, or ``None`` when the calendar cannot resolve it (e.g. an
    unrecognised exchange MIC) — never raises. NOTE the consequence: ``None``
    makes ``_day1_gap_gate_decision`` return "pass", so an unknown-to-calendar
    MIC DISABLES the gate for that pick — visible only through the WARNING
    below, never a refusal (#1238).

    The anchor depends on the pick's provenance (#1246):

    - default (brief picks): ``day1`` is the first trading session STRICTLY
      AFTER ``brief_date`` — a brief holds T-1 data and trades the next
      session (a Monday brief's day1 is Tuesday, a Friday brief's day1 is the
      following Monday; a weekend/holiday ``brief_date`` lands one session
      past the weekend's first session).
    - ``day1_includes_brief_date=True`` (manual picks, whose ``brief_date``
      IS the arm date): ``day1`` is the session ON-OR-AFTER ``brief_date`` —
      an "as of now" operator decision trades its own arm day, not the next
      one (a weekend arm still rolls to the next session).

    Both anchors are one ``advance_trading_sessions`` call (``n=0`` is
    documented as session-on-or-after). Pure calendar math, no I/O — shared
    by ``_day1_gap_gate_decision`` and the placer's probe-gating check
    (``_evaluate_day1_gap_gate``) so the two never disagree on what "day1"
    means."""
    try:
        from alphalens_pipeline.paper.calendar import advance_trading_sessions, session_open_utc

        step = 0 if day1_includes_brief_date else 1
        day1 = advance_trading_sessions(brief_date, step, exchange=exchange_mic)
        return day1, session_open_utc(day1, exchange=exchange_mic)
    except Exception:
        logger.warning(
            "day1 gap gate: calendar resolution failed for brief_date=%s exchange_mic=%s",
            brief_date,
            exchange_mic,
            exc_info=True,
        )
        return None


def _day1_gap_gate_decision(
    now_utc: dt.datetime,
    brief_date: dt.date,
    e1_limit: float | None,
    probe_price: float | None,
    exchange_mic: str,
    *,
    source: str = "brief",
) -> Day1GapGateVerdict:
    """Pure day-1 gap gate verdict — no I/O, total (never raises on weird
    inputs). ``source`` picks the day-1 anchor (#1246): ``"manual"`` anchors
    day 1 on the session on-or-after ``brief_date`` (the arm date itself),
    anything else on the session strictly after it — see
    ``_day1_gap_gate_session_info``.

    - ``e1_limit is None`` (a pick the gate cannot evaluate) -> "pass" with a
      WARNING log — a doubt about GATING must never itself become a
      placement refusal.
    - A calendar resolution failure (see ``_day1_gap_gate_session_info``) ->
      "pass" — same reasoning, already logged there.
    - ``now_utc`` on a date AFTER day1 -> "pass" (later-day gaps are benign
      by the population data, so the gate is inert from day 2 on).
    - Before day1's open + ``_DAY1_GAP_GATE_OPEN_GRACE_S`` -> "defer_preopen"
      (covers every pre-day1 tick too — day1's open is always in the future
      then).
    - Within day1, at/after the grace window, and ``probe_price is None`` ->
      "defer_no_price" (fail-safe: no price, no day-1 placement).
    - Within day1, at/after the grace window, and ``probe_price < e1_limit``
      -> "defer_below_e1".
    - Otherwise -> "pass"."""
    if e1_limit is None:
        logger.warning("day1 gap gate: pick carries no E1 limit — gate cannot evaluate, passing")
        return "pass"
    info = _day1_gap_gate_session_info(
        brief_date, exchange_mic, day1_includes_brief_date=source == "manual"
    )
    if info is None:
        return "pass"
    day1, day1_open = info
    if now_utc.date() > day1:
        return "pass"
    if now_utc < day1_open + dt.timedelta(seconds=_DAY1_GAP_GATE_OPEN_GRACE_S):
        return "defer_preopen"
    if probe_price is None:
        return "defer_no_price"
    if probe_price < e1_limit:
        return "defer_below_e1"
    return "pass"


def _evaluate_day1_gap_gate(
    ticker: str,
    brief_date: dt.date,
    spec: Any,
    exchange_mic: str,
    probe: Callable[[str, str], float | None] | None,
    *,
    source: str = "brief",
) -> Day1GapGateVerdict:
    """Orchestrates the gate for one pick: resolves E1 (the first PULLBACK
    tier — ``ladder.build_entry_tiers`` returns tiers strictly descending, so
    the first pullback tier is the shallowest/highest resting limit; a
    leading immediate "now" tier (#1247) is EXCLUDED — its cap is not a
    pullback rung and must not redefine the gate's threshold), calls the
    price probe ONLY when the pick is within its day1 session at/after the
    open+grace window — every other phase (pre-day1, pre-open, day 2+) needs
    no price at all, and the probe is a real network round-trip — then
    delegates the full verdict to the pure ``_day1_gap_gate_decision``.
    ``source`` threads the day-1 anchor choice (#1246) into BOTH the
    probe-gating check here and the decision, so the two can never disagree
    on what "day1" means. A now-ONLY pick has no pullback rung: the gate is
    not applicable (the group's decision IS the timing, memo §2) — pass
    without probing."""
    tiers = spec.entry_tiers or ()
    e1_limit = next(
        (t.limit_price for t in tiers if getattr(t, "entry_mode", "pullback") == "pullback"),
        None,
    )
    if e1_limit is None and any(getattr(t, "entry_mode", "pullback") == "immediate" for t in tiers):
        logger.info(
            "day1 gap gate: %s carries only an immediate tier — gate not applicable", ticker
        )
        return "pass"
    now_utc = dt.datetime.now(dt.UTC)
    probe_price: float | None = None
    if e1_limit is not None and probe is not None:
        info = _day1_gap_gate_session_info(
            brief_date, exchange_mic, day1_includes_brief_date=source == "manual"
        )
        if info is not None:
            day1, day1_open = info
            grace_open = day1_open + dt.timedelta(seconds=_DAY1_GAP_GATE_OPEN_GRACE_S)
            if now_utc.date() <= day1 and now_utc >= grace_open:
                probe_price = probe(ticker, exchange_mic)
    return _day1_gap_gate_decision(
        now_utc, brief_date, e1_limit, probe_price, exchange_mic, source=source
    )


# US venue probe order for the day-1 gap gate price probe — the SAME shared
# constant routing.resolve_us_instrument's placement-side probe uses
# (``data/alt_data/saxo_exchanges.US_MIC_PROBE_ORDER``), imported rather than
# copied so the two orders can never diverge.
_DAY1_GAP_US_VENUE_PROBE_ORDER = US_MIC_PROBE_ORDER


def _build_day1_gap_price_probe() -> Callable[[str, str], float | None]:
    """The production day-1 gap gate price probe: a ONE-SHOT Saxo LIVE
    indicative-quote snapshot, mirroring ``_default_live_exits_feed_factory``'s
    lazy-import style. Unlike the persistent streaming feed (INC-2), this
    opens a throwaway infoprices subscription, reads its initial snapshot,
    and best-effort deletes it — the gate needs at most a handful of quotes
    per pick (day1 open+grace onward, once per tick until it clears), never a
    standing WebSocket.

    ANY exception (missing LIVE auth env, an unresolvable uic, a non-2xx
    subscription response, a malformed snapshot body) degrades to ``None`` —
    the gate's own fail-safe (``_day1_gap_gate_decision``: no price, no day-1
    placement) already treats ``None`` as a defer, so this probe never needs
    to distinguish WHY it could not get a price. This is also why the SIM
    instance (which has no LIVE marketdata chain configured) self-degrades to
    always deferring day-1 placements rather than crashing, on the rare
    occasion the flag is ever turned on there."""

    def _probe(ticker: str, exchange_mic: str) -> float | None:
        import contextlib

        try:
            from alphalens_pipeline.data.alt_data.saxo_marketdata_auth import (
                LiveAuthConfig,
                LiveTokenProvider,
            )
            from alphalens_pipeline.data.alt_data.saxo_marketdata_client import (
                SaxoMarketDataClient,
            )

            client = SaxoMarketDataClient(
                token_provider=LiveTokenProvider(LiveAuthConfig.from_env())
            )
            # Same hint rule as placement routing (explicit_mic_from_hint,
            # #1238): a US hint is ADVISORY — every brief intent carries
            # "XNYS" while a NASDAQ name resolves only on XNAS (false veto,
            # live-verified with NVAX, XNAS uic 6820, 2026-08-11) — so US
            # hints probe the full US order, hinted venue first. A NON-US
            # hint is AUTHORITATIVE: probe that venue ALONE, because a
            # same-ticker US listing would otherwise price a European entry.
            # One-shot client: the outer finally releases the connection
            # pool on EVERY return path — the unresolvable-uic early return
            # used to leak the session once per tick, all day, on exactly
            # the failing-probe scenario this gate surfaces (zen finding).
            try:
                from alphalens_pipeline.brokers.routing import explicit_mic_from_hint

                explicit = explicit_mic_from_hint(exchange_mic)
                if explicit is not None:
                    candidate_mics = [explicit]
                else:
                    candidate_mics = [exchange_mic] + [
                        m for m in _DAY1_GAP_US_VENUE_PROBE_ORDER if m != exchange_mic
                    ]
                uic = None
                for mic in candidate_mics:
                    uic = client.resolve_uic(ticker, exchange_mic=mic)
                    if uic is not None:
                        break
                if uic is None:
                    return None
                payload = client.get_stock_infoprice(uic)
                return _extract_day1_session_open(payload)
            finally:
                with contextlib.suppress(Exception):
                    client._session.close()
        except Exception:
            logger.debug(
                "day1 gap gate: price probe failed for %s/%s",
                ticker,
                exchange_mic,
                exc_info=True,
            )
            return None

    return _probe


def _extract_day1_session_open(payload: Mapping[str, Any]) -> float | None:
    """The session OPEN off a ``GET /trade/v1/infoprices`` snapshot
    (``PriceInfoDetails.Open`` — live-verified 2026-08-11, NVAX uic 6820,
    Open=7.92). The gate's decision input is the day-1 OPENING PRINT, not
    the instantaneous price: the validated discriminator is "did day-1 OPEN
    gap through E1" (population analysis 2026-08-11) — an intraday dip
    below E1 AFTER a healthy open is the normal pullback fill the ladder
    WANTS, so the verdict is stable for the whole session by construction
    (the open never changes). Any missing/malformed/non-positive field is a
    veto (``None``), never a crash."""
    details = (payload or {}).get("PriceInfoDetails") or {}
    raw = details.get("Open")
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value > 0 else None


def _day1_gap_gate_defers(
    ticker: str,
    brief_date: dt.date,
    spec: Any,
    exchange_mic: str,
    probe: Callable[[str, str], float | None] | None,
    alert_throttled: Callable[[str, str], bool] | None,
    *,
    source: str,
) -> bool:
    """True iff the day-1 gap gate is enabled AND defers this pick (the
    ``_place_pick`` early-return). Pages the operator (throttled) for the
    actionable below-E1 verdict AND for the no-price verdict (an
    INFRASTRUCTURE failure — the probe could not produce a price at all;
    real incident 2026-08-12: LAC's resolve failure silently deferred its
    whole day 1 at DEBUG); "defer_preopen" stays a DEBUG line (expected,
    high-frequency).

    ``source`` (no default — the one production caller must be explicit)
    picks the day-1 anchor: ``"manual"`` gates on the arm date's own session,
    ``"brief"`` on the next session (#1246)."""
    if not _day1_gap_gate_enabled():
        return False
    gate_verdict = _evaluate_day1_gap_gate(
        ticker, brief_date, spec, exchange_mic, probe, source=source
    )
    if gate_verdict == "pass":
        return False
    if gate_verdict == "defer_no_price":
        # Ride the alert throttle for the WARNING too (zen pre-merge finding):
        # the probe can fail every ~45s tick all day, and hundreds of
        # identical journald WARNINGs would crowd out real signals. One
        # WARNING per throttle window (or per tick when no alert sink is
        # wired — unit tests, ad-hoc runs); suppressed repeats log at DEBUG.
        sent = alert_throttled is None or alert_throttled(
            f"day1 gap gate: {ticker} day-1 PRICE PROBE failed — an "
            "infrastructure problem, not a market condition; check "
            "instrument resolution / marketdata chain (the pick stays "
            "deferred all of day 1 until a price arrives)",
            f"day1-gap-noprice:{ticker}",
        )
        log = logger.warning if sent else logger.debug
        log(
            "place_pick %s: day1 gap gate deferred (defer_no_price) — the PRICE "
            "PROBE returned no price (infrastructure problem, not a market "
            "condition); check instrument resolution / marketdata chain",
            ticker,
        )
        return True
    logger.debug("place_pick %s: day1 gap gate deferred (%s)", ticker, gate_verdict)
    if gate_verdict == "defer_below_e1" and alert_throttled is not None:
        alert_throttled(
            f"day1 gap gate: {ticker} trading below E1 at the day-1 open — "
            "entry deferred to day 2+",
            f"day1-gap:{ticker}",
        )
    return True


_GEOMETRY_WITHOUT_TRAIL_ALERT_PREFIX = "geometry-without-entry-trail"


def _geometry_without_entry_trail_note(
    exit_policy: ExitPolicy | None, exit_spec: Any
) -> str | None:
    """Why a NEW entry must not be armed right now, or ``None`` when it may be
    (issue #1112 round 2, point 4).

    The #1112 exit-region arm gate (:func:`_inside_exit_region_note`) and the
    single-tranche contract (:func:`_exit_plan_shape_refusal`) exist ONLY on the
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

    ``None`` when the geometry exit is not active: under the static policy the
    placed exit IS the brief's own ladder, and the arm gate never priced
    anything, so the classic path is no worse than it has always been.
    """
    if exit_policy is None or not exit_policy.applies_geometry or exit_spec is None:
        return None
    if entry_trails.entry_trail_bps() > 0:
        return None
    return (
        f"exit geometry is active but the entry trail is off "
        f"({entry_trails.ENTRY_TRAIL_BPS_ENV}=0) — the classic bracket path has no "
        f"exit-region gate, so a new entry could fill inside its own exit region"
    )


def _entry_trail_intercept(
    broker: Broker,
    intent: Any,
    ticker: str,
    instrument: Any,
    account: Any,
    plan: Any,
    fx: Any,
    entry_trail_fold: entry_trails.EntryTrailFold,
    exit_policy: ExitPolicy | None = None,
    *,
    positions: Iterable[Position] = (),
    reference_qty_override: float | None = None,
) -> bool | None:
    """The _place_pick entry-trailing intercept outcome: ``None`` when the pick
    must fall through to classify + ``_place_tiers`` (flag off, ineligible plan,
    or no native trailing-stop capability), else the drain verdict.
    ``exit_policy`` is threaded through to the watch routing so its journaled
    managed-exit state (tranche_plan ladder + geometry stamp) mirrors what
    ``_place_tiers`` would have journaled for the same pick. ``positions`` is
    the caller's already-fetched broker snapshot (zero extra I/O) feeding the
    live-uic routing guard below.

    PR-T2b: the whole feature needs the native trailing-stop capability; a
    broker lacking it falls through to classify + _place_tiers (the
    resting-limit entry path, BYTE-IDENTICAL to today) — never a watch it
    could not later arm with a real order."""
    d_bps = entry_trails.entry_trail_bps()
    if (
        d_bps <= 0
        or not _entry_trail_eligible(plan)
        or not isinstance(broker, SupportsTrailingStop)
    ):
        return None
    # Crash-recovery exemption: a pick that ALREADY holds an open watch
    # (its watch_open was journaled but it was never retired — a crash
    # between the journal-FIRST watch_open and the note-only submission
    # record) owns its capacity slot. It must NOT be counted against
    # capacity — that would make it self-block on its OWN reservation and
    # re-drive every tick. It re-opens idempotently (deterministic crid,
    # fold latest-wins) and finally writes the retiring submission record.
    # Match _open_entry_watches' pick_key byte-for-byte: the string
    # brief_date, not the caller's parsed date (str(date) happens to agree,
    # but pin the exact form the watch_open records actually carry).
    pick_key = f"{ticker}:{intent.meta.brief_date}"
    already_watching = pick_key in _open_watch_pick_keys(entry_trail_fold)
    if not already_watching and _entry_watch_capacity_reached(entry_trail_fold):
        # Pick-denominated capacity (memo decision #4): stay ARMED (not a
        # terminal refusal) so it opens once an earlier watch clears.
        _log_watch_capacity_deferral(ticker, pick_key)
        return False
    # 2026-08-19 adjudication finding 2: routing while a live long still holds
    # the SAME uic would journal a fresh tranche_plan that replaces the live
    # position's ladder and resets its fired-tranche set with NO order placed
    # — the live-exit engine could then re-sell the runner at the new pick's
    # targets. Defer (stay armed) until the uic is flat. The already_watching
    # re-drive is EXEMPT: the pick's OWN fill on this uic must not deadlock the
    # retirement record (its tranche_plan re-append is identity-idempotent).
    uic = int(instrument.broker_instrument_id)
    if not already_watching and _has_live_long_on_uic(positions, uic):
        # #1247 memo §3.8 own-pick relaxation: a live long GOVERNED BY THIS
        # PICK (its now tranche just filled) must not freeze its own
        # pre-planned pullback tiers — the guard's purpose is a SECOND pick
        # stacking onto an instrument another pick owns. Journal read only on
        # this (rare) branch; mismatch, a keyless governing plan (classic-path
        # fill) and an unreadable journal all stay the conservative defer.
        try:
            governing = _fold_governing_plan_pick_keys(_iter_standalone_stop_journal()).get(uic)
        except OSError:
            governing = None
        if governing != pick_key:
            _log_live_uic_deferral(ticker, pick_key, uic)
            return False
        logger.info(
            "place_pick %s: live long on uic %d is governed by this pick — "
            "routing its own pullback watches",
            ticker,
            uic,
        )
    return _route_pick_to_entry_watch(
        broker,
        intent,
        ticker,
        instrument,
        account,
        plan,
        fx,
        d_bps=d_bps,
        exit_policy=exit_policy,
        reference_qty_override=reference_qty_override,
    )


# --- Immediate ("now") tranche (#1247, memo docs/research/
# arm_manual_immediate_entry_design_2026_09_03.md) ---------------------------

# Per-uic feed scope for the now tranche's marketability gate. Per-uic so two
# concurrent now picks never clobber each other's subscription slice; a DEFER
# deliberately KEEPS the subscription (a fresh subscribe's snapshot arrives
# async — release-on-defer would starve the gate forever).
_FEED_SCOPE_NOW_ENTRY_PREFIX = "now-entry"


class _NowOutcome(enum.Enum):
    ALREADY_DONE = "already_done"  # marker for this arm generation exists
    DEFER = "defer"  # no real-time quote — whole pick stays armed
    PLACED = "placed"
    REFUSED_NOW = "refused_now"  # terminal for the now tranche only
    REFUSED_PICK = "refused_pick"  # cost gate — terminal for the whole pick


def _now_ioc_supported(broker: Any, instrument: Any) -> bool:
    """Whether the instrument supports IOC for Limit orders — fail-open to
    False (DayOrder), never IOC on an unreadable capability (PR-B doctrine)."""
    probe = getattr(broker, "limit_order_durations_for", None)
    if probe is None:
        return False
    try:
        durations = probe(
            int(instrument.broker_instrument_id),
            str(getattr(instrument, "asset_type", "Stock") or "Stock"),
        )
    except Exception:
        return False
    return durations is not None and "ImmediateOrCancel" in durations


def _now_submitted_cap(broker: Any, instrument: Any, operator_cap: float) -> float:
    """The cap floored DOWN to the limit tick (memo §3.2.3) when the adapter
    exposes the floor capability; the verbatim operator cap otherwise (test
    fakes — the adapter's own quantize still runs at POST)."""
    if isinstance(broker, SupportsPriceTickFloor):
        return broker.floor_limit_price_to_tick(
            int(instrument.broker_instrument_id),
            str(getattr(instrument, "asset_type", "Stock") or "Stock"),
            operator_cap,
        )
    return operator_cap


def _now_cost_gate_violation(
    plan: Any,
    fx: Any,
    instrument: Any,
    exit_spec: Any,
    exit_policy: ExitPolicy | None,
    *,
    cap: float,
) -> str | None:
    """Memo §3.3 — the #1112 parity gate at drain: TP1 must clear round-trip
    cost at the CAP (the worst-case fill). Mirrors ``_brief_plan_arm_refusal``
    with ``fill_estimate = cap``; a ``--no-tp`` pick has no TP1 to gate —
    vacuous by design (stop-only plan, the group manages exits)."""
    resolved = exit_policy if exit_policy is not None else SetupStaticPolicy()
    reference_qty = float(sum(t.qty for t in plan.entry_tiers if t.qty > 0))
    if resolved.applies_geometry and exit_spec is not None:
        target = float(exit_spec.initial_levels.tp)
        qty = reference_qty
    else:
        tranches = getattr(plan, "tp_tranches", ()) or ()
        if not tranches:
            logger.info("now cost gate: pick has no TP tranches (--no-tp) — gate vacuous")
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


def _now_meta(
    intent: Any,
    operator_cap: float,
    submitted_cap: float,
    point: Any,
    duration: str | None,
) -> dict[str, Any]:
    """The now half's ``tranche_meta`` telemetry (memo §3.2 observability).
    The quote's delay value is structurally 0 by the feed contract
    (SaxoLivePriceFeed vetoes anything else) and is not on PricePoint."""
    meta: dict[str, Any] = {
        "armed_ts": str(intent.meta.armed_ts),
        "operator_cap": operator_cap,
        "submitted_cap": submitted_cap,
    }
    if point is not None:
        meta["gate_ask"] = float(point.ask)
        meta["gate_bid"] = float(point.bid)
        meta["gate_event_time"] = (
            point.event_time.isoformat(timespec="seconds") if point.event_time else None
        )
        meta["gate_source"] = str(point.source)
    if duration is not None:
        meta["entry_duration"] = duration
    return meta


def _refuse_now_tranche(
    intent: Any,
    ticker: str,
    instrument: Any,
    account: Any,
    fx: Any,
    *,
    note: str,
    meta: Mapping[str, Any],
) -> None:
    """Journal a terminal now-tranche refusal: a ``tranche=="now"`` record is
    SKIPPED by ``picks.submitted_pick_keys`` (the siblings keep draining) and
    found by the arm-generation scan (the now half never retries)."""
    from alphalens_pipeline.brokers.submission_log import (
        append_submission_record,
        build_submission_record,
    )

    try:
        append_submission_record(
            build_submission_record(
                brief_date=intent.meta.brief_date,
                ticker=ticker,
                mic=instrument.exchange_mic,
                uic=instrument.broker_instrument_id,
                brackets=[],
                note=note,
                sizing_currency=account.currency,
                instrument_currency=instrument.currency,
                sizing_equity=_resolve_sizing_equity(account.total_value),
                fx=fx,
                tranche="now",
                tranche_meta=dict(meta),
            )
        )
    except OSError as exc:
        # Mirror _refuse_pick_terminal: a journal-append failure must never
        # crash the tick; without the marker the now half re-evaluates next
        # tick (the page is throttled).
        logger.warning(
            "place_pick %s: now-tranche refusal record append failed "
            "(now half retries next tick): %s",
            ticker,
            exc,
        )


def _handle_now_tranche(
    broker: Broker,
    intent: Any,
    ticker: str,
    instrument: Any,
    account: Any,
    plan: Any,
    fx: Any,
    *,
    now_tier: Any,
    records: Sequence[Mapping[str, Any]],
    spec: Any,
    exit_spec: Any,
    exit_policy: ExitPolicy | None,
    alert_throttled: Callable[[str, str], bool] | None,
    now_entry_feed_factory: Callable[..., Any] | None,
    tranche_plan_override: tuple[str, float],
) -> _NowOutcome:
    """The immediate tranche's drain: idempotency scan → cap floor → cost
    gate → real-time marketability gate → capped placement via the stock
    ``classify`` + ``_place_tiers`` machinery (memo §3.2/§3.5/§3.6)."""
    from alphalens_pipeline.brokers.automanager.placement_planner import classify

    armed_ts = str(intent.meta.armed_ts)
    for record in records:
        # Idempotency / crash re-drive: ANY now record for this arm generation
        # means the now half is done (placed, refused, or write-ahead-then-
        # crashed — the _place_tiers write-ahead contract: an attempt marker
        # with no bracket is an alertable non-retried attempt, never re-POSTed).
        if (
            str(record.get("tranche") or "") == "now"
            and str(record.get("ticker") or "").upper() == ticker
            and str(record.get("brief_date") or "") == str(intent.meta.brief_date)
            and str((record.get("tranche_meta") or {}).get("armed_ts") or "") == armed_ts
        ):
            return _NowOutcome.ALREADY_DONE
    if now_tier.qty <= 0:
        logger.warning("place_pick %s: now tier sized to zero shares — skipped", ticker)
        return _NowOutcome.REFUSED_NOW
    operator_cap = float(now_tier.limit_price)
    submitted_cap = _now_submitted_cap(broker, instrument, operator_cap)
    violation = _now_cost_gate_violation(
        plan, fx, instrument, exit_spec, exit_policy, cap=submitted_cap
    )
    if violation is not None:
        _refuse_pick_terminal(
            ticker,
            dt.date.fromisoformat(str(intent.meta.brief_date)),
            violation,
            f"now-cost:{ticker}",
            alert_throttled,
        )
        return _NowOutcome.REFUSED_PICK
    uic = int(instrument.broker_instrument_id)
    scope = f"{_FEED_SCOPE_NOW_ENTRY_PREFIX}:{uic}"
    point = None
    if now_entry_feed_factory is not None:
        try:
            feed = now_entry_feed_factory({uic: (ticker, instrument.exchange_mic)}, scope=scope)
            point = feed.latest(uic)
        except Exception:
            logger.warning("place_pick %s: now quote read failed — deferred", ticker, exc_info=True)
    if point is None:
        # Feed off / outage / halt / stale (memo §3.6): non-terminal — the
        # whole pick stays armed and retries next tick; the page names the
        # config lever so a mis-set env is visible. Subscription kept (see
        # _FEED_SCOPE_NOW_ENTRY_PREFIX).
        if alert_throttled is not None:
            alert_throttled(
                f"now tranche {ticker}: no real-time quote (feed off/outage/halt/stale) — "
                f"deferred, retried next tick; check ALPHALENS_SAXO_LIVE_PRICES / price reader",
                f"now-noprice:{ticker}",
            )
        return _NowOutcome.DEFER

    factory = now_entry_feed_factory

    def _release_scope() -> None:
        import contextlib

        if factory is None:
            return
        with contextlib.suppress(Exception):
            factory({}, scope=scope)

    if float(point.ask) > submitted_cap:
        _refuse_now_tranche(
            intent,
            ticker,
            instrument,
            account,
            fx,
            note=f"now refused: ask {float(point.ask):.4f} above cap {submitted_cap:.4f}",
            meta=dict(
                _now_meta(intent, operator_cap, submitted_cap, point, None), outcome="refused_cap"
            ),
        )
        if alert_throttled is not None:
            alert_throttled(
                f"now tranche {ticker}: price above cap "
                f"({float(point.ask):.4f} > {submitted_cap:.4f}) — NOT entered; "
                "re-arm with a fresh cap if the signal stands",
                f"now-cap:{ticker}",
            )
        _release_scope()
        return _NowOutcome.REFUSED_NOW
    duration = "ioc" if _now_ioc_supported(broker, instrument) else "day"
    now_plan = replace(plan, entry_tiers=(replace(now_tier, limit_price=submitted_cap),))
    placement = classify(now_plan, instrument, side=_ENTRY_SIDE)
    if not placement.tiers:
        logger.warning("place_pick %s: now tranche sized to zero shares", ticker)
        _release_scope()
        return _NowOutcome.REFUSED_NOW

    def _classify_error(exc: Any) -> str:
        if _is_price_tolerance_reject(exc):
            if alert_throttled is not None:
                alert_throttled(
                    f"now tranche {ticker}: rejected by the venue price-tolerance check "
                    f"({exc}) — NOT entered; re-arm with a fresh cap if the signal stands",
                    f"now-reject:{ticker}",
                )
            return "refused_reject"
        if alert_throttled is not None:
            alert_throttled(f"now tranche {ticker}: placement failed ({exc})", f"now-fail:{ticker}")
        return "failed"

    placed = _place_tiers(
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
        plan=now_plan,
        entry_duration=duration,
        record_tranche="now",
        record_meta=_now_meta(intent, operator_cap, submitted_cap, point, duration),
        write_ahead_note="now placement attempt",
        tranche_plan_override=tranche_plan_override,
        on_broker_error=_classify_error,
    )
    _release_scope()
    return _NowOutcome.PLACED if placed > 0 else _NowOutcome.REFUSED_NOW


def _place_pick(
    broker: Broker,
    intent: Any,
    exit_policy: ExitPolicy | None = None,
    *,
    alert_throttled: Callable[[str, str], bool] | None = None,
    day1_gap_price_probe: Callable[[str, str], float | None] | None = None,
    audit_budget: OutcomeAuditBudget | None = None,
    now_entry_feed_factory: Callable[..., Any] | None = None,
) -> bool:
    """Place one armed :class:`~broker_contract.trade_intent.schema.TradeIntent`
    end-to-end (see _make_place_pick). Module-level so the per-phase helpers
    keep the tick logic flat; every failure path logs and returns False
    rather than raising.

    ``exit_policy`` (Task 4) is the resolved-once cached policy passed straight
    through to ``_place_tiers`` (whose nested ``_journal_tier`` owns the
    geometry-override gate).

    ``alert_throttled`` (design memo §4 fee floor) pages the operator when the
    fee floor refuses a pick; ``None`` is tolerated (refuse + journal, no page).

    ``day1_gap_price_probe`` (execution-quality placement discipline) is
    consulted ONLY when ``ALPHALENS_BROKER_DAY1_GAP_GATE=1`` — see the "Day-1
    gap gate" section above; ``None`` (every pre-day1-gap call site/test) is
    tolerated exactly like ``alert_throttled=None``.

    PR-7 (broker-manager extraction memo §5): the daemon never touches a
    brief any more — ``ticker``/``brief_date``/``spec``/``exit_spec`` are all
    read directly off the drained ``intent`` (the client already parsed +
    validated the brief at arm time, in ``arm_command``)."""
    from broker_contract.contract import BrokerError

    from alphalens_pipeline.brokers.automanager import safety
    from alphalens_pipeline.brokers.automanager.placement_planner import classify
    from alphalens_pipeline.brokers.automanager.reconcile_bridge import (
        verdicts as reconcile_verdicts,
    )
    from alphalens_pipeline.brokers.submission_log import iter_submission_records

    ticker = intent.instrument.ticker.upper()
    brief_date = dt.date.fromisoformat(intent.meta.brief_date)
    spec = intent.spec
    exit_spec = intent.exit

    # Day-1 gap gate (execution-quality placement discipline): evaluated FIRST,
    # before any broker/safety/sizing I/O — a deferral must be cheap. Never
    # journals a refusal (queue-semantics stays unchanged): a deferred pick
    # stays armed and is re-evaluated next tick.
    if _day1_gap_gate_defers(
        ticker,
        brief_date,
        spec,
        intent.instrument.mic,
        day1_gap_price_probe,
        alert_throttled,
        source=intent.meta.source,
    ):
        return False

    try:
        account = broker.get_account()
        positions = broker.get_positions()
        records = list(iter_submission_records(state_paths.submissions_path()))
        # #1094: the placement read draws from the SAME per-tick budget as
        # the verdict and entry-trail passes — a cold-start tick draining an
        # armed pick must not fan out unbudgeted (the third consumer).
        open_verdicts = reconcile_verdicts(records, broker, audit_budget=audit_budget)
    except BrokerError as exc:
        logger.warning("place_pick %s: broker read failed: %s", ticker, exc)
        return False

    # Entry-trailing reservation fold (memo G5) — read ONCE here, BEFORE
    # safety.check, and threaded into the MAX_OPEN admission input, BOTH money
    # gates below AND the watch-capacity / drain-intercept check further down
    # (torn-read fix). A single snapshot per placement attempt. Empty/absent
    # journal (flag off) folds to zero, so this is inert until a watch is open
    # (PR-T0 inertness).
    entry_trail_fold = entry_trails.read_entry_trail_fold()
    # 2026-08-19 adjudication finding 1: open watches are committed risk units
    # invisible to both terms of the MAX_OPEN sum — count the distinct
    # watch-holding picks (own pick + already-live uics excluded, see the
    # helper) so total concurrent risk units stay bounded by MAX_OPEN for ANY
    # value of the watch-capacity rail.
    # NET risk-unit counting (live incident 2026-08-19): under LIVE EOD netting
    # an intraday round-trip is two ledger rows netting to zero — both the
    # MAX_OPEN position term and the watch exclusion below must see distinct
    # net-nonzero uics, never raw rows (see _net_open_position_uics).
    net_position_uics, unresolvable_position_rows = _net_open_position_uics(positions)
    open_watch_picks = _open_watch_picks_for_max_open(
        entry_trail_fold,
        own_pick_key=f"{ticker}:{intent.meta.brief_date}",
        position_uics=net_position_uics,
    )

    open_bracket_count, realized_r_today = _summarize_open_verdicts(
        open_verdicts, dt.date.today().isoformat()
    )
    decision = safety.check(
        intent,
        safety.JournalView(
            open_bracket_count=open_bracket_count + len(open_watch_picks),
            realized_r_today=realized_r_today,
        ),
        safety.BrokerView(
            open_position_count=len(net_position_uics) + unresolvable_position_rows,
            equity=account.total_value,
        ),
        _AlreadyGatedSessionState(),
    )
    if isinstance(decision, safety.Refuse):
        _handle_safety_refusal(decision, ticker, brief_date)
        return False

    resolved = _resolve_and_size(broker, ticker, account, spec, hint_mic=intent.instrument.mic)
    if resolved is None:
        return False
    instrument, fx, plan = resolved

    # Fee floor (design memo §4) — computed AFTER the setup plan + fx are
    # known, BEFORE any bracket construction/placement. A pick below the
    # floor is refused terminal (never re-tried every tick) and NEVER placed;
    # ALPHALENS_BROKER_MAX_FEE_BPS unset (SIM) skips the check entirely.
    fee_violation = _check_fee_floor(
        plan, fx, ticker=ticker, instrument_currency=instrument.currency
    )
    if fee_violation is not None:
        _refuse_pick_terminal(
            ticker, brief_date, fee_violation, f"fee-floor:{ticker}", alert_throttled
        )
        return False

    # (The entry-trailing reservation fold was read ONCE above, before
    # safety.check — the same snapshot feeds the money gates here and the
    # drain intercept below.)

    # Portfolio gross cap (broker sizing memo §3) — the ONLY gross rail since
    # #1192 removed the currency-mismatched pre-sizing arm from safety.check.
    # Account-currency, candidate included (see the section comment above
    # _check_gross_cap). Same inputs already in scope — zero new broker I/O.
    # Staleness bound: `positions`/`account` were snapshotted a few synchronous
    # (non-network) steps above; at the 45s poll cadence that skew is benign.
    # If a future change inserts broker I/O between the snapshot and this
    # check, or drops the cadence to sub-second streaming, re-snapshot here.
    gross_violation = _check_gross_cap(
        plan,
        fx,
        account=account,
        open_verdicts=open_verdicts,
        records=records,
        positions=positions,
        ticker=ticker,
        entry_trail_fold=entry_trail_fold,
        broker=broker,
    )
    if gross_violation is not None:
        _refuse_pick_terminal(
            ticker, brief_date, gross_violation, f"gross-cap:{ticker}", alert_throttled
        )
        return False

    # Cash floor (broker sizing declared-frame memo §4.2) — declared mode
    # only; runs AFTER the gross cap (exposure first, funding second — and the
    # gross cap's fail-closed unjoined check must win, see _check_cash_floor)
    # and BEFORE classify, on the same post-sizing inputs. Zero new broker I/O.
    cash_violation = _check_cash_floor(
        plan,
        fx,
        account=account,
        open_verdicts=open_verdicts,
        records=records,
        ticker=ticker,
        entry_trail_fold=entry_trail_fold,
    )
    if cash_violation is not None:
        _refuse_pick_terminal(
            ticker, brief_date, cash_violation, f"cash-floor:{ticker}", alert_throttled
        )
        return False

    # --- Immediate ("now") tranche (#1247, memo §3.2/§3.5/§3.6) -------------
    # Handled FIRST (time-critical), AFTER the whole-plan money gates (which
    # already price both halves) and BEFORE the sibling routing: a now DEFER
    # returns before any sibling record could retire the pick and strand the
    # now half; a now refusal lets the siblings route the same tick.
    now_tiers = tuple(
        t for t in plan.entry_tiers if getattr(t, "entry_mode", "pullback") == "immediate"
    )
    now_placed = False
    reference_qty_override: float | None = None
    tranche_plan_override: tuple[str, float] | None = None
    if now_tiers:
        pick_key = f"{ticker}:{intent.meta.brief_date}"
        full_ladder_qty = float(sum(t.qty for t in plan.entry_tiers if t.qty > 0))
        outcome = _handle_now_tranche(
            broker,
            intent,
            ticker,
            instrument,
            account,
            plan,
            fx,
            now_tier=now_tiers[0],
            records=records,
            spec=spec,
            exit_spec=exit_spec,
            exit_policy=exit_policy,
            alert_throttled=alert_throttled,
            now_entry_feed_factory=now_entry_feed_factory,
            tranche_plan_override=(pick_key, full_ladder_qty),
        )
        if outcome in (_NowOutcome.DEFER, _NowOutcome.REFUSED_PICK):
            return False
        now_placed = outcome is _NowOutcome.PLACED
        pullback_tiers = tuple(
            t for t in plan.entry_tiers if getattr(t, "entry_mode", "pullback") != "immediate"
        )
        if not pullback_tiers:
            if outcome is _NowOutcome.REFUSED_NOW:
                # A now-only pick has nothing left to drain: retire it so a
                # fresh arm (new armed_ts, latest-wins) is the path back
                # (memo §3.7). The refusal detail is in the submissions
                # journal's tranche record.
                picks.mark_refused(
                    ticker, brief_date, "now tranche refused (see submissions journal)"
                )
            return now_placed
        plan = replace(plan, entry_tiers=pullback_tiers)
        if now_placed:
            # The TP ladder must cover BOTH halves' fills; the watch route
            # re-appends the SAME keyed plan (benign, no generation reset).
            reference_qty_override = full_ladder_qty
            tranche_plan_override = (pick_key, full_ladder_qty)

    # Entry-trailing intercept (memo §5 / drain_intercept): with the flag armed
    # AND the pick trailing-eligible, route it into a WATCH (per-tier watch_open
    # lines, journal-FIRST) INSTEAD of resting the three server-side limit-entry
    # orders — no broker order is placed in PR-T1 (DRY-RUN). Flag OFF (bps == 0)
    # falls straight through to classify + _place_tiers, BYTE-IDENTICAL to today
    # (PR-T0 inertness proof). The intercept lands AFTER the cash floor so a
    # watch only opens once the pick has cleared every money gate.
    intercepted = _entry_trail_intercept(
        broker,
        intent,
        ticker,
        instrument,
        account,
        plan,
        fx,
        entry_trail_fold,
        exit_policy,
        positions=positions,
        reference_qty_override=reference_qty_override,
    )
    if intercepted is not None:
        # Deliberately NOT `or now_placed`: a capacity-deferred sibling half
        # must read as not-placed so the drain retries next tick (the
        # armed_ts scan skips the now half); the pick counts as placed on
        # the tick the siblings actually route.
        return intercepted

    # The pick is about to take the CLASSIC bracket path, which carries neither
    # #1112 arm gate. Refuse a new entry while the geometry exit is active and
    # the trail is off (issue #1112 round 2, point 4). NOT terminal: this is a
    # configuration rail like KILL / ALLOW_ORDERS, so the pick stays armed and
    # places itself once the trail is on — a terminal refusal would destroy the
    # armed queue over an operator setting.
    no_trail_note = _geometry_without_entry_trail_note(exit_policy, exit_spec)
    if no_trail_note is not None:
        logger.warning("place_pick %s: refused — %s", ticker, no_trail_note)
        if alert_throttled is not None:
            alert_throttled(
                f"place_pick {ticker}: {no_trail_note}",
                f"{_GEOMETRY_WITHOUT_TRAIL_ALERT_PREFIX}:{ticker}",
            )
        return False

    placement = classify(plan, instrument, side=_ENTRY_SIDE)
    if not placement.tiers:
        logger.warning("place_pick %s: every entry tier sized to zero shares", ticker)
        return now_placed

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
            tranche_plan_override=tranche_plan_override,
        )
        > 0
    ) or now_placed


def _handle_safety_refusal(decision: Any, ticker: str, brief_date: dt.date) -> None:
    """Log a ``safety.Refuse`` and journal it when terminal.

    Terminal refusal (queue-semantics fix 2026-07-30): ONLY a capacity
    refusal (decision.terminal — the MAX_OPEN cap; the gross and cash rails
    journal their own refusals via ``_refuse_pick_terminal``) journals
    a refused line so the pick never retries — left armed it would retry
    every tick for days and then self-place a stale brief signal once
    capacity frees. Re-arming via `alphalens broker arm` is the explicit
    human path back. The transient rails (KILL file, dead chain,
    ALLOW_ORDERS master arm, daily-loss lockout) keep the pick armed —
    an inert/paused daemon must never destroy the armed queue. The
    append is fallible I/O and must never crash the drain: on OSError
    the pick stays armed and the refusal re-fires next tick
    (re-attempting the append)."""
    logger.warning("place_pick %s: refused — %s", ticker, decision.reason)
    if decision.terminal:
        try:
            picks.mark_refused(ticker, brief_date, decision.reason)
        except OSError as exc:
            logger.warning(
                "place_pick %s: refused-line append failed (pick stays armed): %s", ticker, exc
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


def _net_open_position_uics(positions: Iterable[Position]) -> tuple[frozenset[int], int]:
    """``(uics with nonzero NET quantity, count of unreadable rows)`` — a row
    is unreadable when its uic cannot be resolved OR its quantity is
    missing/None/non-finite.

    LIVE Saxo accounts run End-Of-Day netting
    (``ClosedPositionNotAccessibleInEndOfDayNettingMode``): positions net only
    at EOD, so an intraday round-trip leaves TWO ledger rows (+q and -q) that
    net to zero until the nightly netting. MAX_OPEN counts RISK UNITS, not
    ledger rows — a raw ``len(positions)`` over such a book refuses valid picks
    on phantom slots (live incident 2026-08-19: a net-flat book showed 2 rows
    and terminally refused ETSY on the MAX_OPEN rail all session). Quantities
    are summed per uic; a net magnitude within ``_QTY_EPS`` of zero is flat and
    occupies no slot. Rows whose uic cannot be resolved are counted ONE each
    (fail-conservative — never undercount risk units)."""
    net_by_uic: dict[int, float] = {}
    unresolvable = 0
    for pos in positions:
        uic = _position_uic(pos)
        if uic is None:
            unresolvable += 1
            continue
        # A missing/None/non-finite quantity is UNRESOLVABLE, never flat:
        # abs(nan) > eps is False, so a malformed row would otherwise net to
        # "no open position" — and net-flatness is the fired-class
        # retraction's last live-position gate (#1223 zen M1). A genuine 0.0
        # row still nets flat below.
        raw_qty = getattr(pos, "quantity", None)
        if raw_qty is None or not math.isfinite(float(raw_qty)):
            unresolvable += 1
            continue
        net_by_uic[uic] = net_by_uic.get(uic, 0.0) + float(raw_qty)
    open_uics = frozenset(uic for uic, net in net_by_uic.items() if abs(net) > _QTY_EPS)
    return open_uics, unresolvable


def _netted_all_positions(rows: Iterable[Position]) -> dict[int, Position]:
    """One Position per uic with the NET quantity over the raw ledger rows.

    LIVE Saxo nets only at End-Of-Day, so an intraday round-trip leaves TWO
    rows (+q and -q) for one uic (see ``_net_open_position_uics``). A
    last-row-wins dict over that book shows whichever leg Saxo returned second
    — on the short leg the protection reconciler paged a spurious "unexpected
    SHORT — manual intervention" for a perfectly normal stop fill (#1221).

    A single-row uic keeps its raw row untouched. A multi-row uic keeps the
    FIRST row's fields with the summed quantity — ``avg_price`` is NOT blended
    (the blend helper is the saxo adapter's, and both consumers of this map
    read only ``quantity``). Rows with an unreadable quantity are excluded
    from a multi-row sum, and a non-finite FIRST-row quantity resets the base
    to 0.0 — but the first row's other fields (``avg_price``, ``market_value``,
    ...) stay its own, so a consumer reading beyond ``quantity`` on a multi-row
    uic must not assume they describe the net. A net-flat pair stays in the
    map at 0.0 — callers branch on the quantity, not on presence."""
    netted: dict[int, Position] = {}
    for pos in rows:
        uic = _position_uic(pos)
        if uic is None:
            continue
        first = netted.get(uic)
        if first is None:
            netted[uic] = pos
            continue
        raw_qty = getattr(pos, "quantity", None)
        if raw_qty is None or not math.isfinite(float(raw_qty)):
            continue
        base_qty = getattr(first, "quantity", None)
        base = float(base_qty) if base_qty is not None and math.isfinite(float(base_qty)) else 0.0
        netted[uic] = cast(Position, replace(first, quantity=base + float(raw_qty)))
    return netted


def build_protection_view(
    broker: Broker,
    _records: list[Mapping[str, Any]],
    *,
    exit_policy: ExitPolicy | None = None,
    peak_by_uic: Mapping[int, float] | None = None,
    last_price_by_uic: Mapping[int, float] | None = None,
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
    # #1221: fold same-uic ledger rows to their NET quantity — a raw
    # last-row-wins dict over an un-netted intraday round-trip pair showed
    # whichever leg Saxo returned second and could page a spurious SHORT alert.
    all_positions = _netted_all_positions(broker.get_positions())

    long_positions: dict[int, Position] = {}
    # Boot-unreachable fallback (#1141): build_default_deps refuses a broker
    # without the netted reads, so the else-branch serves only directly composed
    # deps (tests). Since #1221 it yields ONE netted row per uic (previously
    # last-row-wins) — strictly closer to get_long_positions semantics; no
    # production path can reach it.
    longs = (
        broker.get_long_positions()
        if isinstance(broker, SupportsNettedPositionReads)
        else list(all_positions.values())
    )
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
        trailed_stop_by_uic=_fold_trailed_since_latest_plan(journal_lines),
        # Task 4: this tick's high-water peaks / live prices, fetched ONLY on the
        # trailing path (``_run_protection_pass`` passes them when the cached policy
        # trails, else omits them). Default empty -> the trailing arm stays dark (a
        # missing peak is a feed veto), so every non-trailing caller / pure test /
        # second broker keeps today's byte-identical dark path.
        peak_by_uic=peak_by_uic if peak_by_uic is not None else {},
        last_price_by_uic=last_price_by_uic if last_price_by_uic is not None else {},
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
    if isinstance(broker, SupportsNettedPositionReads):
        live = broker.get_positions_by_uic(action.uic)
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
    if isinstance(broker, SupportsNettedPositionReads):
        target = max(broker.get_positions_by_uic(action.uic).quantity, 0.0)
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
    # Latch the post-fill move ONLY on this confirmed success — a failed amend
    # never reaches here (it returned above on the BrokerError branch, having
    # journaled amend_failed like any other amend arm). Both markers are
    # best-effort (like amend_ok): a dropped marker only costs one redundant,
    # harmless re-PATCH next tick (absolute target price + qty, idempotent-in-
    # effect), never a wrong or naked stop. Both a trail and a reanchor AmendStop
    # carry ``reanchor_avg_price``, so the write is keyed on ``reason`` FIRST — a
    # trail must journal ``trailed`` (its ratchet floor + telemetry), NOT
    # ``reanchored`` (the per-blend reanchor latch).
    if action.reason == "trail":
        trail_level = action.stop_price  # the clamped level actually placed
        trail_peak = action.trail_peak
        trail_last_price = action.trail_last_price
        _journal_outcome_best_effort(
            lambda: _journal_trailed(
                action.uic, trail_level, peak=trail_peak, last_price=trail_last_price
            ),
            throttle,
            report,
            uic=action.uic,
            kind="trailed",
        )
    elif action.reanchor_avg_price is not None:
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
    if isinstance(broker, SupportsNettedPositionReads):
        live = broker.get_positions_by_uic(action.uic)
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
        placed_stop = stop_broker.place_standalone_stop(
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
        lambda: _journal_stop_placed(
            action.uic, qty, order_id=placed_stop.entry_order_id, ref=action.request_id
        ),
        throttle,
        report,
        uic=action.uic,
        kind="stop_placed",
    )


__all__ = [
    "LoopDeps",
    "TickReport",
    "build_default_deps",
    "heartbeat_metric",
    "kill_active_metric",
    "run_daemon",
    "run_once",
]
