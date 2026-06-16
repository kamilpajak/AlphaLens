"""Shared read-only loaders for the EDGE parquet stores (research diagnostics).

All read from ~/.alphalens parquet stores; the research side may import
alphalens_pipeline. Pulled out of diagnose_nofill.py so the selection diagnostic
reuses the same loaders (DRY).
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pandas as pd
from alphalens_pipeline.data import rs_history
from alphalens_pipeline.paper import brief_loader

HOME = Path.home() / ".alphalens"


def load_store(store_dir: Path) -> pd.DataFrame:
    """Concat every ``YYYY-MM-DD.parquet`` in ``store_dir``, stamping brief_date from the stem."""
    frames = []
    for path in sorted(store_dir.glob("*.parquet")):
        try:
            d = dt.date.fromisoformat(path.stem)
        except ValueError:
            continue
        df = pd.read_parquet(path)
        df["brief_date"] = d
        frames.append(df)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def setup_index(briefs_dir: Path) -> dict[tuple[dt.date, str], dict]:
    """Map (brief_date, TICKER) -> decoded brief_trade_setup dict."""
    out: dict[tuple[dt.date, str], dict] = {}
    briefs = load_store(briefs_dir)
    if briefs.empty:
        return out
    for _, row in briefs.iterrows():
        ticker = str(row.get("ticker", "")).upper()
        if not ticker:
            continue
        setup = brief_loader._coerce_trade_setup(row.get("brief_trade_setup"))
        if setup is not None:
            out[(row["brief_date"], ticker)] = setup
    return out


class GroupedDailyCache:
    """Memoized ``rs_history.read_grouped_day`` so each session parquet is read once."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._cache: dict[dt.date, dict | None] = {}

    def get(self, session: dt.date) -> dict | None:
        if session not in self._cache:
            self._cache[session] = rs_history.read_grouped_day(self._root, session)
        return self._cache[session]


def newest_session(root: Path) -> dt.date | None:
    """The newest ISO-stem ``*.parquet`` session date in the grouped-daily store."""
    best: dt.date | None = None
    if not root.is_dir():
        return None
    for p in root.glob("*.parquet"):
        try:
            d = dt.date.fromisoformat(p.stem)
        except ValueError:
            continue
        if best is None or d > best:
            best = d
    return best
