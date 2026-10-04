"""One read-only record per pick: plan, entries, exits and outcome (#1701).

``alphalens broker trades`` answers "what did each pick actually do?" from the
keeper's four journals (plan, lifecycle, entry watch, stops) and, unless it runs
``--offline``, from the broker's own record (``SupportsFillHistory``: audit
activities, trades report, bookings report, instrument tick). The design memo
``docs/research/broker_trades_command_design_2026_10_03.md`` is the spec; the
section numbers below refer to it.

Three binding rules shape every line here (§1):

1. Every number carries its SOURCE, as a role name (``keeper.stop_journal``,
   ``venue.audit``, ``derived``...), never a file name, so the keeper repo split
   does not force a schema v2.
2. An unknown value is ``null`` with a reason from a published vocabulary. It is
   never estimated. The one allocation rule (a booking amount shared between
   owners, prorated by quantity) is published and marked ``derived`` with a
   warning.
3. The builder never writes. It reads journals and calls the two read methods of
   the capability, nothing else. The CLI's no-orders AST gate cannot see this
   module, so the module keeps to that by construction: it imports no placement
   code and names no write method.

The broker half is authoritative where it exists: who owns an exit order comes
from its ``ExternalReference`` (§4.4), times are the audit ``ActivityTime``, and
prices are ``AveragePrice``. Journal lines are cross-checks, and offline they are
the values, labelled as such.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from broker_contract.constants import QTY_PRECISION
from broker_contract.trade_intent.legacy import LEGACY_ALLOWANCES

from alphalens_pipeline.brokers.automanager import journal_snapshots, state_paths
from alphalens_pipeline.brokers.automanager.entry_trails import fold_entry_trail_records
from alphalens_pipeline.brokers.automanager.labels import _TP_REF_RE, tp_label_from_tag
from alphalens_pipeline.brokers.automanager.picks import (
    STATUS_ARMED,
    STATUS_DISARMED,
    STATUS_REFUSED,
    _submission_join_key,
    generation_of,
    identity_token,
    pick_key_str,
    read_pick_fold,
)
from alphalens_pipeline.brokers.automanager.stop_journal import _pick_key_from_stop_ref
from alphalens_pipeline.brokers.fill_history import (
    CostBooking,
    Execution,
    FillHistory,
    OrderActivity,
    SupportsFillHistory,
    parse_utc,
)

# --- Published vocabularies (§3.3, §4.4, contract README "broker trades") -----

SOURCE_PLAN = "plan"
SOURCE_PICK_QUEUE = "keeper.pick_queue"
SOURCE_ENTRY_WATCH = "keeper.entry_watch"
SOURCE_STOP_JOURNAL = "keeper.stop_journal"
SOURCE_SUBMISSIONS = "keeper.submissions"
SOURCE_AUDIT = "venue.audit"
SOURCE_TRADES_REPORT = "venue.trades_report"
SOURCE_BOOKINGS = "venue.bookings"
SOURCE_INSTRUMENT = "venue.instrument"
SOURCE_DERIVED = "derived"

SOURCES: tuple[str, ...] = (
    SOURCE_PLAN,
    SOURCE_PICK_QUEUE,
    SOURCE_ENTRY_WATCH,
    SOURCE_STOP_JOURNAL,
    SOURCE_SUBMISSIONS,
    SOURCE_AUDIT,
    SOURCE_TRADES_REPORT,
    SOURCE_BOOKINGS,
    SOURCE_INSTRUMENT,
    SOURCE_DERIVED,
)

NULL_OFFLINE = "offline"
NULL_NOT_JOURNALED = "not_journaled"
NULL_COMPACTED = "compacted_before_snapshots"
NULL_NO_AUDIT_ROW = "no_audit_row"
NULL_SIM_REPORTS = "sim_reports_unusable"
NULL_NOT_CLOSED = "not_closed"
NULL_NEVER_FILLED = "never_filled"
NULL_NON_POSITIVE_RISK = "non_positive_risk"
NULL_AMBIGUOUS = "ambiguous_attribution"
NULL_LEGACY_PLAN = "legacy_plan_shape"
NULL_FX_NOT_REALIZED = "fx_rate_not_realized"
NULL_REPORT_ROW_MISSING = "report_row_missing"
NULL_NON_FINITE = "non_finite"
NULL_STOP_AMEND_UNAVAILABLE = "stop_amend_history_unavailable"

NULL_REASONS: tuple[str, ...] = (
    NULL_OFFLINE,
    NULL_NOT_JOURNALED,
    NULL_COMPACTED,
    NULL_NO_AUDIT_ROW,
    NULL_SIM_REPORTS,
    NULL_NOT_CLOSED,
    NULL_NEVER_FILLED,
    NULL_NON_POSITIVE_RISK,
    NULL_AMBIGUOUS,
    NULL_LEGACY_PLAN,
    NULL_FX_NOT_REALIZED,
    NULL_REPORT_ROW_MISSING,
    NULL_NON_FINITE,
    NULL_STOP_AMEND_UNAVAILABLE,
)

REASON_TAKE_PROFIT = "take_profit"
REASON_DISASTER_STOP = "disaster_stop"
REASON_TRAILED_STOP = "trailed_stop"
REASON_REANCHORED_STOP = "reanchored_stop"
REASON_STOP_MOVED_UNKNOWN = "stop_moved_kind_unknown"
REASON_MANUAL_CLOSE = "manual_close"
REASON_MANUAL_OPEN = "manual_open"
REASON_UNKNOWN = "unknown"

EXIT_REASONS: tuple[str, ...] = (
    REASON_TAKE_PROFIT,
    REASON_DISASTER_STOP,
    REASON_TRAILED_STOP,
    REASON_REANCHORED_STOP,
    REASON_STOP_MOVED_UNKNOWN,
    REASON_MANUAL_CLOSE,
    REASON_MANUAL_OPEN,
    REASON_UNKNOWN,
)

# The daemon's alert vocabulary (`trade_alerts.ExitReason`) mapped onto ours.
# A test checks that every member has a row, so the two cannot drift apart.
EXIT_REASON_BY_ALERT_REASON: Mapping[str, str | None] = {
    "TAKE_PROFIT": REASON_TAKE_PROFIT,
    "PLAN_STOP": REASON_DISASTER_STOP,
    "TRAILED_STOP": REASON_TRAILED_STOP,
    "REANCHORED_STOP": REASON_REANCHORED_STOP,
    # The alert's catch-all for a marker it does not know; this command keeps
    # the honest "a move happened, its kind is lost" instead.
    "STOP": REASON_STOP_MOVED_UNKNOWN,
}

ATTRIBUTION_JOURNAL_TIE = "journal_tie"
ATTRIBUTION_EXTERNAL_REFERENCE = "external_reference"
ATTRIBUTION_POSITION_LINK = "position_link"
ATTRIBUTION_FIFO = "fifo_fallback"

ATTRIBUTIONS: tuple[str, ...] = (
    ATTRIBUTION_JOURNAL_TIE,
    ATTRIBUTION_EXTERNAL_REFERENCE,
    ATTRIBUTION_POSITION_LINK,
    ATTRIBUTION_FIFO,
)

STATE_CLOSED = "closed"
STATE_OPEN = "open"
STATE_NEVER_FILLED = "never_filled"
STATE_UNRESOLVED = "unresolved"
STATES: tuple[str, ...] = (STATE_CLOSED, STATE_OPEN, STATE_NEVER_FILLED, STATE_UNRESOLVED)
STATE_FILTER_ALL = "all"

STATE_REASONS: tuple[str, ...] = (
    "refused",
    "disarmed",
    "expired",
    "cancelled",
    "pending",
    NULL_COMPACTED,
    NULL_NOT_JOURNALED,
    NULL_AMBIGUOUS,
)

TERMINAL_FILLED = "filled"
TERMINAL_EXPIRED = "expired"
TERMINAL_CANCELLED = "cancelled"
TERMINAL_SUSPENDED = "suspended"
TERMINAL_OPEN = "open"
TERMINALS: tuple[str, ...] = (
    TERMINAL_FILLED,
    TERMINAL_EXPIRED,
    TERMINAL_CANCELLED,
    TERMINAL_SUSPENDED,
    TERMINAL_OPEN,
)

PATH_TRAIL_WATCH = "trail_watch"
PATH_NOW_BRACKET = "now_bracket"
TIER_PATHS: tuple[str, ...] = (PATH_TRAIL_WATCH, PATH_NOW_BRACKET)

# `sources[role]`: whether a source was read, and why not when it was not.
SOURCE_STATUS_READ = "read"
SOURCE_STATUS_SKIPPED = "skipped"
SOURCE_STATUS_OFFLINE = "offline"
SOURCE_STATUSES: tuple[str, ...] = (
    SOURCE_STATUS_READ,
    SOURCE_STATUS_SKIPPED,
    SOURCE_STATUS_OFFLINE,
)
SOURCE_REASON_NO_PICKS = "no_picks"
SOURCE_REASONS: tuple[str, ...] = (NULL_OFFLINE, NULL_SIM_REPORTS, SOURCE_REASON_NO_PICKS)

MODE_BROKER = "broker"
MODE_OFFLINE = "offline"
MODES: tuple[str, ...] = (MODE_BROKER, MODE_OFFLINE)

PICK_STATUSES: tuple[str, ...] = (STATUS_ARMED, STATUS_REFUSED, STATUS_DISARMED)
SIZE_SHAPES: tuple[str, ...] = ("by_amount", "by_percent")
PLAN_SOURCES: tuple[str, ...] = ("manual", "brief", "absent")
SIDES: tuple[str, ...] = ("long", "short")

# The envelope id the CLI renders this report under (§3).
TRADES_SCHEMA_ID = "alphalens.broker.trades/v1"

W_FOLD_ORDER_UNCERTAIN = "fold_order_uncertain"
W_ENTRY_NOT_IN_JOURNAL = "entry_not_in_journal"
W_AUDIT_REPORT_DISAGREE = "audit_report_disagree"
W_PARTIAL_FILL_UNVERIFIED = "partial_fill_shape_unverified"
W_JOURNAL_AUDIT_DISAGREE = "journal_audit_disagree"
W_FIFO_ASSUMED = "attribution_fifo_assumed"
W_EXIT_QTY_EXCEEDS_PICK = "exit_qty_exceeds_pick"
W_EXIT_NOT_IN_JOURNAL = "exit_not_in_journal"
W_REPORT_LAGS_AUDIT = "report_lags_audit"
W_BOOKING_TYPE_UNMAPPED = "booking_type_unmapped"
W_BOOKING_PRORATED = "booking_prorated"
W_AMBIGUOUS = "ambiguous_attribution"
W_SIDE_UNRESOLVED = "side_unresolved"

WARNING_CODES: tuple[str, ...] = (
    W_FOLD_ORDER_UNCERTAIN,
    W_ENTRY_NOT_IN_JOURNAL,
    W_AUDIT_REPORT_DISAGREE,
    W_PARTIAL_FILL_UNVERIFIED,
    W_JOURNAL_AUDIT_DISAGREE,
    W_FIFO_ASSUMED,
    W_EXIT_QTY_EXCEEDS_PICK,
    W_EXIT_NOT_IN_JOURNAL,
    W_REPORT_LAGS_AUDIT,
    W_BOOKING_TYPE_UNMAPPED,
    W_BOOKING_PRORATED,
    W_AMBIGUOUS,
    W_SIDE_UNRESOLVED,
)

# Not summed into any fee, and listed on every record so a consumer knows (§5).
FEES_NOT_INCLUDED: tuple[str, ...] = ("financing", "dividends", "withholding_tax")

# `BkAmountType` values the builder maps (§4.7, LIVE probe P3). Anything else
# on a trade-tied row gets `booking_type_unmapped`; the corporate-action types
# are known and deliberately not summed (they are not tied to a fill).
_BK_COMMISSION = "Commission"
_BK_EXCHANGE_FEE = "Exchange Fee"
_BK_SHARE_AMOUNT = "Share Amount"
_BK_KNOWN_NOT_SUMMED = frozenset(
    {
        "Cash Amount",
        "Corporate Actions - Cash Dividends",
        "Corporate Actions - Withholding Tax",
    }
)

# Audit and report stamp one execution about 1 ms apart (VST 13:36:35.916Z vs
# .917Z). Any cross-source time comparison allows this much (§4.3 item 6).
_CROSS_SOURCE_TIME_TOLERANCE_S = 1.0

# The audit window opens this long before the oldest armed pick (§4.7).
_AUDIT_LOOKBACK = dt.timedelta(days=1)

# Quantities are floats on the wire but whole shares on this rail, so two
# quantities closer than the shared precision are equal (#1125). Prices and
# fractions are compared with a float tolerance instead: a stop move of a cent
# must not vanish under a half-share bound.
_QTY_EPS = QTY_PRECISION
_FLOAT_TOLERANCE = 1e-9

_UNIT_SHARES = "shares"
_UNIT_SECONDS = "s"
_UNIT_PERCENT = "%"
_UNIT_R = "R"

_STATUS_FINAL_FILL = "FinalFill"
_STATUS_FILL = "Fill"
_STATUS_PLACED = "Placed"
_STATUS_CHANGED = "Changed"
_STATUS_CANCELLED = "Cancelled"
_STATUS_EXPIRED = "Expired"
_SUB_REJECTED = "Rejected"
_BUY = "Buy"
_SELL = "Sell"
_STOP_ORDER_TYPES = frozenset({"Stop", "StopIfTraded", "StopLimit", "TrailingStopIfTraded"})

_STOP_REF_RE = re.compile(r"-entry-t\d+(?:-fire)?-stop-\d+$")
_BRACKET_STOP_REF_RE = re.compile(r"^(?P<request>.+)-stop-\d+$")
_OCO_LEG_RE = re.compile(r"^(?P<request>.+)-(?P<leg>stop|tp)$")
_UIC_TP_REF_RE = re.compile(r"^u(?P<uic>\d+)-tp(?P<n>\d+)(?:-|$)")
_PICK_KEY_RE = re.compile(r"^[A-Z0-9.\-]+:\d{4}-\d{2}-\d{2}(?:-g[1-9]\d*)?$")


def valid_pick_key(value: str) -> bool:
    """``TICKER:YYYY-MM-DD`` or ``TICKER:YYYY-MM-DD-gN`` (the ``--pick`` shape)."""
    if not _PICK_KEY_RE.match(value):
        return False
    day = value.split(":", 1)[1][:10]
    try:
        dt.date.fromisoformat(day)
    except ValueError:
        return False
    return True


# --- Measured -----------------------------------------------------------------


def format_time(moment: dt.datetime) -> str:
    """RFC 3339 UTC with milliseconds and ``Z`` (§3.1)."""
    utc = moment.astimezone(dt.UTC)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"


@dataclass(frozen=True)
class Measured:
    """One value with its unit, source, row reference and null reason (§3.3)."""

    value: Any
    unit: str | None
    source: str
    ref: str | None = None
    null_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        value = self.value
        if isinstance(value, dt.datetime):
            value = format_time(value)
        return {
            "value": value,
            "unit": self.unit,
            "source": self.source,
            "ref": self.ref,
            "null_reason": self.null_reason,
        }


def _num(value: float | None, unit: str | None, source: str, ref: str | None = None) -> Measured:
    """A numeric Measured; a non-finite number becomes null ``non_finite``."""
    if value is None:
        return Measured(None, unit, source, ref, NULL_NOT_JOURNALED)
    if isinstance(value, bool) or not math.isfinite(float(value)):
        return Measured(None, unit, source, ref, NULL_NON_FINITE)
    return Measured(float(value), unit, source, ref)


def _time(moment: dt.datetime | None, source: str, ref: str | None = None) -> Measured:
    if moment is None:
        return Measured(None, None, source, ref, NULL_NOT_JOURNALED)
    return Measured(moment, None, source, ref)


def _null(reason: str, unit: str | None, source: str, ref: str | None = None) -> Measured:
    return Measured(None, unit, source, ref, reason)


def _price_unit(currency: str | None) -> str | None:
    return f"price:{currency}" if currency else None


def _as_float(value: Any) -> float | None:
    """A journal number AS WRITTEN, non-finite included, so :func:`_num` can
    report ``non_finite`` instead of losing the value as "not journaled"."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


