"""Entry-trailing WATCHER engine (PR-T1) — the pure state machine + trough
tracker behind ``ALPHALENS_BROKER_ENTRY_TRAIL_BPS``.

Design memo: ``docs/research/entry_trailing_design_2026_08_12.md`` (LOCKED
2026-08-12), §5 state machine + journals, §3 guards G2/G5/G6/G9.

**Scope of PR-T1 — DRY-RUN ONLY.** This module NEVER places, amends, or
cancels a broker order. It is a self-contained, dependency-injected unit: it
takes injected per-tick price readings and returns state transitions +
journal-line INTENTS + alert INTENTS. The caller (the WIRE phase, PR-T2) turns
the intents into journal appends and throttled Telegram alerts. The "fire" of
the dry run is a ``would_fire`` alert ("would fire @ trigger X") plus a
``trail_armed`` journal marker — no order object is ever constructed. With the
flag unset/``0`` nothing constructs a watcher at all, so runtime is
byte-identical to today.

Two units, mirroring the exit side but INVERTED:

- :class:`EntryTroughTracker` (T1a) — the per-tier running LOW, the entry-side
  mirror of ``control_loop._update_peaks`` (a running HIGH via ``max``). It
  ratchets DOWN only, yields at most one ``trough`` journal-intent per tick,
  and is SEEDED with the journaled minimum on restart (the exit peak reseeds
  downward safely; the entry trough must not forget its low upward — memo §5).
- :class:`EntryTierWatcher` (T1b) — the per-tier ``WATCHING -> TOUCHED ->``
  terminal state machine. Terminals in the dry run: ``WOULD_FIRE`` (alert-only,
  journals a ``trail_armed`` marker — NO order), ``SUSPENDED`` (G9 deep
  decline), ``EXPIRED`` (TTL window), ``CANCELLED`` (KILL / pick pulled).

The journal KINDS are imported from :mod:`entry_trails` — never re-declared
here, so the fold/compaction whitelist and the emitter can never drift.

**Price side (memo notes trap #8, pinned here):** the exit peak reads
``point.bid`` (selling a long executes at the bid). The entry watcher tracks
the running low and the touch/trigger comparison on the SAME reference the LIVE
V1 probe used (bid + distance; the server repositioned the trigger off the new
bid low — memo §4b L1). The CALLER extracts the reference scalar from the
``PricePoint`` (``point.bid``) and passes it in as ``TickInput.price``; a
``None`` price is the freshness/trust veto. The eventual FILL side is the ask —
T2 pins the exact executor semantics; T1 detection is bid-referenced.
"""

from __future__ import annotations

import datetime as dt
import enum
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from alphalens_pipeline.brokers.automanager.entry_trails import (
    KIND_CANCELLED,
    KIND_EXPIRED,
    KIND_SUSPENDED,
    KIND_TOUCHED,
    KIND_TRAIL_ARMED,
    KIND_TROUGH,
    KIND_WATCH_OPEN,
)

# --- Constants ---------------------------------------------------------------

_BPS_DENOMINATOR = 10_000
"""Basis-point divisor: ``d = d_bps / 10_000`` (50 bps -> 0.005)."""

# G9 LULD/halt gate (memo §3 G9): a would-fire on the FIRST fresh tick after a
# staleness gap wider than this could be a limit-up/limit-down or halt-reopen
# artifact, not a genuine bounce — suppress the fire (touch/trough still track).
STALE_FIRE_GAP = dt.timedelta(minutes=5)

_ALERT_WOULD_FIRE = "would_fire"
"""Alert KIND for the dry-run fire — NOT a journal kind (never persisted as an
order; the journal marker at the same instant is ``trail_armed``)."""

_ALERT_SUSPENDED = "suspended"
"""Alert KIND for a G9 deep-decline suspend (a tier gave up its watch)."""


def _finite_positive(value: float) -> bool:
    """A real, finite, strictly-positive price — the trough/trigger sanity gate.

    A NaN would freeze every later ``<`` comparison (the running min would
    never move again); a zero/negative price is corruption a min would wrongly
    win. Mirrors ``entry_trails._finite_positive_float`` semantics."""
    return math.isfinite(value) and value > 0.0


# --- T1a: the running-LOW trough tracker -------------------------------------


