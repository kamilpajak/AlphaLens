"""The streaming price rail's handles, health state machine and session window.

Extracted from ``control_loop`` unchanged (architecture audit 2026-10-02,
finding 2 / #1677). The tick never calls any of this directly: it reaches the
stream only through ``LoopDeps.stream_tick`` / ``stream_trigger``, which
:func:`_build_stream_handles` builds and ``build_default_deps`` wires. That is
the seam this module is cut along -- nothing here is reachable from
``run_once`` except through the record.

The concrete Saxo imports stay INSIDE the function bodies that need them, as
they were: the ADR 0014 rule in ``tests/test_module_dependencies.py`` forbids
``automanager`` from importing ``brokers.saxo`` at top level, and this module
is not exempt from it.
"""

from __future__ import annotations

import datetime as dt
import logging
import math
import os
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from alphalens_pipeline.brokers.automanager import state_paths

if TYPE_CHECKING:  # annotations only -- see the module docstring on saxo imports
    import threading

    from broker_contract.contract import Broker

    from alphalens_pipeline.brokers.automanager.streaming_trigger import StreamTrigger

logger = logging.getLogger(__name__)


# --- Streaming (dark, SIM-only) env gates + liveness metric --------------------
# Master gate for the Saxo WebSocket early-wake reader (design memo
# saxo_streaming_design_2026_07_24.md). DEFAULTS OFF: unset -> wake_event=None and
# run_daemon is byte-identical to today's blocking sleep. Mirrors the OCO/AMEND
# gates — read at call time (no import-time snapshot), restart-consistent.
_STREAMING_ENABLED_ENV = "ALPHALENS_BROKER_STREAMING_ENABLED"


# Main-thread stale-alert threshold (seconds). Kept <= poll_seconds so the alert
# never lags a full poll cycle behind the already-covered protection, and >= the
# observed ~20-30s SIM heartbeat cadence so a quiet-but-alive stream is not flagged.
_STREAM_STALE_ENV = "ALPHALENS_BROKER_STREAM_STALE_S"


_DEFAULT_STREAM_STALE_S = 45.0


# --- Stream breaker re-arm episode constants (rearm design memo §5) -----------
# All named, all sited here beside _DEFAULT_STREAM_STALE_S; deliberately NO env
# knob (ALPHALENS_BROKER_STREAM_DEBOUNCE_S is a documented knob on this exact
# surface that already rotted dead) — tuning here is a code change with a test.
#
# Cooldown-ladder floor between re-arm trials. Must exceed the probed ~31s
# wall-clock cost of one full in-breaker backoff budget (1+2+4+8+16s sleeps +
# 6 connects — matching the incident's 08:43:26 -> 08:46:21 gap), so a re-arm
# cycle can never spend connects faster than the failing state it replaces. It
# also exceeds both stale_after_s (45s) and the 45s poll grid, guaranteeing at
# most one trial per protective pass.
_STREAM_REARM_FLOOR_S = 60.0


# Ladder ceiling: bounds a long outage at 4 connect attempts/hour. A dark
# stream costs at most one poll period of extra wake latency, never protection,
# so 15 min is the worst-case dark-after-vendor-recovery window (vs the 14h
# observed on 2026-08-22).
_STREAM_REARM_CEILING_S = 900.0


# Delivery-confirmed dwell before (a) the recovery CLOSE page and (b) the
# ladder resets to the floor. 10x recv_timeout_s (30s) ~= 10-15 consecutive SIM
# heartbeats at the documented 20-30s cadence — a deliver-once-then-die flapper
# climbs the ladder instead of looping at the floor. A normal reconnect never
# reaches this code (it never trips), so the dwell cannot affect healthy
# operation.
_STREAM_HEALTHY_DWELL_S = 300.0


# Rolling window for the flap escalation: 4x the ceiling, so a window that saw
# the threshold has necessarily seen the ladder fail to converge.
_STREAM_FLAP_WINDOW_S = 3600.0


