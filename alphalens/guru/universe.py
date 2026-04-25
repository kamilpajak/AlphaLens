"""S&P 500 point-in-time membership loader for GuruAgent pilot.

Eliminates survivorship bias: each test year uses the actual S&P 500 membership
as of that year's start, not today's constituent list. Snapshots sourced from
Wikipedia's historical S&P 500 change log (scraped via scripts/scrape_sp500_history.py).

Layout: ``{data_dir}/{year}.yaml`` with fields:
  year: 2018
  as_of: "2018-01-01"
  source: "wikipedia"
  tickers: [AAPL, ABT, ...]
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import yaml

DEFAULT_DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "sp500_pit"


class UniverseError(RuntimeError):
    """Raised when requested S&P 500 snapshot is not available."""


def load_sp500_pit(*, year: int, data_dir: Path | None = None) -> list[str]:
    """Load S&P 500 constituent list for a given year.

    Returns list of tickers sorted as stored in YAML (no additional sorting).
    Raises UniverseError if the ``{year}.yaml`` file is missing.
    """
    directory = Path(data_dir) if data_dir else DEFAULT_DATA_DIR
    path = directory / f"{year}.yaml"
    if not path.exists():
        raise UniverseError(
            f"No S&P 500 snapshot for year {year} at {path}. "
            f"Run scripts/scrape_sp500_history.py first."
        )
    data = yaml.safe_load(path.read_text()) or {}
    tickers = data.get("tickers") or []
    if not tickers:
        raise UniverseError(f"Empty ticker list in {path}")
    return [str(t).upper() for t in tickers]


def load_sp500_pit_for_date(asof: pd.Timestamp, *, data_dir: Path | None = None) -> list[str]:
    """Pick the snapshot whose ``as_of`` is the latest ≤ ``asof``.

    If no snapshot precedes ``asof``, raises UniverseError.
    """
    directory = Path(data_dir) if data_dir else DEFAULT_DATA_DIR
    candidates: list[tuple[pd.Timestamp, Path]] = []
    for path in sorted(directory.glob("*.yaml")):
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
        raise UniverseError(f"No S&P 500 snapshot with as_of ≤ {asof.date()} in {directory}")
    eligible.sort(key=lambda tp: tp[0])
    _, latest_path = eligible[-1]
    data = yaml.safe_load(latest_path.read_text()) or {}
    return [str(t).upper() for t in (data.get("tickers") or [])]
