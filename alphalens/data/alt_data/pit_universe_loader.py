"""Load pre-built PIT universe yaml snapshots from ``~/.alphalens/pit_universe/``.

Snapshots are produced by ``scripts/build_pit_universe.py`` (one-time build
combining XBRL shares-outstanding × yfinance closes filtered to the
$300M-$3B band per :class:`UniverseConfig`). One yaml per calendar month,
named ``YYYY-MM.yaml``::

    asof: '2022-06-30'
    tickers:
      - AAPL
      - MSFT
      ...

Two query patterns:

  * :func:`load_pit_universe_for_asof` — return the ticker list for the
    snapshot most recently at-or-before ``asof``. Used by per-rebalance
    scorers needing the universe at one point in time.
  * :func:`load_universe_union` — union of all snapshots in a window.
    Used at experiment-driver setup time to discover which tickers might
    enter the universe across the study period (so OHLCV histories can be
    pre-loaded once).
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import yaml

DEFAULT_ROOT = Path.home() / ".alphalens" / "pit_universe"

_SNAPSHOT_PATTERN = re.compile(r"^(\d{4})-(\d{2})\.yaml$")


def _iter_snapshots(root: Path) -> list[tuple[date, Path]]:
    """Yield (snapshot_asof, path) tuples sorted by snapshot date ascending.

    The snapshot's ``asof`` is parsed from the yaml content (authoritative)
    rather than the filename to handle the rare case of a snapshot with
    end-of-month asof that crosses month boundaries.
    """
    if not root.is_dir():
        return []
    out: list[tuple[date, Path]] = []
    for path in root.iterdir():
        m = _SNAPSHOT_PATTERN.match(path.name)
        if m is None:
            continue
        try:
            payload = yaml.safe_load(path.read_text()) or {}
            asof_str = payload.get("asof")
            if asof_str:
                asof = date.fromisoformat(asof_str)
            else:
                # Fallback: derive from filename (last day of month not
                # known without calendar; use day 28 as conservative).
                asof = date(int(m.group(1)), int(m.group(2)), 28)
        except (yaml.YAMLError, ValueError):
            continue
        out.append((asof, path))
    out.sort(key=lambda t: t[0])
    return out


def load_pit_universe_for_asof(asof: date, *, root: Path = DEFAULT_ROOT) -> list[str]:
    """Return tickers from the snapshot whose asof is most recently at-or-before.

    Returns ``[]`` if no snapshot exists at-or-before ``asof`` (i.e. asof
    precedes the earliest available snapshot, or the snapshot directory is
    empty).
    """
    snapshots = _iter_snapshots(root)
    if not snapshots:
        return []
    eligible = [(s_asof, p) for s_asof, p in snapshots if s_asof <= asof]
    if not eligible:
        return []
    _, latest_path = eligible[-1]
    payload = yaml.safe_load(latest_path.read_text()) or {}
    tickers = payload.get("tickers") or []
    return list(tickers)


def load_universe_union(start: date, end: date, *, root: Path = DEFAULT_ROOT) -> list[str]:
    """Sorted union of tickers across all snapshots with ``start <= asof <= end``."""
    snapshots = _iter_snapshots(root)
    seen: set[str] = set()
    for s_asof, path in snapshots:
        if not (start <= s_asof <= end):
            continue
        payload = yaml.safe_load(path.read_text()) or {}
        for t in payload.get("tickers") or []:
            seen.add(t)
    return sorted(seen)