class EntryTroughTracker:
    """Per-tier running LOW — the entry-side mirror of the exit peak tracker.

    ``observe(price)`` is fed ONE fresh reference price per decision tick and
    ratchets the trough DOWN (``min``). It returns the value to JOURNAL this
    tick (a new running low) or ``None`` (no journal line — the min did not
    fall), guaranteeing at most one ``trough`` line per tick (memo §5).

    On restart the tracker is constructed with ``seeded_trough`` = the fold's
    journaled ``min_trough``; the first fresh price then resolves to
    ``min(seeded_trough, price)`` (memo §5 restart rule). A fresh watch passes
    ``seeded_trough=None`` and the first fresh price seeds it.
    """

    def __init__(self, *, seeded_trough: float | None = None) -> None:
        if seeded_trough is not None and not _finite_positive(seeded_trough):
            seeded_trough = None
        self._trough = seeded_trough

    @property
    def trough(self) -> float | None:
        """The current running minimum, or ``None`` before the first fresh
        price on a watch with no journaled low."""
        return self._trough

    def observe(self, price: float) -> float | None:
        """Feed ONE fresh price; return the trough to journal this tick or
        ``None``. A non-finite/non-positive price is a doubt: no progress, no
        crash (the freshness veto upstream should already exclude these, but
        the tracker must not trust its caller)."""
        if not _finite_positive(price):
            return None
        if self._trough is None or price < self._trough:
            self._trough = price
            return price
        return None


# --- T1b: the per-tier watcher state machine ---------------------------------


class WatchState(enum.Enum):
    """The per-tier watch state (memo §5). ``WATCHING``/``TOUCHED`` are live;
    the rest are terminal. In the DRY RUN ``WOULD_FIRE`` stands in for the
    memo's ``FILLED`` — the would-fire is an alert, not an order, so no
    ``fired`` order state is ever reached here."""

    WATCHING = "watching"
    TOUCHED = "touched"
    WOULD_FIRE = "would_fire"
    # PR-T2b: the native Saxo trailing order is RESTING at the broker — the
    # server owns the ratchet + fire. Terminal for the pure engine (the bot no
    # longer drives touch/trough/fire); the wire monitors the fill via reconcile.
    TRAIL_ARMED = "trail_armed"
    SUSPENDED = "suspended"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


_TERMINAL_STATES = frozenset(
    {
        WatchState.WOULD_FIRE,
        WatchState.TRAIL_ARMED,
        WatchState.SUSPENDED,
        WatchState.EXPIRED,
        WatchState.CANCELLED,
    }
)


@dataclass(frozen=True)
class TierWatchConfig:
    """The immutable per-tier watch parameters (one ``crid``).

    ``d_bps`` is INJECTED (from ``entry_trails.entry_trail_bps()`` at the call
    site) — the pure engine never re-reads the environment. ``window_end`` is a
    concrete UTC instant the caller resolves from
    ``advance_trading_sessions(watch_open_date, DEFAULT_ORDER_TTL_DAYS)`` (memo
    §5 TTL). ``next_tier_limit`` is the NEXT-deeper tier's limit for the G9
    depth suspend; ``None`` on the deepest tier (no depth suspend)."""

    crid: str
    tier_limit: float
    d_bps: int
    window_end: dt.datetime
    qty: float
    fx_rate: float | None = None
    next_tier_limit: float | None = None


@dataclass(frozen=True)
class TickInput:
    """One decision tick's input. ``price`` is the reference scalar the caller
    extracted from the fresh ``PricePoint`` (``point.bid``); ``None`` = the
    freshness/trust veto (no trustworthy price this tick). ``session_boundary``
    is ``True`` on the first tick of a new session (caller-detected) — it arms
    the open-check (memo §5 CRITICAL-2)."""

    now: dt.datetime
    price: float | None
    session_boundary: bool = False


@dataclass(frozen=True)
class JournalIntent:
    """A line to append to ``entry_trails.jsonl`` (the WIRE phase persists it).
    The final JSON is ``{"kind": kind, "crid": crid, **payload}`` — so a
    ``watch_open`` payload MUST carry ``limit``/``qty``/``fx_rate`` under those
    exact keys for the G5 reservation fold to value it."""

    crid: str
    kind: str
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AlertIntent:
    """A throttled Telegram alert the caller routes through
    ``deps.alert_throttled(message, throttle_key)`` (memo §3 alert throttling).
    NOT persisted as a journal line and NEVER an order."""

    crid: str
    kind: str
    message: str
    throttle_key: str


@dataclass(frozen=True)
class TickResult:
    """The outcome of one ``process`` / ``cancel`` call: the journal lines to
    append, the alerts to send, and the state AFTER the call."""

    journal_intents: tuple[JournalIntent, ...]
    alerts: tuple[AlertIntent, ...]
    state: WatchState


