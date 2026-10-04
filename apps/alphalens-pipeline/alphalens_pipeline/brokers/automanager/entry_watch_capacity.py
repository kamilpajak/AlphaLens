"""How many entry watches may be open, and why a pick was deferred.

Step 5 of the ``control_loop`` partition. Two rails bound the number of live
entry watches, and both answer by DEFERRING a pick rather than refusing it:

* **A count ceiling** — ``_entry_watch_max_picks`` reads the env rail,
  ``_open_watch_pick_keys`` and ``_open_watch_picks_for_max_open`` count what is
  already open, ``_entry_watch_capacity_reached`` compares them and
  ``_log_watch_capacity_deferral`` says so once per pick.
* **One watch per instrument** — ``_watch_uic_in`` and
  ``_log_live_uic_deferral``: a second pick on an instrument a watch already
  covers waits for that watch to finish.

Both deferral logs are deduplicated through module-level sets
(``_entry_watch_capacity_deferred``, ``_entry_watch_live_uic_deferred``), which
is why they travel with the functions that read them.

What moved is CLOSED under every module-level name it uses, so nothing here
names anything left behind in ``control_loop`` and there is no import cycle.
``control_loop`` reaches it through the module prefix
(``entry_watch_capacity.<name>``), never by a ``from`` import, so a test
patching this module reaches the binding the code actually reads.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Collection, Mapping
from typing import Any

from alphalens_pipeline.brokers.automanager import entry_trails, picks

logger = logging.getLogger(__name__)


_ENTRY_WATCH_MAX_PICKS_ENV = entry_trails.ENTRY_WATCH_MAX_PICKS_ENV
_ENTRY_WATCH_MAX_PICKS_DEFAULT = entry_trails.ENTRY_WATCH_MAX_PICKS_DEFAULT
_ENTRY_WATCH_MAX_PICKS_MIN = entry_trails.ENTRY_WATCH_MAX_PICKS_MIN
_ENTRY_WATCH_MAX_PICKS_MAX = entry_trails.ENTRY_WATCH_MAX_PICKS_MAX

_entry_watch_max_picks_warned = False

_entry_watch_capacity_deferred: set[str] = set()


def _entry_watch_max_picks() -> int:
    """Watch capacity (memo decision #4 / G5 CRITICAL-1): at most this many
    DISTINCT picks may hold open watches at once — a PICK-denominated limit,
    deliberately NOT folded into MAX_OPEN (which counts per tier and would make
    a 3-tier trailing pick un-armable at MAX_OPEN=1). The account is protected
    by the virtual gross/cash reservation fold
    (entry_trails.watching_virtual_gross_acct), not by this capacity number.

    Sourced from :data:`_ENTRY_WATCH_MAX_PICKS_ENV`; unset falls back to the
    default silently, an invalid or out-of-range value falls back too but pages
    the journal with ONE warning per process."""
    global _entry_watch_max_picks_warned  # noqa: PLW0603 — once-per-process warn latch
    raw = os.environ.get(_ENTRY_WATCH_MAX_PICKS_ENV)
    if raw is None:
        return _ENTRY_WATCH_MAX_PICKS_DEFAULT
    try:
        value = int(raw)
    except ValueError:
        value = None
    if value is not None and _ENTRY_WATCH_MAX_PICKS_MIN <= value <= _ENTRY_WATCH_MAX_PICKS_MAX:
        return value
    if not _entry_watch_max_picks_warned:
        _entry_watch_max_picks_warned = True
        logger.warning(
            "%s=%r is invalid (expected an integer in [%d, %d]) — using the default %d",
            _ENTRY_WATCH_MAX_PICKS_ENV,
            raw,
            _ENTRY_WATCH_MAX_PICKS_MIN,
            _ENTRY_WATCH_MAX_PICKS_MAX,
            _ENTRY_WATCH_MAX_PICKS_DEFAULT,
        )
    return _ENTRY_WATCH_MAX_PICKS_DEFAULT


def _entry_watch_crid(
    ticker: str, trade_date: str, tier_index: int, *, generation: int = picks.FIRST_GENERATION
) -> str:
    """Deterministic per-tier watch id in the ``-entry-`` request-id family
    (memo §5 — parallel to the exit ids so entry/exit ids can never collide on
    one uic). DETERMINISTIC, not a uuid: a crash between the journal-first
    watch_open and the note-only pick retirement re-opens the SAME crid on the
    next drain, and the fold's latest-watch_open-wins semantics make that
    re-open idempotent (no double reservation) where a fresh uuid would leak a
    second watch.

    ``generation`` (#1371) is the same-day re-arm counter: 1 renders exactly
    the pre-#1371 crid, so every crid on disk keeps its identity; a later
    generation carries ``-g<N>`` after the date (``picks.identity_token``),
    so a disarmed generation's sticky terminal markers never shadow its
    successor's watches."""
    return f"{ticker}-{picks.identity_token(trade_date, generation)}-entry-t{tier_index}"


def _entry_trail_eligible(plan: Any) -> bool:
    """Whether a sized pick can be routed into an entry-trail watch: it has at
    least one positive-quantity entry tier (an all-zero-tier plan is handled by
    the normal zero-tiers refusal downstream). MVP scope is long single-name
    equities, which every drained pick already is."""
    return any(getattr(tier, "qty", 0) > 0 for tier in getattr(plan, "entry_tiers", ()) or ())


def _open_watch_pick_keys(fold: entry_trails.EntryTrailFold) -> set[str]:
    """The distinct ``pick_key`` of every NON-terminal watch_open tier in the
    fold (falling back to the crid when a record predates the pick_key field)
    — the set of picks that currently hold an open watch."""
    pick_keys: set[str] = set()
    for state in fold.tiers.values():
        if state.terminal_kind is not None or state.watch_open is None:
            continue
        pick_keys.add(str(state.watch_open.get("pick_key") or state.crid))
    return pick_keys


_entry_watch_live_uic_deferred: set[str] = set()


def _log_live_uic_deferral(ticker: str, pick_key: str, uic: int) -> None:
    """Log a live-uic routing deferral: WARNING the FIRST time this pick_key is
    deferred in this process (an abnormal state the operator should see — a
    re-picked ticker is queuing behind its own live position), DEBUG on every
    later tick. Process-lifetime observability only — no behaviour rides on the
    set (mirrors :func:`_log_watch_capacity_deferral`)."""
    log = logger.debug
    if pick_key not in _entry_watch_live_uic_deferred:
        _entry_watch_live_uic_deferred.add(pick_key)
        log = logger.warning
    log(
        "place_pick %s: a live long already holds uic %d — %s stays armed until the "
        "uic is flat (routing a watch now would clobber the live position's ladder)",
        ticker,
        uic,
        ticker,
    )


def _open_watch_picks_for_max_open(
    fold: entry_trails.EntryTrailFold,
    *,
    own_pick_key: str,
    position_uics: Collection[int],
) -> set[str]:
    """The DISTINCT ``pick_key`` of every open entry watch that occupies a
    prospective-position slot in the MAX_OPEN admission check (2026-08-19
    adjudication finding 1).

    An open watch — or its armed unfilled native trail, which is equally
    non-terminal in the fold — is a committed risk unit ``safety.check`` cannot
    see: the note-only watch submission record carries no brackets and no
    position exists until the trail fires, so with N watches open the classic
    sum (journal brackets + live positions) under-counts by N and a raised
    watch capacity could over-commit up to N extra concurrent positions.

    Two exclusions keep the count one-slot-per-risk-unit:

    - ``own_pick_key`` — the candidate pick's own watch (the crash-recovery
      re-drive must not self-block on its own reservation, mirroring the
      intercept's ``already_watching`` exemption);
    - any watch whose uic is in ``position_uics`` — a pick whose tier already
      FIRED shows up as a live position while a deeper tier still watches; that
      unit is already counted in ``BrokerView.open_position_count``. The caller
      passes NET-open uics (``_net_open_position_uics``) — a net-flat uic
      (an EOD-netting round-trip's two ledger rows) is NOT in the position
      count, so its watch must keep occupying a slot here.

    A watch record with no parseable uic still counts (conservative: an
    over-reserved slot refuses one pick too early; an under-count re-opens the
    over-commit)."""
    picks: set[str] = set()
    for state in fold.tiers.values():
        if state.terminal_kind is not None or state.watch_open is None:
            continue
        record = state.watch_open
        pick_key = str(record.get("pick_key") or state.crid)
        if pick_key == own_pick_key or _watch_uic_in(record, position_uics):
            continue
        picks.add(pick_key)
    return picks


def _watch_uic_in(record: Mapping[str, Any], uics: Collection[int]) -> bool:
    """Whether the watch record's uic parses AND is in ``uics``; False on a
    missing/unparseable uic (the caller then counts the watch conservatively)."""
    try:
        return int(record["uic"]) in uics
    except (KeyError, TypeError, ValueError):
        return False


def _entry_watch_capacity_reached(fold: entry_trails.EntryTrailFold) -> bool:
    """True iff opening another watch would exceed :func:`_entry_watch_max_picks`
    DISTINCT watching picks."""
    return len(_open_watch_pick_keys(fold)) >= _entry_watch_max_picks()


def _log_watch_capacity_deferral(ticker: str, pick_key: str) -> None:
    """Log a capacity deferral: INFO the FIRST time this pick_key is deferred in
    this process, DEBUG on every later tick. Process-lifetime observability
    only — no behaviour rides on the set (2026-08-19 incident: ETSY sat
    capacity-deferred for a day with only DEBUG lines to show for it)."""
    log = logger.debug
    if pick_key not in _entry_watch_capacity_deferred:
        _entry_watch_capacity_deferred.add(pick_key)
        log = logger.info
    log(
        "place_pick %s: entry-trail watch capacity reached (cap=%d) — %s stays armed",
        ticker,
        _entry_watch_max_picks(),
        ticker,
    )
