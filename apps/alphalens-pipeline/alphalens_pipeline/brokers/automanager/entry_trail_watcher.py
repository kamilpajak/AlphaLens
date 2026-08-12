"""Entry-trailing WATCHER engine (PR-T1) — the pure state machine + trough
tracker behind ``ALPHALENS_BROKER_ENTRY_TRAIL_BPS``.

Design memo: ``docs/research/entry_trailing_design_2026_08_12.md`` (LOCKED
2026-08-12), §5 state machine + journals, §3 guards G2/G5/G6/G9.

**Scope of PR-T1 — DRY-RUN ONLY.** This module NEVER places, amends, or
cancels a broker order. It is a self-contained, dependency-injected unit: it
takes injected per-tick price readings and returns state transitions +
journal-line INTENTS + alert INTENTS. The caller (the WIRE phase, PR-T2) turns
the intents into journal appends and throttled Telegram alerts. The "fire" of
the dry run is the ``would_fire`` alert ("would fire @ trigger X") — no order
object is ever constructed. With the flag unset/``0`` nothing constructs a
watcher at all, so runtime is byte-identical to today.

T1a lives here — :class:`EntryTroughTracker`, the per-tier running LOW: the
entry-side mirror of ``control_loop._update_peaks`` (a running HIGH via
``max``), INVERTED to ``min``. It ratchets DOWN only, yields at most one
``trough`` journal-intent per tick, and is SEEDED with the journaled minimum on
restart (the exit peak reseeds downward safely; the entry trough must not
forget its low upward — memo §5).
"""

from __future__ import annotations

import math

# --- Constants ---------------------------------------------------------------

_BPS_DENOMINATOR = 10_000
"""Basis-point divisor: ``d = d_bps / 10_000`` (50 bps -> 0.005)."""


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
