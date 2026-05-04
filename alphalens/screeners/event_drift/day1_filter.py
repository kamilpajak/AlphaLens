"""Bernard-Thomas (1989) day-1 sign confirmation gate.

The sign of the market's day-1 reaction to an earnings announcement must
match the sign of the SUE for the name to enter the long-only PEAD
portfolio. Filters "bull-trap" cases where forward guidance destroyed the
historical earnings surprise (positive SUE but negative market reaction).

Empirical: roughly 60% of top-quintile-SUE announcements satisfy the gate
(Bartov-Radhakrishnan-Krinsky 2000 Table III). The rest are dropped to
focus the strategy on cases where market expectations and fundamental
surprises align directionally.
"""

from __future__ import annotations

import math
from datetime import date

import pandas as pd


def day1_sign_confirmed(*, sue: float, day1_return: float | None) -> bool:
    """True iff ``day1_return`` is finite, non-zero, and matches sign(``sue``).

    Filtering rules:
      - ``day1_return is None`` or NaN or +/-inf -> False (data uncertainty)
      - ``day1_return == 0`` -> False (ambiguous reaction)
      - ``sue == 0`` -> False (no surprise to confirm)
      - sign mismatch -> False ("bull trap" or "false bear")
      - sign match -> True
    """
    if day1_return is None:
        return False
    if not math.isfinite(day1_return):
        return False
    if day1_return == 0.0:
        return False
    if sue == 0.0:
        return False
    return (sue > 0.0) == (day1_return > 0.0)


def day1_return(*, prices: pd.Series, market_day: date) -> float | None:
    """Compute close-to-close return at the market-announcement day.

    ``prices`` is a daily close series indexed by ``pd.Timestamp``. Returns
    ``close[market_day] / close[prev_close_day] - 1`` where ``prev_close_day``
    is the most recent index entry strictly before ``market_day``. Returns
    ``None`` when ``market_day`` is missing or there is no prior close, or
    when the prior close is non-positive.
    """
    market_ts = pd.Timestamp(market_day)
    if market_ts not in prices.index:
        return None
    market_close = float(prices.loc[market_ts])

    earlier = prices.loc[prices.index < market_ts]
    if earlier.empty:
        return None
    prev_close = float(earlier.iloc[-1])
    if prev_close <= 0.0:
        return None
    return market_close / prev_close - 1.0
