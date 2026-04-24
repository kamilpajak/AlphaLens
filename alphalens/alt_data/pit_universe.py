"""Point-in-time R2000-approximate universe builder.

Combines XBRL shares-outstanding (:mod:`shares_outstanding`) with yfinance
close prices (:mod:`yfinance_cache`) to derive a PIT market-cap universe at
a given ``asof`` date. Per Perplexity R7 this reconstruction is labeled
"approximate Russell 2000" rather than true reconstitution — the $300M–$3B
band captures the small-cap zone where insider information asymmetry is
strongest (Lakonishok-Lee 2001) without requiring a paid index provider.

Documented bias: survivorship through yfinance partial delisted coverage,
estimated 50-100 bps/y per Perplexity R3 guidance. Evaluated in Phase 3b
under the 3-scenario sensitivity table from design doc §3b.2.6.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Mapping, Protocol

import pandas as pd

from .shares_outstanding import SharesFact, latest_shares_as_of


@dataclass(frozen=True)
class UniverseConfig:
    cap_min_usd: float = 300_000_000.0
    cap_max_usd: float = 3_000_000_000.0


class _CikLookup(Protocol):
    def lookup(self, ticker: str) -> str | None: ...


def close_as_of(history: pd.DataFrame, asof: date) -> float | None:
    """Return the latest close price on or before ``asof``.

    Handles weekends and holidays by taking the most recent prior trading
    day. Returns ``None`` for empty history or for ``asof`` preceding all
    available data.
    """
    if history.empty or "close" not in history.columns:
        return None
    cutoff = pd.Timestamp(asof)
    eligible = history[history.index <= cutoff]
    if eligible.empty:
        return None
    return float(eligible.iloc[-1]["close"])


def build_pit_universe(
    asof: date,
    *,
    shares_by_cik: Mapping[str, list[SharesFact]],
    histories: Mapping[str, pd.DataFrame],
    cik_map: _CikLookup,
    config: UniverseConfig = UniverseConfig(),
) -> list[str]:
    """Return sorted list of tickers whose PIT market cap falls in band.

    A ticker is included iff:
    - ``cik_map.lookup(ticker)`` resolves to a CIK,
    - ``shares_by_cik[cik]`` has at least one fact filed on or before asof,
    - ``histories[ticker]`` has at least one close on or before asof,
    - ``cap_min_usd <= shares × close <= cap_max_usd``.

    Missing any input silently excludes the ticker — the caller is
    responsible for logging upstream data-coverage gaps if desired.
    """
    eligible: list[str] = []
    for ticker in histories:
        cik = cik_map.lookup(ticker)
        if cik is None:
            continue
        shares = latest_shares_as_of(shares_by_cik.get(cik, []), asof)
        if shares is None:
            continue
        close = close_as_of(histories[ticker], asof)
        if close is None:
            continue
        mcap = float(shares) * close
        if config.cap_min_usd <= mcap <= config.cap_max_usd:
            eligible.append(ticker)
    return sorted(eligible)
