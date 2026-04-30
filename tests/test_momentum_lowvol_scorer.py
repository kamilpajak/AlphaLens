"""Unit tests for the mom+lowvol scorer adapter.

Synthetic-fixture coverage of the adapter that previously lived as
inline logic inside `experiment_momentum_lowvol_combo.py`. Used as a
TDD safety net for the S3776 cognitive-complexity refactor: behaviour
is locked here so the inline-vs-helper split can't drift the output.
"""

from __future__ import annotations

import unittest

import numpy as np
import pandas as pd


def _hist(closes: list[float], volumes: list[float] | None = None) -> pd.DataFrame:
    n = len(closes)
    vols = volumes if volumes is not None else [1_000_000.0] * n
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    return pd.DataFrame({"close": closes, "volume": vols}, index=idx)


class MomentumLowvolAdapterTests(unittest.TestCase):
    def test_skips_benchmark_short_history_and_invalid_prices(self):
        from alphalens.screeners.momentum_lowvol import momentum_lowvol_adapter

        # 253 is the minimum required bar count.
        n = 260
        rng = np.random.default_rng(7)
        good_closes = list(100.0 * np.cumprod(1.0 + rng.normal(0.0005, 0.01, size=n)))

        histories = {
            "SPY": _hist(good_closes),  # benchmark — skipped
            "SHORT": _hist(good_closes[:200]),  # too few bars
            "ZERO": _hist([0.0] * n),  # invalid prices
            "OK1": _hist(good_closes),
            "OK2": _hist(list(reversed(good_closes))),  # different price path
        }

        out = momentum_lowvol_adapter(histories, {"benchmark": "SPY"})

        self.assertEqual(set(out["ticker"]), {"OK1", "OK2"})
        self.assertEqual(len(out), 2)
        # Sorted descending by score.
        self.assertGreaterEqual(out.iloc[0]["score"], out.iloc[1]["score"])

    def test_returns_empty_when_universe_filtered_out(self):
        from alphalens.screeners.momentum_lowvol import momentum_lowvol_adapter

        # Every ticker too short → all skipped.
        histories = {f"T{i}": _hist([100.0] * 100) for i in range(5)}

        out = momentum_lowvol_adapter(histories)

        self.assertEqual(len(out), 0)
        self.assertEqual(list(out.columns), ["ticker", "score"])

    def test_adv_filter_excludes_thin_volume_tickers(self):
        from alphalens.screeners.momentum_lowvol import momentum_lowvol_adapter

        rng = np.random.default_rng(11)
        n = 260
        closes = list(100.0 * np.cumprod(1.0 + rng.normal(0.0005, 0.01, size=n)))

        # OK: ~$10M ADV (volume 100k × close ~$100), THIN: ~$100 ADV — far
        # below any realistic floor.
        histories = {
            "OK": _hist(closes, [100_000.0] * n),
            "THIN": _hist(closes, [1.0] * n),
        }

        out_lo = momentum_lowvol_adapter(histories, {"_adv_min_usd": 0.0})
        out_hi = momentum_lowvol_adapter(histories, {"_adv_min_usd": 1_000_000.0})

        self.assertEqual(set(out_lo["ticker"]), {"OK", "THIN"})
        # Hi threshold filters out THIN: OK should remain (ADV ~ price * 10000).
        self.assertEqual(set(out_hi["ticker"]), {"OK"})

    def test_zero_vol_ticker_drops_silently(self):
        """Constant-price ticker has vol=0 and must be excluded —
        otherwise z-scoring would crash on a degenerate column."""
        from alphalens.screeners.momentum_lowvol import momentum_lowvol_adapter

        n = 260
        rng = np.random.default_rng(3)
        good = list(100.0 * np.cumprod(1.0 + rng.normal(0.0005, 0.01, size=n)))

        histories = {
            "OK1": _hist(good),
            "OK2": _hist(list(reversed(good))),
            "FLAT": _hist([100.0] * n),
        }

        out = momentum_lowvol_adapter(histories)

        self.assertNotIn("FLAT", set(out["ticker"]))
        self.assertEqual(len(out), 2)


if __name__ == "__main__":
    unittest.main()