class EntryTierWatcher:
    """The per-tier DRY-RUN watcher (memo §5 state machine).

    ``WATCHING`` waits for the tier limit to be touched; ``TOUCHED`` tracks the
    running low and fires when a bounce reaches ``trough*(1+d)``. Every tick is
    a single :meth:`process` call — pure input to output, no I/O, no clock (the
    tick carries ``now``), no broker."""

    def __init__(
        self,
        config: TierWatchConfig,
        *,
        seeded_trough: float | None = None,
        initial_state: WatchState = WatchState.WATCHING,
        native_trail: bool = False,
    ) -> None:
        self._config = config
        self._state = initial_state
        # PR-T2b: in native mode a real Saxo trailing order is placed by the wire
        # at TOUCH and the SERVER ratchets + fires — so the engine must NOT
        # self-fire (the would-fire branch is suppressed; the watch waits in
        # TOUCHED for the wire to arm the order out-of-band). Default False keeps
        # the PR-T1 dry-run self-fire the engine unit tests exercise directly.
        self._native_trail = native_trail
        self._trough = EntryTroughTracker(seeded_trough=seeded_trough)
        # Wall-clock of the last FRESH reading — the staleness-gap reference.
        self._last_fresh_now: dt.datetime | None = None
        # memo §5 CRITICAL-2: after a session boundary, block the would-fire
        # until a NEW post-open low forms (a fresh bounce reference), so the
        # stale carried trigger is never handed to the market into a gap.
        self._awaiting_fresh_low = False

    @classmethod
    def open_watch(
        cls, config: TierWatchConfig, *, day1_gap_clear: bool
    ) -> tuple[EntryTierWatcher | None, tuple[JournalIntent, ...]]:
        """Open a FRESH watch, honoring the day-1 gap verdict (memo §5): a
        gap-through-open (``day1_gap_clear=False``) opens NO watch on day 1 (no
        trough tracked through a falling-knife day), returning ``(None, ())``.
        Otherwise returns the watcher + its ``watch_open`` journal-intent. The
        engine never re-gates the fire.

        NOTE (PR-T1): the production wiring does NOT call this method — day-1
        gap composition happens UPSTREAM via ``control_loop._day1_gap_gate_defers``
        (it defers the whole pick before the entry-trail intercept ever runs),
        so a gap-through pick opens no watch without the engine needing the
        verdict. This constructor-with-gate stays as the tested single-source
        engine API for the T2 executor; the wiring opens watches through
        ``control_loop._open_entry_watches`` (direct per-tier append)."""
        if not day1_gap_clear:
            return None, ()
        watcher = cls(config)
        intent = JournalIntent(
            crid=config.crid,
            kind=KIND_WATCH_OPEN,
            payload={
                "limit": config.tier_limit,
                "qty": config.qty,
                "d_bps": config.d_bps,
                "window_end": config.window_end.isoformat(),
                "fx_rate": config.fx_rate,
            },
        )
        return watcher, (intent,)

    @property
    def state(self) -> WatchState:
        return self._state

    @property
    def is_terminal(self) -> bool:
        return self._state in _TERMINAL_STATES

    def process(self, tick: TickInput) -> TickResult:
        """Advance the watch by one decision tick and return the intents.

        Order of checks: terminal no-op -> arm the open-check on a boundary ->
        EXPIRY (time-based, runs even on a vetoed tick) -> freshness veto (no
        progress on ``None``/non-finite) -> touch -> trough -> G9 suspend ->
        would-fire (gated by the staleness gap and the open-check)."""
        if self.is_terminal:
            return TickResult((), (), self._state)

        if tick.session_boundary:
            self._awaiting_fresh_low = True

        # EXPIRY is time-based (memo §5 TTL) — it must fire even without a fresh
        # price. G6 (verify-before-terminal against an outstanding order id) is
        # a WIRE-phase concern; the pure engine emits the intent.
        if tick.now >= self._config.window_end:
            self._state = WatchState.EXPIRED
            return TickResult((self._journal(KIND_EXPIRED),), (), self._state)

        price = tick.price
        if price is None or not _finite_positive(price):
            return TickResult((), (), self._state)  # freshness/trust veto

        stale_gap = (
            self._last_fresh_now is not None and tick.now - self._last_fresh_now > STALE_FIRE_GAP
        )
        self._last_fresh_now = tick.now

        intents: list[JournalIntent] = []

        if self._state is WatchState.WATCHING:
            if price > self._config.tier_limit:
                return TickResult((), (), self._state)  # not touched yet
            self._state = WatchState.TOUCHED
            intents.append(self._journal(KIND_TOUCHED))

        # TOUCHED: trough tracking + G9 suspend + would-fire. Also runs on the
        # touch tick (falls through) — but a fire cannot happen there, the
        # trough equals the price and the trigger is above it by d.
        journaled = self._trough.observe(price)
        if journaled is not None:
            intents.append(self._journal(KIND_TROUGH, trough=journaled))
            self._awaiting_fresh_low = False  # a fresh (post-open) low formed

        if self._trough_below_next_tier():
            self._state = WatchState.SUSPENDED
            intents.append(
                self._journal(
                    KIND_SUSPENDED,
                    trough=self._trough.trough,
                    next_tier_limit=self._config.next_tier_limit,
                )
            )
            return TickResult(tuple(intents), (self._suspend_alert(),), self._state)

        if self._native_trail:
            # Native executor (PR-T2b): the resting Saxo trailing order is the
            # fire event, not the bot. Stay in TOUCHED tracking the trough (for
            # measurement + the wire's touch reference) — the wire places the
            # order and transitions the tier to TRAIL_ARMED out-of-band.
            return TickResult(tuple(intents), (), self._state)

        trigger = self._trigger()
        fire_blocked = stale_gap or self._awaiting_fresh_low
        if trigger is not None and not fire_blocked and price >= trigger:
            self._state = WatchState.WOULD_FIRE
            intents.append(self._journal(KIND_TRAIL_ARMED, trigger=trigger))
            return TickResult(tuple(intents), (self._would_fire_alert(trigger),), self._state)

        return TickResult(tuple(intents), (), self._state)

    def mark_armed(self) -> None:
        """Transition to TRAIL_ARMED after the wire places the native trailing
        order (PR-T2b). Terminal for the engine — the resting order is the
        broker's; the bot stops driving touch/trough/fire. A no-op once terminal
        (a CANCELLED/SUSPENDED/EXPIRED tier must never be resurrected to armed)."""
        if self.is_terminal:
            return
        self._state = WatchState.TRAIL_ARMED

    def cancel(self) -> TickResult:
        """Terminate the watch with a ``cancelled`` intent (KILL transition /
        pick pulled). A no-op once terminal. Cancel only removes intent — it
        never fires, so it carries no alert and is ungated by anything (memo G2
        KILL-transition cancels are risk-reducing)."""
        if self.is_terminal:
            return TickResult((), (), self._state)
        self._state = WatchState.CANCELLED
        return TickResult((self._journal(KIND_CANCELLED),), (), self._state)

    # --- helpers -------------------------------------------------------------

    def _journal(self, kind: str, **payload: Any) -> JournalIntent:
        return JournalIntent(crid=self._config.crid, kind=kind, payload=payload)

    def _trough_below_next_tier(self) -> bool:
        """G9 depth suspend: the running low fell below the NEXT tier's limit,
        so a deeper move is that tier's job (memo §3 G9). The deepest tier has
        no next limit and never depth-suspends."""
        nxt = self._config.next_tier_limit
        trough = self._trough.trough
        return nxt is not None and trough is not None and trough < nxt

    def _trigger(self) -> float | None:
        """The would-fire level ``trough*(1+d)``. ``None`` before the trough is
        seeded. NOT clamped to the tier limit: the trough may fall below it, so
        the trigger follows DOWN and a fire below the limit is a better entry
        (negative concession — memo §2 evidence)."""
        trough = self._trough.trough
        if trough is None:
            return None
        return trough * (1.0 + self._config.d_bps / _BPS_DENOMINATOR)

    def _would_fire_alert(self, trigger: float) -> AlertIntent:
        return AlertIntent(
            crid=self._config.crid,
            kind=_ALERT_WOULD_FIRE,
            message=f"entry-trail {self._config.crid} would fire @ trigger {trigger:.4f} (dry run)",
            throttle_key=f"entry-trail:would-fire:{self._config.crid}",
        )

    def _suspend_alert(self) -> AlertIntent:
        return AlertIntent(
            crid=self._config.crid,
            kind=_ALERT_SUSPENDED,
            message=(
                f"entry-trail {self._config.crid} suspended: trough "
                f"{self._trough.trough} below next tier {self._config.next_tier_limit}"
            ),
            throttle_key=f"entry-trail:suspended:{self._config.crid}",
        )
