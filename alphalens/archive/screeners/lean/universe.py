"""Sector-grouped universe loader for the Lean screener.

YAML maps GICS sector -> list of tickers. Overlaps across sectors are tolerated;
`flatten_universe` returns the deduped set with sector memberships.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import yaml

from .config import DELISTED_UNIVERSE_PATH, UNIVERSE_PATH


@dataclass(frozen=True)
class DelistedRecord:
    ticker: str
    delisted: date
    name: str


def load_universe(path: Path | None = None) -> dict[str, list[str]]:
    """Load {sector: [tickers]} from YAML. Raises FileNotFoundError if missing."""
    target = path or UNIVERSE_PATH
    if not target.exists():
        raise FileNotFoundError(f"Universe file not found: {target}")
    data = yaml.safe_load(target.read_text()) or {}
    return {sector: list(tickers or []) for sector, tickers in data.items()}


def flatten_universe(sectors: dict[str, list[str]]) -> dict[str, list[str]]:
    """Dedup tickers across sectors. Returns {ticker: [sector memberships]}.

    Preserves sector insertion order for each ticker's membership list.
    """
    result: dict[str, list[str]] = {}
    for sector, tickers in sectors.items():
        for ticker in tickers:
            result.setdefault(ticker, []).append(sector)
    return result


def all_tickers(path: Path | None = None) -> list[str]:
    """Deduped alphabetised list of every ticker in the universe file."""
    return sorted(flatten_universe(load_universe(path)).keys())


def load_delisted(path: Path | None = None) -> list[DelistedRecord]:
    """Load the companion delisted universe (tickers delisted during the backtest window).

    Returns [] if the file doesn't exist — delisted data is optional for MVP1.
    """
    target = path or DELISTED_UNIVERSE_PATH
    if not target.exists():
        return []
    data = yaml.safe_load(target.read_text()) or {}
    out: list[DelistedRecord] = []
    for entry in data.get("delisted") or []:
        ticker = entry.get("ticker")
        delisted = entry.get("delisted")
        if not ticker or not delisted:
            continue
        out.append(
            DelistedRecord(
                ticker=str(ticker).upper(),
                delisted=date.fromisoformat(str(delisted)),
                name=str(entry.get("name") or ""),
            )
        )
    return out


def delisted_tickers_on_or_after(start: date, path: Path | None = None) -> Iterator[str]:
    """Yield delisted tickers active at or after `start` (so they'd have been
    in the tradable universe for at least one bar in the backtest window).
    """
    for rec in load_delisted(path):
        if rec.delisted >= start:
            yield rec.ticker