# Trips inside the window before ONE CRITICAL page + the OPEN-page latch (the
# CLOSE page is never suppressed). Mirrors _MAX_CONSECUTIVE_PLACE_FAILURES —
# the repo's existing escalate-once threshold on the alert throttle.
_STREAM_FLAP_ESCALATE_AT = 3


# Prometheus liveness gauge: seconds since the last streamed message (age). Watched
# by the AlphalensBrokerStreamStale rule shipped in
# deploy/monitoring/prometheus/rules/alphalens.yaml (repo SoT; the live copy is
# hand-synced — see deploy/systemd/README.md §8.5), distinct from the per-poll
# heartbeat gauge (a dead stream still emits heartbeats — the poll backstop keeps
# running).
_STREAM_LAST_MESSAGE_METRIC_NAME = "alphalens_broker_manager_stream_last_message_age_seconds"


# Stream-state gauge base names (rearm design memo §4.6). All co-emitted with the
# age gauge in ONE atomic emit per tick (_emit_stream_gauge) — the write
# OVERWRITES the whole stream domain textfile, so an omitted key deletes its
# series and a second call to the domain clobbers the first.
_STREAM_READER_UP_METRIC_NAME = "alphalens_broker_manager_stream_reader_up"


# EPISODE-scoped: 1 from the down edge until the delivery-confirmed close. It
# deliberately does NOT flicker per re-arm trial — a per-trial gauge resets to 0
# on every ladder rung, so no Prometheus `for:` longer than one rung could fire.
_STREAM_BREAKER_OPEN_METRIC_NAME = "alphalens_broker_manager_stream_breaker_open"


# A LEVEL for eyeballing streak composition — rate()/increase() on it are
# nonsense. The number the 2026-08-22 incident journal could not recover.
_STREAM_CONSECUTIVE_FAILURES_METRIC_NAME = "alphalens_broker_manager_stream_consecutive_failures"


# Monotonic counter: survives a tick gap; feeds the flapping rule.
_STREAM_TRIPS_TOTAL_METRIC_NAME = "alphalens_broker_manager_stream_trips_total"


# 0/1 trading-window gauge from _make_stream_session_window (memo §3 Q5):
# emitted but referenced by NO shipped rule — making a rule session-aware
# later is a one-line YAML change, not a code change. The trip page itself
# stays unconditional (weekend quiet comes from the episode latch).
_STREAM_IN_SESSION_METRIC_NAME = "alphalens_broker_manager_stream_in_session"


# Session gate for the shared price stream: outside market hours no frames
# flow, so a 24/7 WebSocket recv-times-out every ~3min into a reconnect +
# subscription-recreate cycle all night. Behind its own flag (default OFF ->
# None -> today's behavior); the stream side is fail-open by contract, so a
# raising predicate can never silence the stream during trading hours.
_STREAM_SESSION_GATE_ENV = "ALPHALENS_SAXO_STREAM_SESSION_GATE"


# The window is [session_open - WARMUP, session_close + GRACE]. WARMUP exists
# because the connection must be up and the create-subscription snapshot
# applied BEFORE the open — the DelayedByMinutes flag arrives ONLY in that
# snapshot (2026-08-18 probe), so connecting at the bell would veto the first
# minutes of quotes. GRACE keeps the closing auction's last prints flowing.
_STREAM_SESSION_WARMUP = dt.timedelta(minutes=15)


_STREAM_SESSION_GRACE = dt.timedelta(minutes=10)


# The venue set the window is computed over (#1238 PR 5). Comma-separated
# MICs; unset -> ("XNYS",), byte-identical to the pre-#1238 single-venue gate
# (XNYS and XNAS share the regular session, so one US calendar covers both).
# CONFIG-DRIVEN on purpose, never derived from open watches/positions: the
# stream must be up (warmup included) BEFORE the first watch on a new venue
# can tick — a derived window would hold the socket closed exactly when the
# venue's first pick needs quotes. INVARIANT: every configured venue's hull
# (open - warmup .. close + grace) must stay inside one UTC day — the per-day
# window memo keys on UTC now.date(); an Asian venue opening near 00:00 UTC
# needs that memo redesigned first.
_STREAM_SESSION_VENUES_ENV = "ALPHALENS_SAXO_STREAM_SESSION_VENUES"


