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
from typing import TYPE_CHECKING, Any, cast

from alphalens_pipeline.brokers.automanager.position_manager import (
    Action,
    AlertOnly,
    BrokerView,
    CancelRemaining,
    CancelSellLegs,
    NoOp,
    PlaceStop,
    PlannedExit,
    ProtectionView,
    UpgradeToOco,
    _oco_enabled,
    advance,
    reconcile_protection,
)
from alphalens_pipeline.brokers.contract import (
    _QTY_EPS,
    BrokerCapabilityError,
    BrokerError,
    OrderRejectedError,
    Position,
    SupportsStandaloneStop,
    _is_sell_orders_already_exist,
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
    # Broker-state-truth protection (saxo-oco memo §6): ONE snapshot per tick,
    # then a pure reconcile_protection diff executed action-by-action. The
    # executor closes over the broker + the alert throttle; run_once wires the
    # per-action BrokerError boundary around each call.
    build_protection_view: Callable[[Broker, list[Mapping[str, Any]]], ProtectionView]
    execute_protection: Callable[[Action, bool, TickReport], None]
    sweep_orphans_fn: Callable[[Broker], list[Any]]
    alert: Callable[[str], None]


@dataclass
class TickReport:
    picks_placed: int = 0
    exits_placed: int = 0  # protective stops placed this tick (rung 0 -> 1)
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
    # The verdict-level advance loop (terminal / round-trip CancelRemaining +
    # divergence alerts) and the broker-state protection pass are INDEPENDENT: a
    # reconcile-bridge or position-view BrokerError must not skip protection (the
    # safety-critical path). Each is isolated so one failing never starves the
    # other; the protection pass has its OWN build boundary below.
    try:
        verdicts = deps.verdicts_fn(records, deps.broker)
    except BrokerError as exc:
        deps.alert(f"reconcile failed (broker error) — verdicts skipped this tick: {exc}")
        report.alerts += 1
        verdicts = []
    report.verdict_count = len(verdicts)
    if verdicts:
        try:
            position_view = deps.build_position_view(deps.broker, records)
        except BrokerError as exc:
            deps.alert(
                f"position-view build failed (broker error) — actions skipped this tick: {exc}"
            )
            report.alerts += 1
            position_view = None
        if position_view is not None:
            for verdict in verdicts:
                action = advance(verdict, position_view)
                report.actions.append((verdict.ticker, type(action).__name__))
                # One position's broker call (a cancel of leftover exits) failing
                # must not take down the tick — alert and skip only that verdict.
                try:
                    _execute_action(deps, verdict, action, position_view, report=report)
                except BrokerError as exc:
                    deps.alert(
                        f"{verdict.ticker}: {type(action).__name__} failed "
                        f"(broker error) — skipped: {exc}"
                    )
                    report.alerts += 1

    # Broker-state-truth protection pass (saxo-oco memo §6): ONE snapshot, then a
    # pure desired-vs-actual diff over live positions + live SELL legs, each
    # action executed inside its OWN per-action BrokerError boundary so one uic's
    # failure never aborts the tick or the other uics. This is the ONLY path that
    # places / resizes protective stops now (advance no longer does).
    try:
        protection_view = deps.build_protection_view(deps.broker, records)
    except BrokerError as exc:
        deps.alert(f"protection-view build failed (broker error) — protection skipped: {exc}")
        report.alerts += 1
        return report
    for action in reconcile_protection(protection_view):
        report.actions.append(("protection", type(action).__name__))
        try:
            deps.execute_protection(action, kill, report)
        except BrokerError as exc:
            deps.alert(f"protection {type(action).__name__} failed (broker error) — skipped: {exc}")
            report.alerts += 1
    return report


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
        deps.alert(action.reason)
        report.alerts += 1
        return
    if isinstance(action, CancelRemaining):
        for order_id in position_view.working_children.get(request_id, ()):  # ungated safe op
            deps.broker.cancel_order(order_id)
            report.cancels += 1
        return


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

    # One throttle instance lives for the daemon's lifetime so the re-alert
    # interval + per-uic failure escalation persist across ticks; it wraps the
    # same base sink the generic (un-throttled) tick alerts use.
    base_alert = _default_alert()
    throttle = _AlertThrottle(base_alert)

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
        execute_protection=_make_protection_executor(broker, throttle),
        sweep_orphans_fn=lambda b: orphan_sweeper.sweep(b, _read_records()),
        alert=base_alert,
    )


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
    latest_by_crid: dict[str, tuple[int, Mapping[str, Any]]] = {}
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
        prev = latest_by_crid.get(str(crid))
        if prev is None or gen >= prev[0]:
            latest_by_crid[str(crid)] = (gen, line)

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
        )
    return result


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
    """Fold the submissions journal into a position_manager.BrokerView carrying
    ONLY ``working_children`` — the still-WORKING exit order ids per entry, used
    by the terminal / round-trip ``CancelRemaining`` sweep.

    No journal line confers protection any more (saxo-oco memo §7): the
    disaster-stop / protected halves are gone (Bug A), so those BrokerView fields
    are supplied empty. Protection is derived purely from live broker state by
    the protection pass (``build_protection_view`` + ``reconcile_protection``)."""

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

        return BrokerView(
            protected_request_ids=frozenset(),  # protection is broker-state truth, not journal
            disaster_stops={},
            working_children=working_children,
        )

    return _build


