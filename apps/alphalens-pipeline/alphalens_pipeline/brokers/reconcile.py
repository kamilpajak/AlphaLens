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
  closed-position rows joined on the journal's ``client_request_id``; a
  closed FIFO pair yields the realized r from ``ClosePrice`` vs the
  journal's entry/stop distance.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from alphalens_pipeline.brokers.contract import (
    Broker,
    BrokerError,
    OrderState,
    OrderStatus,
)
from alphalens_pipeline.paper.calendar import trading_days_elapsed

# Reason codes emitted by THIS module (the resolver-side codes travel in the
# resolver's OrderState.raw_status and pass through verbatim).
REASON_CAPABILITY_ABSENT = "capability_absent"
REASON_AUDIT_ERROR = "audit_error"

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
    """Realized r from a closed pair's ClosePrice vs the journal entry/stop.

    ``r = (close - entry) / (entry - stop)``; ``None`` (never a fabricated
    number) when any input is missing or the risk distance is degenerate.
    """
    if close_price is None or entry is None or stop is None:
        return None
    risk = entry - stop
    if risk <= 0:
        return None
    return (float(close_price) - float(entry)) / risk


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
) -> list[ReconcileVerdict]:
    """Reconcile every journal bracket against the broker's current views.

    One ``list_open_orders`` call up front; the optional capabilities are
    each fetched once; disappeared orders then resolve one by one (the
    broker's client throttles the per-order audit reads).
    """
    asof = today or dt.datetime.now(dt.UTC).date()
    open_states = {state.order_id: state for state in broker.list_open_orders()}
    resolver = broker if isinstance(broker, SupportsOrderResolution) else None
    cross_check: _CrossCheckData | None = None
    if isinstance(broker, SupportsFillCrossCheck):
        cross_check = _CrossCheckData(
            open_references=set(broker.get_open_position_references()),
            closed_rows=[_flatten_closed_row(row) for row in broker.get_closed_position_rows()],
        )

    verdicts: list[ReconcileVerdict] = []
    for record in records:
        for bracket in record.get("brackets") or []:
            verdicts.append(
                _reconcile_one(
                    record,
                    bracket,
                    open_states=open_states,
                    resolver=resolver,
                    cross_check=cross_check,
                    today=asof,
                )
            )
    return verdicts


# --------------------------------------------------------------------------
# internals
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _CrossCheckData:
    open_references: set[str]
    closed_rows: list[dict[str, Any]]


def _flatten_closed_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """Accept BOTH closed-position row shapes (inner envelope vs flat)."""
    inner = row.get("ClosedPosition")
    return dict(inner) if isinstance(inner, Mapping) else dict(row)


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


def _reconcile_one(
    record: Mapping[str, Any],
    bracket: Mapping[str, Any],
    *,
    open_states: Mapping[str, OrderState],
    resolver: SupportsOrderResolution | None,
    cross_check: _CrossCheckData | None,
    today: dt.date,
) -> ReconcileVerdict:
    brief_date, ticker, qty, entry_order_id = _base_verdict_fields(record, bracket)
    details: dict[str, Any] = {
        "client_request_id": bracket.get("client_request_id"),
        "mic": record.get("mic"),
        "execution_config_version": record.get("execution_config_version"),
    }

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

    try:
        state = resolver.resolve_order_outcome(entry_order_id)
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
            details=details,
        )
    return _reconcile_resolved(
        bracket,
        state,
        brief=(brief_date, ticker, qty, entry_order_id),
        details=details,
        cross_check=cross_check,
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


def _reconcile_filled(
    bracket: Mapping[str, Any],
    state: OrderState,
    *,
    brief: tuple[str, str, float, str],
    details: dict[str, Any],
    cross_check: _CrossCheckData | None,
    activity_time: str | None,
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
    closed_match = next(
        (
            row
            for row in cross_check.closed_rows
            if request_id and str(row.get("ExternalReference") or "") == request_id
        ),
        None,
    )
    if closed_match is not None:
        realized_r = compute_realized_r(
            closed_match.get("ClosePrice"), bracket.get("entry"), bracket.get("stop")
        )
        details["realized_r"] = realized_r
        if closed_match.get("ProfitLossOnTrade") is not None:
            details["profit_loss_on_trade"] = closed_match.get("ProfitLossOnTrade")
        label = (
            f"FILLED(closed r={realized_r:+.2f})" if realized_r is not None else "FILLED(closed)"
        )
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
    "ReconcileVerdict",
    "SupportsFillCrossCheck",
    "SupportsOrderResolution",
    "compute_realized_r",
    "has_failures",
    "reconcile_brackets",
    "summarize",
]