_STREAM_SESSION_DEFAULT_VENUES: tuple[str, ...] = ("XNYS",)


def _stream_session_venues() -> tuple[str, ...]:
    raw = os.environ.get(_STREAM_SESSION_VENUES_ENV, "")
    venues = tuple(
        dict.fromkeys(token.strip().upper() for token in raw.split(",") if token.strip())
    )
    return venues or _STREAM_SESSION_DEFAULT_VENUES


def _stream_session_gate_enabled() -> bool:
    return os.environ.get(_STREAM_SESSION_GATE_ENV) == "1"


def _make_stream_session_window(
    clock: Callable[[], dt.datetime] | None = None,
) -> Callable[[], bool]:
    """Build the "is now inside the trading window" predicate for the price
    stream's session gate.

    The window comes from the exchange-parametrized calendar helpers
    (``market.calendar`` on ``exchange_calendars`` — real holidays, early
    closes, DST), NEVER hand-rolled hours: half-days resolve to the actual
    per-session close, a non-trading day is False all day.

    A calendar exception PROPAGATES by design — the stream side fails OPEN on
    a raise (connects, warns once). Swallowing it into False here would let a
    calendar bug silence the stream during trading hours, the one failure the
    gate's safety contract forbids.

    Per-day session bounds are memoized (the reader polls the predicate every
    second while asleep); only successful lookups are cached, so a transient
    raise is retried on the next poll.

    The window is the per-day HULL over ``_stream_session_venues()`` — from
    the earliest trading venue's open − WARMUP to the latest close + GRACE,
    skipping each venue on its own holidays (#1238 PR 5); no venue trading
    means False all day. A hull, not a union: the socket stays up between a
    European close and the US open — reconnect churn in that gap is exactly
    what the gate exists to avoid overnight, and the gap is bounded.

    UTC-date note: every supported hull (the European venues XWAR/XETR/XPAR open
    06:45 UTC at the earliest in summer, 07:45 in winter, to XNYS 21:10 UTC
    at the latest) never crosses UTC midnight, so ``now.date()`` in UTC is
    always the session date being asked about.
    """
    venues = _stream_session_venues()
    read_clock = clock or (lambda: dt.datetime.now(dt.UTC))
    # Single-writer by construction: the predicate is called ONLY from the
    # stream's reader thread (_supervise), so this memo needs no lock — do not
    # share the predicate across threads without adding one.
    bounds_by_day: dict[dt.date, tuple[dt.datetime, dt.datetime] | None] = {}

    def _bounds(day: dt.date) -> tuple[dt.datetime, dt.datetime] | None:
        from alphalens_pipeline.market.calendar import (
            is_trading_day,
            session_close_utc,
            session_open_utc,
        )

        opens: list[dt.datetime] = []
        closes: list[dt.datetime] = []
        for venue in venues:
            if not is_trading_day(day, venue):
                continue
            opens.append(session_open_utc(day, venue))
            closes.append(session_close_utc(day, venue))
        if not opens:
            return None
        return (min(opens) - _STREAM_SESSION_WARMUP, max(closes) + _STREAM_SESSION_GRACE)

    def _in_window() -> bool:
        now = read_clock()
        day = now.date()
        if day not in bounds_by_day:
            # One live entry is enough (the daemon runs for months): drop
            # yesterday's bounds before caching today's.
            bounds_by_day.clear()
            bounds_by_day[day] = _bounds(day)
        bounds = bounds_by_day[day]
        if bounds is None:
            return False
        window_start, window_end = bounds
        return window_start <= now <= window_end

    return _in_window


def _stream_session_window_if_enabled() -> Callable[[], bool] | None:
    """The predicate ``get_shared_price_stream`` should construct the stream
    with: None (today's behavior, byte-identical) unless
    ``ALPHALENS_SAXO_STREAM_SESSION_GATE`` is exactly ``"1"``."""
    if not _stream_session_gate_enabled():
        return None
    return _make_stream_session_window()


