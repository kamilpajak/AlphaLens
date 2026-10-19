"""The cash book's wire form: one render per decode (#1689, memo section 3).

Four row types cross this seam. Three are declared beside their own vendor
mapping in :mod:`alphalens_pipeline.brokers.account_cash` -- ``CashEvent``,
``TradeRow``, ``BalanceSnapshot`` -- and :class:`SweepRecord`, the one row type
the cash book itself invents, is declared HERE rather than in ``store.py``:
every row type then sits beside its wire form, and the package DAG stays
``paths -> codec -> store -> sweep`` with no cycle (``store`` renders a sweep
record, so a ``store``-side declaration would make the two import each other).

Symmetric by design, following ``broker_contract/trade_intent/codec.py``: a
render for every decode, pinned by a round trip over real rows of all three
fixture key shapes. Every mapping renders as a plain ``dict`` and every
sequence as a ``list``, because ``dataclasses.asdict`` recurses a tuple INTO a
tuple (#1405) and JSON has no tuple to read it back as.

**Non-finite numbers are refused, in BOTH directions, and this is the only
thing between a NaN and the money journal.** ``brokers/journal.py`` serialises
with ``json.dumps(record, sort_keys=True, default=default)`` and no
``allow_nan=False``, deliberately outside its own write guard, so a rendered
``float("nan")`` reaches the file as the bare token ``NaN`` -- which is not
JSON. The row is then permanently unreadable: the fold counts it malformed and
the booking vanishes from every later read while the published row count still
counts it. Adding ``allow_nan=False`` to the shared appender was rejected: that
file is shared broker code and its docstring states on purpose that a
serialisation failure is a programming error in the caller's payload, not an
I/O condition.

**The decode is strict about a MISSING key and tolerant of an extra one.** A
missing key is how a rename arrives, and defaulting it would silently answer
``None`` for a money field; an extra key is how a NEWER writer's line arrives
at an older reader, and refusing that would turn every line of an additive
version into a malformed one.
"""

from __future__ import annotations

import datetime as dt
import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass, fields
from typing import Any

from alphalens_pipeline.brokers.account_cash import BalanceSnapshot, CashEvent, TradeRow
from alphalens_pipeline.brokers.fill_history import ReadWindow

logger = logging.getLogger(__name__)

__all__ = [
    "CashBookDecodeError",
    "SweepRecord",
    "balance_from_jsonable",
    "balance_to_jsonable",
    "cash_event_from_jsonable",
    "cash_event_to_jsonable",
    "sweep_from_jsonable",
    "sweep_to_jsonable",
    "trade_row_from_jsonable",
    "trade_row_to_jsonable",
]


class CashBookDecodeError(ValueError):
    """A row cannot cross the wire in one direction or the other.

    One class for both directions on purpose: the render and the decode are
    one claim -- that this row and its line say the same thing -- and a caller
    has the same thing to do about either failure, which is to refuse the row
    rather than store half of it.
    """


@dataclass(frozen=True)
class SweepRecord:
    """One sweep: what was asked for, what came back, what it changed.

    ``sweep_id`` is the dedupe key of BOTH ``sweeps.jsonl`` and
    ``balances.jsonl``, so a crash between the two appends leaves a window a
    re-sweep closes with one record per sweep rather than two.
    """

    sweep_id: str
    swept_at: dt.datetime
    env: str
    since: str
    until: str
    events_window: ReadWindow | None
    trades_window: ReadWindow | None
    appended: Mapping[str, int]
    already_present: Mapping[str, int]
    restated: Mapping[str, int]
    malformed: Mapping[str, int]
    refused_reason: str | None


# --- Field-level coercion, one definition per kind ----------------------------


def _require_object(data: Any, *, row: str) -> Mapping[str, Any]:
    if not isinstance(data, Mapping):
        raise CashBookDecodeError(f"a {row} line is not a JSON object: {type(data).__name__}")
    return data


