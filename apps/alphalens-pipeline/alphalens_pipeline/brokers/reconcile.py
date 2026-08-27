"""Vendor-agnostic reconciliation core (P3) — journal x broker -> verdicts.

STRICTLY READ-ONLY: no order placement, no cancels, and the submission
journal is never rewritten (append-only SoT; verdicts are pure functions of
the journal + the broker's order/audit views, recomputed at read time).

Capability model — the frozen ``contract.Broker`` Protocol is NOT widened.
Terminal resolution and the fill cross-check are VENDOR CAPABILITIES reached
through the ``@runtime_checkable`` extension Protocols below (the typed
variant of the CLI's existing ``getattr(broker, "precheck_bracket_order",
None)`` precedent). A broker lacking a capability degrades honestly:

- no :class:`SupportsOrderResolution` -> every disappeared order is
  ``UNRESOLVED(capability_absent)`` — never a guessed terminal state (so
  ``FakeBroker`` and the conformance mixin need zero changes);
- no :class:`SupportsFillCrossCheck` -> FILLED verdicts stand un-cross-checked
  with an explanatory note, and no divergence is claimed.

Graduation path: if a second adapter implements resolution, promote
``SupportsOrderResolution`` from here into ``contract.py`` as an optional
companion Protocol in its own PR (P3 decision record, design memo).

Verdict semantics per journal bracket:

- entry id present in the open-orders view -> ``WORKING`` /
  ``PARTIALLY_FILLED``, annotated with a trading-day expiry sweep
  (``paper.calendar.trading_days_elapsed`` on the venue calendar vs the
  bracket's ``ttl``) — an entry still working past its TTL should have
  expired and is a DIVERGENCE;
- entry id absent -> ``resolve_order_outcome`` terminal classification;
  ``OrderStatus.UNKNOWN`` surfaces as ``UNRESOLVED(<reason>)`` with the
  resolver's reason code (``not_in_retention`` / ``fill_fields_unverified``
  / ``inconsistent_state`` / ``unrecognized``);
- ``FILLED`` cross-checks against open-position ``ExternalReference``s and
  closed-position rows joined on the journal's ``client_request_id`` (matched
  against the opening leg's ``OpeningExternalReferenceId``); a closed FIFO
  pair yields the realized r from ``ClosingPrice`` vs the journal's
  entry/stop distance.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from broker_contract.contract import (
    _QTY_EPS,
    Broker,
    BrokerError,
    OrderState,
    OrderStatus,
    Position,
)

from alphalens_pipeline.paper.calendar import trading_days_elapsed

logger = logging.getLogger(__name__)

# Reason codes emitted by THIS module (the resolver-side codes travel in the
# resolver's OrderState.raw_status and pass through verbatim).
REASON_CAPABILITY_ABSENT = "capability_absent"
REASON_AUDIT_ERROR = "audit_error"

# Per-pass cap on audit-log resolution reads (GET /cs/v1/audit/orderactivities),
# the cold-start 429-burst shaper (audit-429 memo §3 + Amendment 1). 6/pass at
# the 45s tick cadence = 8 GETs/min — under BOTH passive brackets of the
# unmeasured /cs audit bucket (July 2026: ~10/min tripped it; August 2026:
# ~60 per rolling ~60s). A transient-only shaper: steady state resolves from
# the terminal memo (SupportsOutcomeCachePeek) and spends ~0 budget per pass.
_MAX_OUTCOME_AUDITS_PER_PASS = 6  # single tuning point; env knob only after the
# header instrument reports the real quota (memo §5 Q2 — no invented numbers)

# Non-alerting marker for a bracket whose audit was NOT ATTEMPTED this pass
# (budget exhausted): a log line + pass counter — never an AlertOnly, never a
# verdict. Distinct from UNRESOLVED(audit_error), which is an ATTEMPTED audit
# that failed (and keeps its alert).
VERDICT_AUDIT_DEFERRED = "AUDIT_DEFERRED"

_WORKING_STATUSES = frozenset({OrderStatus.WORKING, OrderStatus.PARTIALLY_FILLED})
_TERMINAL_STATUSES = frozenset(
    {OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED, OrderStatus.EXPIRED}
)
_UNRESOLVED = "UNRESOLVED"

_ACTIVITY_TIME_RE = re.compile(r"ActivityTime=(\S+)")
_DEFAULT_EXCHANGE_MIC = "XNYS"


@runtime_checkable
class SupportsOrderResolution(Protocol):
    """Extension capability: audit-log terminal resolution (Saxo today)."""

    def resolve_order_outcome(self, order_id: str) -> OrderState: ...


@runtime_checkable
class SupportsFillCrossCheck(Protocol):
    """Extension capability: raw position/closed-position reads for the fill join."""

    def get_open_position_references(self) -> list[str]: ...

    def get_closed_position_rows(self) -> list[dict[str, Any]]: ...


@runtime_checkable
class SupportsPositionNetting(Protocol):
    """Extension capability: netted position reads for the per-uic multi-tier match.

    ``get_positions`` is on the base :class:`~contract.Broker` Protocol, but
    reconcile intentionally requires only ``list_open_orders`` + the optional
    capabilities (see the module docstring), so a broker that cannot net
    positions degrades honestly: no ``owned_by_uic`` map, so a second filled
    tier falls back to the source-tier / closed-pair join and no per-uic match
    is attempted (never a fabricated ``owned``).
    """

    def get_positions(self) -> list[Position]: ...


@runtime_checkable
class SupportsOutcomeCachePeek(Protocol):
    """Extension capability: whether ``resolve_order_outcome`` would answer from
    a terminal memo (no audit HTTP read). Lets the audit budget cap only REAL
    reads — memoized terminals resolve budget-free, so steady state (terminal
    majority cached) is byte-identical to the un-budgeted pass."""

    def has_cached_order_outcome(self, order_id: str) -> bool: ...


class OutcomeAuditBudget:
    """Per-tick cap on audit-log resolution reads, shared by every consumer.

    ONE instance per daemon (built once, mirroring the loop's other lifetime
    state); ``start_tick`` resets it at the top of each tick so the cap is
    per-pass. Both audit consumers — the verdict pass (``reconcile_brackets``)
    and the entry-trail reconcile pass — draw from the same budget, so their
    combined fan-out never exceeds the cap in one tick. Deliberately NOT
    thread-safe: only the single tick thread ever resolves outcomes."""

    def __init__(self, limit: int = _MAX_OUTCOME_AUDITS_PER_PASS) -> None:
        if limit < 1:
            raise ValueError(f"audit budget limit must be >= 1, got {limit}")
        self.limit = limit
        self.spent = 0
        self.deferred = 0

    def start_tick(self) -> None:
        """Reset the per-pass counters (called once at the top of each tick)."""
        self.spent = 0
        self.deferred = 0

    def try_acquire(self) -> bool:
        """Reserve one audit read; False once the pass cap is exhausted."""
        if self.spent >= self.limit:
            return False
        self.spent += 1
        return True

    def note_deferred(self) -> None:
        """Count one bracket whose audit was not attempted this pass."""
        self.deferred += 1


@dataclass(frozen=True)
class ReconcileVerdict:
    """One journal bracket's reconciliation outcome (a fact, not state)."""

    brief_date: str
    ticker: str
    qty: float
    entry_order_id: str
    status: str  # WORKING / PARTIALLY_FILLED / FILLED / CANCELLED / REJECTED / EXPIRED / UNRESOLVED
    verdict: (
        str  # rendered label incl. qualifiers, e.g. WORKING(PAST-TTL!) / FILLED(closed r=+1.00)
    )
    reason: str | None = None  # populated for UNRESOLVED and divergence rows
    activity_time: str | None = None
    note: str | None = None
    divergence: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def unresolved(self) -> bool:
        return self.status == _UNRESOLVED

    def as_dict(self) -> dict[str, Any]:
        """JSON-ready dict (``--json`` scripting surface)."""
        return {
            "brief_date": self.brief_date,
            "ticker": self.ticker,
            "qty": self.qty,
            "entry_order_id": self.entry_order_id,
            "status": self.status,
            "verdict": self.verdict,
            "reason": self.reason,
            "activity_time": self.activity_time,
            "note": self.note,
            "divergence": self.divergence,
            "details": self.details,
        }