# --- Inputs -------------------------------------------------------------------


@dataclass(frozen=True)
class TradesFilters:
    """What the caller asked for. ``limit=None`` means every row (``--all``)."""

    state: str = STATE_FILTER_ALL
    since: dt.date | None = None
    ticker: str | None = None
    pick: str | None = None
    limit: int | None = 200


@dataclass(frozen=True)
class TradesReport:
    """The finished result; :meth:`body` is the envelope body after schema/env."""

    env: str
    generated_at: dt.datetime
    mode: str
    sources: Mapping[str, Any]
    snapshot_horizon: dt.datetime | None
    counts: Mapping[str, Any]
    truncated: bool
    trades: tuple[Mapping[str, Any], ...]
    unattributed_fills: tuple[Mapping[str, Any], ...]

    def body(self) -> dict[str, Any]:
        return {
            "generated_at": format_time(self.generated_at),
            "mode": self.mode,
            "sources": dict(self.sources),
            "snapshot_horizon": None
            if self.snapshot_horizon is None
            else format_time(self.snapshot_horizon),
            "counts": dict(self.counts),
            "truncated": self.truncated,
            "trades": list(self.trades),
            "unattributed_fills": list(self.unattributed_fills),
        }


# --- Journal reading (§4.1) ---------------------------------------------------

_JOURNALS = ("picks", "entry_trails", "standalone_stops", "submissions")
_SOURCE_BY_JOURNAL = {
    "picks": SOURCE_PICK_QUEUE,
    "entry_trails": SOURCE_ENTRY_WATCH,
    "standalone_stops": SOURCE_STOP_JOURNAL,
    "submissions": SOURCE_SUBMISSIONS,
}
_COMPACTED_JOURNALS = ("entry_trails", "standalone_stops")


@dataclass
class _Journals:
    paths: dict[str, Path]
    records: dict[str, list[dict[str, Any]]]
    malformed: dict[str, int]
    duplicated: dict[str, list[dict[str, Any]]]
    horizon: dt.datetime | None


def _journal_paths(env: str) -> dict[str, Path]:
    return {
        "picks": state_paths.picks_path(env=env),
        "entry_trails": state_paths.entry_trails_path(env=env),
        "standalone_stops": state_paths.standalone_stops_path(env=env),
        "submissions": state_paths.submissions_path(env=env),
    }


def _repeated_within_one_file(path: Path) -> list[dict[str, Any]]:
    """Records that appear twice in ONE file (snapshot or current journal).

    History dedup keys on canonical JSON, so such a record reaches a fold once
    and the fold can see the wrong order (a re-arm ``watch_open`` with no
    ``ts``). A record repeated ACROSS files is a snapshot copy, not this."""
    repeated: list[dict[str, Any]] = []
    for candidate in [*journal_snapshots._snapshots_of(path), path]:
        seen: set[str] = set()
        for record in journal_snapshots._records_in(candidate):
            key = json.dumps(record, sort_keys=True, default=str)
            if key in seen:
                repeated.append(record)
            seen.add(key)
    return repeated


def _read_journals(env: str) -> _Journals:
    paths = _journal_paths(env)
    records: dict[str, list[dict[str, Any]]] = {}
    malformed: dict[str, int] = {}
    duplicated: dict[str, list[dict[str, Any]]] = {}
    for name in _JOURNALS:
        bad: list[str] = []
        records[name] = list(journal_snapshots.iter_journal_history(paths[name], malformed=bad))
        malformed[name] = len(bad)
        duplicated[name] = _repeated_within_one_file(paths[name])
    stamps = [
        stamp
        for name in _COMPACTED_JOURNALS
        if (stamp := journal_snapshots.earliest_snapshot_stamp(paths[name])) is not None
    ]
    return _Journals(
        paths=paths,
        records=records,
        malformed=malformed,
        duplicated=duplicated,
        horizon=min(stamps) if stamps else None,
    )


# --- Working model ------------------------------------------------------------


@dataclass
class _Fill:
    """One fill, with every fact the output names (§3.4 Fill)."""

    order_id: str | None
    external_reference: str | None
    uic: int | None
    venue_time: Measured
    price: Measured
    qty: Measured
    executions: list[dict[str, Any]] = field(default_factory=list)
    detected_at: Measured = field(
        default_factory=lambda: _null(NULL_NOT_JOURNALED, None, SOURCE_DERIVED)
    )
    position_id: str | None = None
    related_position_id: str | None = None
    fees: dict[str, Measured] = field(default_factory=dict)
    realized_fx: dict[str, Measured] = field(default_factory=dict)
    # Booking amounts in a form the outcome can prorate: per member a list of
    # (amount, ref) or a null reason.
    booking_sums: dict[str, tuple[float | None, str | None, str | None]] = field(
        default_factory=dict
    )
    when: dt.datetime | None = None
    filled_qty: float | None = None
    closing: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "external_reference": self.external_reference,
            "venue_time": self.venue_time.to_dict(),
            "price": self.price.to_dict(),
            "qty": self.qty.to_dict(),
            "executions": list(self.executions),
            "detected_at": self.detected_at.to_dict(),
            "position_id": self.position_id,
            "related_position_id": self.related_position_id,
            "fees": {name: m.to_dict() for name, m in self.fees.items()},
            "realized_fx": {name: m.to_dict() for name, m in self.realized_fx.items()},
        }


@dataclass
class _Tier:
    tier_index: int
    path: str
    crid: str
    planned_limit: Measured
    planned_qty: Measured
    window_end: Measured
    touched_at: Measured
    touch_price: Measured
    trigger_order_id: str | None
    armed_at: Measured
    terminal: str
    terminal_at: Measured
    terminal_at_source: str | None
    fill: _Fill | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "tier_index": self.tier_index,
            "path": self.path,
            "crid": self.crid,
            "planned_limit": self.planned_limit.to_dict(),
            "planned_qty": self.planned_qty.to_dict(),
            "window_end": self.window_end.to_dict(),
            "touched_at": self.touched_at.to_dict(),
            "touch_price": self.touch_price.to_dict(),
            "trigger_order_id": self.trigger_order_id,
            "armed_at": self.armed_at.to_dict(),
            "terminal": self.terminal,
            "terminal_at": self.terminal_at.to_dict(),
            "terminal_at_source": self.terminal_at_source,
            "fill": None if self.fill is None else self.fill.to_dict(),
        }


@dataclass
class _Exit:
    fill: _Fill
    reason: str | None
    reason_null_reason: str | None
    evidence: list[str]
    stop_level_at_fill: Measured
    tp_label: str | None
    attributed_qty: Measured
    attribution: str | None

    def to_dict(self) -> dict[str, Any]:
        out = self.fill.to_dict()
        out.update(
            {
                "reason": self.reason,
                "reason_null_reason": self.reason_null_reason,
                "reason_evidence": list(self.evidence),
                "stop_level_at_fill": self.stop_level_at_fill.to_dict(),
                "tp_label": self.tp_label,
                "attributed_qty": self.attributed_qty.to_dict(),
                "attribution": self.attribution,
            }
        )
        return out


@dataclass
class _Pick:
    """Everything known about one pick while its record is built."""

    ticker: str
    trade_date: str
    generation: int
    key: str
    token: str
    status: str
    status_record: Mapping[str, Any]
    plan_line: Mapping[str, Any] | None
    uic: int | None = None
    exchange_mic: str | None = None
    instrument_currency: str | None = None
    sizing_currency: str | None = None
    sizing_fx: dict[str, Measured] = field(default_factory=dict)
    submissions: list[Mapping[str, Any]] = field(default_factory=list)
    tiers: list[_Tier] = field(default_factory=list)
    exits: list[_Exit] = field(default_factory=list)
    warnings: list[dict[str, str]] = field(default_factory=list)
    stop_refs: set[str] = field(default_factory=set)
    ambiguous: bool = False

    @property
    def crid_prefix(self) -> str:
        return f"{self.ticker}-{self.token}-entry-t"

    @property
    def plan(self) -> Mapping[str, Any] | None:
        if self.plan_line is None:
            return None
        intent = self.plan_line.get("intent")
        return intent if isinstance(intent, Mapping) else None

    @property
    def plan_armed_at(self) -> dt.datetime | None:
        if self.plan_line is not None:
            return parse_utc(self.plan_line.get("armed_ts"))
        return parse_utc(self.status_record.get("armed_ts"))

    def warn(self, code: str, detail: str) -> None:
        entry = {"code": code, "detail": detail}
        if entry not in self.warnings:
            self.warnings.append(entry)

    def entry_fills(self) -> list[_Fill]:
        return [tier.fill for tier in self.tiers if tier.fill is not None]


# --- Picks and plans (§4.2) ---------------------------------------------------


def _pick_status_fields(status: str, record: Mapping[str, Any]) -> tuple[Any, str | None]:
    if status == STATUS_DISARMED:
        note = record.get("note")
        return record.get("disarmed_ts"), str(note) if note else None
    if status == STATUS_REFUSED:
        reason = record.get("reason")
        return record.get("refused_ts"), str(reason) if reason else None
    return record.get("armed_ts"), None