def _take(data: Mapping[str, Any], key: str, *, row: str) -> Any:
    """``data[key]``, refusing an ABSENT key rather than defaulting it."""
    if key not in data:
        raise CashBookDecodeError(
            f"a {row} line carries no {key!r}; defaulting it would answer None for a "
            f"field the journal states, and a rename arrives in exactly this shape"
        )
    return data[key]


def _finite(value: Any, *, key: str, row: str) -> float | None:
    """A money number, or ``None``. Refuses a bool, a non-number, a non-finite.

    ``isinstance(True, int)`` is ``True`` in Python, so the bool case has to be
    named or ``True`` reaches a money field as ``1.0``.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise CashBookDecodeError(
            f"a {row} line carries {key}={value!r} ({type(value).__name__}), which is not "
            f"a number; a money field must not be coerced"
        )
    number = float(value)
    if not math.isfinite(number):
        raise CashBookDecodeError(
            f"a {row} line carries a non-finite {key} ({value!r}); json.dumps writes it as "
            f"the bare token NaN/Infinity, which is not JSON, and the row would be "
            f"permanently unreadable while the published row count still counted it"
        )
    return number


def _opt_text(value: Any, *, key: str, row: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise CashBookDecodeError(
            f"a {row} line carries {key}={value!r} ({type(value).__name__}), expected a string"
        )
    return value


def _text(value: Any, *, key: str, row: str) -> str:
    text = _opt_text(value, key=key, row=row)
    if text is None:
        raise CashBookDecodeError(f"a {row} line carries no {key} (null), and it is the row's key")
    return text


def _opt_whole(value: Any, *, key: str, row: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise CashBookDecodeError(
            f"a {row} line carries {key}={value!r} ({type(value).__name__}), expected an integer"
        )
    return value


def _stamp_to_text(value: dt.datetime, *, key: str, row: str) -> str:
    """An ISO-8601 string of an AWARE instant. A naive one is refused.

    A naive stamp read back naive makes two sweeps' balance stamps
    incomparable -- Python raises on the comparison -- and the vendor path
    never produces one (``fill_history.parse_utc`` always stamps a timezone),
    so a naive value can only arrive from code.
    """
    if value.tzinfo is None:
        raise CashBookDecodeError(
            f"a {row} {key} is naive ({value!r}); a stored stamp with no offset cannot be "
            f"compared with the next sweep's"
        )
    return value.astimezone(dt.UTC).isoformat()


def _text_to_stamp(value: Any, *, key: str, row: str) -> dt.datetime:
    text = _text(value, key=key, row=row)
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError as exc:
        raise CashBookDecodeError(f"a {row} line carries an unreadable {key}: {text!r}") from exc
    if parsed.tzinfo is None:
        raise CashBookDecodeError(f"a {row} line carries a naive {key}: {text!r}")
    return parsed.astimezone(dt.UTC)


def _window_to_jsonable(window: ReadWindow | None) -> dict[str, Any] | None:
    if window is None:
        return None
    return {"start": window.start, "end": window.end, "rows": window.rows}


def _window_from_jsonable(value: Any, *, key: str, row: str) -> ReadWindow | None:
    if value is None:
        return None
    data = _require_object(value, row=f"{row} {key}")
    return ReadWindow(
        start=_text(_take(data, "start", row=f"{row} {key}"), key="start", row=row),
        end=_text(_take(data, "end", row=f"{row} {key}"), key="end", row=row),
        rows=_whole(_take(data, "rows", row=f"{row} {key}"), key="rows", row=row),
    )


def _whole(value: Any, *, key: str, row: str) -> int:
    number = _opt_whole(value, key=key, row=row)
    if number is None:
        raise CashBookDecodeError(f"a {row} line carries no {key}, expected an integer")
    return number


def _counts_to_jsonable(counts: Mapping[str, int]) -> dict[str, int]:
    return {str(name): int(value) for name, value in counts.items()}


def _counts_from_jsonable(value: Any, *, key: str, row: str) -> dict[str, int]:
    data = _require_object(value, row=f"{row} {key}")
    return {str(name): _whole(count, key=f"{key}.{name}", row=row) for name, count in data.items()}


def _warn_about_extra_keys(data: Mapping[str, Any], known: frozenset[str], *, row: str) -> None:
    """A key this reader does not model: kept out of the record, logged once.

    Not a refusal, on purpose. An extra key is how a line written by a NEWER,
    additive version arrives here, and refusing it would make every such line
    malformed; a MISSING key, which is how a rename arrives, IS refused.
    """
    extra = sorted(set(data) - known)
    if extra:
        logger.warning(
            "a %s line carries %d key(s) this reader does not model: %s", row, len(extra), extra
        )


def _known_fields(row_type: type) -> frozenset[str]:
    return frozenset(field.name for field in fields(row_type))


# --- CashEvent ---------------------------------------------------------------

_CASH_EVENT_ROW = "cash_events"


def cash_event_to_jsonable(event: CashEvent) -> dict[str, Any]:
    """One ``cash_events.jsonl`` line's payload. Refuses a non-finite amount."""
    return {
        "bk_amount_id": event.bk_amount_id,
        "bk_amount_type": event.bk_amount_type,
        "amount_class": event.amount_class,
        "amount": _finite(event.amount, key="amount", row=_CASH_EVENT_ROW),
        "currency": event.currency,
        "amount_account_currency": _finite(
            event.amount_account_currency, key="amount_account_currency", row=_CASH_EVENT_ROW
        ),
        "account_currency": event.account_currency,
        "conversion_rate": _finite(
            event.conversion_rate, key="conversion_rate", row=_CASH_EVENT_ROW
        ),
        "conversion_cost_account_currency": _finite(
            event.conversion_cost_account_currency,
            key="conversion_cost_account_currency",
            row=_CASH_EVENT_ROW,
        ),
        "date": event.date,
        "value_date": event.value_date,
        "uic": event.uic,
        "instrument_symbol": event.instrument_symbol,
        "related_trade_id": event.related_trade_id,
        "ca_event_name": event.ca_event_name,
    }