def _streaming_enabled() -> bool:
    """Whether the dark streaming early-wake reader is enabled (read at call time).

    Master gate, mirroring ``_oco_enabled`` / ``_amend_enabled``. Defaults OFF —
    unset -> the daemon runs poll-only, byte-identical to today."""
    return os.environ.get(_STREAMING_ENABLED_ENV) == "1"


def _stream_stale_s() -> float:
    """The main-thread stale-alert threshold in seconds (read at call time).

    ``ALPHALENS_BROKER_STREAM_STALE_S`` override, falling back to
    ``_DEFAULT_STREAM_STALE_S``. A non-finite / non-positive / unparsable value
    falls back to the default (a bad env value must never disable the backstop
    alert with a zero threshold or a never-firing infinity)."""
    raw = os.environ.get(_STREAM_STALE_ENV)
    if raw is None:
        return _DEFAULT_STREAM_STALE_S
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_STREAM_STALE_S
    if not math.isfinite(value) or value <= 0:
        return _DEFAULT_STREAM_STALE_S
    return value


def _emit_stream_gauge(values: Mapping[str, float]) -> None:
    """Best-effort Prometheus stream-state gauges, in ONE atomic write per tick
    (rearm design memo §4.6). ``values`` maps gauge BASE names to values; the
    per-instance ``{job=...}`` label is applied here (the SAME job as the
    heartbeat's, ``state_paths.metrics_job()`` — it is the same daemon instance).
    The write atomically OVERWRITES the stream's OWN domain textfile
    (``state_paths.stream_metrics_job()``) — never the heartbeat's, which a
    shared domain would clobber — so every stream gauge MUST land in this single
    call: an omitted key deletes its series. A textfile-dir hiccup must never
    crash the loop — the poll backstop covers protection regardless of
    observability."""
    from alphalens_pipeline.observability.textfile import emit_domain_metrics

    job = state_paths.metrics_job()
    try:
        emit_domain_metrics(
            state_paths.stream_metrics_job(),
            {f'{name}{{job="{job}"}}': value for name, value in values.items()},
        )
    except OSError:
        logger.warning("broker-manager stream gauges emit failed", exc_info=True)


@dataclass
class _StreamEpisodeState:
    """Daemon-lifetime breaker-episode state threaded through the stream-tick
    helpers (memo §4.5) — one instance per ``_make_stream_tick`` closure."""

    started_mono: float
    last_trips_total: int
    stale_s: float
    episode_open: bool = False
    episode_rearms: int = 0
    cooldown_s: float = _STREAM_REARM_FLOOR_S
    next_trial_mono: float = 0.0
    delivered_at_rearm: int = 0
    up_since: float | None = None
    trip_times: deque[float] = field(default_factory=deque)
    flap_latched: bool = False


@dataclass(frozen=True)
class _StreamHealthSample:
    """One tick's delivery-backed health sample (memo §4.4 step 2). ``up``
    requires a frame delivered on THIS trial (``frames > delivered_at_rearm``)
    — never ``is_streaming``, which ``rearm()`` sets True before any evidence;
    ``reader_dark`` includes ``not is_running()`` so a reader thread that
    crashed WITHOUT tripping is recovered by the same path."""

    running: bool
    streaming: bool
    frames: int
    silence: float | None
    reader_dark: bool
    up: bool


def _sample_stream_health(
    trigger: StreamTrigger, state: _StreamEpisodeState
) -> _StreamHealthSample:
    running = trigger.is_running()
    streaming = trigger.is_streaming
    frames = trigger.frames_delivered
    silence = trigger.seconds_since_last_message()
    return _StreamHealthSample(
        running=running,
        streaming=streaming,
        frames=frames,
        silence=silence,
        reader_dark=(not running) or (not streaming),
        up=(
            running
            and streaming
            and frames > state.delivered_at_rearm
            and silence is not None
            and silence <= state.stale_s
        ),
    )


