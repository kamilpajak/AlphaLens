"""Transaction cost model for MVP1 net-of-cost performance analysis.

Three common parameterisations for small/mid-cap retail (per Perplexity 2026):
  - aggressive:    75 bps annual drag (tight execution, IB retail at $0/trade)
  - moderate:     100 bps annual drag (default — 0.10% spread, no impact, $0 commission)
  - conservative: 150 bps annual drag (0.20%+ spread on thin names, impact)

The model converts an annual basis-point drag to a **per-rebalance** deduction
scaled by realised turnover, so strategies with stable top-N see less drag than
high-churn ones. Net returns = gross returns − (turnover × per-trade cost).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Sequence

import numpy as np
import pandas as pd


CostProfile = Literal["aggressive", "moderate", "conservative", "gross"]

_PROFILE_BPS = {
    "aggressive": 75.0,
    "moderate": 100.0,
    "conservative": 150.0,
    "gross": 0.0,
}


@dataclass(frozen=True)
class CostModel:
    """Applies a turnover-scaled transaction cost drag to daily portfolio returns."""

    annual_drag_bps: float = 100.0  # default moderate

    @classmethod
    def from_profile(cls, profile: CostProfile) -> "CostModel":
        if profile not in _PROFILE_BPS:
            raise ValueError(
                f"unknown cost profile {profile!r}; expected one of {list(_PROFILE_BPS)}"
            )
        return cls(annual_drag_bps=_PROFILE_BPS[profile])

    def per_period_drag(self, periods_per_year: int = 252) -> float:
        """Daily fractional drag assuming uniform turnover.

        `1bps = 0.0001`, so 100 bps/yr ÷ 252 ≈ 0.0397 bps/day ≈ 0.000397%/day.
        """
        return (self.annual_drag_bps / 10_000.0) / max(1, periods_per_year)

    def apply(
        self,
        daily_returns: Sequence[float] | pd.Series,
        daily_turnover: Sequence[float] | pd.Series | None = None,
        periods_per_year: int = 252,
    ) -> pd.Series:
        """Return a Series of net-of-cost daily returns.

        When `daily_turnover` is None we assume 100% turnover every bar (worst
        case — every name changes every day). When provided, it should be a
        per-day value in [0, 1] indicating what fraction of the portfolio
        churned on that day; cost scales linearly with it.
        """
        gross = pd.Series(list(daily_returns), dtype=float)
        drag = self.per_period_drag(periods_per_year=periods_per_year)
        if daily_turnover is None:
            cost = pd.Series(drag, index=gross.index)
        else:
            turnover = pd.Series(list(daily_turnover), dtype=float)
            if len(turnover) != len(gross):
                raise ValueError(
                    f"turnover length {len(turnover)} != returns length {len(gross)}"
                )
            cost = turnover * drag
        return gross - cost

    def apply_scalar_to_sharpe(
        self, gross_sharpe: float, volatility_daily: float, periods_per_year: int = 252
    ) -> float:
        """Quick-and-dirty estimate of post-cost Sharpe from gross Sharpe + daily vol.

        Useful for sensitivity tables without re-running the backtest.
        """
        if volatility_daily <= 0:
            return gross_sharpe
        drag = self.per_period_drag(periods_per_year=periods_per_year)
        annualised_drag_sharpe = drag / volatility_daily * np.sqrt(periods_per_year)
        return float(gross_sharpe - annualised_drag_sharpe)


def cost_sensitivity_table(
    daily_returns: Sequence[float] | pd.Series,
    daily_turnover: Sequence[float] | pd.Series | None = None,
    periods_per_year: int = 252,
) -> pd.DataFrame:
    """Compare portfolio returns across gross / 75 / 100 / 150 bps.

    Returns a DataFrame with one row per profile and columns (annualised):
        profile, drag_bps, sharpe, annual_return
    """
    from .metrics import sharpe

    gross = pd.Series(list(daily_returns), dtype=float)
    rows: list[dict] = []
    for name, bps in _PROFILE_BPS.items():
        net = CostModel(annual_drag_bps=bps).apply(
            gross, daily_turnover=daily_turnover, periods_per_year=periods_per_year
        )
        rows.append(
            {
                "profile": name,
                "drag_bps": bps,
                "sharpe": sharpe(net.tolist(), periods_per_year=periods_per_year),
                "annual_return": float((1 + net).prod() ** (periods_per_year / max(len(net), 1)) - 1),
            }
        )
    return pd.DataFrame(rows).sort_values("drag_bps").reset_index(drop=True)