def cash_event_from_jsonable(data: Any) -> CashEvent:
    """One ``cash_events.jsonl`` line's payload, back to a :class:`CashEvent`."""
    row = _CASH_EVENT_ROW
    body = _require_object(data, row=row)
    _warn_about_extra_keys(body, _known_fields(CashEvent), row=row)
    return CashEvent(
        bk_amount_id=_text(_take(body, "bk_amount_id", row=row), key="bk_amount_id", row=row),
        bk_amount_type=_text(_take(body, "bk_amount_type", row=row), key="bk_amount_type", row=row),
        amount_class=_opt_text(_take(body, "amount_class", row=row), key="amount_class", row=row),
        amount=_finite(_take(body, "amount", row=row), key="amount", row=row),
        currency=_opt_text(_take(body, "currency", row=row), key="currency", row=row),
        amount_account_currency=_finite(
            _take(body, "amount_account_currency", row=row),
            key="amount_account_currency",
            row=row,
        ),
        account_currency=_opt_text(
            _take(body, "account_currency", row=row), key="account_currency", row=row
        ),
        conversion_rate=_finite(
            _take(body, "conversion_rate", row=row), key="conversion_rate", row=row
        ),
        conversion_cost_account_currency=_finite(
            _take(body, "conversion_cost_account_currency", row=row),
            key="conversion_cost_account_currency",
            row=row,
        ),
        date=_opt_text(_take(body, "date", row=row), key="date", row=row),
        value_date=_opt_text(_take(body, "value_date", row=row), key="value_date", row=row),
        uic=_opt_whole(_take(body, "uic", row=row), key="uic", row=row),
        instrument_symbol=_opt_text(
            _take(body, "instrument_symbol", row=row), key="instrument_symbol", row=row
        ),
        related_trade_id=_opt_text(
            _take(body, "related_trade_id", row=row), key="related_trade_id", row=row
        ),
        ca_event_name=_opt_text(
            _take(body, "ca_event_name", row=row), key="ca_event_name", row=row
        ),
    )