def compute_realized_r(
    close_price: float | None,
    entry: float | None,
    stop: float | None,
) -> float | None:
    """Realized r from a closed pair's ClosingPrice vs the journal entry/stop.

    ``r = (close - entry) / (entry - stop)``; ``None`` (never a fabricated
    number) when any input is missing or the risk distance is degenerate.
    """
    if close_price is None or entry is None or stop is None:
        return None
    risk = entry - stop
    if risk <= 0:
        return None
    return (float(close_price) - float(entry)) / risk


def filled_sum_matches_owned(
    filled_amounts: Iterable[float],
    owned: float,
    *,
    eps: float = _QTY_EPS,
) -> bool:
    """Σ FilledAmount == netted owned, within the qty tolerance (saxo-oco §8).

    The correlation validator behind the per-uic multi-tier match: a netted
    long collapses N filled tiers into one ``owned``; the audit ``FilledAmount``
    of those tiers must sum back to it. Uses :data:`_QTY_EPS` (never a bare
    ``==`` on floats) so sub-share wire noise (``45.9999999`` vs ``46.0``) still
    reconciles.
    """
    return abs(sum(filled_amounts) - owned) <= eps


def summarize(verdicts: Iterable[ReconcileVerdict]) -> dict[str, int]:
    """Counts for the CLI summary line."""
    summary = {"total": 0, "working": 0, "terminal": 0, "unresolved": 0, "divergent": 0}
    terminal_tokens = {status.value for status in _TERMINAL_STATUSES}
    working_tokens = {status.value for status in _WORKING_STATUSES}
    for verdict in verdicts:
        summary["total"] += 1
        if verdict.status in working_tokens:
            summary["working"] += 1
        elif verdict.status in terminal_tokens:
            summary["terminal"] += 1
        elif verdict.unresolved:
            summary["unresolved"] += 1
        if verdict.divergence:
            summary["divergent"] += 1
    return summary


