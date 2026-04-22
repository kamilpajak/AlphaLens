"""Russell 2000 / IWM universe loader.

Phase 1 provides current-holdings loading from a YAML snapshot. Historical
PIT reconstruction (Phase 2) will supplement this with market-cap-band
filtering from EDGAR 10-Q filings; see design doc §3 R6 amendment and
`docs/research/layer2d_alt_data_design.md` for the documented ~100-150 bps/y
survivorship bias such reconstruction carries.
"""

from __future__ import annotations

from pathlib import Path

import yaml


def load_iwm_current(path: Path) -> list[str]:
    """Load IWM current holdings from YAML snapshot.

    YAML schema: ``{"tickers": [<TICKER>, ...]}``.
    Tickers are upper-cased and deduplicated preserving first occurrence.
    """
    if not path.exists():
        raise FileNotFoundError(f"IWM snapshot not found: {path}")
    data = yaml.safe_load(path.read_text()) or {}
    if "tickers" not in data:
        raise KeyError(f"Missing 'tickers' key in {path}")
    seen: set[str] = set()
    result: list[str] = []
    for raw in data["tickers"] or []:
        ticker = str(raw).upper()
        if ticker not in seen:
            seen.add(ticker)
            result.append(ticker)
    return result