# --- TradeRow ----------------------------------------------------------------

_TRADE_ROW = "trade_rows"


def trade_row_to_jsonable(row_record: TradeRow) -> dict[str, Any]:
    """One ``trade_rows.jsonl`` line's payload. Refuses a non-finite amount."""
    return {
        "trade_id": row_record.trade_id,
        "order_id": row_record.order_id,
        "uic": row_record.uic,
        "instrument_symbol": row_record.instrument_symbol,
        "amount": _finite(row_record.amount, key="amount", row=_TRADE_ROW),
        "price": _finite(row_record.price, key="price", row=_TRADE_ROW),
        "trade_event_type": row_record.trade_event_type,
        "to_open_or_close": row_record.to_open_or_close,
        "trade_date": row_record.trade_date,
        "value_date": row_record.value_date,
        "execution_time": (
            None
            if row_record.execution_time is None
            else _stamp_to_text(row_record.execution_time, key="execution_time", row=_TRADE_ROW)
        ),
        # The field memo Phase 4 sums. Non-null on all 41 captured rows, which
        # is why its non-finite refusal needs a constructed case.
        "booked_amount_account_currency": _finite(
            row_record.booked_amount_account_currency,
            key="booked_amount_account_currency",
            row=_TRADE_ROW,
        ),
        "account_currency": row_record.account_currency,
        "venue": row_record.venue,
    }


def trade_row_from_jsonable(data: Any) -> TradeRow:
    """One ``trade_rows.jsonl`` line's payload, back to a :class:`TradeRow`."""
    row = _TRADE_ROW
    body = _require_object(data, row=row)
    _warn_about_extra_keys(body, _known_fields(TradeRow), row=row)
    stamp = _take(body, "execution_time", row=row)
    return TradeRow(
        trade_id=_text(_take(body, "trade_id", row=row), key="trade_id", row=row),
        order_id=_text(_take(body, "order_id", row=row), key="order_id", row=row),
        uic=_opt_whole(_take(body, "uic", row=row), key="uic", row=row),
        instrument_symbol=_opt_text(
            _take(body, "instrument_symbol", row=row), key="instrument_symbol", row=row
        ),
        amount=_finite(_take(body, "amount", row=row), key="amount", row=row),
        price=_finite(_take(body, "price", row=row), key="price", row=row),
        trade_event_type=_opt_text(
            _take(body, "trade_event_type", row=row), key="trade_event_type", row=row
        ),
        to_open_or_close=_opt_text(
            _take(body, "to_open_or_close", row=row), key="to_open_or_close", row=row
        ),
        trade_date=_opt_text(_take(body, "trade_date", row=row), key="trade_date", row=row),
        value_date=_opt_text(_take(body, "value_date", row=row), key="value_date", row=row),
        execution_time=(
            None if stamp is None else _text_to_stamp(stamp, key="execution_time", row=row)
        ),
        booked_amount_account_currency=_finite(
            _take(body, "booked_amount_account_currency", row=row),
            key="booked_amount_account_currency",
            row=row,
        ),
        account_currency=_opt_text(
            _take(body, "account_currency", row=row), key="account_currency", row=row
        ),
        venue=_opt_text(_take(body, "venue", row=row), key="venue", row=row),
    )


# --- BalanceSnapshot ---------------------------------------------------------

_BALANCE_ROW = "balances"


def balance_to_jsonable(snapshot: BalanceSnapshot) -> dict[str, Any]:
    """One ``balances.jsonl`` line's payload (the store adds the sweep key)."""
    return {
        "currency": snapshot.currency,
        "cash_balance": _finite(snapshot.cash_balance, key="cash_balance", row=_BALANCE_ROW),
        "non_margin_positions_value": _finite(
            snapshot.non_margin_positions_value,
            key="non_margin_positions_value",
            row=_BALANCE_ROW,
        ),
        "total_value": _finite(snapshot.total_value, key="total_value", row=_BALANCE_ROW),
        "transactions_not_booked": _finite(
            snapshot.transactions_not_booked, key="transactions_not_booked", row=_BALANCE_ROW
        ),
        "calculation_reliability": snapshot.calculation_reliability,
        "asof": _stamp_to_text(snapshot.asof, key="asof", row=_BALANCE_ROW),
    }


