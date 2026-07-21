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
    DisasterStop,
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
# cycle (test_control_loop.py injects LoopDeps as stubs; build_default_deps and
# everything it wires is exercised end-to-end only by the deferred
# SAXO_LIVE_TEST=1 SIM live probe). _make_place_pick and _make_standalone_stop_placer
# both read/write STANDALONE_STOP_JOURNAL_PATH — a tiny out-of-band append-only
# journal (mirrors the picks.jsonl / submissions.jsonl append-only pattern) that
# correlates one entry bracket's client_request_id with its plan-level disaster
# stop ("planned", written at placement time) and marks a Uic protected once its
# standalone stop actually posts ("placed", written at stop-placement time).
# _make_position_view_builder folds both halves back into position_manager.BrokerView.

STANDALONE_STOP_JOURNAL_PATH = (
    Path.home() / ".alphalens" / "broker_orders" / "standalone_stops.jsonl"
)

_DEFAULT_BRIEFS_DIR = Path.home() / ".alphalens" / "thematic_briefs"
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
    """Append one line to the out-of-band standalone-stop journal (never rewrites)."""
    import json

    STANDALONE_STOP_JOURNAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    with STANDALONE_STOP_JOURNAL_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True, default=str) + "\n")


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
    place_bracket_order + the submissions journal for one armed pick, plus the
    "planned" half of the out-of-band standalone-stop journal (the entry's
    plan-level disaster stop, correlated by client_request_id for
    _make_position_view_builder to fold back later). A safety refusal or a
    resolve/size/placement failure logs and returns False rather than raising —
    one bad pick must never crash a tick."""

    def _place(pick: Any) -> bool:
        import datetime as _dt

        from alphalens_pipeline.brokers.automanager import safety
        from alphalens_pipeline.brokers.automanager.placement_planner import classify
        from alphalens_pipeline.brokers.automanager.reconcile_bridge import (
            verdicts as reconcile_verdicts,
        )
        from alphalens_pipeline.brokers.contract import BrokerError
        from alphalens_pipeline.brokers.execution import build_fx_conversion
        from alphalens_pipeline.brokers.routing import resolve_us_instrument
        from alphalens_pipeline.brokers.submission_log import (
            DEFAULT_SUBMISSIONS_PATH,
            append_submission_record,
            build_submission_record,
            iter_submission_records,
        )
        from alphalens_pipeline.paper.brief_loader import load_brief
        from alphalens_pipeline.paper.sizing import (
            TradeSetupNotPlannableError,
            compute_setup_plan,
        )

        ticker = pick.ticker.upper()
        try:
            candidates = load_brief(pick.date, _DEFAULT_BRIEFS_DIR)
        except (FileNotFoundError, ValueError) as exc:
            logger.warning("place_pick %s: brief unavailable for %s: %s", ticker, pick.date, exc)
            return False
        candidate = next((c for c in candidates if c.ticker.upper() == ticker), None)
        if candidate is None or candidate.trade_setup is None:
            logger.warning(
                "place_pick %s: no plannable trade_setup in the %s brief", ticker, pick.date
            )
            return False

        try:
            account = broker.get_account()
            positions = broker.get_positions()
            records = list(iter_submission_records(DEFAULT_SUBMISSIONS_PATH))
            open_verdicts = reconcile_verdicts(records, broker)
        except BrokerError as exc:
            logger.warning("place_pick %s: broker read failed: %s", ticker, exc)
            return False

        entry_by_request_id = {
            str(bracket.get("client_request_id")): bracket
            for record in records
            for bracket in record.get("brackets") or []
        }
        today_iso = _dt.date.today().isoformat()
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
                bracket = entry_by_request_id.get(
                    str(verdict.details.get("client_request_id") or "")
                )
                if bracket and bracket.get("entry") is not None and bracket.get("qty") is not None:
                    gross_committed += float(bracket["entry"]) * float(bracket["qty"])

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

        try:
            instrument = resolve_us_instrument(broker, ticker)
            if not instrument.currency:
                logger.warning("place_pick %s: resolved with no instrument currency", ticker)
                return False
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
                    return False
                fx = build_fx_conversion(get_fx_rate(account.currency, instrument.currency))
            plan = compute_setup_plan(
                brief_trade_setup=candidate.trade_setup,
                paper_equity=account.total_value,
                scale_factor=1.0,
                fx=fx,
            )
        except (BrokerError, TradeSetupNotPlannableError) as exc:
            logger.warning("place_pick %s: resolve/size failed: %s", ticker, exc)
            return False

        placement = classify(plan, instrument, side=_ENTRY_SIDE)
        if not placement.tiers:
            logger.warning("place_pick %s: every entry tier sized to zero shares", ticker)
            return False

        placed_records: list[dict[str, Any]] = []
        planned_stop_lines: list[dict[str, Any]] = []
        failure_note: str | None = None
        try:
            for tier in placement.tiers:
                bracket = tier.bracket
                placed = broker.place_bracket_order(bracket)
                placed_records.append(
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
                )
                planned_stop_lines.append(
                    {
                        "kind": "planned",
                        "client_request_id": bracket.client_request_id,
                        "uic": int(instrument.broker_instrument_id),
                        "side": _DISASTER_STOP_SIDE,
                        "stop_price": placement.disaster_stop_price,
                    }
                )
        except BrokerError as exc:
            failure_note = (
                f"placement stopped after {len(placed_records)}/{len(placement.tiers)} "
                f"bracket(s): {exc}"
            )
        finally:
            if placed_records or failure_note:
                record = build_submission_record(
                    brief_date=pick.date.isoformat(),
                    ticker=ticker,
                    mic=instrument.exchange_mic,
                    uic=instrument.broker_instrument_id,
                    brackets=placed_records,
                    note=failure_note,
                    sizing_currency=account.currency,
                    instrument_currency=instrument.currency,
                    sizing_equity=account.total_value,
                    fx=fx,
                )
                append_submission_record(record)
                for line in planned_stop_lines:
                    _append_standalone_stop_journal(line)

        if failure_note:
            logger.warning("place_pick %s: %s", ticker, failure_note)
        return bool(placed_records)

    return _place


def _make_position_view_builder(
    broker: Broker,
) -> Callable[[Broker, list[Mapping[str, Any]]], BrokerView]:
    """Fold the submissions journal + the out-of-band standalone-stop journal into
    a position_manager.BrokerView: working_children reads exit order ids straight
    from the submission records, filtered to broker orders still WORKING;
    disaster_stops comes from the "planned" journal lines (written by
    _make_place_pick); protected_request_ids is every entry whose Uic has a
    matching "placed" line (written by _make_standalone_stop_placer)."""

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

        disaster_stops: dict[str, DisasterStop] = {}
        protected_uics: set[int] = set()
        for line in _iter_standalone_stop_journal():
            kind = line.get("kind")
            try:
                if kind == "planned" and line.get("client_request_id"):
                    disaster_stops[str(line["client_request_id"])] = DisasterStop(
                        uic=int(line["uic"]),
                        side=str(line["side"]),
                        stop_price=float(line["stop_price"]),
                    )
                elif kind == "placed" and line.get("uic") is not None:
                    protected_uics.add(int(line["uic"]))
            except (KeyError, TypeError, ValueError):
                continue

        protected_request_ids = frozenset(
            request_id
            for request_id, disaster in disaster_stops.items()
            if disaster.uic in protected_uics
        )
        return BrokerView(
            protected_request_ids=protected_request_ids,
            disaster_stops=disaster_stops,
            working_children=working_children,
        )

    return _build


def _make_standalone_stop_placer(broker: Broker) -> Callable[[int, str, float, float], None]:
    """Adapt SaxoBroker.place_standalone_stop, then write the "placed" half of
    the out-of-band standalone-stop journal — read back by
    _make_position_view_builder on the NEXT tick to mark every entry sharing
    this Uic protected. The placement and its journal record are NOT
    transactional (a crash between the two is exactly the orphan window
    orphan_sweeper flags), matching _make_place_pick's placement->journal order."""

    def _place(uic: int, side: str, qty: float, stop_price: float) -> None:
        placed = broker.place_standalone_stop(uic, side, qty, stop_price)
        _append_standalone_stop_journal(
            {
                "kind": "placed",
                "uic": uic,
                "side": side,
                "qty": qty,
                "stop_price": stop_price,
                "order_id": placed.entry_order_id,
            }
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