def has_failures(verdicts: Iterable[ReconcileVerdict]) -> bool:
    """True when any verdict is UNRESOLVED or divergent (CLI exit-1 signal)."""
    return any(v.unresolved or v.divergence for v in verdicts)


def reconcile_brackets(
    records: Iterable[Mapping[str, Any]],
    broker: Broker,
    *,
    today: dt.date | None = None,
    audit_budget: OutcomeAuditBudget | None = None,
) -> list[ReconcileVerdict]:
    """Reconcile every journal bracket against the broker's current views.

    One ``list_open_orders`` call up front; the optional capabilities are
    each fetched once; disappeared orders then resolve one by one (the
    broker's client throttles the per-order audit reads).

    With an ``audit_budget`` (the daemon's per-tick cap — audit-429 memo §3 +
    Amendment 1), disappeared brackets are audited MOST-RECENT journal
    activity first; brackets over the budget are DEFERRED — no verdict, no
    alert, a log line + budget counter — and retried next pass in the same
    recency order. Memoized terminals (``SupportsOutcomeCachePeek``) resolve
    budget-free, so steady state is unchanged. ``None`` (the CLI one-off
    path) keeps today's full fan-out.

    Contract on ``records``: each entry MUST carry a ``"ts"`` field (the ISO
    submission timestamp ``build_submission_record`` always stamps) for the
    recency-first ordering to be meaningful; a missing/unparseable ``ts``
    sorts OLDEST (fail-safe: it never claims budget over known-recency rows).
    """
    asof = today or dt.datetime.now(dt.UTC).date()
    open_states = {state.order_id: state for state in broker.list_open_orders()}
    resolver = broker if isinstance(broker, SupportsOrderResolution) else None
    cross_check = _build_cross_check(broker)

    slots: list[ReconcileVerdict | _PendingAudit] = []
    for record in records:
        for bracket in record.get("brackets") or []:
            slots.append(
                _triage_one(
                    record,
                    bracket,
                    open_states=open_states,
                    resolver=resolver,
                    today=asof,
                )
            )
    audited = _resolve_pending_audits(
        [slot for slot in slots if isinstance(slot, _PendingAudit)],
        broker=broker,
        cross_check=cross_check,
        asof=asof,
        audit_budget=audit_budget,
    )
    return _merge_verdicts(slots, audited)


def _build_cross_check(broker: Broker) -> _CrossCheckData | None:
    """Snapshot the fill cross-check inputs when the broker supports them.

    Netted owned qty per uic — the per-uic multi-tier match source (§8),
    gated on its own capability so a broker that cannot net positions
    degrades to the source-tier / closed-pair join (empty map)."""
    if not isinstance(broker, SupportsFillCrossCheck):
        return None
    owned_by_uic = (
        _owned_by_uic(broker.get_positions()) if isinstance(broker, SupportsPositionNetting) else {}
    )
    return _CrossCheckData(
        open_references=set(broker.get_open_position_references()),
        closed_rows=[_flatten_closed_row(row) for row in broker.get_closed_position_rows()],
        owned_by_uic=owned_by_uic,
    )