def _account_stream_flaps(
    state: _StreamEpisodeState, trips: int, now: float, alert: Callable[[str], None]
) -> None:
    """Track breaker trips inside the flap window; latch ONE CRITICAL page when
    the escalation threshold is crossed (memo §7.1)."""
    for _ in range(max(0, trips - state.last_trips_total)):
        state.trip_times.append(now)
    state.last_trips_total = trips
    while state.trip_times and now - state.trip_times[0] > _STREAM_FLAP_WINDOW_S:
        state.trip_times.popleft()
    flap_active = len(state.trip_times) >= _STREAM_FLAP_ESCALATE_AT
    if flap_active and not state.flap_latched:
        alert(
            f"CRITICAL: saxo stream flapping — {len(state.trip_times)} breaker trips "
            f"inside {_STREAM_FLAP_WINDOW_S / 60:.0f} min; episode-OPEN pages "
            "suppressed until the window clears (recovery pages never are)"
        )
    state.flap_latched = flap_active


def _drive_stream_state_machine(
    state: _StreamEpisodeState,
    trigger: StreamTrigger,
    alert: Callable[[str], None],
    *,
    now: float,
    health: _StreamHealthSample,
) -> float | None:
    """CLOSED -> OPEN -> TRIAL -> CLOSED episode machine (memo §4.5). Returns
    the silence sample, refreshed after a TRIAL's ``rearm()``."""
    silence = health.silence
    if not state.episode_open:
        if health.reader_dark:
            # CLOSED -> OPEN: arm the ladder; NO trial on the opening tick.
            state.episode_open = True
            state.episode_rearms = 0
            state.cooldown_s = _STREAM_REARM_FLOOR_S
            state.next_trial_mono = now + state.cooldown_s
            state.up_since = None
            if not state.flap_latched:
                alert(
                    "saxo stream DOWN — reader dark, re-arm ladder engaged; "
                    "running on poll backstop"
                )
    elif health.up:
        if state.up_since is None:
            state.up_since = now
        if now - state.up_since >= _STREAM_HEALTHY_DWELL_S:
            # OPEN -> CLOSED: delivery-confirmed recovery held for the full
            # dwell. NEVER suppressed by the flap latch — the operator must
            # always see an episode end.
            state.episode_open = False
            state.cooldown_s = _STREAM_REARM_FLOOR_S
            state.up_since = None
            alert(
                f"saxo stream RECOVERED — delivery-confirmed after "
                f"{state.episode_rearms} re-arm trial(s); ladder reset"
            )
    else:
        state.up_since = None  # the dwell must be CONTINUOUS delivery-backed health
        if health.reader_dark and now >= state.next_trial_mono:
            # OPEN -> TRIAL: at most ONE trial per tick, no page. The ladder
            # advances BEFORE the rearm so a raising spawn cannot burn
            # trials at the floor rate.
            state.delivered_at_rearm = health.frames
            trigger.reset_liveness()  # an hours-old epoch must never page stream-dead
            state.cooldown_s = min(state.cooldown_s * 2.0, _STREAM_REARM_CEILING_S)
            state.next_trial_mono = now + state.cooldown_s
            state.episode_rearms += 1
            trigger.rearm()
            silence = trigger.seconds_since_last_message()
    return silence


def _emit_stream_tick_gauges(
    state: _StreamEpisodeState,
    trigger: StreamTrigger,
    emit_gauge: Callable[[Mapping[str, float]], None],
    session_predicate: Callable[[], bool],
    *,
    now: float,
    health: _StreamHealthSample,
    silence: float | None,
    trips: int,
) -> None:
    """ONE atomic multi-key gauge write (all SIX keys: an omitted key deletes
    its series). The age key is never omitted: epoch None -> seconds since
    closure build. Fallback diverges from memo §4.6's "seconds since reader
    start": started_mono is the CLOSURE build time (daemon start). Harmless —
    no alert keys on the absolute value while the breaker is open."""
    age = silence if silence is not None else now - state.started_mono
    try:
        session = 1.0 if session_predicate() else 0.0
    except Exception:  # calendar fail-open: report in-session, keep gauges
        logger.warning("streaming: session predicate raised — reporting in-session")
        session = 1.0
    emit_gauge(
        {
            _STREAM_READER_UP_METRIC_NAME: 1.0 if (health.running and health.streaming) else 0.0,
            _STREAM_BREAKER_OPEN_METRIC_NAME: 1.0 if state.episode_open else 0.0,
            _STREAM_LAST_MESSAGE_METRIC_NAME: age,
            _STREAM_CONSECUTIVE_FAILURES_METRIC_NAME: float(trigger.consecutive_failures),
            _STREAM_TRIPS_TOTAL_METRIC_NAME: float(trips),
            _STREAM_IN_SESSION_METRIC_NAME: session,
        }
    )


