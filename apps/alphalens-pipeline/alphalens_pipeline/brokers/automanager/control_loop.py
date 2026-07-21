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
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from alphalens_pipeline.brokers.automanager.position_manager import (
    AlertOnly,
    BrokerView,
    CancelRemaining,
    NoOp,
    PlaceStandaloneStop,
    advance,
)

if TYPE_CHECKING:
    from alphalens_pipeline.brokers.contract import Broker
    from alphalens_pipeline.brokers.reconcile import ReconcileVerdict

logger = logging.getLogger(__name__)

KILL_FILE_PATH = Path.home() / ".alphalens" / "broker_orders" / "KILL"

# Prometheus heartbeat gauge (Task 13 wires _default_emit_heartbeat as the
# run_daemon default; the metric name has one home here).
HEARTBEAT_METRIC = 'alphalens_broker_manager_last_tick_timestamp_seconds{job="broker-manager"}'


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
    place_standalone_stop: Callable[[int, str, float, float], None]
    sweep_orphans_fn: Callable[[Broker], list[Any]]
    alert: Callable[[str], None]


@dataclass
class TickReport:
    picks_placed: int = 0
    stops_placed: int = 0
    cancels: int = 0
    alerts: int = 0
    orphans: int = 0
    verdict_count: int = 0
    actions: list[tuple[str, str]] = field(default_factory=list)  # (ticker, Action class)


def _always() -> bool:
    return True


def _default_emit_heartbeat() -> None:
    """Placeholder — Task 13 replaces this with the real textfile emitter."""


def run_once(deps: LoopDeps, *, sweep_orphans: bool = False) -> TickReport:
    """One control-loop tick. Placement is gated on (no KILL) AND (chain alive);
    reconcile + Action execution ALWAYS run so a KILL still cancels and a dead
    chain still surfaces terminal state."""
    report = TickReport()
    kill = deps.kill_file.exists()
    chain = deps.ensure_alive()
    if not getattr(chain, "alive", False):
        deps.alert(
            f"session-keeper: chain dead — {getattr(chain, 'reason', None)}; placement halted"
        )

    if sweep_orphans:
        for orphan in deps.sweep_orphans_fn(deps.broker):
            deps.alert(f"orphan (placed but never journaled): {orphan}")
            report.orphans += 1

    if not kill and getattr(chain, "alive", False):
        for pick in deps.iter_picks():
            if deps.place_pick(pick):
                report.picks_placed += 1

    records = deps.read_records()
    verdicts = deps.verdicts_fn(records, deps.broker)
    report.verdict_count = len(verdicts)
    position_view = deps.build_position_view(deps.broker, records)
    for verdict in verdicts:
        action = advance(verdict, position_view)
        report.actions.append((verdict.ticker, type(action).__name__))
        _execute_action(deps, verdict, action, position_view, kill=kill, report=report)
    return report


def _execute_action(
    deps: LoopDeps,
    verdict: ReconcileVerdict,
    action: Any,
    position_view: BrokerView,
    *,
    kill: bool,
    report: TickReport,
) -> None:
    request_id = str(verdict.details.get("client_request_id") or "")
    if isinstance(action, NoOp):
        return
    if isinstance(action, AlertOnly):
        deps.alert(action.reason)
        report.alerts += 1
        return
    if isinstance(action, CancelRemaining):
        for order_id in position_view.working_children.get(request_id, ()):  # ungated safe op
            deps.broker.cancel_order(order_id)
            report.cancels += 1
        return
    if isinstance(action, PlaceStandaloneStop):
        if kill:
            deps.alert(f"KILL active — NOT placing standalone stop for {verdict.ticker}")
            return
        disaster = position_view.disaster_stops.get(request_id)
        if disaster is None:  # defence: advance already alerted, but never place blind
            deps.alert(
                f"{verdict.ticker}: standalone-stop placement skipped — no journaled disaster stop"
            )
            return
        deps.place_standalone_stop(disaster.uic, disaster.side, action.qty, action.stop_price)
        report.stops_placed += 1


def run_daemon(
    deps: LoopDeps,
    *,
    once: bool,
    poll_seconds: float,
    sleep_fn: Callable[[float], None] = time.sleep,
    is_running: Callable[[], bool] = _always,
    heartbeat_fn: Callable[[], None] = _default_emit_heartbeat,
) -> None:
    """Drive run_once forever (orphan sweep on the FIRST tick only), or once."""
    first = True
    while is_running():
        run_once(deps, sweep_orphans=first)
        heartbeat_fn()  # Task 13: writes the Prometheus heartbeat gauge
        first = False
        if once:
            return
        sleep_fn(poll_seconds)