def _merge_verdicts(
    slots: list[ReconcileVerdict | _PendingAudit],
    audited: dict[int, ReconcileVerdict],
) -> list[ReconcileVerdict]:
    """Journal order is preserved for every EMITTED verdict; a deferred bracket
    simply has no row this pass (never a fabricated verdict)."""
    verdicts: list[ReconcileVerdict] = []
    for slot in slots:
        if isinstance(slot, _PendingAudit):
            verdict = audited.get(id(slot))
            if verdict is not None:
                verdicts.append(verdict)
        else:
            verdicts.append(slot)
    return verdicts


# --------------------------------------------------------------------------
# internals
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _CrossCheckData:
    open_references: set[str]
    closed_rows: list[dict[str, Any]]
    owned_by_uic: dict[str, float] = field(default_factory=dict)


def _uic_key(uic: Any) -> str:
    """Normalise a uic (int on the wire, str in the journal) to a stable key."""
    return "" if uic is None else str(uic)


def _owned_by_uic(positions: Iterable[Position]) -> dict[str, float]:
    """Net signed position quantities per uic (``broker_instrument_id == str(Uic)``)."""
    owned: dict[str, float] = {}
    for pos in positions:
        key = _uic_key(pos.instrument.broker_instrument_id)
        owned[key] = owned.get(key, 0.0) + pos.quantity
    return owned


def _flatten_closed_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """Accept BOTH closed-position row shapes (inner envelope vs flat)."""
    inner = row.get("ClosedPosition")
    return dict(inner) if isinstance(inner, Mapping) else dict(row)


def _effective_settlement_rate(closed_row: Mapping[str, Any]) -> float | None:
    """``ProfitLossOnTrade / ProfitLossOnTradeInBaseCurrency`` or ``None``.

    ``None`` (never a fabricated number) when either PnL field is missing,
    non-numeric (booleans are EXCLUDED — the ``ConversionRateInstrumentTo-
    BaseSettled*`` gotcha class), or the base-currency PnL is zero.
    """
    pnl_trade = closed_row.get("ProfitLossOnTrade")
    pnl_base = closed_row.get("ProfitLossOnTradeInBaseCurrency")
    if isinstance(pnl_trade, bool) or isinstance(pnl_base, bool):
        return None
    try:
        # float() coercion, not isinstance: numpy/pandas scalars are not
        # subclasses of int/float and would silently disable the diagnostic
        # (review finding, PR #849).
        pnl_trade_f = float(pnl_trade)  # type: ignore[arg-type]
        pnl_base_f = float(pnl_base)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if pnl_base_f == 0:
        return None
    return pnl_trade_f / pnl_base_f


def _submission_date(record: Mapping[str, Any]) -> dt.date | None:
    ts = record.get("ts")
    if not ts:
        return None
    try:
        return dt.datetime.fromisoformat(str(ts)).date()
    except ValueError:
        return None


def _extract_activity_time(raw_status: str) -> str | None:
    """Display-only extraction of the ActivityTime diagnostics token."""
    match = _ACTIVITY_TIME_RE.search(raw_status)
    return match.group(1) if match else None


def _base_verdict_fields(
    record: Mapping[str, Any], bracket: Mapping[str, Any]
) -> tuple[str, str, float, str]:
    qty_raw = bracket.get("qty")
    try:
        qty = float(qty_raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        qty = 0.0
    return (
        str(record.get("brief_date", "")),
        str(record.get("ticker", "")),
        qty,
        str(bracket.get("entry_order_id") or ""),
    )


@dataclass
class _PendingAudit:
    """A disappeared bracket awaiting its audit-log resolution (one per
    bracket; resolved most-recent ``ts_key`` first under a budget)."""

    record: Mapping[str, Any]
    bracket: Mapping[str, Any]
    brief: tuple[str, str, float, str]
    details: dict[str, Any]
    resolver: SupportsOrderResolution
    ts_key: dt.datetime

    @property
    def entry_order_id(self) -> str:
        return self.brief[3]


_OLDEST_TS_KEY = dt.datetime.min.replace(tzinfo=dt.UTC)


def _record_ts_key(record: Mapping[str, Any]) -> dt.datetime:
    """The record's journal timestamp as a recency sort key (UTC-aware).

    A missing / unparseable ``ts`` sorts OLDEST — it is audited last, matching
    fail-safe intent: an anomalous row (ts should always exist —
    build_submission_record stamps it) never claims budget over brackets with
    known recency (Amendment 1)."""
    ts = record.get("ts")
    if not ts:
        return _OLDEST_TS_KEY
    try:
        parsed = dt.datetime.fromisoformat(str(ts))
    except ValueError:
        return _OLDEST_TS_KEY
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=dt.UTC)


