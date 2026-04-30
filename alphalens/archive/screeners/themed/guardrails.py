"""Anti-pump guardrails — filter out penny stocks, micro-caps, illiquid names,
recently reverse-split tickers, and (when enabled) near-bankruptcy companies
before momentum scoring."""

from __future__ import annotations

import pandas as pd

from alphalens.data.fundamentals.gate import should_hard_reject

from .config import THEMED_DEFAULTS


class Guardrails:
    def __init__(self, config: dict | None = None, asof: pd.Timestamp | None = None):
        self.config = config or THEMED_DEFAULTS
        self.asof = asof or pd.Timestamp.today().normalize()

    def check(
        self,
        price_df: pd.DataFrame | None,
        info: dict,
    ) -> tuple[bool, str]:
        """Return (passed, reason). Reason is empty string on pass."""
        if price_df is None or price_df.empty:
            return False, "no_data"

        last_close = float(price_df["Close"].iloc[-1])
        if last_close < self.config["min_price"]:
            return False, f"price<{self.config['min_price']}"

        mkt_cap = info.get("marketCap")
        if mkt_cap is None or mkt_cap < self.config["min_market_cap"]:
            return False, "market_cap"

        avg_vol = info.get("averageVolume")
        if avg_vol is None and len(price_df) >= 20:
            avg_vol = float(price_df["Volume"].tail(20).mean())
        if avg_vol is None or avg_vol < self.config["min_avg_volume"]:
            return False, "volume"

        if self._has_recent_reverse_split(info):
            return False, "reverse_split"

        # Fundamental hard reject (opt-in via config). `info` carries the
        # extracted features alongside yfinance basics — when absent (e.g.
        # fundamental_gate_enabled=False) this is a no-op.
        hard_rejected, reason = should_hard_reject(info, self.config)
        if hard_rejected:
            return False, reason

        return True, ""

    def filter(
        self,
        tickers: list[str],
        prices: dict[str, pd.DataFrame],
        fundamentals: dict[str, dict],
    ) -> tuple[list[str], dict[str, str]]:
        """Apply check() to each ticker. Returns (kept, {rejected_ticker: reason})."""
        kept: list[str] = []
        rejected: dict[str, str] = {}
        for ticker in tickers:
            ok, reason = self.check(prices.get(ticker), fundamentals.get(ticker, {}))
            if ok:
                kept.append(ticker)
            else:
                rejected[ticker] = reason
        return kept, rejected

    def _has_recent_reverse_split(self, info: dict) -> bool:
        actions = info.get("actions")
        if actions is None or not isinstance(actions, pd.DataFrame) or actions.empty:
            return False
        if "Stock Splits" not in actions.columns:
            return False

        cutoff = self.asof - pd.Timedelta(days=self.config["reverse_split_lookback_days"])
        try:
            recent = actions[actions.index >= cutoff]
        except TypeError:
            return False

        splits = recent["Stock Splits"]
        # Ratio < 1.0 and > 0 = reverse split (e.g. 0.1 = 1-for-10)
        return bool(((splits > 0) & (splits < 1.0)).any())
