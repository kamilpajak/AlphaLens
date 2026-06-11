import datetime as dt
import unittest

import numpy as np
import pandas as pd
from alphalens_pipeline.thematic.trade_setup import builder
from alphalens_pipeline.thematic.trade_setup.model import (
    SCHEMA_VERSION,
    STATUS_NO_STRUCTURE,
    STATUS_OK,
)


def _synth_frame(closes: list[float]) -> pd.DataFrame:
    """Build an OHLCV frame (lowercase yfinance schema) with ~2% daily range."""
    idx = pd.date_range("2024-01-01", periods=len(closes), freq="B")
    close = np.asarray(closes, dtype=float)
    return pd.DataFrame(
        {
            "open": close,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": np.full(len(closes), 1_000_000.0),
        },
        index=idx,
    )


def _oscillating_uptrend(n: int = 250) -> list[float]:
    t = np.arange(n)
    return list(100.0 + 0.16 * t + 8.0 * np.sin(2 * np.pi * t / 40.0))


class TestBuildFromFrame(unittest.TestCase):
    def test_oscillating_uptrend_yields_valid_setup(self):
        setup = builder.build_trade_setup_from_frame(_synth_frame(_oscillating_uptrend()))
        self.assertEqual(setup.status, STATUS_OK)
        self.assertGreaterEqual(len(setup.entry_tiers), 1)

        close = setup.asof_close
        prices = [t.limit for t in setup.entry_tiers]
        # All tiers strictly below close, strictly descending (monotone guard).
        self.assertTrue(all(p < close for p in prices))
        self.assertEqual(prices, sorted(prices, reverse=True))
        # Stop sits below every entry tier.
        self.assertLess(setup.disaster_stop, min(prices))
        # Allocations sum ~100 and every tier is far enough from the stop.
        self.assertAlmostEqual(sum(t.alloc_pct for t in setup.entry_tiers), 100.0, places=1)
        self.assertTrue(all((p - setup.disaster_stop) >= 0.5 * setup.atr for p in prices))
        # ATR distance is positive (tiers below close) and grows with depth.
        dists = [t.atr_distance for t in setup.entry_tiers]
        self.assertTrue(all(d > 0 for d in dists))
        self.assertEqual(dists, sorted(dists))

    def test_take_profit_targets_above_close_and_ascending(self):
        setup = builder.build_trade_setup_from_frame(_synth_frame(_oscillating_uptrend()))
        targets = [t.target for t in setup.tp_tranches]
        self.assertGreaterEqual(len(targets), 1)
        self.assertTrue(all(t > setup.asof_close for t in targets))
        self.assertEqual(targets, sorted(targets))

    def test_disaster_stop_respects_minus_25_floor(self):
        from alphalens_pipeline.thematic.trade_setup import sizing

        setup = builder.build_trade_setup_from_frame(_synth_frame(_oscillating_uptrend()))
        # Floor is anchored on the blended entry: stop >= blended * 0.75.
        blended = sizing.blended_entry(
            [t.limit for t in setup.entry_tiers], [t.alloc_pct for t in setup.entry_tiers]
        )
        self.assertGreaterEqual(setup.disaster_stop, blended * 0.75 - 1e-6)

    def test_downtrend_with_ma_above_close_does_not_invert(self):
        # Pure downtrend 200 -> 100: SMA50/SMA200 sit ABOVE the last close.
        closes = list(np.linspace(200.0, 100.0, 250))
        setup = builder.build_trade_setup_from_frame(_synth_frame(closes))
        self.assertEqual(setup.status, STATUS_OK)
        # No entry tier may sit above close (the max() inversion guard).
        self.assertTrue(all(t.limit < setup.asof_close for t in setup.entry_tiers))

    def test_short_frame_is_no_structure(self):
        setup = builder.build_trade_setup_from_frame(_synth_frame([100.0] * 10))
        self.assertEqual(setup.status, STATUS_NO_STRUCTURE)
        self.assertIsNone(setup.disaster_stop)
        self.assertEqual(setup.entry_tiers, ())

    def test_empty_frame_is_no_structure(self):
        setup = builder.build_trade_setup_from_frame(pd.DataFrame())
        self.assertEqual(setup.status, STATUS_NO_STRUCTURE)

    def test_to_dict_carries_schema_version_and_status(self):
        setup = builder.build_trade_setup_from_frame(_synth_frame(_oscillating_uptrend()))
        d = setup.to_dict()
        self.assertEqual(d["schema_version"], SCHEMA_VERSION)
        self.assertEqual(d["status"], STATUS_OK)
        self.assertIn("entry_tiers", d)
        self.assertIn("order_ttl_days", d)

    def test_default_order_ttl_is_single_sourced_from_constants(self):
        # The builder default and the replay fallback MUST agree on one value, or
        # a brief stamps one TTL while the replay falls back to another (the live
        # 10-vs-7 divergence). Pin the builder default to the canonical constant.
        from alphalens_pipeline.paper.constants import DEFAULT_ORDER_TTL_DAYS

        setup = builder.build_trade_setup_from_frame(_synth_frame(_oscillating_uptrend()))
        self.assertEqual(setup.to_dict()["order_ttl_days"], DEFAULT_ORDER_TTL_DAYS)


class TestBuildViaLoader(unittest.TestCase):
    def test_loader_failure_degrades_to_no_structure(self):
        def _boom(ticker: str, asof: dt.date) -> pd.DataFrame:
            raise RuntimeError("yfinance down")

        setup = builder.build_trade_setup(ticker="FDS", asof=dt.date(2024, 6, 1), loader=_boom)
        self.assertEqual(setup.status, STATUS_NO_STRUCTURE)

    def test_loader_supplies_frame(self):
        frame = _synth_frame(_oscillating_uptrend())
        setup = builder.build_trade_setup(
            ticker="FDS", asof=dt.date(2024, 6, 1), loader=lambda t, a: frame
        )
        self.assertEqual(setup.status, STATUS_OK)


if __name__ == "__main__":
    unittest.main()