def _resolve_pending_audits(
    pending: list[_PendingAudit],
    *,
    broker: Broker,
    cross_check: _CrossCheckData | None,
    asof: dt.date,
    audit_budget: OutcomeAuditBudget | None,
) -> dict[int, ReconcileVerdict]:
    """Resolve disappeared brackets, capped per pass by the audit budget.

    Returns ``id(pending_item) -> verdict`` for every AUDITED bracket; a
    deferred bracket is simply absent (its non-alerting marker is the
    ``VERDICT_AUDIT_DEFERRED`` log line + the budget's ``deferred`` counter).
    """
    results: dict[int, ReconcileVerdict] = {}
    if not pending:
        return results
    if audit_budget is None:
        for item in pending:
            results[id(item)] = _audit_one(item, cross_check=cross_check, asof=asof)
        return results
    peek = broker if isinstance(broker, SupportsOutcomeCachePeek) else None
    needs_budget: list[_PendingAudit] = []
    for item in pending:
        if peek is not None and peek.has_cached_order_outcome(item.entry_order_id):
            # Memoized terminal — no audit HTTP read, so no budget draw.
            results[id(item)] = _audit_one(item, cross_check=cross_check, asof=asof)
        else:
            needs_budget.append(item)
    # Most-recent journal activity first (Amendment 1 / §5 Q1): a genuine
    # divergence on a recent bracket is detected in the first passes; stable
    # sort keeps journal order among equal timestamps.
    needs_budget.sort(key=lambda item: item.ts_key, reverse=True)
    deferred: list[_PendingAudit] = []
    for item in needs_budget:
        if audit_budget.try_acquire():
            results[id(item)] = _audit_one(item, cross_check=cross_check, asof=asof)
        else:
            audit_budget.note_deferred()
            deferred.append(item)
    if deferred:
        logger.info(
            "%s: audit budget exhausted (%d/%d this pass) — deferred %d of %d "
            "disappeared brackets to the next pass (no verdict, no alert): %s",
            VERDICT_AUDIT_DEFERRED,
            audit_budget.spent,
            audit_budget.limit,
            len(deferred),
            len(pending),
            ", ".join(item.entry_order_id for item in deferred),
        )
    return results


def _audit_one(
    item: _PendingAudit,
    *,
    cross_check: _CrossCheckData | None,
    asof: dt.date,
) -> ReconcileVerdict:
    """One bracket's audit-log resolution -> verdict (the pre-cap inline body)."""
    brief_date, ticker, qty, entry_order_id = item.brief
    try:
        state = item.resolver.resolve_order_outcome(entry_order_id)
    except BrokerError as exc:
        # Transient by contract — the audit store is durable; retry next run.
        return ReconcileVerdict(
            brief_date=brief_date,
            ticker=ticker,
            qty=qty,
            entry_order_id=entry_order_id,
            status=_UNRESOLVED,
            verdict=f"{_UNRESOLVED}({REASON_AUDIT_ERROR})",
            reason=f"{REASON_AUDIT_ERROR}: {exc}",
            details=item.details,
        )
    return _reconcile_resolved(
        item.bracket,
        state,
        brief=item.brief,
        details=item.details,
        cross_check=cross_check,
        submission_date=_submission_date(item.record),
        asof=asof,
    )