def build_default_deps(*, poll_seconds: float) -> LoopDeps:
    """Wire the real Task 1-10 seams. Imported lazily so the alphalens binary's
    startup budget stays off this path (lazy-CLI doctrine); covered by the
    SAXO_LIVE_TEST=1 SIM probe, not the hermetic unit tests. The four factory
    helpers (_default_oauth_provider, _make_place_pick, _make_position_view_builder,
    _make_standalone_stop_placer) compose the Task 1-10 seams; they are validated
    only by the SIM probe. The pluggable fill-source (fill_source.PollingFillSource)
    stays a tested seam for the phase-B streaming drop-in; the MVP loop detects
    fills through reconcile_bridge.verdicts (reconcile classifies FILLED), so no
    PollingFillSource instance is wired into LoopDeps here."""
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

    broker = get_default_broker()
    keeper = session_keeper.SessionKeeper(_default_oauth_provider())

    def _read_records() -> list[Mapping[str, Any]]:
        return list(iter_submission_records(DEFAULT_SUBMISSIONS_PATH))

    return LoopDeps(
        broker=broker,
        kill_file=KILL_FILE_PATH,
        ensure_alive=keeper.ensure_alive,
        iter_picks=picks.iter_picks,
        place_pick=_make_place_pick(broker),
        read_records=_read_records,
        verdicts_fn=reconcile_bridge.verdicts,
        build_position_view=_make_position_view_builder(broker),
        place_standalone_stop=_make_standalone_stop_placer(broker),
        sweep_orphans_fn=lambda b: orphan_sweeper.sweep(b, _read_records()),
        alert=_default_alert(),
    )


# --- SIM-probe-only factory helpers (Component 6 "placer" home) --------------
# Thin composers over the Task 1-10 seams. They carry NO hermetic unit-test
# cycle (build_default_deps and everything it wires is exercised end-to-end only
# by the SAXO_LIVE_TEST=1 SIM live probe — a deferred follow-up). Where a seam is
# not yet shipped (the pick -> SetupPlan resolution + append-only journal write,
# and the out-of-band standalone-stop journal that feeds position_manager.BrokerView),
# the composer raises NotImplementedError at exactly that boundary rather than
# guessing an unshipped API; the SIM live probe closes those seams.


def _default_oauth_provider() -> Any:
    """Return the shipped OAuthTokenProvider wired from the Saxo env vars."""
    from alphalens_pipeline.brokers.saxo.tokens import OAuthTokenProvider

    return OAuthTokenProvider.from_env()


def _default_alert() -> Callable[[str], None]:
    """Env-driven Telegram alert sink over the canonical TelegramClient
    (TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID). send_message never raises, so a
    delivery blip cannot crash a tick. SIM-probe-only."""
    import os

    from alphalens_pipeline.data.alt_data.telegram_client import TelegramClient

    client = TelegramClient(os.environ["TELEGRAM_BOT_TOKEN"])
    chat_id = os.environ["TELEGRAM_CHAT_ID"]

    def _alert(message: str) -> None:
        client.send_message(chat_id, message)

    return _alert


def _make_place_pick(broker: Broker) -> Callable[[Any], bool]:
    """Compose safety.check -> placement_planner.classify -> placer loop over
    place_bracket_order + append-only journal for one armed pick (reusing the
    client_request_id on retry). The pick -> SetupPlan/InstrumentRef resolution
    and the submissions-journal write land with the SIM live probe."""

    def _place(pick: Any) -> bool:
        raise NotImplementedError(
            "place-pick wiring (safety.check -> placement_planner.classify -> "
            "place_bracket_order + submissions journal) is closed by the "
            "SAXO_LIVE_TEST=1 SIM live probe — a deferred follow-up"
        )

    return _place


def _make_position_view_builder(
    broker: Broker,
) -> Callable[[Broker, list[Mapping[str, Any]]], BrokerView]:
    """Fold a broker snapshot + the out-of-band standalone-stop journal into a
    position_manager.BrokerView. The standalone-stop journal read lands with the
    SIM live probe (it is written by _make_standalone_stop_placer)."""

    def _build(_broker: Broker, _records: list[Mapping[str, Any]]) -> BrokerView:
        raise NotImplementedError(
            "position-view wiring (broker snapshot + standalone-stop journal -> "
            "BrokerView) is closed by the SAXO_LIVE_TEST=1 SIM live probe — a "
            "deferred follow-up"
        )

    return _build


def _make_standalone_stop_placer(broker: Broker) -> Callable[[int, str, float, float], None]:
    """Adapt SaxoBroker.place_standalone_stop + the out-of-band standalone-stop
    journal write (feeding the position-view builder) into the LoopDeps placer
    seam. Both land together with the SIM live probe so the placement and its
    journal record are never split (a placed-but-unjournaled stop is an orphan)."""

    def _place(uic: int, side: str, qty: float, stop_price: float) -> None:
        raise NotImplementedError(
            "standalone-stop placer wiring (SaxoBroker.place_standalone_stop + "
            "out-of-band journal write) is closed by the SAXO_LIVE_TEST=1 SIM "
            "live probe — a deferred follow-up"
        )

    return _place


__all__ = [
    "HEARTBEAT_METRIC",
    "KILL_FILE_PATH",
    "LoopDeps",
    "TickReport",
    "build_default_deps",
    "run_daemon",
    "run_once",
]
