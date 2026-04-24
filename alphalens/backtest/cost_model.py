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

import math
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


@dataclass(frozen=True)
class RealisticCostModel:
    """Dual-track cost model per design doc §5 (Layer 2b post-mortem lesson).

    ``CostModel`` above assumes a flat annual drag; that over-simplification let
    Layer 2b claim "100 bps/y moderate" alpha in a daily-rebalance microcap
    universe whose true spread drag was ~100% annualised. This class replaces
    the flat assumption with a per-trade formula that scales with spread,
    trade size, ADV and volatility.

    Primary (spread-dominated, retail R2000 empirical — Perplexity R5):
        one_way_bps   = half_spread_bps + adverse_selection_bps
        round_trip    = 2 × one_way
        period_drag   = round_trip × turnover_fraction

    Secondary (Almgren-Chriss-style impact, robustness check — R5):
        impact_bps    = k × sqrt(size / adv) × annual_vol × sqrt(horizon/252) × 10_000
        one_way       = half_spread + adverse_selection + impact

    The ``k`` coefficient is empirical; calibrate via :func:`calibrate_k`
    against realised slippage on a sample of historical rebalances.
    """

    adverse_selection_bps: float = 5.0
    k: float = 0.05

    def primary_one_way_bps(self, half_spread_bps: float) -> float:
        return half_spread_bps + self.adverse_selection_bps

    def primary_round_trip_bps(self, half_spread_bps: float) -> float:
        return 2.0 * self.primary_one_way_bps(half_spread_bps)

    def primary_period_drag_bps(
        self, half_spread_bps: float, turnover_fraction: float
    ) -> float:
        return self.primary_round_trip_bps(half_spread_bps) * turnover_fraction

    def secondary_market_impact_bps(
        self,
        *,
        trade_size: float,
        adv: float,
        annual_vol: float,
        horizon_days: float,
    ) -> float:
        if adv <= 0 or trade_size <= 0:
            return 0.0
        return (
            self.k
            * math.sqrt(trade_size / adv)
            * annual_vol
            * math.sqrt(horizon_days / 252.0)
            * 10_000.0
        )

    def secondary_one_way_bps(
        self,
        half_spread_bps: float,
        *,
        trade_size: float,
        adv: float,
        annual_vol: float,
        horizon_days: float,
    ) -> float:
        return (
            half_spread_bps
            + self.adverse_selection_bps
            + self.secondary_market_impact_bps(
                trade_size=trade_size,
                adv=adv,
                annual_vol=annual_vol,
                horizon_days=horizon_days,
            )
        )

    def apply_annual_drag_bps(
        self,
        gross_returns: pd.Series,
        *,
        annual_drag_bps: float,
        periods_per_year: int = 252,
    ) -> pd.Series:
        """Subtract a uniform per-period drag, matching ``CostModel.apply`` semantics.

        Useful when you've used the formulae above to derive an **effective**
        annualised drag from a realistic spread × turnover × impact profile
        and want to see its impact on an existing return series without
        re-running the backtest.
        """
        daily_drag = (annual_drag_bps / 10_000.0) / max(1, periods_per_year)
        return gross_returns - daily_drag


def calibrate_k(
    realized_impact_bps: Sequence[float],
    *,
    trade_sizes: Sequence[float],
    advs: Sequence[float],
    annual_vols: Sequence[float],
    horizon_days: float = 21.0,
) -> float:
    """Least-squares fit of the Almgren-Chriss ``k`` coefficient.

    Rows with ``adv <= 0`` or ``trade_size <= 0`` contribute no information
    and are silently dropped; ``ValueError`` if no usable rows remain.
    """
    if not realized_impact_bps:
        raise ValueError("realized_impact_bps is empty")
    if not (len(realized_impact_bps) == len(trade_sizes) == len(advs) == len(annual_vols)):
        raise ValueError("input sequences must be same length")

    xs: list[float] = []
    ys: list[float] = []
    for y, size, adv, vol in zip(realized_impact_bps, trade_sizes, advs, annual_vols):
        if adv <= 0 or size <= 0:
            continue
        x = math.sqrt(size / adv) * vol * math.sqrt(horizon_days / 252.0) * 10_000.0
        xs.append(x)
        ys.append(float(y))

    if not xs:
        raise ValueError("no usable rows (all had adv<=0 or size<=0)")

    x_arr = np.asarray(xs, dtype=float)
    y_arr = np.asarray(ys, dtype=float)
    denom = float((x_arr * x_arr).sum())
    if denom == 0.0:
        raise ValueError("degenerate inputs (all predictors zero)")
    return float((x_arr * y_arr).sum() / denom)


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