def _triage_one(
    record: Mapping[str, Any],
    bracket: Mapping[str, Any],
    *,
    open_states: Mapping[str, OrderState],
    resolver: SupportsOrderResolution | None,
    today: dt.date,
) -> ReconcileVerdict | _PendingAudit:
    brief_date, ticker, qty, entry_order_id = _base_verdict_fields(record, bracket)
    details: dict[str, Any] = {
        "client_request_id": bracket.get("client_request_id"),
        "mic": record.get("mic"),
        "execution_config_version": record.get("execution_config_version"),
    }
    # Per-uic netting key (§8) — the unit Saxo nets to and the multi-tier match
    # correlates on. Additive; absent-on-record leaves it out of the verdict.
    if record.get("uic") is not None:
        details["uic"] = record.get("uic")
    # Journal schema-2 FX provenance (absent on schema-1 lines = the
    # same-currency no-op era; forward-compat, never back-migrated). The
    # instrument currency labels PnL amounts; the sizing rate sits next to
    # the reconstructed effective settlement rate for the FX cross-check.
    if record.get("instrument_currency") is not None:
        details["instrument_currency"] = record.get("instrument_currency")
    if record.get("fx_rate") is not None:
        details["sizing_fx_rate"] = record.get("fx_rate")

    open_state = open_states.get(entry_order_id) if entry_order_id else None
    if open_state is not None:
        return _reconcile_open(
            record,
            bracket,
            open_state,
            brief=(brief_date, ticker, qty, entry_order_id),
            details=details,
            today=today,
        )

    if resolver is None:
        return ReconcileVerdict(
            brief_date=brief_date,
            ticker=ticker,
            qty=qty,
            entry_order_id=entry_order_id,
            status=_UNRESOLVED,
            verdict=f"{_UNRESOLVED}({REASON_CAPABILITY_ABSENT})",
            reason=(
                "this broker exposes no order-outcome resolution capability "
                "(SupportsOrderResolution); terminal state cannot be determined"
            ),
            details=details,
        )

    return _PendingAudit(
        record=record,
        bracket=bracket,
        brief=(brief_date, ticker, qty, entry_order_id),
        details=details,
        resolver=resolver,
        ts_key=_record_ts_key(record),
    )


def _reconcile_open(
    record: Mapping[str, Any],
    bracket: Mapping[str, Any],
    state: OrderState,
    *,
    brief: tuple[str, str, float, str],
    details: dict[str, Any],
    today: dt.date,
) -> ReconcileVerdict:
    brief_date, ticker, qty, entry_order_id = brief
    base = state.status.value if state.status in _WORKING_STATUSES else OrderStatus.WORKING.value
    details["raw_status"] = state.raw_status
    if state.filled_quantity:
        details["filled_quantity"] = state.filled_quantity

    ttl = bracket.get("ttl")
    submitted = _submission_date(record)
    divergence = False
    reason: str | None = None
    verdict_label = base
    if ttl is not None and submitted is not None:
        exchange = str(record.get("mic") or _DEFAULT_EXCHANGE_MIC)
        elapsed = trading_days_elapsed(submitted, today, exchange=exchange)
        details["trading_days_elapsed"] = elapsed
        details["ttl"] = ttl
        if elapsed > int(ttl):
            divergence = True
            verdict_label = f"{base}(PAST-TTL!)"
            reason = (
                f"entry still working after {elapsed} trading days on {exchange} "
                f"vs ttl {ttl} — it should have expired"
            )
    return ReconcileVerdict(
        brief_date=brief_date,
        ticker=ticker,
        qty=qty,
        entry_order_id=entry_order_id,
        status=base,
        verdict=verdict_label,
        reason=reason,
        divergence=divergence,
        details=details,
    )


def _short_reason(reason: str) -> str:
    """First token of a resolver reason for the compact verdict label."""
    return reason.split(None, 1)[0].rstrip(":(") if reason else "unknown"


def _reconcile_resolved(
    bracket: Mapping[str, Any],
    state: OrderState,
    *,
    brief: tuple[str, str, float, str],
    details: dict[str, Any],
    cross_check: _CrossCheckData | None,
    submission_date: dt.date | None = None,
    asof: dt.date | None = None,
) -> ReconcileVerdict:
    brief_date, ticker, qty, entry_order_id = brief
    details["raw_status"] = state.raw_status
    activity_time = _extract_activity_time(state.raw_status)

    if state.status is OrderStatus.FILLED:
        return _reconcile_filled(
            bracket,
            state,
            brief=brief,
            details=details,
            cross_check=cross_check,
            activity_time=activity_time,
            submission_date=submission_date,
            asof=asof,
        )
    if state.status in (OrderStatus.CANCELLED, OrderStatus.REJECTED, OrderStatus.EXPIRED):
        note = None
        if state.status is OrderStatus.CANCELLED and bracket.get("exit_order_ids"):
            note = "children cancelled via cascade"
        return ReconcileVerdict(
            brief_date=brief_date,
            ticker=ticker,
            qty=qty,
            entry_order_id=entry_order_id,
            status=state.status.value,
            verdict=state.status.value,
            activity_time=activity_time,
            note=note,
            details=details,
        )
    if state.status is OrderStatus.UNKNOWN:
        reason = state.raw_status or "unknown"
        return ReconcileVerdict(
            brief_date=brief_date,
            ticker=ticker,
            qty=qty,
            entry_order_id=entry_order_id,
            status=_UNRESOLVED,
            verdict=f"{_UNRESOLVED}({_short_reason(reason)})",
            reason=reason,
            activity_time=activity_time,
            details=details,
        )
    # A resolver answering WORKING/PARTIALLY_FILLED for an order ABSENT from
    # the open-orders view is itself an inconsistency — surface, never guess.
    reason = f"resolver returned {state.status.value} for an order absent from the open-orders view"
    return ReconcileVerdict(
        brief_date=brief_date,
        ticker=ticker,
        qty=qty,
        entry_order_id=entry_order_id,
        status=_UNRESOLVED,
        verdict=f"{_UNRESOLVED}(inconsistent_state)",
        reason=reason,
        activity_time=activity_time,
        details=details,
    )


