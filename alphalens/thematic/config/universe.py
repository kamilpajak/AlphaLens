"""Loader for the news-source input universe (S&P 100 + sector leaders).

The YAML file is the source of truth; this module flattens and deduplicates.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

UNIVERSE_PATH = Path(__file__).parent / "input_universe.yaml"


@lru_cache(maxsize=1)
def load_input_universe() -> frozenset[str]:
    """Return the deduped set of news-source tickers (S&P 100 ∪ sector leaders)."""
    with UNIVERSE_PATH.open() as f:
        data = yaml.safe_load(f)
    tickers: set[str] = set(data.get("sp100", []))
    for group in (data.get("sector_leaders") or {}).values():
        tickers.update(group)
    return frozenset(tickers)


def is_in_universe(ticker: str) -> bool:
    return ticker.upper() in load_input_universe()
