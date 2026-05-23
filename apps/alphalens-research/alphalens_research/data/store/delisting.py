"""Delisting-event data store.

Provides the ``DelistingEvent`` record and loaders that merge the
backfill parquet + existing YAML into a single event list. Pure data
infrastructure — no dependency on backtest engine or attribution layer.

Consumers:
- ``alphalens_research.diagnostics.survivorship_pit`` (the diagnostic
  battery — C1/C2/C3 stress tests against this event list).
- ``alphalens_research.data.store.form4_pit`` (filters Form-4 records
  against delisted-ticker dates).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd
import yaml


@dataclass(frozen=True)
class DelistingEvent:
    ticker: str
    delisted_date: date
    reason: str  # "bankruptcy" | "merger" | "acquisition" | "unknown"


def _load_events_from_parquet(parquet_path: Path) -> list[DelistingEvent]:
    df = pd.read_parquet(parquet_path)
    return [
        DelistingEvent(
            ticker=str(r["ticker"]),
            delisted_date=pd.Timestamp(r["delisted_date"]).date(),
            reason=str(r.get("reason", "unknown")),
        )
        for _, r in df.iterrows()
    ]


def _load_events_from_yaml(yaml_path: Path) -> list[DelistingEvent]:
    data = yaml.safe_load(yaml_path.read_text()) or {}
    out: list[DelistingEvent] = []
    for entry in data.get("delisted", []) or []:
        ticker = str(entry.get("ticker") or "")
        d_raw = entry.get("delisted")
        if not ticker or not d_raw:
            continue
        d = d_raw if isinstance(d_raw, date) else pd.Timestamp(d_raw).date()
        out.append(DelistingEvent(ticker=ticker, delisted_date=d, reason="unknown"))
    return out


def load_delisting_events(
    parquet_path: Path | None = None,
    yaml_path: Path | None = None,
) -> list[DelistingEvent]:
    """Merge the backfill parquet + existing YAML into a single event list.

    Either source can be missing — the caller is responsible for knowing
    what window is covered. The parquet is produced by
    ``scripts/backfill_delisted_2021_2024.py``; the YAML ships with the
    repo.

    On collision (same ticker+date), parquet wins (carries the better
    reason).
    """
    rows: dict[tuple[str, date], DelistingEvent] = {}

    if parquet_path and parquet_path.exists():
        for ev in _load_events_from_parquet(parquet_path):
            rows[(ev.ticker, ev.delisted_date)] = ev

    if yaml_path and yaml_path.exists():
        for ev in _load_events_from_yaml(yaml_path):
            rows.setdefault((ev.ticker, ev.delisted_date), ev)

    return sorted(rows.values(), key=lambda e: (e.delisted_date, e.ticker))