# --- Broker-state-truth protection (saxo-oco memo §5/§6) ---------------------


def _position_uic(pos: Position) -> int | None:
    """The uic a Position belongs to (``broker_instrument_id`` is ``str(Uic)``)."""
    try:
        return int(pos.instrument.broker_instrument_id)
    except (TypeError, ValueError, AttributeError):
        return None


def build_protection_view(broker: Broker, _records: list[Mapping[str, Any]]) -> ProtectionView:
    """Assemble the ONE per-tick protection snapshot (saxo-oco memo §6): live
    netted positions + live working SELL legs (correlated by uic) + the plan
    PRICES folded from the append-only ``planned`` journal. Protection status is
    then a pure function of this view — no journal line asserts it (kills Bug A).

    ``oco_unsupported`` is empty in Stage 1 (STOP-ONLY: the OCO rung stays dark);
    Stage 2 folds the persisted per-instrument capability flag here."""
    all_positions: dict[int, Position] = {}
    for pos in broker.get_positions():
        uic = _position_uic(pos)
        if uic is not None:
            all_positions[uic] = pos

    long_positions: dict[int, Position] = {}
    get_long = getattr(broker, "get_long_positions", None)
    longs = get_long() if get_long is not None else list(all_positions.values())
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

    return ProtectionView(
        long_positions=long_positions,
        all_positions=all_positions,
        sell_legs_by_uic={uic: tuple(legs) for uic, legs in sell_legs.items()},
        planned_by_uic=_fold_planned_exits(_iter_standalone_stop_journal()),
        oco_unsupported=frozenset(),  # Stage 1 STOP-ONLY — OCO rung dark
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


def _make_protection_executor(
    broker: Broker, throttle: _AlertThrottle
) -> Callable[[Action, bool, TickReport], None]:
    """The protection-pass executor (saxo-oco memo §6). Per Action:

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
    - ``UpgradeToOco`` — Stage 2 only; skipped whenever ``_oco_enabled()`` is
      False (it is, in Stage 1) so the rung-2 arm stays dark."""

    def _execute(action: Action, kill: bool, report: TickReport) -> None:
        if isinstance(action, NoOp):
            return
        if isinstance(action, AlertOnly):
            if throttle.emit(action.reason):
                report.alerts += 1
            return
        if isinstance(action, CancelSellLegs):
            for order_id in action.order_ids:
                _idempotent_cancel(broker, order_id)
                report.cancels += 1
            if throttle.emit(action.reason, uic=action.uic, reason=f"cancel:{action.uic}"):
                report.alerts += 1
            return
        if isinstance(action, PlaceStop):
            _execute_place_stop(broker, throttle, action, report)
            return
        if isinstance(action, UpgradeToOco):
            # Stage 2 rung only; never emitted while _oco_enabled() is False.
            if not _oco_enabled():
                return
            return  # pragma: no cover — Stage 2 wiring lands with place_oco_exit

    return _execute


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
