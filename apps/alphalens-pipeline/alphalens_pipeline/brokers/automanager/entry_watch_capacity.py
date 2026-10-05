"""Entry-watch helpers: the per-tier watch id, eligibility, the open-watch set,
and the one deferral left on opening a watch.

Step 5 of the ``control_loop`` partition. Nothing here limits HOW MANY watches
may be open (#1732, owner decision 2026-10-05): free capital is the only limit
on how many picks the daemon takes, and an open watch is valued by both money
gates through ``entry_trails.watching_virtual_gross_acct``. What remains:

* ``_entry_watch_crid`` — the deterministic per-tier watch id;
* ``_entry_trail_eligible`` — whether a sized plan can be routed to a watch;
* ``_open_watch_pick_keys`` — the picks that currently hold an open watch (the
  drain's crash-recovery re-drive exemption);
* **one watch per instrument** — ``_log_live_uic_deferral``: a pick whose
  instrument a live long already holds waits until that instrument is flat.

The deferral log is deduplicated through a module-level set
(``_entry_watch_live_uic_deferred``), which is why it travels with the function
that reads it.

What moved is CLOSED under every module-level name it uses, so nothing here
names anything left behind in ``control_loop`` and there is no import cycle.
``control_loop`` reaches it through the module prefix
(``entry_watch_capacity.<name>``), never by a ``from`` import, so a test
patching this module reaches the binding the code actually reads.
"""

from __future__ import annotations

import logging
from typing import Any

from alphalens_pipeline.brokers.automanager import entry_trails, picks

logger = logging.getLogger(__name__)


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
    set."""
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