def _make_stream_tick(
    trigger: StreamTrigger,
    *,
    get_bearer: Callable[[], str],
    alert: Callable[[str], None],
    alert_throttled: Callable[[str, str], bool],
    stale_s: float,
    emit_gauge: Callable[[Mapping[str, float]], None] = _emit_stream_gauge,
    monotonic: Callable[[], float] = time.monotonic,
    in_session: Callable[[], bool] | None = None,
) -> Callable[[], None]:
    """Build the per-tick streaming hook run by ``run_daemon`` on the MAIN thread.

    Rearm design memo ``saxo_stream_breaker_rearm_design_2026_08_22.md``
    §4.4-§4.6. Every tick, unconditionally, in this order:

    1. **Push the bearer FIRST** — before any dark-branch return. The pre-rearm
       tick returned from the breaker branch before ``push_token``, freezing the
       reader's token at the trip instant while ``alphalens-saxo-refresh``
       rotated the real one every ~20 min — a re-armed reader would have burned
       its single half-open trial on a 401.
    2. **Sample delivery-backed health.** ``up`` requires a frame delivered on
       THIS trial (``frames_delivered > delivered_at_rearm``) — never
       ``is_streaming``, which ``rearm()`` sets True before any evidence; and
       ``reader_dark`` includes ``not is_running()`` so a reader thread that
       crashed WITHOUT tripping is recovered by the same path.
    3. **Drive the episode state machine**: CLOSED -> OPEN on ``reader_dark``
       (ONE guaranteed-send page); OPEN -> TRIAL at each cooldown-ladder rung
       (60s doubling to 900s, at most one ``trigger.rearm()`` per tick, no
       page); OPEN -> CLOSED once ``up`` has held ``_STREAM_HEALTHY_DWELL_S``
       (ONE guaranteed-send page, ladder back to the floor). Telegram gets
       EDGES once per EPISODE; Prometheus owns every level — a sustained dark
       stream NEVER pages on an interval (the 2026-08-22 metronome). Flapping
       (``_STREAM_FLAP_ESCALATE_AT`` trips inside ``_STREAM_FLAP_WINDOW_S``)
       escalates ONE CRITICAL and then suppresses further OPEN pages only; the
       CLOSE page is never suppressed (an unpaired page is worse than none).
       The throttled ``stream-dead`` alert covers only the dark-but-CONNECTED
       case (``not episode_open``) — an open episode already reports the dark
       stream via its own page and gauge.
    4. **Emit the stream gauges** — always, including while dark, in ONE atomic
       multi-key write; the age key is never omitted (epoch ``None`` reports
       seconds since this closure was built).

    Both ``alert`` (guaranteed-send, edges only — mirrors
    ``_alert_kill_transition``: edges are rare and each transition must deliver)
    and ``alert_throttled`` are MAIN-THREAD-ONLY sinks; neither may ever be
    handed to the reader thread's client (memo §7.14 / PR #900). Everything
    after the bearer push is best-effort: ``run_daemon`` calls ``on_tick()``
    bare and the CLI catches only ``BrokerError``, so a raising ``rearm()``
    (e.g. ``Thread.start()`` under thread exhaustion) must never unwind the
    protective daemon."""

    # Episode state lives in a per-closure _StreamEpisodeState, constructed once
    # per daemon by _build_stream_handles — the same daemon-lifetime one-slot
    # shape as deps.kill_state, without a new LoopDeps field (memo §4.5).
    #
    # The session predicate feeds the in_session GAUGE only (memo §3 Q5) —
    # nothing here gates on it. Built per-tick-closure (main-thread-only, so
    # _make_stream_session_window's single-writer memo holds), injectable for
    # tests. It FAILS OPEN: the calendar contract says a raising predicate is
    # treated as in-session, and a calendar bug must never take the other five
    # gauges down with it.
    session_predicate = in_session if in_session is not None else _make_stream_session_window()
    state = _StreamEpisodeState(
        started_mono=monotonic(), last_trips_total=trigger.trips_total, stale_s=stale_s
    )

    def _drive_episode() -> None:
        now = monotonic()
        # (2) Delivery-backed health sample (memo §4.4 step 2).
        health = _sample_stream_health(trigger, state)
        # Flap accounting off the monotonic trips_total counter — a trip whose
        # whole lifetime falls between two ticks is still counted (memo §7.1).
        trips = trigger.trips_total
        _account_stream_flaps(state, trips, now, alert)
        # (3) Episode state machine (memo §4.5).
        silence = _drive_stream_state_machine(state, trigger, alert, now=now, health=health)
        # stream-dead is for the dark-but-CONNECTED case only (memo §7.2): an
        # open episode already reports the dark stream via its own page + gauge.
        if not state.episode_open and silence is not None and silence > stale_s:
            alert_throttled(
                f"saxo stream silent >{stale_s:.0f}s ({silence:.0f}s) — running on poll backstop",
                "stream-dead",
            )
        # (4) Gauges — every tick, including while dark.
        _emit_stream_tick_gauges(
            state,
            trigger,
            emit_gauge,
            session_predicate,
            now=now,
            health=health,
            silence=silence,
            trips=trips,
        )

    def _tick() -> None:
        # (1) Bearer FIRST — before any dark-branch logic (memo §4.4 step 1).
        try:
            bearer = get_bearer()
        except Exception:  # a token/chain error must never crash the protective loop
            logger.warning("streaming: bearer read failed — skipping push this tick", exc_info=True)
            bearer = None
        if bearer:
            trigger.push_token(bearer)
        try:
            _drive_episode()
        except Exception:
            # run_daemon calls on_tick() bare and the CLI catches only
            # BrokerError — a raising rearm()/read must degrade to poll-only,
            # never unwind the protective daemon (memo §7.5).
            logger.warning(
                "streaming: episode tick failed — poll backstop covers protection",
                exc_info=True,
            )

    return _tick


