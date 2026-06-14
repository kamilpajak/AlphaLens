"""Persistent market-wide daily-close history for the O'Neil relative-strength term.

A dated, split-adjusted grouped-daily store: one parquet per session at
``~/.alphalens/grouped_daily_history/<YYYY-MM-DD>.parquet`` (all bars ``adjusted=True``).
Built once by ``scripts/backfill_grouped_daily_history.py`` and kept fresh by a nightly
top-up; READ at the score stage to compute a candidate's trailing-return percentile vs
the market (the RS-approx). Zero in-pass Polygon calls — the score stage only reads disk.

SEPARATE from the population-monitor grouped cache (``~/.alphalens/population_ladders/grouped/``,
``adjusted=False``, used for intraday touch detection). The two MUST NOT be merged: the
monitor needs RAW closes to match minute bars + absolute ladder levels, RS needs
SPLIT-ADJUSTED closes for clean trailing returns. Mixing the adjustment flag corrupts one.

CAVEAT: Polygon ``adjusted=True`` on grouped-daily is **split-only, NOT dividend-adjusted**.
RS is therefore a SPLIT-CLEAN trailing-PRICE-return percentile — dividend drift is a minor
relative-rank approximation, documented in the design memo. The reference universe is the
PIT intersection of the ``asof`` and ``asof − n`` snapshots: DELISTING-survivorship-clean by
construction (a past date's snapshot holds names that traded that day, incl. later-delisted);
symbol-remap (Polygon ``T`` is the CURRENT symbol, e.g. FB→META) and M&A-stitched history are
residual minor approximations the percentile over ~8000 names tolerates — NOT PIT-exact.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pandas as pd

from alphalens_pipeline.data.alt_data.polygon_client import _GROUPED_DAILY_FIELDS
from alphalens_pipeline.paper.calendar import DEFAULT_EXCHANGE, n_sessions_before

logger = logging.getLogger(__name__)

# The RS store root — DISTINCT from the monitor cache root (do not collapse them).
DEFAULT_RS_HISTORY_ROOT = Path.home() / ".alphalens" / "grouped_daily_history"

# 12-month trailing lookback for the RS-approx (pinned, UNVALIDATED config constant).
RS_LOOKBACK_SESSIONS = 252

# (date) -> {TICKER: {t,o,h,l,c,v,vw}}. Injected in tests; the default routes through
# the canonical PolygonClient with adjusted=True (NO polygon.io URL literal in this file).
GroupedFetch = Callable[[dt.date], dict[str, dict[str, Any]]]


def _history_path(root: Path, date: dt.date) -> Path:
    return root / f"{date.isoformat()}.parquet"


def read_grouped_day(root: Path, date: dt.date) -> dict[str, dict[str, Any]] | None:
    """The whole-market split-adjusted snapshot for ``date``, or ``None`` when not on disk.

    ``None`` means "not fetched / unreadable" — distinct from ``{}`` (a genuinely empty
    session). A present map is the ENTIRE market for that session, so a ticker absent from
    it did not trade that day. Keyed by upper-cased symbol.
    """
    path = _history_path(root, date)
    if not path.exists():
        return None
    try:
        df = pd.read_parquet(path)
    except (OSError, ValueError) as exc:
        logger.warning("rs_history: bad parquet for %s — %s; treating as not-on-disk.", date, exc)
        return None
    out: dict[str, dict[str, Any]] = {}
    for _, r in df.iterrows():
        symbol = str(r["T"]).upper()
        out[symbol] = {k: r[k] for k in _GROUPED_DAILY_FIELDS if k in r.index}
    return out


def write_grouped_day_atomic(root: Path, date: dt.date, payload: dict[str, dict[str, Any]]) -> Path:
    """Write one session's whole-market snapshot atomically (temp + ``os.replace``)."""
    path = _history_path(root, date)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {"T": symbol, **{k: bar.get(k) for k in _GROUPED_DAILY_FIELDS}}
        for symbol, bar in payload.items()
    ]
    df = pd.DataFrame(rows, columns=["T", *_GROUPED_DAILY_FIELDS])
    tmp = path.with_suffix(path.suffix + ".tmp")
    df.to_parquet(tmp, index=False)
    os.replace(tmp, path)
    return path