def balance_from_jsonable(data: Any) -> BalanceSnapshot:
    """One ``balances.jsonl`` payload, back to a :class:`BalanceSnapshot`."""
    row = _BALANCE_ROW
    body = _require_object(data, row=row)
    _warn_about_extra_keys(body, _known_fields(BalanceSnapshot), row=row)
    return BalanceSnapshot(
        currency=_opt_text(_take(body, "currency", row=row), key="currency", row=row),
        cash_balance=_finite(_take(body, "cash_balance", row=row), key="cash_balance", row=row),
        non_margin_positions_value=_finite(
            _take(body, "non_margin_positions_value", row=row),
            key="non_margin_positions_value",
            row=row,
        ),
        total_value=_finite(_take(body, "total_value", row=row), key="total_value", row=row),
        transactions_not_booked=_finite(
            _take(body, "transactions_not_booked", row=row),
            key="transactions_not_booked",
            row=row,
        ),
        calculation_reliability=_opt_text(
            _take(body, "calculation_reliability", row=row),
            key="calculation_reliability",
            row=row,
        ),
        asof=_text_to_stamp(_take(body, "asof", row=row), key="asof", row=row),
    )


# --- SweepRecord -------------------------------------------------------------

_SWEEP_ROW = "sweeps"


def sweep_to_jsonable(record: SweepRecord) -> dict[str, Any]:
    """One ``sweeps.jsonl`` line's payload."""
    return {
        "sweep_id": record.sweep_id,
        "swept_at": _stamp_to_text(record.swept_at, key="swept_at", row=_SWEEP_ROW),
        "env": record.env,
        "since": record.since,
        "until": record.until,
        "events_window": _window_to_jsonable(record.events_window),
        "trades_window": _window_to_jsonable(record.trades_window),
        "appended": _counts_to_jsonable(record.appended),
        "already_present": _counts_to_jsonable(record.already_present),
        "restated": _counts_to_jsonable(record.restated),
        "malformed": _counts_to_jsonable(record.malformed),
        "refused_reason": record.refused_reason,
    }


def sweep_from_jsonable(data: Any) -> SweepRecord:
    """One ``sweeps.jsonl`` line's payload, back to a :class:`SweepRecord`."""
    row = _SWEEP_ROW
    body = _require_object(data, row=row)
    _warn_about_extra_keys(body, _known_fields(SweepRecord), row=row)
    return SweepRecord(
        sweep_id=_text(_take(body, "sweep_id", row=row), key="sweep_id", row=row),
        swept_at=_text_to_stamp(_take(body, "swept_at", row=row), key="swept_at", row=row),
        env=_text(_take(body, "env", row=row), key="env", row=row),
        since=_text(_take(body, "since", row=row), key="since", row=row),
        until=_text(_take(body, "until", row=row), key="until", row=row),
        events_window=_window_from_jsonable(
            _take(body, "events_window", row=row), key="events_window", row=row
        ),
        trades_window=_window_from_jsonable(
            _take(body, "trades_window", row=row), key="trades_window", row=row
        ),
        appended=_counts_from_jsonable(_take(body, "appended", row=row), key="appended", row=row),
        already_present=_counts_from_jsonable(
            _take(body, "already_present", row=row), key="already_present", row=row
        ),
        restated=_counts_from_jsonable(_take(body, "restated", row=row), key="restated", row=row),
        malformed=_counts_from_jsonable(
            _take(body, "malformed", row=row), key="malformed", row=row
        ),
        refused_reason=_opt_text(
            _take(body, "refused_reason", row=row), key="refused_reason", row=row
        ),
    )
