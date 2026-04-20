"""Applies a `PerTickerCostModel` to a `BacktestReport`.

Input:
  - `BacktestReport` with populated `DailyResult.trades` lists
  - `MarketCharacteristicsStore` primed over the same window
  - `PerTickerCostModel` with chosen profile
  - optional `theme_map: dict[ticker, theme]` for theme-level breakdown

Output (`CostApplicationResult`):
  - `net_returns`: pd.Series (portfolio_return − daily_cost/portfolio_value)
  - `per_ticker_breakdown`: DataFrame[ticker, enter_count, exit_count,
    total_cost_usd, total_cost_bps_of_nav]
  - `per_theme_breakdown`: same but aggregated by theme (empty if no map)
  - `total_cost_bps_annualized`: summary scalar

When spread/volatility/ADV is unavailable for a ticker-day, uses
configurable fallbacks.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

import pandas as pd

from .cost_model import PerTickerCostModel
from .engine import BacktestReport, Trade
from .market_chars_store import MarketCharacteristicsStore


@dataclass(frozen=True)
class CostApplicationResult:
    net_returns: pd.Series
    per_ticker_breakdown: pd.DataFrame
    per_theme_breakdown: pd.DataFrame
    total_cost_bps_annualized: float


class CostApplier:
    """Computes net returns + per-ticker/per-theme cost breakdowns post-hoc."""

    def __init__(
        self,
        market_chars: MarketCharacteristicsStore,
        cost_model: PerTickerCostModel,
        theme_map: Mapping[str, str] | None = None,
        fallback_spread_bps: float = 50.0,
        fallback_volatility: float = 0.02,
        fallback_adv_dollar: float = 10_000_000.0,
    ):
        self._chars = market_chars
        self._cost_model = cost_model
        self._theme_map = dict(theme_map) if theme_map else {}
        self._fallback_spread = float(fallback_spread_bps)
        self._fallback_vol = float(fallback_volatility)
        self._fallback_adv = float(fallback_adv_dollar)

    def apply(self, report: BacktestReport) -> CostApplicationResult:
        pv = report.portfolio_value
        daily_gross = []
        daily_cost_usd = []
        dates = []
        per_ticker: dict[str, dict] = {}

        for snap in report.daily_results:
            dates.append(snap.date)
            daily_gross.append(snap.portfolio_return)
            day_cost = 0.0
            if snap.trades:
                for trade in snap.trades:
                    cost = self._cost_for_trade(trade)
                    day_cost += cost
                    stats = per_ticker.setdefault(
                        trade.ticker,
                        {"ticker": trade.ticker, "enter_count": 0, "exit_count": 0, "total_cost_usd": 0.0},
                    )
                    if trade.direction == "enter":
                        stats["enter_count"] += 1
                    else:
                        stats["exit_count"] += 1
                    stats["total_cost_usd"] += cost
            daily_cost_usd.append(day_cost)

        index = pd.DatetimeIndex(dates)
        gross_series = pd.Series(daily_gross, index=index, name="gross")
        cost_fraction = pd.Series(
            [c / pv if pv > 0 else 0.0 for c in daily_cost_usd],
            index=index,
            name="cost_fraction",
        )
        net_returns = (gross_series - cost_fraction).rename("net")

        per_ticker_df = pd.DataFrame(list(per_ticker.values())).sort_values(
            "total_cost_usd", ascending=False, ignore_index=True
        )
        if per_ticker_df.empty:
            per_ticker_df = pd.DataFrame(
                columns=["ticker", "enter_count", "exit_count", "total_cost_usd", "total_cost_bps_of_nav"]
            )
        else:
            per_ticker_df["total_cost_bps_of_nav"] = (
                per_ticker_df["total_cost_usd"] / pv * 10_000.0 if pv > 0 else 0.0
            )

        per_theme_df = self._build_theme_breakdown(per_ticker_df, pv)

        total_cost_usd = sum(daily_cost_usd)
        days = max(len(daily_cost_usd), 1)
        total_cost_bps_annual = (
            (total_cost_usd / pv) * (252.0 / days) * 10_000.0 if pv > 0 else 0.0
        )

        return CostApplicationResult(
            net_returns=net_returns,
            per_ticker_breakdown=per_ticker_df,
            per_theme_breakdown=per_theme_df,
            total_cost_bps_annualized=float(total_cost_bps_annual),
        )

    def _cost_for_trade(self, trade: Trade) -> float:
        asof = trade.date.date()
        raw_spread = self._chars.spread_at(trade.ticker, asof)
        # `spread_at` returns decimal (e.g. 0.02 = 2%). Convert to bps.
        spread_bps = (
            raw_spread * 10_000.0 if raw_spread is not None else self._fallback_spread
        )
        volatility = self._chars.volatility_at(trade.ticker, asof) or self._fallback_vol
        adv_dollar = self._chars.adv_dollar_at(trade.ticker, asof) or self._fallback_adv
        share_price = trade.price if trade.price > 0 else 1.0
        return self._cost_model.cost_for_trade(
            trade_notional=trade.notional,
            spread_bps=spread_bps,
            volatility=volatility,
            adv_dollar=adv_dollar,
            share_price=share_price,
        )

    def _build_theme_breakdown(
        self, per_ticker_df: pd.DataFrame, pv: float
    ) -> pd.DataFrame:
        if per_ticker_df.empty or not self._theme_map:
            return pd.DataFrame(columns=["theme", "total_cost_usd", "pct_of_total"])
        tagged = per_ticker_df.assign(
            theme=per_ticker_df["ticker"].map(self._theme_map).fillna("untagged")
        )
        agg = (
            tagged.groupby("theme", as_index=False)["total_cost_usd"]
            .sum()
            .sort_values("total_cost_usd", ascending=False, ignore_index=True)
        )
        total = agg["total_cost_usd"].sum()
        if total > 0:
            agg["pct_of_total"] = agg["total_cost_usd"] / total
        else:
            agg["pct_of_total"] = 0.0
        return agg