def _reconcile_closed_pair(
    closed_match: Mapping[str, Any],
    bracket: Mapping[str, Any],
    *,
    brief: tuple[str, str, float, str],
    details: dict[str, Any],
    activity_time: str | None,
) -> ReconcileVerdict:
    """The FILLED verdict for a closed FIFO round-trip pair — realized R plus the
    optional P/L and effective settlement rate folded into ``details``."""
    brief_date, ticker, qty, entry_order_id = brief
    realized_r = compute_realized_r(
        closed_match.get("ClosingPrice"), bracket.get("entry"), bracket.get("stop")
    )
    details["realized_r"] = realized_r
    if closed_match.get("ProfitLossOnTrade") is not None:
        details["profit_loss_on_trade"] = closed_match.get("ProfitLossOnTrade")
    effective_rate = _effective_settlement_rate(closed_match)
    if effective_rate is not None:
        # The ONLY empirical FX-slippage signal: ClosedPosition does NOT expose the
        # settlement rate (the ConversionRateInstrumentToBaseSettled* fields are
        # BOOLEANS — never read them as numbers), so the effective conversion is
        # reconstructed as ProfitLossOnTrade / ProfitLossOnTradeInBaseCurrency and
        # recorded next to the journaled sizing_fx_rate for the cross-check.
        details["effective_settlement_rate"] = effective_rate
    label = f"FILLED(closed r={realized_r:+.2f})" if realized_r is not None else "FILLED(closed)"
    return ReconcileVerdict(
        brief_date=brief_date,
        ticker=ticker,
        qty=qty,
        entry_order_id=entry_order_id,
        status=OrderStatus.FILLED.value,
        verdict=label,
        activity_time=activity_time,
        note="round trip closed (FIFO pair)",
        details=details,
    )


