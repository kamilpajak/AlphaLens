import unittest

import pandas as pd


def _df(prices: list[float], volumes: list[float]) -> pd.DataFrame:
    idx = pd.date_range(end="2026-04-17", periods=len(prices), freq="B")
    return pd.DataFrame(
        {"Open": prices, "High": prices, "Low": prices, "Close": prices, "Volume": volumes},
        index=idx,
    )


class TestGuardrailCheckSingle(unittest.TestCase):
    def setUp(self):
        from alphalens.screeners.themed.guardrails import Guardrails

        self.g = Guardrails()

    def test_passes_healthy_stock(self):
        df = _df([10.0] * 60, [2_000_000] * 60)
        info = {"marketCap": 1_000_000_000, "averageVolume": 2_000_000}
        ok, reason = self.g.check("ABC", df, info)
        self.assertTrue(ok, f"expected pass, got reason={reason}")
        self.assertEqual(reason, "")

    def test_rejects_below_min_market_cap(self):
        df = _df([10.0] * 60, [2_000_000] * 60)
        info = {"marketCap": 100_000_000, "averageVolume": 2_000_000}
        ok, reason = self.g.check("ABC", df, info)
        self.assertFalse(ok)
        self.assertIn("market_cap", reason)

    def test_rejects_below_min_avg_volume(self):
        df = _df([10.0] * 60, [100_000] * 60)
        info = {"marketCap": 1_000_000_000, "averageVolume": 100_000}
        ok, reason = self.g.check("ABC", df, info)
        self.assertFalse(ok)
        self.assertIn("volume", reason)

    def test_rejects_penny_stock(self):
        df = _df([1.5] * 60, [2_000_000] * 60)
        info = {"marketCap": 1_000_000_000, "averageVolume": 2_000_000}
        ok, reason = self.g.check("ABC", df, info)
        self.assertFalse(ok)
        self.assertIn("price", reason)

    def test_rejects_empty_price_data(self):
        info = {"marketCap": 1_000_000_000, "averageVolume": 2_000_000}
        ok, reason = self.g.check("ABC", None, info)
        self.assertFalse(ok)
        self.assertIn("no_data", reason)

    def test_rejects_missing_market_cap(self):
        df = _df([10.0] * 60, [2_000_000] * 60)
        info = {"averageVolume": 2_000_000}
        ok, reason = self.g.check("ABC", df, info)
        self.assertFalse(ok)
        self.assertIn("market_cap", reason)

    def test_rejects_recent_reverse_split(self):
        """actions DataFrame with split ratio < 1.0 in the lookback window -> reverse split."""
        from alphalens.screeners.themed.guardrails import Guardrails

        df = _df([10.0] * 60, [2_000_000] * 60)
        info = {"marketCap": 1_000_000_000, "averageVolume": 2_000_000}
        # Reverse split (1-for-10) 90 days ago
        actions = pd.DataFrame(
            {"Stock Splits": [0.1]},
            index=pd.DatetimeIndex([pd.Timestamp("2026-01-17")]),
        )
        info["actions"] = actions
        g = Guardrails(asof=pd.Timestamp("2026-04-17"))
        ok, reason = g.check("ABC", df, info)
        self.assertFalse(ok)
        self.assertIn("reverse_split", reason)

    def test_allows_forward_split(self):
        from alphalens.screeners.themed.guardrails import Guardrails

        df = _df([10.0] * 60, [2_000_000] * 60)
        info = {"marketCap": 1_000_000_000, "averageVolume": 2_000_000}
        actions = pd.DataFrame(
            {"Stock Splits": [2.0]},  # 2-for-1 forward split
            index=pd.DatetimeIndex([pd.Timestamp("2026-01-17")]),
        )
        info["actions"] = actions
        g = Guardrails(asof=pd.Timestamp("2026-04-17"))
        ok, reason = g.check("ABC", df, info)
        self.assertTrue(ok, f"forward split should pass, got reason={reason}")

    def test_allows_old_reverse_split(self):
        """Reverse split older than lookback is ignored."""
        from alphalens.screeners.themed.guardrails import Guardrails

        df = _df([10.0] * 60, [2_000_000] * 60)
        info = {"marketCap": 1_000_000_000, "averageVolume": 2_000_000}
        actions = pd.DataFrame(
            {"Stock Splits": [0.1]},
            index=pd.DatetimeIndex([pd.Timestamp("2024-01-17")]),  # >1yr ago
        )
        info["actions"] = actions
        g = Guardrails(asof=pd.Timestamp("2026-04-17"))
        ok, reason = g.check("ABC", df, info)
        self.assertTrue(ok, f"old reverse split should pass, got reason={reason}")


class TestGuardrailsFilterBatch(unittest.TestCase):
    def test_returns_kept_and_rejected(self):
        from alphalens.screeners.themed.guardrails import Guardrails

        g = Guardrails()
        prices = {
            "GOOD": _df([10.0] * 60, [2_000_000] * 60),
            "TINY": _df([10.0] * 60, [2_000_000] * 60),
            "PENNY": _df([1.0] * 60, [2_000_000] * 60),
        }
        fundamentals = {
            "GOOD": {"marketCap": 1_000_000_000, "averageVolume": 2_000_000},
            "TINY": {"marketCap": 100_000_000, "averageVolume": 2_000_000},
            "PENNY": {"marketCap": 1_000_000_000, "averageVolume": 2_000_000},
        }
        kept, rejected = g.filter(["GOOD", "TINY", "PENNY"], prices, fundamentals)
        self.assertEqual(kept, ["GOOD"])
        self.assertEqual(set(rejected.keys()), {"TINY", "PENNY"})
        self.assertIn("market_cap", rejected["TINY"])
        self.assertIn("price", rejected["PENNY"])


if __name__ == "__main__":
    unittest.main()
