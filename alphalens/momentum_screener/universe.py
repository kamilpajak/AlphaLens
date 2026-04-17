"""Theme-based universe loader — curated small/mid-cap tickers per theme."""

from __future__ import annotations

from pathlib import Path

import yaml

from .config import UNIVERSE_PATH


def load_universe(path: Path | None = None) -> dict[str, list[str]]:
    """Load {theme: [tickers]} from YAML. Raises FileNotFoundError if missing."""
    target = path or UNIVERSE_PATH
    if not target.exists():
        raise FileNotFoundError(f"Universe file not found: {target}")
    data = yaml.safe_load(target.read_text()) or {}
    return {theme: list(tickers or []) for theme, tickers in data.items()}


def flatten_universe(themes: dict[str, list[str]]) -> dict[str, list[str]]:
    """Dedup tickers across themes. Returns {ticker: [themes it belongs to]}.

    Preserves theme insertion order for each ticker's membership list.
    """
    result: dict[str, list[str]] = {}
    for theme, tickers in themes.items():
        for ticker in tickers:
            result.setdefault(ticker, []).append(theme)
    return result
