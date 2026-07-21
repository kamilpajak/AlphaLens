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
from collections.abc import Callable, Iterable, Iterator, Mapping
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
from alphalens_pipeline.brokers.contract import (
    BrokerCapabilityError,
    BrokerError,
    SupportsStandaloneStop,
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
    place_standalone_stop: Callable[[int, str, float, float, str], None]
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
    chain still surfaces terminal state."""
    report = TickReport()
    kill = deps.kill_file.exists()
    chain = deps.ensure_alive()
    if not getattr(chain, "alive", False):
        deps.alert(
            f"session-keeper: chain dead — {getattr(chain, 'reason', None)}; placement halted"
        )

    if sweep_orphans:
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

    if not kill and getattr(chain, "alive", False):
        # Drain only picks NOT yet joined to submissions.jsonl (design §Data-flow
        # step 4). Read the journal ONCE before the drain; a pick placed earlier in
        # THIS tick is added to already_submitted below so a duplicate armed line
        # later in the same tick is skipped (an out-of-tick placement is caught by
        # the next tick's fresh read).
        already_submitted = _submitted_pick_keys(deps.read_records())
        for pick in deps.iter_picks():
            key = _pick_key(pick)
            if key in already_submitted:
                continue
            if deps.place_pick(pick):
                report.picks_placed += 1
                already_submitted.add(key)

    records = deps.read_records()
    # reconcile up-front calls list_open_orders / get_open_position_references /
    # get_closed_position_rows; a persistent BrokerError here must not crash the
    # daemon (systemd Restart=on-failure would then permanently give up, leaving
    # every position unreconciled). Alert and skip verdict processing this tick.
    try:
        verdicts = deps.verdicts_fn(records, deps.broker)
    except BrokerError as exc:
        deps.alert(f"reconcile failed (broker error) — verdicts skipped this tick: {exc}")
        report.alerts += 1
        return report
    report.verdict_count = len(verdicts)
    try:
        position_view = deps.build_position_view(deps.broker, records)
    except BrokerError as exc:
        deps.alert(f"position-view build failed (broker error) — actions skipped this tick: {exc}")
        report.alerts += 1
        return report
    for verdict in verdicts:
        action = advance(verdict, position_view)
        report.actions.append((verdict.ticker, type(action).__name__))
        # One position's broker call (cancel / standalone-stop) failing must not
        # take down the tick — alert and skip only that verdict.
        try:
            _execute_action(deps, verdict, action, position_view, kill=kill, report=report)
        except BrokerError as exc:
            deps.alert(
                f"{verdict.ticker}: {type(action).__name__} failed (broker error) — skipped: {exc}"
            )
            report.alerts += 1
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
        deps.place_standalone_stop(
            disaster.uic, disaster.side, action.qty, action.stop_price, request_id
        )
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
    if not isinstance(broker, SupportsStandaloneStop):
        raise BrokerCapabilityError(
            f"broker {broker.name!r} does not implement place_standalone_stop "
            "(SupportsStandaloneStop) — the auto-manager's disaster-stop flow "
            "requires it; wire a different broker or add the capability."
        )
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


def _fold_standalone_stop_journal(
    lines: Iterable[Mapping[str, Any]],
) -> tuple[dict[str, DisasterStop], frozenset[str]]:
    """Fold the out-of-band standalone-stop journal into (disaster_stops keyed by
    the entry's client_request_id, the set of protected client_request_ids).

    Protection is correlated by the entry's client_request_id — NOT its Uic. Two
    entries sharing one Uic each require their OWN placed stop; a Uic-keyed
    correlation would mark both protected the moment the first one's stop posts,
    leaving the second entry silently unprotected. A "placed" / "intent" line
    without a client_request_id (a legacy Uic-only line) therefore confers no
    protection.

    An "intent" line (written BEFORE the POST) confers protection just like
    "placed": a crash between the POST and the "placed" write leaves only the
    "intent" line, and treating it as protected/in-flight stops advance from
    re-issuing a SECOND standalone stop on restart. Pure — no I/O; malformed
    lines are skipped."""
    disaster_stops: dict[str, DisasterStop] = {}
    protected: set[str] = set()
    for line in lines:
        kind = line.get("kind")
        try:
            if kind == "planned" and line.get("client_request_id"):
                disaster_stops[str(line["client_request_id"])] = DisasterStop(
                    uic=int(line["uic"]),
                    side=str(line["side"]),
                    stop_price=float(line["stop_price"]),
                )
            elif kind in {"intent", "placed"} and line.get("client_request_id"):
                protected.add(str(line["client_request_id"]))
        except (KeyError, TypeError, ValueError):
            continue
    return disaster_stops, frozenset(protected)


def _default_oauth_provider() -> Any:
    """Return the shipped OAuthTokenProvider wired from the Saxo env vars."""
    from alphalens_pipeline.brokers.saxo.tokens import OAuthTokenProvider

    return OAuthTokenProvider.from_env()


def _default_alert() -> Callable[[str], None]:
    """Env-driven Telegram alert sink over the canonical TelegramClient
    (TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID). send_message never raises, so a
    delivery blip cannot crash a tick. SIM-probe-only.

    Operational alert bodies carry raw request-id reprs / reasons with `_`, `*`,
    `[` — under the client's default parse_mode="Markdown" those trip a Telegram
    400 and the alert is SILENTLY dropped (defeating the safety-alert path). Send
    plain: parse_mode="" disables entity parsing so the body goes through
    verbatim."""
    import os

    from alphalens_pipeline.data.alt_data.telegram_client import TelegramClient

    client = TelegramClient(os.environ["TELEGRAM_BOT_TOKEN"])
    chat_id = os.environ["TELEGRAM_CHAT_ID"]

    def _alert(message: str) -> None:
        client.send_message(chat_id, message, parse_mode="")

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

        def _journal_tier(tier: Any, placed: Any) -> None:
            # Journal each tier IMMEDIATELY after its place_bracket_order — NOT
            # batched after the whole loop (HIGH-2). A crash mid-loop then leaves
            # the pick already joined to submissions.jsonl (at most a partial
            # ladder), so the pick-drain does not re-place the full set on restart.
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
                bracket = tier.bracket
                placed = broker.place_bracket_order(bracket)
                _journal_tier(tier, placed)
                placed_count += 1
        except BrokerError as exc:
            failure_note = (
                f"placement stopped after {placed_count}/{len(placement.tiers)} bracket(s): {exc}"
            )
            # Journal a note-only record so the failure is traced (and, when
            # nothing placed, the pick is not silently retried forever).
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
        return placed_count > 0

    return _place


def _make_position_view_builder(
    broker: Broker,
) -> Callable[[Broker, list[Mapping[str, Any]]], BrokerView]:
    """Fold the submissions journal + the out-of-band standalone-stop journal into
    a position_manager.BrokerView: working_children reads exit order ids straight
    from the submission records, filtered to broker orders still WORKING;
    disaster_stops comes from the "planned" journal lines (written by
    _make_place_pick); protected_request_ids is every entry whose OWN
    client_request_id has a matching "placed" line (written by
    _make_standalone_stop_placer) — correlated by client_request_id, not Uic."""

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

        disaster_stops, protected_request_ids = _fold_standalone_stop_journal(
            _iter_standalone_stop_journal()
        )
        return BrokerView(
            protected_request_ids=protected_request_ids,
            disaster_stops=disaster_stops,
            working_children=working_children,
        )

    return _build


def _standalone_stop_request_id(entry_request_id: str) -> str:
    """Deterministic stop x-request-id derived from the entry client_request_id.

    Deterministic (not uuid4) so a crash-window re-POST reuses the SAME
    x-request-id and hits Saxo's 15 s dedup instead of placing a second stop."""
    return f"{entry_request_id}-stop"


def _make_standalone_stop_placer(
    broker: SupportsStandaloneStop,
) -> Callable[[int, str, float, float, str], None]:
    """Adapt SaxoBroker.place_standalone_stop with a recoverable place-then-journal.

    Journals an "intent" line (carrying the entry client_request_id + the
    DETERMINISTIC stop request_id) BEFORE the POST, then the "placed" line after.
    Both are read back by _make_position_view_builder to mark THIS entry's
    client_request_id protected (correlated by client_request_id, not Uic, so an
    entry sharing the Uic is not falsely marked protected). If the daemon crashes
    between the POST and the "placed" write, the "intent" line already marks the
    position protected/in-flight, so advance will not re-issue; and the
    deterministic request_id makes a within-window re-POST idempotent under Saxo's
    x-request-id dedup rather than a duplicate live stop."""

    def _place(uic: int, side: str, qty: float, stop_price: float, request_id: str) -> None:
        stop_request_id = _standalone_stop_request_id(request_id)
        _append_standalone_stop_journal(
            {
                "kind": "intent",
                "client_request_id": request_id,
                "stop_request_id": stop_request_id,
                "uic": uic,
                "side": side,
                "qty": qty,
                "stop_price": stop_price,
            }
        )
        placed = broker.place_standalone_stop(uic, side, qty, stop_price, stop_request_id)
        _append_standalone_stop_journal(
            {
                "kind": "placed",
                "client_request_id": request_id,
                "stop_request_id": stop_request_id,
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