def _default_grouped_fetch(date: dt.date) -> dict[str, dict[str, Any]]:
    """Production fetch: the canonical PolygonClient with adjusted=True (split-adjusted)."""
    from alphalens_pipeline.data.alt_data.polygon_client import get_default_polygon_client

    return get_default_polygon_client().get_grouped_daily(date, adjusted=True)


def _newest_session_on_or_before(root: Path, asof: dt.date) -> dt.date | None:
    """The newest stored session date on or before ``asof``, or ``None`` when the store
    holds none.

    Rolls a non-session / lagging ``asof`` (a weekend brief date — the thematic pipeline
    runs 7 days/week — or an ``asof`` ahead of the nightly top-up) back to the latest real
    session present on disk. PIT-correct: the 'as of' close is the most recent one on or
    before ``asof``, never a future bar. Lexicographic order over the ISO-date ``.parquet``
    stems is a valid chronological order (zero-padded ``YYYY-MM-DD``).
    """
    if not root.exists():
        return None
    cutoff = asof.isoformat()
    best: str | None = None
    for p in root.glob("*.parquet"):
        stem = p.stem
        if len(stem) == 10 and stem <= cutoff and (best is None or stem > best):
            best = stem
    if best is None:
        return None
    try:
        return dt.date.fromisoformat(best)
    except ValueError:
        return None


def _close(bar: dict[str, Any]) -> float | None:
    """The adjusted close, or ``None`` when missing / non-positive."""
    c = bar.get("c")
    if c is None:
        return None
    try:
        val = float(c)
    except (TypeError, ValueError):
        return None
    return val if val > 0 else None


def rs_percentile(
    root: Path,
    ticker: str,
    asof: dt.date,
    *,
    n_sessions: int = RS_LOOKBACK_SESSIONS,
    exchange: str = DEFAULT_EXCHANGE,
) -> float | None:
    """The candidate's trailing-``n_sessions`` return percentile vs the market, or ``None``.

    Reads ONLY the on-disk store (never fetches). The CURRENT endpoint is the newest stored
    session on or before ``asof`` (so a weekend brief date — the pipeline runs 7 days/week — or
    an ``asof`` ahead of the nightly top-up rolls back to the latest real session, never a future
    bar); the lookback is anchored to THAT resolved session, not the raw ``asof``. ``None``
    (tri-state, never a fake ``0.0``) when no session is on disk on or before ``asof`` (empty /
    too-young store), the lookback snapshot is off-disk (unhealed gap), OR the candidate is absent
    from either endpoint (recent IPO / trading gap / delisted). The reference universe is the PIT
    intersection of the two snapshots with a positive close in BOTH. ``rs_pct`` is a WITHIN-DATE
    cross-sectional rank in [0, 100] (the deferred study must treat it as a per-date rank, never a
    cross-date cardinal level — the intersection denominator shifts daily).
    """
    current = _newest_session_on_or_before(root, asof)
    if current is None:
        return None
    lookback = n_sessions_before(current, n_sessions, exchange)
    asof_map = read_grouped_day(root, current)
    lookback_map = read_grouped_day(root, lookback)
    if asof_map is None or lookback_map is None:
        return None

    key = ticker.upper()
    cand_now = _close(asof_map.get(key, {}))
    cand_then = _close(lookback_map.get(key, {}))
    if cand_now is None or cand_then is None:
        return None
    cand_ret = cand_now / cand_then - 1.0

    rets: list[float] = []
    for sym in asof_map.keys() & lookback_map.keys():
        now = _close(asof_map[sym])
        then = _close(lookback_map[sym])
        if now is not None and then is not None:
            rets.append(now / then - 1.0)
    if not rets:
        return None
    return 100.0 * sum(1 for r in rets if r <= cand_ret) / len(rets)


__all__ = [
    "DEFAULT_RS_HISTORY_ROOT",
    "RS_LOOKBACK_SESSIONS",
    "GroupedFetch",
    "read_grouped_day",
    "rs_percentile",
    "write_grouped_day_atomic",
]