def _plans_by_key(records: Iterable[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    """The LAST armed line that carries an intent, per pick key, in file order.

    LEGACY(size_pct_v2): unlike ``iter_picks``, a percent-sized document is kept
    here undecoded; only its size-derived fields become null."""
    plans: dict[str, Mapping[str, Any]] = {}
    for record in records:
        if record.get("status") != STATUS_ARMED or not isinstance(record.get("intent"), Mapping):
            continue
        try:
            generation = generation_of(record)
            trade_date = dt.date.fromisoformat(str(record["date"])).isoformat()
        except (KeyError, ValueError):
            continue
        key = pick_key_str(str(record.get("ticker", "")), trade_date, generation)
        plans[key] = record
    return plans


def _load_picks(journals: _Journals) -> list[_Pick]:
    fold = read_pick_fold(path=journals.paths["picks"])
    plans = _plans_by_key(journals.records["picks"])
    picks: list[_Pick] = []
    for record in fold.records:
        trade_date = record.trade_date.isoformat()
        key = pick_key_str(record.ticker, trade_date, record.generation)
        picks.append(
            _Pick(
                ticker=record.ticker,
                trade_date=trade_date,
                generation=record.generation,
                key=key,
                token=identity_token(trade_date, record.generation),
                status=record.status,
                status_record=record.record,
                plan_line=plans.get(key),
            )
        )
    return picks


def _plan_size_shape(plan: Mapping[str, Any] | None) -> str:
    if plan is not None and LEGACY_ALLOWANCES["size_pct_v2"].still_needed(plan):
        return "by_percent"
    return "by_amount"


def _plan_source(plan: Mapping[str, Any] | None) -> str:
    meta = plan.get("meta") if plan is not None else None
    source = meta.get("source") if isinstance(meta, Mapping) else None
    return str(source) if source in ("manual", "brief") else "absent"


def _plan_schema_version(plan: Mapping[str, Any] | None) -> str | None:
    meta = plan.get("meta") if plan is not None else None
    version = meta.get("schema_version") if isinstance(meta, Mapping) else None
    return None if version is None else str(version)


def _plan_side(plan: Mapping[str, Any] | None) -> str | None:
    if plan is None:
        return None
    spec = plan.get("spec")
    if not isinstance(spec, Mapping):
        return None
    side = spec.get("side", "long")  # the schema default (TradeSpec.side)
    return side if side in ("long", "short") else None


# --- Venue index --------------------------------------------------------------


@dataclass
class _Order:
    order_id: str
    rows: list[OrderActivity]

    @property
    def reference(self) -> str | None:
        return next((r.external_reference for r in self.rows if r.external_reference), None)

    @property
    def uic(self) -> int | None:
        return next((r.uic for r in self.rows if r.uic is not None), None)

    @property
    def buy_sell(self) -> str | None:
        return next((r.buy_sell for r in self.rows if r.buy_sell), None)

    @property
    def placed(self) -> OrderActivity | None:
        placed = [r for r in self.rows if r.status == _STATUS_PLACED]
        confirmed = [r for r in placed if r.sub_status != "Requested"]
        return (confirmed or placed or [None])[0]

    @property
    def placed_order_type(self) -> str | None:
        placed = self.placed
        return placed.order_type if placed is not None else None

    @property
    def rejected(self) -> bool:
        return any(r.sub_status == _SUB_REJECTED for r in self.rows)

    @property
    def final_fill(self) -> OrderActivity | None:
        return next((r for r in reversed(self.rows) if r.status == _STATUS_FINAL_FILL), None)

    @property
    def partial_fills(self) -> list[OrderActivity]:
        return [r for r in self.rows if r.status == _STATUS_FILL]

    @property
    def related_position_id(self) -> str | None:
        return next((r.related_position_id for r in self.rows if r.related_position_id), None)

    def terminal_row(self) -> OrderActivity | None:
        for row in reversed(self.rows):
            if row.status in (_STATUS_FINAL_FILL, _STATUS_CANCELLED, _STATUS_EXPIRED):
                return row
        return None


@dataclass
class _Venue:
    history: FillHistory | None
    orders: dict[str, _Order]
    executions_by_order: dict[str, list[Execution]]
    bookings_by_trade: dict[str, list[CostBooking]]
    reports_null_reason: str | None
    broker: SupportsFillHistory | None
    ticks: dict[int, float | None] = field(default_factory=dict)
    tick_uics: set[int] = field(default_factory=set)

    def tick(self, uic: int | None, price: float | None) -> float | None:
        if self.broker is None or uic is None or price is None:
            return None
        self.tick_uics.add(uic)
        return self.broker.tick_size(uic, price)


def _index_venue(history: FillHistory, broker: SupportsFillHistory) -> _Venue:
    rows_by_order: dict[str, list[OrderActivity]] = defaultdict(list)
    for activity in history.activities:
        if activity.order_id:
            rows_by_order[activity.order_id].append(activity)
    far_past = dt.datetime.min.replace(tzinfo=dt.UTC)
    orders = {
        order_id: _Order(
            order_id,
            sorted(rows, key=lambda r: (r.activity_time or far_past, r.log_id or 0)),
        )
        for order_id, rows in rows_by_order.items()
    }
    executions: dict[str, list[Execution]] = defaultdict(list)
    for execution in history.executions or ():
        executions[execution.order_id].append(execution)
    bookings: dict[str, list[CostBooking]] = defaultdict(list)
    for booking in history.bookings or ():
        bookings[booking.related_trade_id].append(booking)
    reports_null = None
    if history.executions is None or history.bookings is None:
        reports_null = history.reports_skipped_reason or NULL_SIM_REPORTS
    return _Venue(
        history=history,
        orders=orders,
        executions_by_order=dict(executions),
        bookings_by_trade=dict(bookings),
        reports_null_reason=reports_null,
        broker=broker,
    )


# --- Fills from the venue (§3.4 Fill, §4.3 item 4) ----------------------------


def _venue_fill(
    order: _Order, venue: _Venue, pick: _Pick | None, *, currency: str | None
) -> _Fill | None:
    final = order.final_fill
    row = final
    partial_only = False
    if row is None:
        partials = order.partial_fills
        if not partials:
            return None
        row = partials[-1]
        partial_only = True
    ref = f"order:{order.order_id}"
    price_value = row.average_price
    executions = venue.executions_by_order.get(order.order_id, [])
    if price_value is None and row.execution_price is not None and len(executions) <= 1:
        price_value = row.execution_price
    fill = _Fill(
        order_id=order.order_id,
        external_reference=order.reference,
        uic=order.uic,
        venue_time=_time(row.activity_time, SOURCE_AUDIT, ref),
        price=_num(price_value, _price_unit(currency), SOURCE_AUDIT, ref),
        qty=_num(row.filled_amount, _UNIT_SHARES, SOURCE_AUDIT, ref),
        position_id=row.position_id,
        related_position_id=row.related_position_id or order.related_position_id,
        when=row.activity_time,
        filled_qty=row.filled_amount,
        closing=order.buy_sell == _SELL,
    )
    if partial_only and pick is not None:
        pick.warn(
            W_PARTIAL_FILL_UNVERIFIED,
            f"order {order.order_id}: only partial Fill rows; the last cumulative row is used",
        )
    _attach_report_facts(fill, executions, venue, pick, currency=currency)
    return fill


def _attach_report_facts(
    fill: _Fill,
    executions: Sequence[Execution],
    venue: _Venue,
    pick: _Pick | None,
    *,
    currency: str | None,
) -> None:
    """Executions, fees and realized FX for one fill, from the two reports."""
    if venue.reports_null_reason is not None:
        _null_report_facts(fill, venue.reports_null_reason, NULL_FX_NOT_REALIZED)
        return
    if not executions:
        _null_report_facts(fill, NULL_REPORT_ROW_MISSING, NULL_REPORT_ROW_MISSING)
        if pick is not None:
            pick.warn(
                W_REPORT_LAGS_AUDIT,
                f"order {fill.order_id}: the audit has the fill, the trades report has no row yet",
            )
        return
    fill.executions = [
        {
            "trade_id": e.trade_id,
            "time": None if e.execution_time is None else format_time(e.execution_time),
            "price": e.price,
            "qty": None if e.amount is None else abs(e.amount),
        }
        for e in executions
    ]
    _cross_check_executions(fill, executions, venue, pick)
    trade_ids = [e.trade_id for e in executions]
    trade_ref = ",".join(f"trade:{t}" for t in trade_ids)
    rows = [b for t in trade_ids for b in venue.bookings_by_trade.get(t, [])]
    if not rows:
        _null_report_facts(fill, NULL_REPORT_ROW_MISSING, NULL_REPORT_ROW_MISSING)
        if pick is not None:
            pick.warn(
                W_REPORT_LAGS_AUDIT,
                f"order {fill.order_id}: the bookings report has no row for {trade_ref} yet",
            )
        return
    sums: dict[str, float] = {"commission": 0.0, "exchange_fee": 0.0, "fx_conversion": 0.0}
    share_acct = 0.0
    share_native = 0.0
    rates: list[float] = []
    booking_ccy: str | None = None
    account_ccy: str | None = None
    has_share = False
    for booking in rows:
        account_ccy = account_ccy or booking.account_currency
        if booking.bk_amount_type == _BK_COMMISSION:
            sums["commission"] += booking.amount or 0.0
            booking_ccy = booking_ccy or booking.currency
        elif booking.bk_amount_type == _BK_EXCHANGE_FEE:
            sums["exchange_fee"] += booking.amount or 0.0
            booking_ccy = booking_ccy or booking.currency
        elif booking.bk_amount_type == _BK_SHARE_AMOUNT:
            has_share = True
            sums["fx_conversion"] += booking.conversion_cost_account_currency or 0.0
            share_acct += booking.amount_account_currency or 0.0
            share_native += booking.amount or 0.0
            if booking.conversion_rate is not None:
                rates.append(booking.conversion_rate)
        elif booking.bk_amount_type not in _BK_KNOWN_NOT_SUMMED and pick is not None:
            pick.warn(
                W_BOOKING_TYPE_UNMAPPED,
                f"trade {booking.related_trade_id}: BkAmountType {booking.bk_amount_type!r}",
            )
    booking_ccy = booking_ccy or currency
    fill.fees = {
        "commission": _num(sums["commission"], booking_ccy, SOURCE_BOOKINGS, trade_ref),
        "exchange_fee": _num(sums["exchange_fee"], booking_ccy, SOURCE_BOOKINGS, trade_ref),
        "fx_conversion": _num(sums["fx_conversion"], account_ccy, SOURCE_BOOKINGS, trade_ref)
        if has_share
        else _null(NULL_FX_NOT_REALIZED, account_ccy, SOURCE_BOOKINGS, trade_ref),
    }
    fill.booking_sums = {
        "commission": (sums["commission"], booking_ccy, None),
        "exchange_fee": (sums["exchange_fee"], booking_ccy, None),
        "fx_conversion": (sums["fx_conversion"], account_ccy, None)
        if has_share
        else (None, account_ccy, NULL_FX_NOT_REALIZED),
        "share_amount_acct": (share_acct, account_ccy, None)
        if has_share
        else (None, account_ccy, NULL_FX_NOT_REALIZED),
    }
    if not has_share:
        fill.realized_fx = {
            "conversion_rate": _null(NULL_FX_NOT_REALIZED, None, SOURCE_BOOKINGS, trade_ref),
            "share_amount_acct": _null(
                NULL_FX_NOT_REALIZED, account_ccy, SOURCE_BOOKINGS, trade_ref
            ),
        }
        return
    if len(set(rates)) == 1:
        rate = _num(rates[0], None, SOURCE_BOOKINGS, trade_ref)
    elif share_native:
        # Several executions booked at different rates: the amount-weighted
        # rate, which is a computation, so its source says so.
        rate = _num(share_acct / share_native, None, SOURCE_DERIVED, trade_ref)
    else:
        rate = _null(NULL_FX_NOT_REALIZED, None, SOURCE_BOOKINGS, trade_ref)
    fill.realized_fx = {
        "conversion_rate": rate,
        "share_amount_acct": _num(share_acct, account_ccy, SOURCE_BOOKINGS, trade_ref),
    }


def _null_report_facts(fill: _Fill, fee_reason: str, fx_reason: str) -> None:
    ref = f"order:{fill.order_id}"
    fill.fees = {
        "commission": _null(fee_reason, None, SOURCE_BOOKINGS, ref),
        "exchange_fee": _null(fee_reason, None, SOURCE_BOOKINGS, ref),
        "fx_conversion": _null(fee_reason, None, SOURCE_BOOKINGS, ref),
    }
    fill.realized_fx = {
        "conversion_rate": _null(fx_reason, None, SOURCE_BOOKINGS, ref),
        "share_amount_acct": _null(fx_reason, None, SOURCE_BOOKINGS, ref),
    }
    fill.booking_sums = dict.fromkeys(
        ("commission", "exchange_fee", "fx_conversion"), (None, None, fee_reason)
    )
    fill.booking_sums["share_amount_acct"] = (None, None, NULL_FX_NOT_REALIZED)


def _cross_check_executions(
    fill: _Fill, executions: Sequence[Execution], venue: _Venue, pick: _Pick | None
) -> None:
    if pick is None:
        return
    total = sum(abs(e.amount or 0.0) for e in executions)
    price = fill.price.value
    if fill.filled_qty is not None and abs(total - fill.filled_qty) > _QTY_EPS:
        pick.warn(
            W_AUDIT_REPORT_DISAGREE,
            f"order {fill.order_id}: executions sum to {total:g}, the audit says {fill.filled_qty:g}",
        )
    if total > 0 and price is not None:
        vwap = sum(abs(e.amount or 0.0) * (e.price or 0.0) for e in executions) / total
        tick = venue.tick(fill.uic, price)
        if tick is not None and abs(vwap - price) > tick + _FLOAT_TOLERANCE:
            pick.warn(
                W_AUDIT_REPORT_DISAGREE,
                f"order {fill.order_id}: execution VWAP {vwap:.6g} vs AveragePrice {price:g}",
            )
    if fill.when is not None:
        for execution in executions:
            if execution.execution_time is None:
                continue
            gap = abs((execution.execution_time - fill.when).total_seconds())
            if gap > _CROSS_SOURCE_TIME_TOLERANCE_S:
                pick.warn(
                    W_AUDIT_REPORT_DISAGREE,
                    f"order {fill.order_id}: trade {execution.trade_id} executed {gap:.3f} s "
                    "away from the audit fill time",
                )


# --- Entry tiers (§4.3) -------------------------------------------------------


def _tier_index_from_crid(crid: str, prefix: str) -> int | None:
    if not crid.startswith(prefix):
        return None
    tail = crid[len(prefix) :]
    return int(tail) if tail.isdigit() else None


def _entry_trail_lines_for(
    pick: _Pick, records: Sequence[Mapping[str, Any]]
) -> dict[str, list[Mapping[str, Any]]]:
    by_crid: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    crids = {
        str(r.get("crid"))
        for r in records
        if r.get("kind") == "watch_open" and str(r.get("pick_key", "")) == pick.key
    }
    for record in records:
        crid = record.get("crid")
        if not isinstance(crid, str):
            continue
        if crid in crids or _tier_index_from_crid(crid, pick.crid_prefix) is not None:
            by_crid[crid].append(record)
    return by_crid


def _absorb_watch_open(pick: _Pick, watch: Mapping[str, Any]) -> None:
    pick.uic = pick.uic or _int_or_none(watch.get("uic"))
    pick.exchange_mic = pick.exchange_mic or _str_or_none(watch.get("exchange_mic"))
    pick.instrument_currency = pick.instrument_currency or _str_or_none(
        watch.get("instrument_currency")
    )
    pick.sizing_currency = pick.sizing_currency or _str_or_none(watch.get("sizing_currency"))
    if "rate" not in pick.sizing_fx and _finite(watch.get("fx_rate")) is not None:
        pick.sizing_fx["rate"] = _num(
            _finite(watch.get("fx_rate")), None, SOURCE_ENTRY_WATCH, "line:entry_trails:watch_open"
        )


def _int_or_none(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _str_or_none(value: Any) -> str | None:
    return None if value in (None, "") else str(value)


def _build_trail_tiers(
    pick: _Pick,
    journals: _Journals,
    venue: _Venue | None,
    *,
    offline: bool,
) -> None:
    records = journals.records["entry_trails"]
    by_crid = _entry_trail_lines_for(pick, records)
    duplicated_crids = {
        str(r.get("crid")) for r in journals.duplicated["entry_trails"] if r.get("crid")
    }
    for crid in sorted(by_crid, key=lambda c: _tier_index_from_crid(c, pick.crid_prefix) or 0):
        lines = by_crid[crid]
        state = fold_entry_trail_records(lines).tiers.get(crid)
        watch = state.watch_open if state is not None else None
        if watch is not None:
            _absorb_watch_open(pick, watch)
        tier_index = _int_or_none((watch or {}).get("tier_index"))
        if tier_index is None:
            tier_index = _tier_index_from_crid(crid, pick.crid_prefix) or 0
        fired = [r for r in lines if r.get("kind") == "fired"]
        touched = [r for r in lines if r.get("kind") == "touched"]
        if offline and crid in duplicated_crids:
            pick.warn(
                W_FOLD_ORDER_UNCERTAIN,
                f"{crid}: a line is repeated in one journal file, so history dedup "
                "may have collapsed a re-arm and the offline fold order is uncertain",
            )
        currency = pick.instrument_currency
        line_ref = "line:entry_trails:watch_open"
        tier = _Tier(
            tier_index=tier_index,
            path=PATH_TRAIL_WATCH,
            crid=crid,
            planned_limit=_num(
                _as_float((watch or {}).get("limit")),
                _price_unit(currency),
                SOURCE_ENTRY_WATCH,
                line_ref,
            ),
            planned_qty=_num(
                _as_float((watch or {}).get("qty")), _UNIT_SHARES, SOURCE_ENTRY_WATCH, line_ref
            ),
            window_end=_time(
                parse_utc((watch or {}).get("window_end")), SOURCE_ENTRY_WATCH, line_ref
            ),
            touched_at=_time(
                parse_utc(touched[-1].get("touch_ts")) if touched else None,
                SOURCE_ENTRY_WATCH,
                "line:entry_trails:touched",
            ),
            touch_price=_num(
                _finite(touched[-1].get("touch_price")) if touched else None,
                _price_unit(currency),
                SOURCE_ENTRY_WATCH,
                "line:entry_trails:touched",
            ),
            trigger_order_id=None,
            armed_at=_null(NULL_OFFLINE, None, SOURCE_AUDIT),
            terminal=TERMINAL_OPEN,
            terminal_at=_null(NULL_NOT_JOURNALED, None, SOURCE_ENTRY_WATCH),
            terminal_at_source=None,
            fill=None,
        )
        journal_order_ids = [str(r["order_id"]) for r in fired if r.get("order_id")]
        if state is not None and state.armed_order_id:
            journal_order_ids.append(state.armed_order_id)
        if venue is None:
            _offline_tier(pick, tier, state, fired, journal_order_ids)
        else:
            _broker_tier(pick, tier, state, fired, journal_order_ids, venue)
        pick.tiers.append(tier)


def _terminal_from_fold(kind: str | None) -> str:
    return {
        "fired": TERMINAL_FILLED,
        "expired": TERMINAL_EXPIRED,
        "cancelled": TERMINAL_CANCELLED,
        "suspended": TERMINAL_SUSPENDED,
    }.get(kind or "", TERMINAL_OPEN)


def _operator_disarm_time(pick: _Pick, tier: _Tier) -> None:
    if pick.status == STATUS_DISARMED and tier.terminal == TERMINAL_CANCELLED:
        tier.terminal_at = _time(
            parse_utc(pick.status_record.get("disarmed_ts")),
            SOURCE_PICK_QUEUE,
            "line:picks:disarmed",
        )
        tier.terminal_at_source = SOURCE_PICK_QUEUE


def _offline_tier(
    pick: _Pick,
    tier: _Tier,
    state: Any,
    fired: list[Mapping[str, Any]],
    journal_order_ids: list[str],
) -> None:
    tier.trigger_order_id = journal_order_ids[-1] if journal_order_ids else None
    tier.terminal = _terminal_from_fold(state.terminal_kind if state is not None else None)
    if tier.terminal == TERMINAL_CANCELLED:
        _operator_disarm_time(pick, tier)
    if not fired:
        return
    line = fired[-1]
    detected = parse_utc(line.get("ts"))
    venue_activity = parse_utc(line.get("venue_activity_time"))
    ref = "line:entry_trails:fired"
    currency = pick.instrument_currency
    tier.terminal_at = _time(venue_activity or detected, SOURCE_ENTRY_WATCH, ref)
    tier.terminal_at_source = SOURCE_ENTRY_WATCH
    fill = _Fill(
        order_id=_str_or_none(line.get("order_id")),
        external_reference=None,
        uic=pick.uic,
        venue_time=_time(venue_activity or detected, SOURCE_ENTRY_WATCH, ref),
        price=_num(
            _as_float(line.get("avg_price")), _price_unit(currency), SOURCE_ENTRY_WATCH, ref
        ),
        qty=_num(_as_float(line.get("realized_qty")), _UNIT_SHARES, SOURCE_ENTRY_WATCH, ref),
        detected_at=_time(detected, SOURCE_ENTRY_WATCH, ref),
        when=venue_activity or detected,
        filled_qty=_finite(line.get("realized_qty")),
    )
    _null_report_facts(fill, NULL_OFFLINE, NULL_FX_NOT_REALIZED)
    tier.fill = fill


def _broker_tier(
    pick: _Pick,
    tier: _Tier,
    state: Any,
    fired: list[Mapping[str, Any]],
    journal_order_ids: list[str],
    venue: _Venue,
) -> None:
    fire_ref = f"{tier.crid}-fire"
    candidates = {
        order.order_id: order
        for order in venue.orders.values()
        if order.reference in (fire_ref, tier.crid)
    }
    for order_id in journal_order_ids:
        if order_id in venue.orders:
            candidates[order_id] = venue.orders[order_id]
    filled = [o for o in candidates.values() if o.final_fill is not None or o.partial_fills]
    far_past = dt.datetime.min.replace(tzinfo=dt.UTC)
    if filled:
        trigger = filled[0]
    elif journal_order_ids and journal_order_ids[-1] in venue.orders:
        trigger = venue.orders[journal_order_ids[-1]]
    elif candidates:
        trigger = max(candidates.values(), key=lambda o: o.rows[-1].activity_time or far_past)
    else:
        trigger = None
    if trigger is None:
        tier.trigger_order_id = journal_order_ids[-1] if journal_order_ids else None
        tier.armed_at = (
            _null(NULL_NO_AUDIT_ROW, None, SOURCE_AUDIT, f"order:{tier.trigger_order_id}")
            if tier.trigger_order_id
            else _null(NULL_NOT_JOURNALED, None, SOURCE_AUDIT)
        )
        tier.terminal = _terminal_from_fold(state.terminal_kind if state is not None else None)
        if tier.terminal == TERMINAL_CANCELLED:
            _operator_disarm_time(pick, tier)
        if tier.terminal == TERMINAL_FILLED:
            # The journal says it filled and the audit window has no row.
            _offline_tier(pick, tier, state, fired, journal_order_ids)
            if tier.fill is not None:
                _null_report_facts(tier.fill, NULL_NO_AUDIT_ROW, NULL_FX_NOT_REALIZED)
        return
    tier.trigger_order_id = trigger.order_id
    placed = trigger.placed
    order_ref = f"order:{trigger.order_id}"
    tier.armed_at = (
        _time(placed.activity_time, SOURCE_AUDIT, order_ref)
        if placed is not None
        else _null(NULL_NO_AUDIT_ROW, None, SOURCE_AUDIT, order_ref)
    )
    terminal_row = trigger.terminal_row()
    if terminal_row is None and trigger.partial_fills:
        terminal_row = trigger.partial_fills[-1]
    if terminal_row is None:
        tier.terminal = TERMINAL_OPEN
    else:
        tier.terminal = {
            _STATUS_FINAL_FILL: TERMINAL_FILLED,
            _STATUS_FILL: TERMINAL_FILLED,
            _STATUS_CANCELLED: TERMINAL_CANCELLED,
            _STATUS_EXPIRED: TERMINAL_EXPIRED,
        }[terminal_row.status]
        tier.terminal_at = _time(terminal_row.activity_time, SOURCE_AUDIT, order_ref)
        tier.terminal_at_source = SOURCE_AUDIT
    fill = _venue_fill(trigger, venue, pick, currency=pick.instrument_currency)
    if fill is None:
        return
    pick.uic = pick.uic or fill.uic
    if fired:
        line = fired[-1]
        fill.detected_at = _time(
            parse_utc(line.get("ts")), SOURCE_ENTRY_WATCH, "line:entry_trails:fired"
        )
        _cross_check_journal_fill(pick, fill, line, venue)
    tier.fill = fill


def _cross_check_journal_fill(
    pick: _Pick, fill: _Fill, line: Mapping[str, Any], venue: _Venue
) -> None:
    journal_price = _finite(line.get("avg_price"))
    journal_qty = _finite(line.get("realized_qty"))
    price = fill.price.value
    tick = venue.tick(fill.uic, price)
    if (
        journal_qty is not None
        and fill.filled_qty is not None
        and abs(journal_qty - fill.filled_qty) > _QTY_EPS
    ):
        pick.warn(
            W_JOURNAL_AUDIT_DISAGREE,
            f"order {fill.order_id}: journal qty {journal_qty:g}, audit {fill.filled_qty:g}",
        )
    if (
        journal_price is not None
        and price is not None
        and tick is not None
        and abs(journal_price - price) > tick + _FLOAT_TOLERANCE
    ):
        pick.warn(
            W_JOURNAL_AUDIT_DISAGREE,
            f"order {fill.order_id}: journal price {journal_price:g}, audit {price:g}",
        )


def _build_bracket_tiers(pick: _Pick, venue: _Venue | None) -> None:
    seen: set[str] = set()
    next_index = 0
    for record in pick.submissions:
        brackets = record.get("brackets")
        if not isinstance(brackets, list):
            continue
        for bracket in brackets:
            if not isinstance(bracket, Mapping):
                continue
            order_id = _str_or_none(bracket.get("entry_order_id"))
            crid = _str_or_none(bracket.get("client_request_id")) or (order_id or "")
            if crid in seen:
                continue
            seen.add(crid)
            ref = f"line:submissions:{crid}"
            tier = _Tier(
                tier_index=next_index,
                path=PATH_NOW_BRACKET,
                crid=crid,
                planned_limit=_num(
                    _finite(bracket.get("entry")),
                    _price_unit(pick.instrument_currency),
                    SOURCE_SUBMISSIONS,
                    ref,
                ),
                planned_qty=_num(
                    _finite(bracket.get("qty")), _UNIT_SHARES, SOURCE_SUBMISSIONS, ref
                ),
                window_end=_null(NULL_NOT_JOURNALED, None, SOURCE_SUBMISSIONS, ref),
                touched_at=_null(NULL_NOT_JOURNALED, None, SOURCE_ENTRY_WATCH),
                touch_price=_null(NULL_NOT_JOURNALED, None, SOURCE_ENTRY_WATCH),
                trigger_order_id=order_id,
                armed_at=_null(NULL_OFFLINE, None, SOURCE_AUDIT),
                terminal=TERMINAL_OPEN,
                terminal_at=_null(NULL_NOT_JOURNALED, None, SOURCE_SUBMISSIONS),
                terminal_at_source=None,
                fill=None,
            )
            next_index += 1
            if venue is not None and order_id is not None:
                order = venue.orders.get(order_id)
                if order is None:
                    tier.armed_at = _null(
                        NULL_NO_AUDIT_ROW, None, SOURCE_AUDIT, f"order:{order_id}"
                    )
                else:
                    placed = order.placed
                    if placed is not None:
                        tier.armed_at = _time(
                            placed.activity_time, SOURCE_AUDIT, f"order:{order_id}"
                        )
                    terminal_row = order.terminal_row()
                    if terminal_row is not None:
                        tier.terminal = {
                            _STATUS_FINAL_FILL: TERMINAL_FILLED,
                            _STATUS_CANCELLED: TERMINAL_CANCELLED,
                            _STATUS_EXPIRED: TERMINAL_EXPIRED,
                        }[terminal_row.status]
                        tier.terminal_at = _time(
                            terminal_row.activity_time, SOURCE_AUDIT, f"order:{order_id}"
                        )
                        tier.terminal_at_source = SOURCE_AUDIT
                    tier.fill = _venue_fill(order, venue, pick, currency=pick.instrument_currency)
                    if tier.fill is not None:
                        pick.uic = pick.uic or tier.fill.uic
            pick.tiers.append(tier)


def _audit_found_tiers(pick: _Pick, venue: _Venue) -> None:
    """An entry fill whose ref names this pick and no journal tier (§4.3 item 3)."""
    known = {tier.crid for tier in pick.tiers}
    known_orders = {tier.trigger_order_id for tier in pick.tiers if tier.trigger_order_id}
    for order in venue.orders.values():
        reference = order.reference or ""
        if not reference.endswith("-fire") or order.order_id in known_orders:
            continue
        crid = reference[: -len("-fire")]
        index = _tier_index_from_crid(crid, pick.crid_prefix)
        if index is None or crid in known or order.buy_sell != _BUY:
            continue
        fill = _venue_fill(order, venue, pick, currency=pick.instrument_currency)
        if fill is None:
            continue
        known.add(crid)
        pick.uic = pick.uic or fill.uic
        placed = order.placed
        terminal_row = order.terminal_row()
        pick.tiers.append(
            _Tier(
                tier_index=index,
                path=PATH_TRAIL_WATCH,
                crid=crid,
                planned_limit=_null(NULL_NOT_JOURNALED, None, SOURCE_ENTRY_WATCH),
                planned_qty=_null(NULL_NOT_JOURNALED, _UNIT_SHARES, SOURCE_ENTRY_WATCH),
                window_end=_null(NULL_NOT_JOURNALED, None, SOURCE_ENTRY_WATCH),
                touched_at=_null(NULL_NOT_JOURNALED, None, SOURCE_ENTRY_WATCH),
                touch_price=_null(NULL_NOT_JOURNALED, None, SOURCE_ENTRY_WATCH),
                trigger_order_id=order.order_id,
                armed_at=_time(
                    placed.activity_time if placed else None,
                    SOURCE_AUDIT,
                    f"order:{order.order_id}",
                ),
                terminal=TERMINAL_FILLED,
                terminal_at=_time(
                    terminal_row.activity_time if terminal_row else fill.when,
                    SOURCE_AUDIT,
                    f"order:{order.order_id}",
                ),
                terminal_at_source=SOURCE_AUDIT,
                fill=fill,
            )
        )
        pick.warn(
            W_ENTRY_NOT_IN_JOURNAL,
            f"order {order.order_id} ({reference}) filled with no entry-watch line for {crid}",
        )


# --- Pick context from submissions and stops ----------------------------------


def _attach_submissions(picks: Sequence[_Pick], records: Sequence[Mapping[str, Any]]) -> None:
    by_key: dict[tuple[str, str], list[_Pick]] = defaultdict(list)
    for pick in picks:
        by_key[(pick.ticker, pick.token)].append(pick)
    for record in records:
        key = _submission_join_key(record)
        if key is None:
            continue
        for pick in by_key.get(key, []):
            pick.submissions.append(record)
            pick.uic = pick.uic or _int_or_none(record.get("uic"))
            pick.exchange_mic = pick.exchange_mic or _str_or_none(record.get("mic"))
            pick.instrument_currency = pick.instrument_currency or _str_or_none(
                record.get("instrument_currency")
            )
            pick.sizing_currency = pick.sizing_currency or _str_or_none(
                record.get("sizing_currency")
            )
            ref = "line:submissions:fx_rate"
            for name, key_name in (
                ("rate", "fx_rate"),
                ("bid", "fx_rate_bid"),
                ("ask", "fx_rate_ask"),
            ):
                if name not in pick.sizing_fx and _finite(record.get(key_name)) is not None:
                    pick.sizing_fx[name] = _num(
                        _finite(record.get(key_name)), None, SOURCE_SUBMISSIONS, ref
                    )
            if "asof" not in pick.sizing_fx and parse_utc(record.get("fx_rate_asof")):
                pick.sizing_fx["asof"] = _time(
                    parse_utc(record.get("fx_rate_asof")), SOURCE_SUBMISSIONS, ref
                )
            if "source" not in pick.sizing_fx and record.get("fx_rate_source"):
                pick.sizing_fx["source"] = Measured(
                    str(record["fx_rate_source"]), None, SOURCE_SUBMISSIONS, ref
                )


# --- Stop journal facts -------------------------------------------------------


@dataclass
class _StopFacts:
    """The standalone-stop journal, sorted into what the builder looks up."""

    placed_by_order: dict[str, Mapping[str, Any]]
    placed_by_ref: dict[str, list[Mapping[str, Any]]]
    stop_fills: list[Mapping[str, Any]]
    markers_by_uic: dict[int, list[Mapping[str, Any]]]
    tranche_fired: list[tuple[Mapping[str, Any], str | None]]
    planned_by_pick: dict[str, Mapping[str, Any]]


def _read_stop_facts(records: Sequence[Mapping[str, Any]]) -> _StopFacts:
    placed_by_order: dict[str, Mapping[str, Any]] = {}
    placed_by_ref: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    stop_fills: list[Mapping[str, Any]] = []
    markers: dict[int, list[Mapping[str, Any]]] = defaultdict(list)
    tranche_fired: list[tuple[Mapping[str, Any], str | None]] = []
    planned_by_pick: dict[str, Mapping[str, Any]] = {}
    governing: dict[int, str | None] = {}
    for record in records:
        kind = record.get("kind")
        uic = _int_or_none(record.get("uic"))
        if kind == "stop_placed":
            if record.get("order_id"):
                placed_by_order[str(record["order_id"])] = record
            if record.get("ref"):
                placed_by_ref[str(record["ref"])].append(record)
        elif kind == "stop_filled":
            stop_fills.append(record)
        elif kind in ("trailed", "reanchored") and uic is not None:
            markers[uic].append(record)
        elif kind == "tranche_plan" and uic is not None:
            key = record.get("pick_key")
            governing[uic] = None if key is None else str(key)
        elif kind == "tranche_plan_retracted" and uic is not None:
            governing.pop(uic, None)
        elif kind == "tranche_fired":
            tranche_fired.append((record, governing.get(uic) if uic is not None else None))
        elif kind == "planned" and record.get("pick_key"):
            planned_by_pick.setdefault(str(record["pick_key"]), record)
    return _StopFacts(
        placed_by_order=placed_by_order,
        placed_by_ref=dict(placed_by_ref),
        stop_fills=stop_fills,
        markers_by_uic=dict(markers),
        tranche_fired=tranche_fired,
        planned_by_pick=planned_by_pick,
    )


# --- Exits: ownership and reasons (§4.4) --------------------------------------


@dataclass
class _Ownership:
    owner: _Pick | None
    attribution: str | None
    kind: str  # "stop" | "tp" | "manual" | "unknown"
    tp_label: str | None = None
    evidence: list[str] = field(default_factory=list)


def _owner_by_key(picks_by_key: Mapping[str, _Pick], key: str | None) -> _Pick | None:
    return None if key is None else picks_by_key.get(key)


def _resolve_ownership(
    order: _Order,
    *,
    picks_by_key: Mapping[str, _Pick],
    picks_on_uic: Sequence[_Pick],
    tp_tie: Mapping[str, str | None],
    bracket_children: Mapping[str, _Pick],
    bracket_requests: Mapping[str, _Pick],
) -> _Ownership:
    reference = order.reference
    if order.order_id in bracket_children:
        kind = "stop" if (order.placed_order_type or "") in _STOP_ORDER_TYPES else "tp"
        return _Ownership(bracket_children[order.order_id], ATTRIBUTION_JOURNAL_TIE, kind)
    if not reference:
        return _Ownership(None, None, "manual", evidence=["no_external_reference"])
    if _STOP_REF_RE.search(reference):
        owner = _owner_by_key(picks_by_key, _pick_key_from_stop_ref(reference))
        if owner is not None:
            return _Ownership(owner, ATTRIBUTION_EXTERNAL_REFERENCE, "stop")
    bracket = _BRACKET_STOP_REF_RE.match(reference)
    if bracket and bracket.group("request") in bracket_requests:
        return _Ownership(
            bracket_requests[bracket.group("request")], ATTRIBUTION_EXTERNAL_REFERENCE, "stop"
        )
    oco = _OCO_LEG_RE.match(reference)
    if oco:
        request = oco.group("request")
        owner = bracket_requests.get(request) or next(
            (
                p
                for p in picks_on_uic
                if request.startswith(p.crid_prefix)
                or any(request.startswith(t.crid) for t in p.tiers)
            ),
            None,
        )
        if owner is not None:
            kind = "stop" if oco.group("leg") == "stop" else "tp"
            return _Ownership(owner, ATTRIBUTION_EXTERNAL_REFERENCE, kind)
    uic_tp = _UIC_TP_REF_RE.match(reference)
    tp_match = _TP_REF_RE.search(reference)
    if uic_tp and tp_match and _int_or_none(uic_tp.group("uic")) == order.uic:
        label = tp_label_from_tag(f"tp{tp_match.group(1)}")
        tied = _owner_by_key(picks_by_key, tp_tie.get(order.order_id))
        if tied is not None:
            return _Ownership(
                tied,
                ATTRIBUTION_JOURNAL_TIE,
                "tp",
                tp_label=label,
                evidence=[f"keeper:tranche_fired sell_order_id={order.order_id}"],
            )
        # The uic rule is applied at allocation time, when open lots are known.
        return _Ownership(
            None, ATTRIBUTION_EXTERNAL_REFERENCE, "tp", tp_label=label, evidence=["uic_rule"]
        )
    return _Ownership(None, None, "unknown", evidence=[f"unclaimed_reference:{reference}"])


def _price_changes(order: _Order) -> list[OrderActivity]:
    """``Changed`` rows whose price differs from this order's previous price."""
    placed = order.placed
    previous = placed.price if placed is not None else None
    changes: list[OrderActivity] = []
    for row in order.rows:
        if row.status != _STATUS_CHANGED or row.price is None:
            continue
        if previous is None or abs(row.price - previous) > _FLOAT_TOLERANCE:
            changes.append(row)
        previous = row.price
    return changes


def _stop_reason(
    order: _Order, fill: _Fill, stop_facts: _StopFacts, venue: _Venue, currency: str | None
) -> tuple[str, list[str], Measured]:
    placed = order.placed
    order_ref = f"order:{order.order_id}"
    changes = _price_changes(order)
    if not changes:
        level = placed.price if placed is not None else None
        evidence = [f"audit:Placed {level:g}"] if level is not None else []
        return (
            REASON_DISASTER_STOP,
            evidence,
            _num(level, _price_unit(currency), SOURCE_AUDIT, order_ref),
        )
    last = changes[-1]
    level = last.price
    evidence = [
        f"audit:Changed {row.price:g} @{format_time(row.activity_time)}"
        for row in changes
        if row.price is not None and row.activity_time is not None
    ]
    stop_level = _num(level, _price_unit(currency), SOURCE_AUDIT, order_ref)
    start = placed.activity_time if placed is not None else None
    end = fill.when
    tick = venue.tick(order.uic, level)
    if level is None or tick is None or start is None or end is None or order.uic is None:
        return REASON_STOP_MOVED_UNKNOWN, evidence, stop_level
    for marker_kind, field_name, reason in (
        ("trailed", "level", REASON_TRAILED_STOP),
        ("reanchored", "stop_price", REASON_REANCHORED_STOP),
    ):
        for marker in stop_facts.markers_by_uic.get(order.uic, []):
            if marker.get("kind") != marker_kind:
                continue
            when = parse_utc(marker.get("ts"))
            value = _finite(marker.get(field_name))
            if when is None or value is None or not (start <= when <= end):
                continue
            if abs(value - level) <= tick + _FLOAT_TOLERANCE:
                return reason, [*evidence, f"keeper:{marker_kind} {value:g}"], stop_level
    return REASON_STOP_MOVED_UNKNOWN, evidence, stop_level


@dataclass
class _ClosingEvent:
    fill: _Fill
    order: _Order | None
    ownership: _Ownership
    reason: str | None
    reason_null_reason: str | None
    evidence: list[str]
    stop_level: Measured


def _rejected_attempt_evidence(
    order: _Order, venue: _Venue, since: dt.datetime | None
) -> list[str]:
    """Rejected no-reference orders on the same uic and side before this fill."""
    evidence: list[str] = []
    fill_time = order.final_fill.activity_time if order.final_fill else None
    for other in venue.orders.values():
        if other.order_id == order.order_id or other.uic != order.uic or other.reference:
            continue
        if not other.rejected or other.buy_sell != order.buy_sell:
            continue
        when = other.rows[0].activity_time
        if when is None or (fill_time is not None and when > fill_time):
            continue
        if since is not None and when < since:
            continue
        text = f"rejected:{other.order_id}"
        if other.related_position_id:
            text += f" RelatedPositionId={other.related_position_id}"
        evidence.append(text)
    return evidence


# --- Attribution (§4.5) -------------------------------------------------------


@dataclass
class _Lot:
    pick: _Pick
    when: dt.datetime | None
    remaining: float
    position_id: str | None


def _make_exit(
    event: _ClosingEvent, qty: float | None, attribution: str | None, null_reason: str | None
) -> _Exit:
    ref = f"order:{event.fill.order_id}" if event.fill.order_id else None
    attributed = (
        _null(null_reason or NULL_AMBIGUOUS, _UNIT_SHARES, SOURCE_DERIVED, ref)
        if qty is None
        else _num(qty, _UNIT_SHARES, SOURCE_DERIVED, ref)
    )
    return _Exit(
        fill=event.fill,
        reason=event.reason,
        reason_null_reason=event.reason_null_reason,
        evidence=list(event.evidence),
        stop_level_at_fill=event.stop_level,
        tp_label=event.ownership.tp_label,
        attributed_qty=attributed,
        attribution=attribution,
    )


def _allocate(
    events: Sequence[_ClosingEvent],
    lots: list[_Lot],
    *,
    offline: bool,
    unattributed: list[dict[str, Any]],
) -> None:
    """Give each closing fill to picks by the rules of §4.5, in venue-time order."""
    far_past = dt.datetime.min.replace(tzinfo=dt.UTC)
    for event in sorted(events, key=lambda e: e.fill.when or far_past):
        total = event.fill.filled_qty
        if total is None:
            continue
        when = event.fill.when
        open_lots = [
            lot
            for lot in lots
            if lot.remaining > _QTY_EPS and (when is None or lot.when is None or lot.when <= when)
        ]
        remaining = total
        owner = event.ownership.owner

        def take(lot_list: list[_Lot], qty: float) -> float:
            given = 0.0
            for lot in lot_list:
                if qty - given <= _QTY_EPS:
                    break
                part = min(lot.remaining, qty - given)
                lot.remaining -= part
                given += part
            return given

        if owner is not None:
            owned = [lot for lot in open_lots if lot.pick is owner]
            given = take(owned, remaining)
            if given > _QTY_EPS:
                owner.exits.append(_make_exit(event, given, event.ownership.attribution, None))
            remaining -= given
        if remaining > _QTY_EPS and owner is None and event.fill.related_position_id:
            linked = [lot for lot in open_lots if lot.position_id == event.fill.related_position_id]
            linked_picks = {id(lot.pick) for lot in linked}
            if len(linked_picks) == 1:
                pick = linked[0].pick
                given = take([lot for lot in open_lots if lot.pick is pick], remaining)
                if given > _QTY_EPS:
                    pick.exits.append(_make_exit(event, given, ATTRIBUTION_POSITION_LINK, None))
                remaining -= given
            elif not linked_picks:
                event.evidence.append(
                    f"related_position_unmatched:{event.fill.related_position_id}"
                )
        if remaining > _QTY_EPS and owner is None and event.ownership.kind == "tp":
            # The uic rule: the single pick with an open lot takes it (§4.4).
            candidates = {id(lot.pick): lot.pick for lot in open_lots if lot.remaining > _QTY_EPS}
            if len(candidates) == 1:
                (pick,) = candidates.values()
                given = take([lot for lot in open_lots if lot.pick is pick], remaining)
                if given > _QTY_EPS:
                    pick.exits.append(
                        _make_exit(event, given, ATTRIBUTION_EXTERNAL_REFERENCE, None)
                    )
                remaining -= given
            elif len(candidates) > 1:
                for pick in candidates.values():
                    pick.ambiguous = True
                    pick.exits.append(_make_exit(event, None, None, NULL_AMBIGUOUS))
                    pick.warn(
                        W_AMBIGUOUS,
                        f"order {event.fill.order_id}: take-profit on uic {event.fill.uic} "
                        "with open lots of several picks",
                    )
                continue
        if remaining > _QTY_EPS:
            candidates = {id(lot.pick): lot.pick for lot in open_lots if lot.remaining > _QTY_EPS}
            if offline and len(candidates) > 1:
                for pick in candidates.values():
                    pick.ambiguous = True
                    pick.exits.append(_make_exit(event, None, None, NULL_AMBIGUOUS))
                    pick.warn(
                        W_AMBIGUOUS,
                        f"order {event.fill.order_id}: offline, no venue times to order the "
                        "lots of several picks",
                    )
                continue
            fifo_lots = sorted(
                (lot for lot in open_lots if lot.remaining > _QTY_EPS),
                key=lambda lot: lot.when or far_past,
            )
            for lot in fifo_lots:
                if remaining <= _QTY_EPS:
                    break
                part = min(lot.remaining, remaining)
                lot.remaining -= part
                remaining -= part
                lot.pick.exits.append(_make_exit(event, part, ATTRIBUTION_FIFO, None))
                lot.pick.warn(
                    W_FIFO_ASSUMED,
                    f"order {event.fill.order_id}: {part:g} share(s) given by FIFO over "
                    "open lots (an assumption about end-of-day netting, not a measured fact)",
                )
        if remaining > _QTY_EPS:
            if owner is not None:
                owner.warn(
                    W_EXIT_QTY_EXCEEDS_PICK,
                    f"order {event.fill.order_id} closed {total:g}; {remaining:g} share(s) "
                    "exceed every open lot and are listed as unattributed",
                )
            entry = event.fill.to_dict()
            entry.update(
                {
                    "reason": event.reason,
                    "reason_null_reason": event.reason_null_reason,
                    "reason_evidence": list(event.evidence),
                    "unattributed_qty": _num(
                        remaining, _UNIT_SHARES, SOURCE_DERIVED, f"order:{event.fill.order_id}"
                    ).to_dict(),
                }
            )
            unattributed.append(entry)


# --- Broker-mode exits --------------------------------------------------------


def _broker_closing_events(
    uic: int,
    picks_on_uic: Sequence[_Pick],
    picks_by_key: Mapping[str, _Pick],
    venue: _Venue,
    stop_facts: _StopFacts,
    unattributed: list[dict[str, Any]],
    *,
    window_start: dt.datetime | None,
) -> list[_ClosingEvent]:
    tp_tie = {
        str(record["telemetry"]["sell_order_id"]): governing
        for record, governing in stop_facts.tranche_fired
        if isinstance(record.get("telemetry"), Mapping) and record["telemetry"].get("sell_order_id")
    }
    bracket_children: dict[str, _Pick] = {}
    bracket_requests: dict[str, _Pick] = {}
    for pick in picks_on_uic:
        for record in pick.submissions:
            for bracket in record.get("brackets") or []:
                if not isinstance(bracket, Mapping):
                    continue
                crid = _str_or_none(bracket.get("client_request_id"))
                if crid:
                    bracket_requests[crid] = pick
                for child in bracket.get("exit_order_ids") or []:
                    bracket_children[str(child)] = pick
    entry_orders = {
        tier.trigger_order_id for pick in picks_on_uic for tier in pick.tiers if tier.fill
    }
    currency = next((p.instrument_currency for p in picks_on_uic if p.instrument_currency), None)
    events: list[_ClosingEvent] = []
    for order in venue.orders.values():
        if order.uic != uic or order.order_id in entry_orders:
            continue
        fill_row = order.final_fill or (order.partial_fills[-1] if order.partial_fills else None)
        if fill_row is None or fill_row.activity_time is None:
            continue
        if window_start is not None and fill_row.activity_time < window_start:
            continue
        fill = _venue_fill(order, venue, None, currency=currency)
        if fill is None:
            continue
        ownership = _resolve_ownership(
            order,
            picks_by_key=picks_by_key,
            picks_on_uic=picks_on_uic,
            tp_tie=tp_tie,
            bracket_children=bracket_children,
            bracket_requests=bracket_requests,
        )
        # Report-row facts were attached without a pick; re-attach so the
        # warnings land on the owner (or on every pick of the uic if unowned).
        target = ownership.owner
        if target is not None:
            _attach_report_facts(
                fill,
                venue.executions_by_order.get(order.order_id, []),
                venue,
                target,
                currency=currency,
            )
        if order.buy_sell == _BUY:
            # An opening fill no pick owns (UBER's manual Buy 4 @69.55).
            entry = fill.to_dict()
            entry.update(
                {
                    "reason": REASON_MANUAL_OPEN if not order.reference else REASON_UNKNOWN,
                    "reason_null_reason": None,
                    "reason_evidence": ownership.evidence,
                    "unattributed_qty": _num(
                        fill.filled_qty, _UNIT_SHARES, SOURCE_DERIVED, f"order:{order.order_id}"
                    ).to_dict(),
                }
            )
            unattributed.append(entry)
            continue
        reason: str | None
        evidence = list(ownership.evidence)
        stop_level = _null(NULL_NOT_JOURNALED, _price_unit(currency), SOURCE_AUDIT)
        if ownership.kind == "tp":
            reason = REASON_TAKE_PROFIT
        elif ownership.kind == "stop":
            reason, stop_evidence, stop_level = _stop_reason(
                order, fill, stop_facts, venue, currency
            )
            evidence.extend(stop_evidence)
        elif ownership.kind == "manual":
            reason = REASON_MANUAL_CLOSE
            evidence.extend(_rejected_attempt_evidence(order, venue, window_start))
        else:
            reason = REASON_UNKNOWN
        events.append(
            _ClosingEvent(
                fill=fill,
                order=order,
                ownership=ownership,
                reason=reason,
                reason_null_reason=None,
                evidence=evidence,
                stop_level=stop_level,
            )
        )
    return events


# --- Offline exits ------------------------------------------------------------


def _offline_closing_events(
    uic: int,
    picks_on_uic: Sequence[_Pick],
    picks_by_key: Mapping[str, _Pick],
    stop_facts: _StopFacts,
) -> list[_ClosingEvent]:
    events: list[_ClosingEvent] = []
    currency = next((p.instrument_currency for p in picks_on_uic if p.instrument_currency), None)
    for line in stop_facts.stop_fills:
        if _int_or_none(line.get("uic")) != uic:
            continue
        order_id = _str_or_none(line.get("order_id"))
        reference = _str_or_none(line.get("ref"))
        placed = stop_facts.placed_by_order.get(order_id or "")
        if reference is None and placed is not None:
            reference = _str_or_none(placed.get("ref"))
        # Either reference form names the owner: the crid form through the
        # pick key, the bracket form through the pick's own submission (§4.4).
        owner = _owner_by_key(picks_by_key, _pick_key_from_stop_ref(reference)) or _stop_ref_owner(
            reference, picks_on_uic
        )
        detected = parse_utc(line.get("ts"))
        ref = "line:standalone_stops:stop_filled"
        qty = _as_float(line.get("qty"))
        fill = _Fill(
            order_id=order_id,
            external_reference=reference,
            uic=uic,
            venue_time=_time(detected, SOURCE_STOP_JOURNAL, ref),
            price=_num(
                _as_float(line.get("avg_price")), _price_unit(currency), SOURCE_STOP_JOURNAL, ref
            ),
            qty=_num(qty, _UNIT_SHARES, SOURCE_STOP_JOURNAL, ref),
            detected_at=_time(detected, SOURCE_STOP_JOURNAL, ref),
            when=detected,
            filled_qty=_finite(qty),
            closing=True,
        )
        _null_report_facts(fill, NULL_OFFLINE, NULL_FX_NOT_REALIZED)
        start = parse_utc(placed.get("ts")) if placed is not None else None
        reason: str | None = None
        reason_null: str | None = NULL_STOP_AMEND_UNAVAILABLE
        evidence: list[str] = []
        for marker in stop_facts.markers_by_uic.get(uic, []):
            when = parse_utc(marker.get("ts"))
            # Without the stop's own placement time the window is unknown, and
            # a marker of another stop generation could match: no reason then.
            if when is None or detected is None or start is None:
                continue
            if not (start <= when <= detected):
                continue
            kind = marker.get("kind")
            reason = REASON_TRAILED_STOP if kind == "trailed" else REASON_REANCHORED_STOP
            reason_null = None
            value = marker.get("level") if kind == "trailed" else marker.get("stop_price")
            evidence = [f"keeper:{kind} {value}"]
        if start is None:
            evidence.append("stop_placed_not_journaled")
        stop_level = (
            _num(_finite(placed.get("stop_price")), _price_unit(currency), SOURCE_STOP_JOURNAL)
            if placed is not None and reason is None
            else _null(NULL_STOP_AMEND_UNAVAILABLE, _price_unit(currency), SOURCE_STOP_JOURNAL)
        )
        events.append(
            _ClosingEvent(
                fill=fill,
                order=None,
                ownership=_Ownership(
                    owner, ATTRIBUTION_EXTERNAL_REFERENCE if owner else None, "stop"
                ),
                reason=reason,
                reason_null_reason=reason_null,
                evidence=evidence,
                stop_level=stop_level,
            )
        )
    for record, governing in stop_facts.tranche_fired:
        if _int_or_none(record.get("uic")) != uic:
            continue
        telemetry = record.get("telemetry") if isinstance(record.get("telemetry"), Mapping) else {}
        assert isinstance(telemetry, Mapping)
        owner = _owner_by_key(picks_by_key, governing)
        event_time = parse_utc(telemetry.get("event_time"))
        qty = _finite(telemetry.get("qty"))
        ref = "line:standalone_stops:tranche_fired"
        fill = _Fill(
            order_id=_str_or_none(telemetry.get("sell_order_id")),
            external_reference=None,
            uic=uic,
            venue_time=_time(event_time, SOURCE_STOP_JOURNAL, ref),
            price=_null(NULL_NOT_JOURNALED, _price_unit(currency), SOURCE_STOP_JOURNAL, ref),
            qty=_num(qty, _UNIT_SHARES, SOURCE_STOP_JOURNAL, ref),
            detected_at=_time(event_time, SOURCE_STOP_JOURNAL, ref),
            when=event_time,
            filled_qty=qty,
            closing=True,
        )
        _null_report_facts(fill, NULL_OFFLINE, NULL_FX_NOT_REALIZED)
        tag = record.get("tag")
        events.append(
            _ClosingEvent(
                fill=fill,
                order=None,
                ownership=_Ownership(
                    owner,
                    ATTRIBUTION_JOURNAL_TIE if owner else None,
                    "tp",
                    tp_label=tp_label_from_tag(str(tag)) if tag else None,
                ),
                reason=REASON_TAKE_PROFIT,
                reason_null_reason=None,
                evidence=["keeper:tranche_fired"],
                stop_level=_null(NULL_NOT_JOURNALED, _price_unit(currency), SOURCE_STOP_JOURNAL),
            )
        )
    return events


# --- Placed stop and plan stop ------------------------------------------------


def _bracket_request_ids(pick: _Pick) -> set[str]:
    """The submission request ids of the pick's own now-bracket tiers."""
    return {tier.crid for tier in pick.tiers if tier.path == PATH_NOW_BRACKET}


def _is_pick_stop_ref(reference: str | None, pick: _Pick) -> bool:
    """Whether a stop reference names this pick, in either of its two forms
    (§4.4): ``<crid>-entry-t<k>[-fire]-stop-<n>`` or the bracket form
    ``<uuid>-stop-<n>`` of one of the pick's own now-bracket tiers."""
    if not reference:
        return False
    if _STOP_REF_RE.search(reference):
        return _pick_key_from_stop_ref(reference) == pick.key
    bracket = _BRACKET_STOP_REF_RE.match(reference)
    return bracket is not None and bracket.group("request") in _bracket_request_ids(pick)


def _stop_ref_owner(reference: str | None, picks_on_uic: Sequence[_Pick]) -> _Pick | None:
    """The single pick on the uic that a stop reference names, or None."""
    owners = [pick for pick in picks_on_uic if _is_pick_stop_ref(reference, pick)]
    return owners[0] if len(owners) == 1 else None


def _placed_stop(pick: _Pick, venue: _Venue | None, stop_facts: _StopFacts, state: str) -> Measured:
    unit = _price_unit(pick.instrument_currency)
    if venue is not None:
        far_future = dt.datetime.max.replace(tzinfo=dt.UTC)
        stops = [
            order
            for order in venue.orders.values()
            if order.uic == pick.uic
            and _is_pick_stop_ref(order.reference, pick)
            and order.placed is not None
        ]
        if stops:
            first = min(stops, key=lambda o: o.placed.activity_time or far_future)  # type: ignore[union-attr]
            placed = first.placed
            assert placed is not None
            return _num(placed.price, unit, SOURCE_AUDIT, f"order:{first.order_id}")
    for ref, lines in stop_facts.placed_by_ref.items():
        if _is_pick_stop_ref(ref, pick) and lines:
            return _num(
                _finite(lines[0].get("stop_price")),
                unit,
                SOURCE_STOP_JOURNAL,
                "line:standalone_stops:stop_placed",
            )
    source = SOURCE_STOP_JOURNAL if venue is None else SOURCE_AUDIT
    if state == STATE_NEVER_FILLED:
        # No entry filled, so no stop order was ever placed for the pick.
        return _null(NULL_NEVER_FILLED, unit, source)
    if venue is None:
        return _null(NULL_NOT_JOURNALED, unit, source)
    return _null(NULL_NO_AUDIT_ROW, unit, source)


def _plan_disaster_stop(pick: _Pick, stop_facts: _StopFacts) -> Measured:
    unit = _price_unit(pick.instrument_currency)
    plan = pick.plan
    spec = plan.get("spec") if plan is not None else None
    value = _finite(spec.get("disaster_stop")) if isinstance(spec, Mapping) else None
    if value is not None:
        return _num(value, unit, SOURCE_PLAN, "spec.disaster_stop")
    planned = stop_facts.planned_by_pick.get(pick.key)
    if planned is not None and _finite(planned.get("stop_price")) is not None:
        return _num(
            _finite(planned.get("stop_price")),
            unit,
            SOURCE_STOP_JOURNAL,
            "line:standalone_stops:planned",
        )
    return _null(NULL_LEGACY_PLAN, unit, SOURCE_PLAN, "spec.disaster_stop")


# --- State (§4.6) -------------------------------------------------------------


def _sum_known(values: Iterable[Measured]) -> float:
    return sum(float(m.value) for m in values if m.value is not None)


def _state(pick: _Pick, *, offline: bool, horizon: dt.datetime | None) -> tuple[str, str | None]:
    # Offline, a now-bracket tier's fill is never journaled: its quantity is
    # unknown, so neither ``closed`` nor ``never_filled`` can be said (§4.6).
    if offline and any(t.path == PATH_NOW_BRACKET and t.fill is None for t in pick.tiers):
        return STATE_UNRESOLVED, NULL_NOT_JOURNALED
    entry_qty = _sum_known(f.qty for f in pick.entry_fills())
    if entry_qty <= _QTY_EPS:
        return STATE_NEVER_FILLED, _never_filled_reason(pick)
    exit_qty = _sum_known(e.attributed_qty for e in pick.exits)
    if abs(entry_qty - exit_qty) <= _QTY_EPS and not pick.ambiguous:
        return STATE_CLOSED, None
    if pick.ambiguous:
        return (STATE_UNRESOLVED, NULL_AMBIGUOUS) if offline else (STATE_OPEN, None)
    if not offline:
        return STATE_OPEN, None
    armed = pick.plan_armed_at
    if horizon is None or armed is None or armed < horizon:
        return STATE_UNRESOLVED, NULL_COMPACTED
    return STATE_OPEN, None


def _never_filled_reason(pick: _Pick) -> str:
    if pick.status == STATUS_REFUSED:
        return "refused"
    if pick.status == STATUS_DISARMED:
        return "disarmed"
    terminals = [tier.terminal for tier in pick.tiers]
    if not terminals or TERMINAL_OPEN in terminals:
        return "pending"
    if all(t == TERMINAL_EXPIRED for t in terminals):
        return "expired"
    if all(t == TERMINAL_CANCELLED for t in terminals):
        return "cancelled"
    return terminals[-1] if terminals[-1] in ("expired", "cancelled") else "pending"


# --- Outcome (§5) -------------------------------------------------------------


def _weighted(pairs: Sequence[tuple[float, float]]) -> float | None:
    total = sum(q for q, _p in pairs)
    if total <= _QTY_EPS:
        return None
    return sum(q * p for q, p in pairs) / total


def _outcome(
    pick: _Pick,
    state: str,
    state_reason: str | None,
    denominator: Measured,
    stop_facts: _StopFacts,
    now: dt.datetime,
) -> dict[str, Any]:
    ccy = pick.instrument_currency
    price_unit = _price_unit(ccy)
    names_native = (
        ("entry_qty", _UNIT_SHARES),
        ("avg_entry_price", price_unit),
        ("exit_qty", _UNIT_SHARES),
        ("avg_exit_price", price_unit),
        ("notional_spent", ccy),
        ("pnl_cash", ccy),
        ("pnl_pct_of_spent", _UNIT_PERCENT),
        ("risk_per_share", price_unit),
        ("r_multiple", _UNIT_R),
        ("holding_seconds", _UNIT_SECONDS),
        ("notional_spent_acct", pick.sizing_currency),
        ("pnl_cash_acct", pick.sizing_currency),
        ("mfe_lower_bound", price_unit),
    )
    out: dict[str, Measured] = {}
    fees_out: dict[str, Measured] = {}

    def all_null(reason: str) -> dict[str, Any]:
        for name, unit in names_native:
            out[name] = _null(reason, unit, SOURCE_DERIVED)
        for name in ("commission", "exchange_fee", "fx_conversion"):
            fees_out[name] = _null(reason, None, SOURCE_DERIVED)
        return _render_outcome(out, fees_out, denominator)

    if state == STATE_NEVER_FILLED:
        return all_null(NULL_NEVER_FILLED)
    if state == STATE_UNRESOLVED:
        return all_null(state_reason or NULL_COMPACTED)
    side = _plan_side(pick.plan)
    if side is None:
        pick.warn(W_SIDE_UNRESOLVED, "the plan does not resolve a side; outcome math refused")
        return all_null(NULL_NON_FINITE)
    sign = 1.0 if side == "long" else -1.0

    entries = pick.entry_fills()
    entry_pairs = [
        (float(f.qty.value), float(f.price.value))
        for f in entries
        if f.qty.value is not None and f.price.value is not None
    ]
    entry_refs = ",".join(f"order:{f.order_id}" for f in entries if f.order_id)
    entry_complete = len(entry_pairs) == len(entries)
    entry_qty = sum(q for q, _ in entry_pairs) if entry_complete else None
    avg_entry = _weighted(entry_pairs) if entry_complete else None
    reason_missing = next(
        (
            m.null_reason
            for f in entries
            for m in (f.qty, f.price)
            if m.value is None and m.null_reason is not None
        ),
        NULL_NOT_JOURNALED,
    )
    out["entry_qty"] = (
        _num(entry_qty, _UNIT_SHARES, SOURCE_DERIVED, entry_refs)
        if entry_qty is not None
        else _null(reason_missing, _UNIT_SHARES, SOURCE_DERIVED, entry_refs)
    )
    out["avg_entry_price"] = (
        _num(avg_entry, price_unit, SOURCE_DERIVED, entry_refs)
        if avg_entry is not None
        else _null(reason_missing, price_unit, SOURCE_DERIVED, entry_refs)
    )
    notional = sum(q * p for q, p in entry_pairs) if entry_complete else None
    out["notional_spent"] = (
        _num(notional, ccy, SOURCE_DERIVED, entry_refs)
        if notional is not None
        else _null(reason_missing, ccy, SOURCE_DERIVED, entry_refs)
    )
    stop = denominator.value
    risk: float | None = None
    if avg_entry is not None and stop is not None:
        risk = sign * (avg_entry - float(stop))
        out["risk_per_share"] = (
            _num(risk, price_unit, SOURCE_DERIVED, f"avg_entry_price,{denominator.ref}")
            if risk > 0
            else _null(NULL_NON_POSITIVE_RISK, price_unit, SOURCE_DERIVED, denominator.ref)
        )
    else:
        out["risk_per_share"] = _null(
            denominator.null_reason or reason_missing, price_unit, SOURCE_DERIVED, denominator.ref
        )

    closed = state == STATE_CLOSED
    exit_names = (
        "exit_qty",
        "avg_exit_price",
        "pnl_cash",
        "pnl_pct_of_spent",
        "r_multiple",
        "holding_seconds",
        "pnl_cash_acct",
    )
    exit_null_reason = NULL_NOT_CLOSED if state == STATE_OPEN else (state_reason or NULL_NOT_CLOSED)
    exits = pick.exits
    exit_refs = ",".join(f"order:{e.fill.order_id}" for e in exits if e.fill.order_id)
    if closed:
        exit_pairs = [
            (float(e.attributed_qty.value), float(e.fill.price.value))
            for e in exits
            if e.attributed_qty.value is not None and e.fill.price.value is not None
        ]
        if len(exit_pairs) != len(exits):
            missing = next(
                (
                    e.fill.price.null_reason or e.attributed_qty.null_reason
                    for e in exits
                    if e.fill.price.value is None or e.attributed_qty.value is None
                ),
                reason_missing,
            )
            for name in exit_names:
                unit = dict(names_native)[name]
                out[name] = _null(missing or reason_missing, unit, SOURCE_DERIVED, exit_refs)
        else:
            exit_qty = sum(q for q, _ in exit_pairs)
            avg_exit = _weighted(exit_pairs)
            out["exit_qty"] = _num(exit_qty, _UNIT_SHARES, SOURCE_DERIVED, exit_refs)
            out["avg_exit_price"] = _num(avg_exit, price_unit, SOURCE_DERIVED, exit_refs)
            if avg_entry is not None:
                pnl = sign * sum(q * (p - avg_entry) for q, p in exit_pairs)
                out["pnl_cash"] = _num(pnl, ccy, SOURCE_DERIVED, f"{entry_refs};{exit_refs}")
                spent_on_exit = avg_entry * exit_qty
                out["pnl_pct_of_spent"] = (
                    _num(pnl / spent_on_exit * 100.0, _UNIT_PERCENT, SOURCE_DERIVED, "pnl_cash")
                    if spent_on_exit
                    else _null(NULL_NON_FINITE, _UNIT_PERCENT, SOURCE_DERIVED)
                )
                if risk is not None and risk > 0 and exit_qty > 0:
                    out["r_multiple"] = _num(
                        pnl / (exit_qty * risk), _UNIT_R, SOURCE_DERIVED, "pnl_cash,risk_per_share"
                    )
                else:
                    out["r_multiple"] = _null(
                        out["risk_per_share"].null_reason or NULL_NON_POSITIVE_RISK,
                        _UNIT_R,
                        SOURCE_DERIVED,
                        denominator.ref,
                    )
            else:
                for name in ("pnl_cash", "pnl_pct_of_spent", "r_multiple"):
                    out[name] = _null(reason_missing, dict(names_native)[name], SOURCE_DERIVED)
            first_entry = min((f.when for f in entries if f.when is not None), default=None)
            last_exit = max((e.fill.when for e in exits if e.fill.when is not None), default=None)
            sources = {f.venue_time.source for f in entries} | {
                e.fill.venue_time.source for e in exits
            }
            holding_source = (
                SOURCE_DERIVED
                if sources <= {SOURCE_AUDIT}
                else next((s for s in sorted(sources) if s.startswith("keeper.")), SOURCE_DERIVED)
            )
            out["holding_seconds"] = (
                _num(
                    (last_exit - first_entry).total_seconds(),
                    _UNIT_SECONDS,
                    holding_source,
                    "first entry venue time,last exit venue time",
                )
                if first_entry is not None and last_exit is not None
                else _null(reason_missing, _UNIT_SECONDS, SOURCE_DERIVED)
            )
    else:
        for name in exit_names:
            out[name] = _null(exit_null_reason, dict(names_native)[name], SOURCE_DERIVED)

    # Account-currency cash legs and fees, from the bookings (P3). Fees and the
    # cash P&L cover both legs, so an open pick reports them as not closed
    # rather than as a partial sum that reads like a total.
    shares: list[tuple[_Fill, float | None]] = [(f, 1.0) for f in entries] + [
        (e.fill, _share_of(e)) for e in exits
    ]
    prorated = any(
        fraction is not None and fraction < 1.0 - _FLOAT_TOLERANCE for _, fraction in shares
    )
    entry_share = _booking_total([(f, 1.0) for f in entries], "share_amount_acct")
    out["notional_spent_acct"] = (
        _num(-entry_share[0], pick.sizing_currency or entry_share[1], SOURCE_DERIVED, entry_refs)
        if entry_share[0] is not None
        else _null(NULL_FX_NOT_REALIZED, pick.sizing_currency, SOURCE_DERIVED, entry_refs)
    )
    if closed:
        exit_share = _booking_total([(e.fill, _share_of(e)) for e in exits], "share_amount_acct")
        if entry_share[0] is not None and exit_share[0] is not None:
            out["pnl_cash_acct"] = _num(
                entry_share[0] + exit_share[0],
                pick.sizing_currency or entry_share[1],
                SOURCE_DERIVED,
                f"{entry_refs};{exit_refs}",
            )
        else:
            out["pnl_cash_acct"] = _null(
                NULL_FX_NOT_REALIZED,
                pick.sizing_currency,
                SOURCE_DERIVED,
                f"{entry_refs};{exit_refs}",
            )
    for name in ("commission", "exchange_fee", "fx_conversion"):
        if not closed:
            fees_out[name] = _null(exit_null_reason, None, SOURCE_DERIVED)
            continue
        total, unit, reason = _booking_total(shares, name)
        fees_out[name] = (
            _num(total, unit, SOURCE_DERIVED, f"{entry_refs};{exit_refs}")
            if total is not None
            else _null(reason or NULL_NOT_JOURNALED, unit, SOURCE_DERIVED)
        )
    if (
        closed
        and prorated
        and any(f.booking_sums.get("commission", (None,))[0] is not None for f, _ in shares)
    ):
        pick.warn(
            W_BOOKING_PRORATED,
            "a fill shared between owners: this pick's booking amounts are "
            "amount x attributed_qty / FilledAmount",
        )

    peaks = _peaks_in_window(pick, stop_facts, now)
    out["mfe_lower_bound"] = (
        _num(
            max(peaks) - avg_entry,
            price_unit,
            SOURCE_DERIVED,
            "max(trailed.peak) - avg_entry_price",
        )
        if peaks and avg_entry is not None
        else _null(NULL_NOT_JOURNALED, price_unit, SOURCE_DERIVED, "trailed.peak")
    )
    return _render_outcome(out, fees_out, denominator)


def _share_of(exit_: _Exit) -> float | None:
    qty = exit_.attributed_qty.value
    total = exit_.fill.filled_qty
    if qty is None or not total:
        return None
    return float(qty) / float(total)


def _booking_total(
    inputs: Sequence[tuple[_Fill, float | None]], name: str
) -> tuple[float | None, str | None, str | None]:
    total = 0.0
    unit: str | None = None
    for fill, fraction in inputs:
        amount, member_unit, reason = fill.booking_sums.get(name, (None, None, NULL_NOT_JOURNALED))
        if amount is None or fraction is None:
            return None, member_unit or unit, reason or NULL_AMBIGUOUS
        total += amount * fraction
        unit = unit or member_unit
    if not inputs:
        return None, unit, NULL_NOT_JOURNALED
    return total, unit, None


def _peaks_in_window(pick: _Pick, stop_facts: _StopFacts, now: dt.datetime) -> list[float]:
    if pick.uic is None:
        return []
    entries = [f.when for f in pick.entry_fills() if f.when is not None]
    if not entries:
        return []
    start = min(entries)
    exit_times = [e.fill.when for e in pick.exits if e.fill.when is not None]
    end = max(exit_times) if exit_times else now
    peaks: list[float] = []
    for marker in stop_facts.markers_by_uic.get(pick.uic, []):
        if marker.get("kind") != "trailed":
            continue
        when = parse_utc(marker.get("ts"))
        peak = _finite(marker.get("peak"))
        if when is not None and peak is not None and start <= when <= end:
            peaks.append(peak)
    return peaks


def _render_outcome(
    out: Mapping[str, Measured], fees: Mapping[str, Measured], denominator: Measured
) -> dict[str, Any]:
    ordered = (
        "entry_qty",
        "avg_entry_price",
        "exit_qty",
        "avg_exit_price",
        "notional_spent",
        "pnl_cash",
        "pnl_pct_of_spent",
    )
    rendered: dict[str, Any] = {name: out[name].to_dict() for name in ordered}
    rendered["denominator"] = Measured(
        denominator.value,
        denominator.unit,
        SOURCE_DERIVED if denominator.value is not None else denominator.source,
        f"{denominator.source}:{denominator.ref} (replay spec 5.1: spec.disaster_stop)",
        denominator.null_reason,
    ).to_dict()
    for name in ("risk_per_share", "r_multiple", "holding_seconds"):
        rendered[name] = out[name].to_dict()
    rendered["fees"] = {
        name: fees[name].to_dict() for name in ("commission", "exchange_fee", "fx_conversion")
    }
    for name in ("notional_spent_acct", "pnl_cash_acct", "mfe_lower_bound"):
        rendered[name] = out[name].to_dict()
    rendered["fees_not_included"] = list(FEES_NOT_INCLUDED)
    return rendered


# --- Record assembly ----------------------------------------------------------


def _instrument(pick: _Pick, offline: bool) -> dict[str, Any]:
    def member(value: Any, source: str) -> dict[str, Any]:
        if value is None:
            return _null(NULL_NOT_JOURNALED, None, source).to_dict()
        return Measured(value, None, source).to_dict()

    return {
        "uic": member(pick.uic, SOURCE_ENTRY_WATCH),
        "exchange_mic": member(pick.exchange_mic, SOURCE_ENTRY_WATCH),
        "instrument_currency": member(pick.instrument_currency, SOURCE_ENTRY_WATCH),
        "sizing_currency": member(pick.sizing_currency, SOURCE_ENTRY_WATCH),
    }


def _sizing_fx(pick: _Pick) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name in ("rate", "bid", "ask", "asof", "source"):
        measured = pick.sizing_fx.get(name)
        out[name] = (
            measured.to_dict()
            if measured is not None
            else _null(NULL_NOT_JOURNALED, None, SOURCE_SUBMISSIONS).to_dict()
        )
    return out


def _record(
    pick: _Pick,
    *,
    venue: _Venue | None,
    stop_facts: _StopFacts,
    horizon: dt.datetime | None,
    now: dt.datetime,
) -> dict[str, Any]:
    offline = venue is None
    state, state_reason = _state(pick, offline=offline, horizon=horizon)
    if offline and state == STATE_OPEN and not pick.exits:
        pick.warn(
            W_EXIT_NOT_IN_JOURNAL,
            "the journals hold no exit for this pick; it may still be open, or its exit "
            "may sit behind the snapshot horizon",
        )
    elif offline and state == STATE_OPEN:
        pick.warn(W_EXIT_NOT_IN_JOURNAL, "the journals hold fewer exit shares than entry shares")
    denominator = _plan_disaster_stop(pick, stop_facts)
    outcome = _outcome(pick, state, state_reason, denominator, stop_facts, now)
    status_at, status_reason = _pick_status_fields(pick.status, pick.status_record)
    plan = pick.plan
    plan_armed_at = pick.plan_armed_at
    side = _plan_side(plan)
    exits_sorted = sorted(
        pick.exits,
        key=lambda e: e.fill.when or dt.datetime.min.replace(tzinfo=dt.UTC),
    )
    return {
        "pick_key": pick.key,
        "ticker": pick.ticker,
        "trade_date": pick.trade_date,
        "generation": pick.generation,
        "pick_status": pick.status,
        "pick_status_at": _time(
            parse_utc(status_at), SOURCE_PICK_QUEUE, f"line:picks:{pick.status}"
        ).to_dict(),
        "pick_status_reason": status_reason,
        "state": state,
        "state_reason": state_reason,
        "plan": None if plan is None else dict(plan),
        "plan_null_reason": None if plan is not None else NULL_NOT_JOURNALED,
        "plan_armed_at": _time(plan_armed_at, SOURCE_PICK_QUEUE, "line:picks:armed").to_dict(),
        "plan_schema_version": _plan_schema_version(plan),
        "plan_size_shape": _plan_size_shape(plan),
        "plan_source": _plan_source(plan),
        "plan_disaster_stop": denominator.to_dict(),
        "placed_stop": _placed_stop(pick, venue, stop_facts, state).to_dict(),
        "instrument": _instrument(pick, offline),
        "sizing_fx": _sizing_fx(pick),
        "side": side,
        "entries": [
            tier.to_dict() for tier in sorted(pick.tiers, key=lambda t: (t.path, t.tier_index))
        ],
        "exits": [exit_.to_dict() for exit_ in exits_sorted],
        "outcome": outcome,
        "warnings": list(pick.warnings),
    }


def _empty_counts() -> dict[str, int]:
    return {**dict.fromkeys(STATES, 0), "total": 0}


def _count(records: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    counts = _empty_counts()
    for record in records:
        counts[str(record["state"])] = counts.get(str(record["state"]), 0) + 1
        counts["total"] += 1
    return counts


def _matches(pick: _Pick, filters: TradesFilters) -> bool:
    if filters.since is not None and dt.date.fromisoformat(pick.trade_date) < filters.since:
        return False
    if filters.ticker is not None and pick.ticker != filters.ticker.upper():
        return False
    return not (filters.pick is not None and pick.key != filters.pick)


def _sort_key(record: Mapping[str, Any]) -> tuple[int, str, int]:
    ordinal = dt.date.fromisoformat(str(record["trade_date"])).toordinal()
    return (-ordinal, str(record["ticker"]), int(record["generation"]))


def build_trades(
    env: str,
    *,
    broker: SupportsFillHistory | None,
    filters: TradesFilters,
    now: dt.datetime,
) -> TradesReport:
    """One record per pick, from the journals and (``broker`` given) the venue.

    ``broker=None`` is ``--offline``: the journals are the only source and every
    venue-derived field is null with reason ``offline``. Any ``BrokerError`` the
    capability raises propagates; the caller reports it, so a failed read is
    never rendered as a partial result."""
    offline = broker is None
    journals = _read_journals(env)
    picks = _load_picks(journals)
    _attach_submissions(picks, journals.records["submissions"])
    stop_facts = _read_stop_facts(journals.records["standalone_stops"])
    # The uic each pick trades, from the journals, so picks that share one are
    # rebuilt together (attribution needs every lot on the uic, §4.5).
    for pick in picks:
        for record in journals.records["entry_trails"]:
            if record.get("kind") == "watch_open" and (
                str(record.get("pick_key", "")) == pick.key
                or _tier_index_from_crid(str(record.get("crid", "")), pick.crid_prefix) is not None
            ):
                _absorb_watch_open(pick, record)
    selected = [pick for pick in picks if _matches(pick, filters)]
    selected_uics = {pick.uic for pick in selected if pick.uic is not None}
    considered = [
        pick
        for pick in picks
        if pick in selected or (pick.uic is not None and pick.uic in selected_uics)
    ]

    sources: dict[str, Any] = {}
    for name in _JOURNALS:
        sources[_SOURCE_BY_JOURNAL[name]] = {
            "status": SOURCE_STATUS_READ,
            "reason": None,
            "window": None,
            "rows": len(journals.records[name]),
        }
    sources[SOURCE_PLAN] = {
        "status": SOURCE_STATUS_READ,
        "reason": None,
        "window": None,
        "rows": sum(1 for p in picks if p.plan is not None),
    }

    venue: _Venue | None = None
    window_start: dt.datetime | None = None
    if not offline:
        assert broker is not None
        armed = [p.plan_armed_at for p in considered if p.plan_armed_at is not None]
        if armed:
            window_start = min(armed) - _AUDIT_LOOKBACK
            history = broker.list_fill_history(window_start, now)
            venue = _index_venue(history, broker)
            _venue_sources(sources, history)
        else:
            for role in (SOURCE_AUDIT, SOURCE_TRADES_REPORT, SOURCE_BOOKINGS, SOURCE_INSTRUMENT):
                sources[role] = {
                    "status": SOURCE_STATUS_SKIPPED,
                    "reason": SOURCE_REASON_NO_PICKS,
                    "window": None,
                    "rows": 0,
                }
    else:
        for role in (SOURCE_AUDIT, SOURCE_TRADES_REPORT, SOURCE_BOOKINGS, SOURCE_INSTRUMENT):
            sources[role] = {
                "status": SOURCE_STATUS_OFFLINE,
                "reason": NULL_OFFLINE,
                "window": None,
                "rows": 0,
            }

    for pick in considered:
        _build_trail_tiers(pick, journals, venue, offline=venue is None)
        _build_bracket_tiers(pick, venue)
        if venue is not None:
            _audit_found_tiers(pick, venue)

    unattributed: list[dict[str, Any]] = []
    picks_by_key = {pick.key: pick for pick in considered}
    by_uic: dict[int, list[_Pick]] = defaultdict(list)
    for pick in considered:
        if pick.uic is not None:
            by_uic[pick.uic].append(pick)
    for uic, on_uic in by_uic.items():
        lots = [
            _Lot(
                pick=pick,
                when=fill.when,
                remaining=float(fill.qty.value),
                position_id=fill.position_id,
            )
            for pick in on_uic
            for fill in pick.entry_fills()
            if fill.qty.value is not None
        ]
        first_entry = min((lot.when for lot in lots if lot.when is not None), default=None)
        if not lots:
            continue  # nothing was ever opened by a pick here: no fill on it is ours
        if venue is not None:
            events = _broker_closing_events(
                uic,
                on_uic,
                picks_by_key,
                venue,
                stop_facts,
                unattributed if uic in selected_uics else [],
                window_start=first_entry,
            )
        else:
            events = _offline_closing_events(uic, on_uic, picks_by_key, stop_facts)
        _allocate(
            events,
            lots,
            offline=venue is None,
            unattributed=unattributed if uic in selected_uics else [],
        )

    if venue is not None:
        sources[SOURCE_INSTRUMENT] = {
            "status": SOURCE_STATUS_READ,
            "reason": None,
            "window": None,
            "rows": len(venue.tick_uics),
        }

    records = [
        _record(pick, venue=venue, stop_facts=stop_facts, horizon=journals.horizon, now=now)
        for pick in selected
    ]
    records.sort(key=_sort_key)
    counts_all = _count(records)
    chosen = (
        records
        if filters.state == STATE_FILTER_ALL
        else [r for r in records if r["state"] == filters.state]
    )
    counts_selected = _count(chosen)
    truncated = filters.limit is not None and len(chosen) > filters.limit
    shown = chosen if filters.limit is None else chosen[: filters.limit]
    far = "9999"
    unattributed.sort(key=lambda u: str((u.get("venue_time") or {}).get("value") or far))
    return TradesReport(
        env=env,
        generated_at=now,
        mode=MODE_OFFLINE if offline else MODE_BROKER,
        sources=sources,
        snapshot_horizon=journals.horizon,
        counts={
            "all": counts_all,
            "selected": counts_selected,
            "malformed": dict(journals.malformed),
            "unattributed_fills": len(unattributed),
        },
        truncated=truncated,
        trades=tuple(shown),
        unattributed_fills=tuple(unattributed),
    )


def _window_bound(raw: str) -> str:
    moment = parse_utc(raw)
    return raw if moment is None else format_time(moment)


def _venue_sources(sources: dict[str, Any], history: FillHistory) -> None:
    window = history.audit_window
    sources[SOURCE_AUDIT] = {
        "status": SOURCE_STATUS_READ,
        "reason": None,
        # The audit is read over datetimes; render them like every other time
        # (§3.1). The report windows below are dates and stay dates.
        "window": {"from": _window_bound(window.start), "to": _window_bound(window.end)},
        "rows": window.rows,
    }
    for role, read in (
        (SOURCE_TRADES_REPORT, history.trades_window),
        (SOURCE_BOOKINGS, history.bookings_window),
    ):
        if read is None:
            sources[role] = {
                "status": SOURCE_STATUS_SKIPPED,
                "reason": history.reports_skipped_reason or NULL_SIM_REPORTS,
                "window": None,
                "rows": 0,
            }
        else:
            sources[role] = {
                "status": SOURCE_STATUS_READ,
                "reason": None,
                "window": {"from": read.start, "to": read.end},
                "rows": read.rows,
            }


__all__ = [
    "ATTRIBUTIONS",
    "EXIT_REASONS",
    "EXIT_REASON_BY_ALERT_REASON",
    "FEES_NOT_INCLUDED",
    "MODES",
    "NULL_REASONS",
    "PICK_STATUSES",
    "PLAN_SOURCES",
    "SIDES",
    "SIZE_SHAPES",
    "SOURCES",
    "SOURCE_REASONS",
    "SOURCE_STATUSES",
    "STATES",
    "STATE_FILTER_ALL",
    "STATE_REASONS",
    "TERMINALS",
    "TIER_PATHS",
    "TRADES_SCHEMA_ID",
    "WARNING_CODES",
    "Measured",
    "TradesFilters",
    "TradesReport",
    "build_trades",
    "format_time",
    "valid_pick_key",
]
