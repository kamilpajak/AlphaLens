"""Entry-trailing scaffolding (PR-T0) — flag ownership + the ``entry_trails.jsonl`` journal.

Design memo: ``docs/research/entry_trailing_design_2026_08_12.md`` (LOCKED
2026-08-12). PR-T0 is strictly INERT: this module owns the flag NAME and the
journal fold/compaction primitives; nothing here watches prices or places
orders (that is PR-T1/T2). With the flag unset/``0`` and the journal absent,
runtime behavior is byte-identical to the pre-trailing daemon.

**Module-ownership doctrine** (mirrors ``safety.py`` / ``position_manager.py``):
:data:`ENTRY_TRAIL_BPS_ENV` and :data:`ENTRY_TRAIL_BPS_MAX` live ONLY here —
``live_rails`` imports them for the 8th boot-assert pin, so the boot-assert
and the runtime reader can never drift onto different names or bounds. The
watch-capacity rail (:data:`ENTRY_WATCH_MAX_PICKS_ENV` and its bounds) follows
the same rule since #1189: ``live_rails`` pins it as the 9th rail and
``control_loop`` re-exports it for the runtime reader.

**Journal** (memo §5, G4): per-tier trail state lives in its OWN per-env
``entry_trails.jsonl`` (sibling of ``standalone_stops.jsonl``), JSON lines
keyed by the per-tier ``crid`` (client request id, the ``-entry-`` family —
PR-T0 defines only the schema, nothing generates them yet). Kinds:
``watch_open`` / ``touched`` / ``trough`` / ``trail_armed`` plus the terminal
``fired`` / ``expired`` / ``suspended`` / ``cancelled``. The compactor keeps a
minimal fold-equivalent set per crid and PRESERVES UNKNOWN KINDS VERBATIM —
the standalone-stops compactor eats unknown kinds (memo G4) and that bug
class must not recur here.

**Virtual reservation** (memo G5): watching tiers have NO broker order, so
they fold to zero in the committed-working gross — the gross cap and cash
floor add :func:`watching_virtual_gross_acct` (non-terminal ``watch_open``
records valued at tier LIMIT, conservative-high) as their watching term.

**Minimum-frame arithmetic** (memo §10 M2, intentional-conservative): the
limit-valued reservation of ALL tiers from watch-open equals the full-pick
gross at limits, so on a small declared frame the gross cap may refuse a
second watching pick BEFORE any tier fires. That over-reservation is by
design — a watch that cannot be funded at its limits must not open.
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import os
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from alphalens_pipeline.brokers.automanager import state_paths

logger = logging.getLogger(__name__)

# --- Flag (memo §6) ----------------------------------------------------------

ENTRY_TRAIL_BPS_ENV = "ALPHALENS_BROKER_ENTRY_TRAIL_BPS"

# Bound = 150, NOT 300: the replay's edge is negative by d≈2% and clearly
# worse at 3% — the bound must exclude the measured-bad region (memo §6; grid
# support is {50, 100} with 150 marginal).
ENTRY_TRAIL_BPS_MAX = 150

_FEATURE_OFF_BPS = 0  # trailing disabled — today's limit-at-touch behavior

# --- Watch capacity (memo decision #4 / G5 CRITICAL-1) -----------------------
#
# Owned here for the same reason as ENTRY_TRAIL_BPS above: `live_rails` imports
# these for the 9th boot-assert pin and `control_loop` imports them for the
# runtime reader, so the assert and the reader can never drift onto different
# names or bounds. `control_loop` re-exports them under its historical private
# names for its own call sites.

ENTRY_WATCH_MAX_PICKS_ENV = "ALPHALENS_BROKER_ENTRY_WATCH_MAX_PICKS"
"""Env rail for the watch capacity (2026-08-19 live incident, ETSY: the old
hardcoded constant of 1 silently capacity-deferred every second armed pick
after MAX_OPEN was raised to 2). Read at CALL time so an operator bump takes
effect on the next tick without a daemon restart."""

ENTRY_WATCH_MAX_PICKS_DEFAULT = 1
ENTRY_WATCH_MAX_PICKS_MIN = 1

# 25, raised from 10 on 2026-08-28 (#1189). This is a VALIDATION range, not a
# risk control: the account is bounded by MAX_OPEN and by the money-denominated
# virtual gross/cash reservation fold below. The old ceiling of 10 had become
# the binding limit on SIM sample collection — watches hold their slot for up
# to the 7-day order TTL, and on 2026-08-28 seven picks from two prior briefs
# still held slots while only three had ever touched their tier limit.
#
# LIVE does NOT rely on this number. Until #1189 this shared ceiling was the
# only code-level bound on LIVE's watch capacity; `live_rails.assert_live_rails`
# now pins the rail to [1, 2] for LIVE and FAILS ON UNSET, so raising the shared
# ceiling for the SIM lab cannot widen the LIVE one.
#
# The LIVE upper bound is NOT here: it lives with the other LIVE bounds in
# ``live_rails._ENTRY_WATCH_MAX_PICKS_UPPER``, so an audit of "what bounds LIVE"
# finds the whole table in one module. Raising the value below does not widen
# LIVE — that is the point of the pin.
ENTRY_WATCH_MAX_PICKS_MAX = 25

_entry_trail_bps_warned = False
"""Once-per-process latch for the invalid-flag warning (mirrors
``control_loop._entry_watch_max_picks_warned``): the reader runs on every
daemon tick (~45s, several call sites), so a SIM boot with a garbage env value
must warn ONCE, not spam the journal for the whole daemon lifetime."""


def _warn_invalid_entry_trail_bps(message: str, *args: Any) -> None:
    """Emit the invalid-flag warning at most once per process (see the latch)."""
    global _entry_trail_bps_warned  # noqa: PLW0603 — once-per-process warn latch
    if _entry_trail_bps_warned:
        return
    _entry_trail_bps_warned = True
    logger.warning(message, *args)


def entry_trail_bps() -> int:
    """The runtime trail distance in basis points; ``0`` = feature OFF.

    Fail-CLOSED reader: unset/blank is OFF; a malformed value or one outside
    ``[0, ENTRY_TRAIL_BPS_MAX]`` is OFF with a once-per-process warning
    (today's behavior — a bad flag must never arm a live trail). Explicit
    ``"0"`` is a valid OFF. The LIVE boot-assert (``live_rails``) additionally
    REFUSES to boot on unset/malformed — this lenient reader is the
    SIM/runtime path."""
    raw = os.environ.get(ENTRY_TRAIL_BPS_ENV)
    if raw is None or not raw.strip():
        return _FEATURE_OFF_BPS
    try:
        value = int(raw)
    except ValueError:
        _warn_invalid_entry_trail_bps(
            "%s=%r is not an integer — entry trailing stays OFF", ENTRY_TRAIL_BPS_ENV, raw
        )
        return _FEATURE_OFF_BPS
    if not 0 <= value <= ENTRY_TRAIL_BPS_MAX:
        _warn_invalid_entry_trail_bps(
            "%s=%d is outside [0, %d] — entry trailing stays OFF",
            ENTRY_TRAIL_BPS_ENV,
            value,
            ENTRY_TRAIL_BPS_MAX,
        )
        return _FEATURE_OFF_BPS
    return value


# --- Journal kinds (memo §5) -------------------------------------------------

KIND_WATCH_OPEN = "watch_open"
KIND_TOUCHED = "touched"
KIND_TROUGH = "trough"
KIND_TRAIL_ARMED = "trail_armed"
KIND_FIRED = "fired"
KIND_EXPIRED = "expired"
KIND_SUSPENDED = "suspended"
KIND_CANCELLED = "cancelled"

ENTRY_TRAIL_TERMINAL_KINDS = frozenset({KIND_FIRED, KIND_EXPIRED, KIND_SUSPENDED, KIND_CANCELLED})
ENTRY_TRAIL_KINDS = (
    frozenset({KIND_WATCH_OPEN, KIND_TOUCHED, KIND_TROUGH, KIND_TRAIL_ARMED})
    | ENTRY_TRAIL_TERMINAL_KINDS
)

# Compaction growth bound: per crid the minimal fold-equivalent set is at most
# the latest watch_open + the min-trough record + the latest non-terminal
# state record + the latest terminal record (memo G4 "growth bound from day
# one"). Unknown/malformed lines are preserved verbatim ON TOP of this bound —
# they are an alarm state, not steady-state growth.
COMPACTED_LINES_PER_CRID_BOUND = 4

_CRID_KEY = "crid"


# --- Fold --------------------------------------------------------------------


@dataclass(frozen=True)
class EntryTrailTierState:
    """The folded state of ONE entry tier (one ``crid``)."""

    crid: str
    watch_open: Mapping[str, Any] | None  # the latest watch_open record, verbatim
    latest_kind: str | None  # latest NON-terminal kind, file order
    min_trough: float | None  # the minimum trough ever journaled (memo §5 restart rule)
    terminal_kind: str | None  # a terminal marker, if any
    # PR-T2b: the order id from the LATEST ``trail_armed`` line (``None`` while a
    # tier is arm-in-progress — the G3 write-ahead line is journaled with a null
    # id BEFORE the POST and filled in after). ``None`` when no trail_armed line
    # exists at all. The wire distinguishes a RESTING native order (real id ->
    # the broker owns it, excluded from the watch pass) from an unconfirmed POST
    # (null id -> re-drive to complete the arm) on this field.
    armed_order_id: str | None = None


@dataclass(frozen=True)
class EntryTrailFold:
    """Per-crid tier states + the count of malformed lines.

    ``malformed`` counts non-JSON / non-object lines and records missing a
    usable ``crid`` — surfaced, never silently dropped: the gross-cap
    consumer fails CLOSED on them (a record it cannot attribute may be a
    reservation it cannot see)."""

    tiers: dict[str, EntryTrailTierState]
    malformed: int


def _parse_record(raw_line: str) -> dict[str, Any] | None:
    """The parsed JSON object, or ``None`` when the line is not one."""
    try:
        record = json.loads(raw_line)
    except json.JSONDecodeError:
        return None
    return record if isinstance(record, dict) else None


def _record_crid(record: Mapping[str, Any]) -> str | None:
    crid = record.get(_CRID_KEY)
    if isinstance(crid, str) and crid.strip():
        return crid
    return None


def _finite_positive_float(value: Any) -> float | None:
    """``float(value)`` iff it is a real, finite, strictly-positive number;
    ``None`` otherwise.

    SEMANTIC validation, not mere castability: a zero/negative/non-finite
    limit, qty, fx_rate or trough is journal corruption — ``fx_rate=0.0``
    would divide by zero on a money gate, a negative limit would silently
    SHRINK the virtual reservation, and a NaN trough freezes every later
    min-comparison. ``bool`` is rejected explicitly (JSON ``true`` must not
    coerce to ``1.0``)."""
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result) or result <= 0.0:
        return None
    return result


def _fold_record_into_state(state: dict[str, Any], kind: str, record: Mapping[str, Any]) -> None:
    """Fold ONE known-kind journal record into a tracker ``state`` dict
    (the mutable per-crid accumulator behind :func:`fold_entry_trail_lines`)."""
    if kind in ENTRY_TRAIL_TERMINAL_KINDS:
        state["terminal_kind"] = kind
        return
    state["latest_kind"] = kind
    if kind == KIND_WATCH_OPEN:
        state["watch_open"] = dict(record)
        # A (re-)opened watch has no armed order yet. The memo §5 CRITICAL-2
        # re-arm re-appends watch_open to reset a DayOrder-cancelled tier back
        # to WATCHING, so the arm state must clear too — else the stale
        # resting-order id lingers past the re-arm (harmless to the current
        # latest_kind-gated readers, but the fold must state the arm truth).
        state["armed_order_id"] = None
    elif kind == KIND_TRAIL_ARMED:
        # The LATEST trail_armed wins (a real-id line overrides the earlier
        # null-id write-ahead); a missing/blank order id folds back to None.
        order_id = record.get("order_id")
        state["armed_order_id"] = str(order_id) if order_id else None
    elif kind == KIND_TROUGH:
        trough = _finite_positive_float(record.get(KIND_TROUGH))
        if trough is not None and (state["min_trough"] is None or trough < state["min_trough"]):
            state["min_trough"] = trough


def fold_entry_trail_lines(raw_lines: Iterable[str]) -> EntryTrailFold:
    """Fold RAW journal lines into per-crid tier state (memo §5).

    Blank lines are skipped; malformed JSON and records missing ``crid`` are
    COUNTED into ``malformed``; unknown kinds contribute nothing here (they
    belong to a newer binary — the compactor preserves them verbatim).
    File order is time order (append-only journal), so "latest" is the last
    matching line."""
    trackers: dict[str, dict[str, Any]] = {}
    malformed = 0
    for raw_line in raw_lines:
        line = raw_line.strip()
        if not line:
            continue
        record = _parse_record(line)
        crid = None if record is None else _record_crid(record)
        if record is None or crid is None:
            malformed += 1
            continue
        kind = record.get("kind")
        if kind not in ENTRY_TRAIL_KINDS:
            continue
        state = trackers.setdefault(
            crid,
            {
                "watch_open": None,
                "latest_kind": None,
                "min_trough": None,
                "terminal_kind": None,
                "armed_order_id": None,
            },
        )
        _fold_record_into_state(state, kind, record)
    tiers = {crid: EntryTrailTierState(crid=crid, **state) for crid, state in trackers.items()}
    return EntryTrailFold(tiers=tiers, malformed=malformed)


# --- Virtual watching reservation (memo G5) ----------------------------------


def watching_virtual_gross_acct(fold: EntryTrailFold) -> tuple[float, int]:
    """``(total, bad)`` — the ACCOUNT-currency gross reserved by NON-terminal
    watching tiers, plus the count of malformed/unvaluable records.

    Mirrors the shape of ``control_loop._committed_working_gross_acct``: each
    non-terminal tier's latest ``watch_open`` is valued at ``limit x qty``
    (INSTRUMENT currency, conservative-high — memo G5) and converted through
    the record's OWN journaled ``fx_rate`` (instrument-ccy per 1 account-ccy,
    so acct = instr / rate; ``None`` = same-currency, folds as-is).

    ``bad`` = the fold's malformed count + every non-terminal tier whose
    reservation cannot be valued (no ``watch_open``, or uncastable
    limit/qty/fx_rate). The gross-cap consumer fails CLOSED on ``bad`` — an
    unvaluable virtual reservation is exposure the cap cannot see. An empty
    fold (no journal) is exactly ``(0.0, 0)``."""
    total = 0.0
    bad = fold.malformed
    for state in fold.tiers.values():
        if state.terminal_kind is not None:
            continue
        record = state.watch_open
        if record is None:
            bad += 1
            continue
        limit = _finite_positive_float(record.get("limit"))
        qty = _finite_positive_float(record.get("qty"))
        if limit is None or qty is None:
            bad += 1
            continue
        notional = limit * qty
        fx_rate = record.get("fx_rate")
        if fx_rate is not None:
            rate = _finite_positive_float(fx_rate)
            if rate is None:
                bad += 1
                continue
            # rate is instrument-ccy per 1 account-ccy -> acct = instr / rate.
            notional /= rate
        total += notional
    return total, bad


# --- Journal path + read seam ------------------------------------------------


def _entry_trail_journal_path() -> Path:
    """The per-env entry-trails journal path — funnels through the ONE
    broker-state path seam (``state_paths.entry_trails_path()``, ADR 0016),
    resolved fresh on EVERY call. A thin named wrapper so tests monkeypatch
    ONE attribute (same pattern as ``_standalone_stop_journal_path``)."""
    return state_paths.entry_trails_path()


def read_entry_trail_fold(*, path: Path | None = None) -> EntryTrailFold:
    """The fold of the current journal; a missing file folds empty (PR-T0
    inertness: no journal -> zero watching reservation, zero malformed).

    ``path`` overrides the per-env seam (``_entry_trail_journal_path``) — the
    same explicit-target shape as ``picks.iter_picks(path=...)``, so a CLI can
    address a specific instance's journal without touching
    ``ALPHALENS_BROKER_ENVIRONMENT``. Omitted = current behavior.

    An UNREADABLE journal (a directory at the path, permissions, I/O error)
    is contained as a fail-closed ``malformed=1`` fold rather than raised:
    the gross-cap/cash-floor callers run inside ``_place_pick``, which only
    catches ``BrokerError`` — an escaping ``OSError`` would abort the whole
    tick BEFORE the protection pass instead of merely refusing the pick."""
    path = path if path is not None else _entry_trail_journal_path()
    if not path.exists():
        return EntryTrailFold(tiers={}, malformed=0)
    try:
        with path.open("r", encoding="utf-8") as fh:
            return fold_entry_trail_lines(fh)
    except OSError as exc:
        logger.warning("entry-trails journal unreadable — failing closed: %s", exc)
        return EntryTrailFold(tiers={}, malformed=1)


# --- Journal append (PR-T1 writer) -------------------------------------------


def append_entry_trail_line(record: Mapping[str, Any], *, path: Path | None = None) -> None:
    """Append one line to the entry-trails journal (never rewrites).

    The WIRE-phase counterpart of the read/fold primitives above: the watcher
    (PR-T1) persists one ``{"kind": ..., "crid": ..., **payload}`` line per
    state transition through here. Mirrors
    ``control_loop._append_standalone_stop_journal`` (a DISTINCT seam — this
    one funnels through :func:`_entry_trail_journal_path`, never the
    standalone-stop path): create the parent dir, append, then flush + fsync
    so a watch_open reservation / terminal measurement line is durable the
    instant it is written — a buffered write lost to a crash (or systemd
    SIGKILL) would silently drop a virtual reservation the gross cap can no
    longer see, or re-fire a watch the journal no longer records as terminal.

    ``sort_keys`` keeps the on-disk form stable across runs (the compaction
    round-trip test relies on it); ``default=str`` lets a stray
    ``datetime``/``Path`` in a payload serialize rather than crash the append.
    ``path`` overrides the per-env seam (see :func:`read_entry_trail_fold`)."""
    path = path if path is not None else _entry_trail_journal_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True, default=str) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


# --- Operator disarm (the watch half of `alphalens broker disarm`) -----------


class DisarmRestingOrderError(RuntimeError):
    """A tier of the pick has a resting (or in-flight) native entry BUY.

    ``cancelled`` sits outside the resting-bearing terminals: every producer
    of a ``cancelled`` line is expected to have no broker order behind the
    tier. The reconcile pass filters terminal tiers, so cancelling a
    ``trail_armed`` tier from the outside would ORPHAN its live order —
    nothing would ever cancel it or reconcile its fill. The operator path is
    `alphalens broker cancel <order_id>` first, then disarm again."""


def cancel_open_watches(pick_key: str, *, note: str, path: Path | None = None) -> list[str]:
    """Append one terminal ``cancelled`` line per OPEN watch tier of
    ``pick_key``; return the cancelled crids (empty when none are open).

    The watch half of `alphalens broker disarm`: retiring the pick from the
    queue does not stop an open watch (the watch pass reads only this
    journal), so the disarm must terminate the tiers here. ``cancelled`` is a
    kind the running daemon already folds — it drops the tier from the active
    set, releases the virtual gross reservation and the watch-capacity slot,
    and lets the stale-ladder retraction retire the routed ``tranche_plan``
    on the next tick. No daemon restart is needed.

    Refuse-first atomicity: if ANY open tier of the pick has a ``trail_armed``
    latest kind — a REAL resting order id, or the null-id write-ahead of an
    in-flight POST that may have landed one — raise
    :class:`DisarmRestingOrderError` and write NOTHING.

    Sticky-terminal caveat (deliberate): the fold never clears
    ``terminal_kind`` and crids are deterministic per (ticker, date, tier),
    so a later re-arm of the SAME (ticker, date) will not re-open these
    tiers; a fresh brief date is the path back.

    Best-effort vs a live daemon: the daemon can arm a native trail between
    this fold read and the append — rerun after `broker cancel` if the
    refusal fires late."""
    fold = read_entry_trail_fold(path=path)
    open_tiers = []
    for tier in fold.tiers.values():
        if tier.terminal_kind is not None or tier.watch_open is None:
            continue
        tier_key = tier.watch_open.get("pick_key")
        if tier_key is None:
            # Every watch_open the daemon writes carries pick_key; a keyless
            # one (future code change, hand-edited journal) must be a VISIBLE
            # skip, never a silent one — the tier stays open and disarm
            # cannot see it belongs to anyone.
            logger.warning(
                "cancel_open_watches: open tier %s has a watch_open without a "
                "pick_key — skipped (cannot attribute it to %s)",
                tier.crid,
                pick_key,
            )
            continue
        if str(tier_key) == pick_key:
            open_tiers.append(tier)
    for tier in open_tiers:
        # latest_kind == trail_armed covers the normal state machine (a real
        # resting order, or the null-id write-ahead of an in-flight POST). The
        # armed_order_id check is defense in depth: the rearm path clears it
        # only via a fresh watch_open written AFTER the broker confirmed the
        # order gone — if a future bug ever leaves a real id behind on a
        # non-armed latest_kind, refusing is still the only safe answer.
        if tier.latest_kind == KIND_TRAIL_ARMED or tier.armed_order_id is not None:
            raise DisarmRestingOrderError(
                f"tier {tier.crid} has a native entry arm "
                f"(order_id={tier.armed_order_id!r}) — cancel it at the broker "
                f"first (`alphalens broker cancel`), then disarm again"
            )
    cancelled: list[str] = []
    for tier in open_tiers:
        append_entry_trail_line(
            {"kind": KIND_CANCELLED, "crid": tier.crid, "note": note}, path=path
        )
        cancelled.append(tier.crid)
    return cancelled


# --- Compaction (memo G4) ----------------------------------------------------


@dataclass
class _CompactionTracker:
    """The per-crid latest/min index bookkeeping behind
    :func:`compact_entry_trail_lines` — which known-kind line indexes must
    survive compaction for the fold to come out identical.

    MUTABLE by design (unlike this module's frozen dataclasses): ``note()``
    updates the four index dicts in place across a single compaction pass and
    the instance is discarded after ``kept_indexes()``."""

    latest_watch_open: dict[str, int] = field(default_factory=dict)
    min_trough: dict[str, tuple[float, int]] = field(default_factory=dict)
    latest_state: dict[str, int] = field(default_factory=dict)
    latest_terminal: dict[str, int] = field(default_factory=dict)

    def note(self, crid: str, kind: str, record: Mapping[str, Any], index: int) -> None:
        """Track one known-kind record at ``index`` (file order = time order)."""
        if kind in ENTRY_TRAIL_TERMINAL_KINDS:
            self.latest_terminal[crid] = index
            return
        self.latest_state[crid] = index
        if kind == KIND_WATCH_OPEN:
            self.latest_watch_open[crid] = index
        elif kind == KIND_TROUGH:
            trough = _finite_positive_float(record.get(KIND_TROUGH))
            if trough is not None:
                prior = self.min_trough.get(crid)
                if prior is None or trough <= prior[0]:
                    self.min_trough[crid] = (trough, index)

    def kept_indexes(self) -> set[int]:
        """Every tracked index that must be preserved."""
        kept = set(self.latest_watch_open.values())
        kept.update(index for _trough, index in self.min_trough.values())
        kept.update(self.latest_state.values())
        kept.update(self.latest_terminal.values())
        return kept


def compact_entry_trail_lines(raw_lines: Iterable[str]) -> list[str]:
    """The MINIMAL set of raw lines that folds IDENTICALLY to ``raw_lines``,
    in their original relative order (the fold's latest-kind semantics are
    file-order based).

    Kept per crid: the latest ``watch_open``, the record achieving the MIN
    trough (equals the latest under the ratchet invariant, but min is the
    fold-equivalent choice), the latest non-terminal state record, and the
    latest terminal record — :data:`COMPACTED_LINES_PER_CRID_BOUND` lines at
    most, however long the input. UNKNOWN kinds and malformed/missing-crid
    lines are PRESERVED VERBATIM (memo G4 — dropping a malformed line would
    silently clear the fold's fail-closed count; dropping an unknown kind is
    the standalone-stops compactor bug this journal must never inherit).
    Blank lines are dropped. Pure: no I/O, input never mutated."""
    materialized = [raw_line.rstrip("\n") for raw_line in raw_lines]
    keep: set[int] = set()
    tracker = _CompactionTracker()

    for index, raw_line in enumerate(materialized):
        line = raw_line.strip()
        if not line:
            continue
        record = _parse_record(line)
        crid = None if record is None else _record_crid(record)
        if record is None or crid is None:
            keep.add(index)  # malformed / missing crid — preserved verbatim
            continue
        kind = record.get("kind")
        if kind not in ENTRY_TRAIL_KINDS:
            keep.add(index)  # unknown kind — preserved verbatim (G4)
            continue
        tracker.note(crid, kind, record, index)

    keep.update(tracker.kept_indexes())
    return [materialized[index] for index in sorted(keep)]


def compact_entry_trail_journal() -> None:
    """Atomically rewrite the entry-trails journal with its compacted form.

    Mirrors ``control_loop._compact_standalone_stop_journal``: a NO-OP when
    the journal is absent or holds no non-blank lines (never creates or
    truncates a file with nothing to compact); otherwise temp file in the
    SAME dir + ``os.replace`` (atomic rename on POSIX — a crash mid-rewrite
    leaves the old journal intact). Call ONCE at daemon startup
    (``build_default_deps``), BEFORE the tick loop, so no concurrent tick can
    race the rewrite against an append."""
    path = _entry_trail_journal_path()
    if not path.exists():
        return
    raw_lines = path.read_text(encoding="utf-8").splitlines()
    if not any(line.strip() for line in raw_lines):
        return
    compacted = compact_entry_trail_lines(raw_lines)

    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=".entry_trails.compact-", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            for line in compacted:
                fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, str(path))
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
