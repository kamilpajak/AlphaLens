"""The ``now`` tranche: the part of a pick that is placed on this tick.

Step 5 of the ``control_loop`` partition. A pick's entry ladder may declare one
tranche to place immediately. This module holds what that tranche needs
separately from the pullback tiers:

* **Capability and cap** — ``_now_ioc_supported`` and ``_now_submitted_cap``.
* **Idempotency** — ``_now_already_done`` reads the journal so a replayed tick
  does not place twice.
* **Identity and refusals** — ``_now_meta``, ``_refuse_now_tranche`` and
  ``_classify_now_broker_error``.

What moved is CLOSED under every module-level name it uses, so nothing here
names anything left behind in ``control_loop`` and there is no import cycle.
``control_loop`` reaches it through the module prefix
(``now_tranche.<name>``), never by a ``from`` import.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from broker_contract.contract import SupportsPriceTickFloor, _is_price_tolerance_reject

from alphalens_pipeline.brokers.automanager import picks

logger = logging.getLogger(__name__)


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
        SizingStamp,
        append_submission_record,
        build_submission_record,
    )

    try:
        append_submission_record(
            build_submission_record(
                trade_date=intent.meta.trade_date,
                generation=picks._pick_generation(intent),
                ticker=ticker,
                mic=instrument.exchange_mic,
                uic=instrument.broker_instrument_id,
                brackets=[],
                note=note,
                sizing=SizingStamp(
                    sizing_currency=account.currency,
                    instrument_currency=instrument.currency,
                    fx=fx,
                ),
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


def _now_already_done(records: Sequence[Mapping[str, Any]], ticker: str, intent: Any) -> bool:
    """Idempotency / crash re-drive: ANY now record for this arm generation
    means the now half is done (placed, refused, or write-ahead-then-crashed —
    the ``_place_tiers`` write-ahead contract: an attempt marker with no
    bracket is an alertable non-retried attempt, never re-POSTed)."""
    armed_ts = str(intent.meta.armed_ts)
    return any(
        str(record.get("tranche") or "") == "now"
        and str(record.get("ticker") or "").upper() == ticker
        # #1252: the journal date key was renamed brief_date -> trade_date.
        # Legacy records on disk still carry the old key (append-only
        # journals are never rewritten), so fall back to it.
        and str(record.get("trade_date") or record.get("brief_date") or "")
        == str(intent.meta.trade_date)
        and str((record.get("tranche_meta") or {}).get("armed_ts") or "") == armed_ts
        for record in records
    )


def _classify_now_broker_error(
    exc: Any, ticker: str, alert_throttled: Callable[[str, str], bool] | None
) -> str:
    """#1247: classify a now-placement BrokerError (price-tolerance reject vs
    generic failure) + page; the returned string is the failure record's
    ``tranche_meta`` outcome."""
    if _is_price_tolerance_reject(exc):
        if alert_throttled is not None:
            alert_throttled(
                f"now tranche {ticker}: rejected by the venue price-tolerance check "
                f"({exc}) — NOT entered; if the signal stands, disarm the pick and arm a "
                "new document with a fresh cap (a replace keeps this tier done)",
                f"now-reject:{ticker}",
            )
        return "refused_reject"
    if alert_throttled is not None:
        alert_throttled(f"now tranche {ticker}: placement failed ({exc})", f"now-fail:{ticker}")
    return "failed"