def _reconcile_filled(
    bracket: Mapping[str, Any],
    state: OrderState,
    *,
    brief: tuple[str, str, float, str],
    details: dict[str, Any],
    cross_check: _CrossCheckData | None,
    activity_time: str | None,
    submission_date: dt.date | None = None,
    asof: dt.date | None = None,
) -> ReconcileVerdict:
    brief_date, ticker, qty, entry_order_id = brief
    if state.filled_quantity:
        details["filled_quantity"] = state.filled_quantity
    if cross_check is None:
        return ReconcileVerdict(
            brief_date=brief_date,
            ticker=ticker,
            qty=qty,
            entry_order_id=entry_order_id,
            status=OrderStatus.FILLED.value,
            verdict=OrderStatus.FILLED.value,
            activity_time=activity_time,
            note="fill cross-check unavailable for this broker",
            details=details,
        )

    request_id = str(bracket.get("client_request_id") or "")
    # The opening leg of a closed FIFO pair carries the journaled ENTRY order's
    # client_request_id as ``OpeningExternalReferenceId`` (the closing leg is
    # ``ClosingExternalReferenceId``). There is NO ``ExternalReference`` on a
    # closedposition row (that field lives on OPEN positions / audit rows).
    closed_match = next(
        (
            row
            for row in cross_check.closed_rows
            if request_id and str(row.get("OpeningExternalReferenceId") or "") == request_id
        ),
        None,
    )
    if closed_match is not None:
        return _reconcile_closed_pair(
            closed_match,
            bracket,
            brief=brief,
            details=details,
            activity_time=activity_time,
        )
    if request_id and request_id in cross_check.open_references:
        return ReconcileVerdict(
            brief_date=brief_date,
            ticker=ticker,
            qty=qty,
            entry_order_id=entry_order_id,
            status=OrderStatus.FILLED.value,
            verdict=OrderStatus.FILLED.value,
            activity_time=activity_time,
            note="position open, exit orders working",
            details=details,
        )
    # Per-uic multi-tier match (§8, fixes C-S6): ``get_open_position_references``
    # returns ONE reference per netted row (the source/oldest tier crid), so a
    # SECOND filled tier on the same uic is absent from ``open_references`` and
    # from any closed pair. It is NOT a divergence — it is the same netted long,
    # reached through a different tier. Match it by uic: "position open" iff the
    # uic has ``owned > 0`` (a live netted position) AND this tier's own audit
    # ``FilledAmount > 0``. Sizing to netted owned is thus structural, and the
    # per-tick AlertOnly storm (and the FIFO-flip un-protect) are gone.
    uic_key = _uic_key(details.get("uic"))
    owned = cross_check.owned_by_uic.get(uic_key, 0.0) if uic_key else 0.0
    filled_amount = state.filled_quantity or 0.0
    if owned > _QTY_EPS and filled_amount > _QTY_EPS:
        details["netted_owned"] = owned
        return ReconcileVerdict(
            brief_date=brief_date,
            ticker=ticker,
            qty=qty,
            entry_order_id=entry_order_id,
            status=OrderStatus.FILLED.value,
            verdict=OrderStatus.FILLED.value,
            activity_time=activity_time,
            note="position open (netted tier), matched by uic",
            details=details,
        )
    # Presumed-closed (round-trip aged out of the broker window): the uic is
    # FLAT (no live position rescued it above), no open reference and no
    # closed-position row matched — but the SUBMISSION predates today, and
    # Saxo SIM's closedpositions view only spans a small trailing window (a
    # few sessions), so an entry that filled and round-tripped days ago can
    # legitimately have no closed row left to join against. A FRESH (same-day)
    # or unknown-age submission stays on the divergence path below — same-day
    # flat-and-unmatched could be a real anomaly or a broker position-
    # appearance lag, and must stay loud. Gated on the SUBMISSION date (robust,
    # from the journal), never on ``activity_time`` (display-only regex token).
    # The ``owned <= _QTY_EPS`` guard re-asserts flatness LOCALLY: the earlier
    # netted-tier arm needs BOTH owned>0 AND filled_amount>0, so a broker that
    # reports FILLED with filled_quantity==0 on a still-open uic would slip past
    # it — this arm must never presume-close a live position on its own.
    # ``uic_key`` must be truthy too: without a recorded uic, ``_uic_key`` yields
    # "" and ``owned`` is forced to 0.0 VACUOUSLY (no position lookup happened),
    # so an uncorrelatable FILLED entry must stay a (loud) divergence, never be
    # silently presumed-closed (zen review).
    if (
        uic_key
        and owned <= _QTY_EPS
        and submission_date is not None
        and asof is not None
        and submission_date < asof
    ):
        details["submission_date"] = submission_date.isoformat()
        return ReconcileVerdict(
            brief_date=brief_date,
            ticker=ticker,
            qty=qty,
            entry_order_id=entry_order_id,
            status=OrderStatus.FILLED.value,
            verdict="FILLED(closed, record unavailable)",
            reason=(
                "audit log says FILLED, uic is flat, and no live position or closed "
                f"pair matched client_request_id {request_id!r} — the broker's closed-"
                "position window is short-lived and the submission predates it; "
                "presumed round-tripped"
            ),
            note="presumed round trip (closed record aged out of broker window)",
            activity_time=activity_time,
            divergence=False,
            details=details,
        )
    return ReconcileVerdict(
        brief_date=brief_date,
        ticker=ticker,
        qty=qty,
        entry_order_id=entry_order_id,
        status=OrderStatus.FILLED.value,
        verdict=OrderStatus.FILLED.value,
        reason=(
            "audit log says FILLED but no open position or closed pair matched "
            f"client_request_id {request_id!r}"
        ),
        activity_time=activity_time,
        divergence=True,
        details=details,
    )


__all__ = [
    "REASON_AUDIT_ERROR",
    "REASON_CAPABILITY_ABSENT",
    "VERDICT_AUDIT_DEFERRED",
    "OutcomeAuditBudget",
    "ReconcileVerdict",
    "SupportsFillCrossCheck",
    "SupportsOrderResolution",
    "SupportsOutcomeCachePeek",
    "SupportsPositionNetting",
    "compute_realized_r",
    "filled_sum_matches_owned",
    "has_failures",
    "reconcile_brackets",
    "summarize",
]
