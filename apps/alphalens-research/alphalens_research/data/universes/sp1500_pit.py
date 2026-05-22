"""S&P 1500 PIT membership loader (S&P 500 + S&P 400 + S&P 600).

Loads year-snapshot YAMLs from ``data/{sp500_pit, sp400_pit, sp600_pit}/`` and
returns the latest snapshot whose ``as_of`` is ≤ the requested ``asof``. Mirrors
the schema of the legacy ``alphalens_research.archive.guru.universe`` loader.

Schema per ``{index}_pit/{year}.yaml``:

    as_of: '2024-01-01'
    source: 'iShares IJH current snapshot' | 'wikipedia' | etc
    notes: 'optional caveat string'
    tickers:
      - AAPL
      - ...

Survivorship caveat: when only a single recent fallback snapshot is available
per index (the current Phase-1 MVP state), every asof in the holdout window
returns the same membership. Companies that left the index between the
snapshot date and today are MISSING from the universe — biases performance
upward by ~100-300 bps/y depending on the holdout span. Documented in the v4
verdict memo.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import yaml

DEFAULT_DATA_ROOT = Path(__file__).resolve().parents[3] / "data"


class UniverseError(RuntimeError):
    """Raised when a requested PIT snapshot is not available."""


def _load_for_date(asof: pd.Timestamp, *, data_dir: Path) -> list[str]:
    if not data_dir.exists() or not data_dir.is_dir():
        raise UniverseError(f"Universe data directory does not exist: {data_dir}")

    candidates: list[tuple[pd.Timestamp, Path]] = []
    for path in sorted(data_dir.glob("*.yaml")):
        data = yaml.safe_load(path.read_text()) or {}
        as_of_str = data.get("as_of")
        if not as_of_str:
            continue
        try:
            ts = pd.Timestamp(as_of_str)
        except (ValueError, TypeError):
            continue
        candidates.append((ts, path))

    eligible = [(ts, p) for ts, p in candidates if ts <= asof]
    if not eligible:
        raise UniverseError(f"No snapshot in {data_dir} with as_of ≤ {asof.date()}")
    eligible.sort(key=lambda tp: tp[0])
    _, latest_path = eligible[-1]
    data = yaml.safe_load(latest_path.read_text()) or {}
    return [str(t).upper() for t in (data.get("tickers") or [])]


def load_sp500_pit_for_date(asof: pd.Timestamp, *, data_dir: Path | None = None) -> list[str]:
    """Return S&P 500 tickers active as of the latest snapshot ≤ ``asof``."""
    directory = Path(data_dir) if data_dir else DEFAULT_DATA_ROOT / "sp500_pit"
    return _load_for_date(asof, data_dir=directory)


def load_sp400_pit_for_date(asof: pd.Timestamp, *, data_dir: Path | None = None) -> list[str]:
    """Return S&P MidCap 400 tickers active as of the latest snapshot ≤ ``asof``."""
    directory = Path(data_dir) if data_dir else DEFAULT_DATA_ROOT / "sp400_pit"
    return _load_for_date(asof, data_dir=directory)


def load_sp600_pit_for_date(asof: pd.Timestamp, *, data_dir: Path | None = None) -> list[str]:
    """Return S&P SmallCap 600 tickers active as of the latest snapshot ≤ ``asof``."""
    directory = Path(data_dir) if data_dir else DEFAULT_DATA_ROOT / "sp600_pit"
    return _load_for_date(asof, data_dir=directory)


def load_sp1500_pit_for_date(asof: pd.Timestamp, *, data_root: Path | None = None) -> list[str]:
    """Return sorted, deduplicated union of S&P 500 + 400 + 600 tickers."""
    root = Path(data_root) if data_root else DEFAULT_DATA_ROOT
    sp500 = load_sp500_pit_for_date(asof, data_dir=root / "sp500_pit")
    sp400 = load_sp400_pit_for_date(asof, data_dir=root / "sp400_pit")
    sp600 = load_sp600_pit_for_date(asof, data_dir=root / "sp600_pit")
    return sorted(set(sp500) | set(sp400) | set(sp600))


def _load_pit_union(data_dir: Path) -> list[str]:
    """Sorted, uppercased union of tickers across all snapshots in `data_dir`.

    Unlike the `_for_date` loaders, this does NOT pick a single snapshot — it
    accumulates every ticker that has appeared in any year's membership.
    Useful for bulk-prefetch jobs (e.g. AV EARNINGS backfill) where the
    operational goal is "cache every name that any historical PEAD window
    would touch".
    """
    if not data_dir.exists() or not data_dir.is_dir():
        raise UniverseError(f"Universe data directory does not exist: {data_dir}")
    tickers: set[str] = set()
    for path in sorted(data_dir.glob("*.yaml")):
        data = yaml.safe_load(path.read_text()) or {}
        if not data.get("as_of"):
            continue
        for t in data.get("tickers") or []:
            tickers.add(str(t).upper())
    return sorted(tickers)


def load_sp500_pit_union(data_dir: Path | None = None) -> list[str]:
    """Sorted union of every ticker appearing in any sp500_pit snapshot.

    Returns ~503 tickers given current survivorship-biased fallback snapshots
    (2018/2020/2022/2024 all share current S&P 500 membership). A true PIT
    backfill at AV free-tier 25/day quota completes in ~21 calendar days.
    """
    directory = Path(data_dir) if data_dir else DEFAULT_DATA_ROOT / "sp500_pit"
    return _load_pit_union(directory)


def load_sp1500_pit_union(data_root: Path | None = None) -> list[str]:
    """Sorted union across S&P 500 + 400 + 600 snapshot directories.

    Returns ~2000 tickers (S&P 1500). At AV free-tier 25/day quota, a full
    EARNINGS backfill against this universe takes ~80 calendar days — usable
    for future paradigms operating beyond the large-cap window, but slow.
    """
    root = Path(data_root) if data_root else DEFAULT_DATA_ROOT
    return sorted(
        set(_load_pit_union(root / "sp500_pit"))
        | set(_load_pit_union(root / "sp400_pit"))
        | set(_load_pit_union(root / "sp600_pit"))
    )