def _build_streaming_subscriber(provider: Any) -> Any:
    """The subscription-REST client for the streaming reader thread.

    A DEDICATED SaxoClient with its OWN ``requests.Session`` — never the shared
    ``get_default_saxo_client()`` singleton. ``requests.Session`` is not
    thread-safe, so sharing it between the reader thread (subscription POST/DELETE
    on connect / reconnect) and the main protective thread (``get_positions`` /
    ``place_standalone_stop``) could corrupt the urllib3 connection pool and skip a
    protection pass — leaving a position naked longer than ``poll_seconds`` (worse
    than poll-only). The thread-safe OAuth ``provider`` IS shared so both clients
    see the same rotated bearer; only the HTTP session is isolated. Streaming REST
    is bounded by the reader's circuit breaker, so an independent throttle budget is
    acceptable (zen HIGH, PR #900)."""
    from alphalens_pipeline.brokers.saxo.client import SaxoClient

    return SaxoClient(provider)


def _build_stream_handles(
    broker: Broker,
    provider: Any,
    base_alert: Callable[[str], None],
    alert_throttled: Callable[[str, str], bool],
) -> tuple[threading.Event | None, Callable[[], None] | None, StreamTrigger | None]:
    """Construct + start the dark streaming reader when every structural
    precondition holds, else return the poll-only ``(None, None, None)``.

    ``base_alert`` (guaranteed-send, episode edges) and ``alert_throttled`` are
    both MAIN-THREAD-ONLY sinks consumed by the tick closure. NEITHER may ever
    be passed into the StreamTrigger / streaming-client construction below —
    the client's optional ``alert=`` kwarg must stay unset so ``_trip_breaker``
    stays journald-only on the READER thread (rearm design memo §7.14; pinned
    by ``test_client_factory_is_never_given_an_alert_sink``).

    Preconditions (each a fail-safe-to-poll gate, design memo §Env gates):
      0. ``env != live`` — the order-WS subscriber
         (:func:`_build_streaming_subscriber`) is a SIM-rail ``SaxoClient``
         (no ``standing_live_authorized``); a LIVE instance is structurally
         refused this reader regardless of the flag below (design memo §3:
         "order-WS early-wake needs its own LIVE re-validation" — a separate,
         not-yet-built follow-up), so the pin recommending
         ``STREAMING_ENABLED=0`` for the LIVE unit is defense-in-depth, not
         the only guard;
      1. ``ALPHALENS_BROKER_STREAMING_ENABLED=1`` (master dark gate);
      2. the broker is Saxo (the streaming REST + SIM rail live on ``SaxoClient``);
      3. the provider is OAuth — a static 24h token cannot be PUT-reauthorized in
         place, so :meth:`SaxoStreamingClient.start` would refuse anyway;
      4. the reader thread actually started (``start()`` returns True).

    SIM-probe-only (no hermetic cycle — the run_daemon wait + the per-tick hook are
    unit-tested against stubs). A construction / start failure logs once and falls
    back to poll-only rather than raising — streaming is a pure latency win and its
    absence must never block the protective loop."""
    if state_paths.broker_environment() == state_paths.ENV_LIVE:
        logger.info(
            "streaming early-wake reader structurally skipped for the LIVE broker "
            "instance regardless of %s (design memo §3 — the order-WS subscriber "
            "is a SIM-rail SaxoClient; LIVE re-validation of the reader is a "
            "separate, not-yet-built follow-up)",
            _STREAMING_ENABLED_ENV,
        )
        return None, None, None
    if not _streaming_enabled():
        return None, None, None

    from alphalens_pipeline.brokers.automanager.streaming_trigger import (
        StreamTrigger,
        default_context_id_factory,
    )
    from alphalens_pipeline.brokers.saxo.broker import SaxoBroker
    from alphalens_pipeline.brokers.saxo.tokens import OAuthTokenProvider

    if not isinstance(broker, SaxoBroker):
        logger.warning(
            "streaming enabled but broker %r is not Saxo — running poll-only", broker.name
        )
        return None, None, None
    if not isinstance(provider, OAuthTokenProvider):
        logger.warning(
            "streaming enabled but the token provider is not OAuth (a static token "
            "cannot be re-authorized in place) — running poll-only"
        )
        return None, None, None

    stale_s = _stream_stale_s()
    # The contextId format has ONE home (streaming_trigger.default_context_id_factory,
    # <=50 chars, [a-zA-Z0-9-]) so the initial context and every rearm() rotation
    # stay consistent (rearm design memo §4.3).
    context_id = default_context_id_factory()
    try:
        trigger = StreamTrigger(
            token_provider=provider,
            subscriber=_build_streaming_subscriber(
                provider
            ),  # dedicated session, never the singleton
            context_id=context_id,
            client_stale_after_s=stale_s,
        )
        started = trigger.start()
    except Exception:  # any streaming-setup failure degrades to poll-only, never raises
        logger.warning(
            "streaming client construction/start failed — running poll-only", exc_info=True
        )
        return None, None, None
    if not started:
        logger.warning("streaming client refused to start — running poll-only")
        return None, None, None

    logger.info("saxo streaming reader started (context_id=%s, stale_s=%.0f)", context_id, stale_s)
    stream_tick = _make_stream_tick(
        trigger,
        get_bearer=provider.get_access_token,
        alert=base_alert,
        alert_throttled=alert_throttled,
        stale_s=stale_s,
    )
    return trigger.wake_event, stream_tick, trigger
