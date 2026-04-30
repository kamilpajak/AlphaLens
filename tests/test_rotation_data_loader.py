"""Tests for alphalens.archive.rotation.data_loader — yfinance + FRED orchestration.

Covers the MultiIndex-columns edge case (modern yfinance returns
DataFrames with ``(column, ticker)`` MultiIndex even for single-ticker
downloads when ``progress=False`` or other flags are set). Flatten must
happen BEFORE ``str.lower`` rename, else the rename silently operates on
the wrong level and downstream ``df[["open", ...]]`` KeyErrors.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import pandas as pd


def _flat_ohlcv(n: int = 260) -> pd.DataFrame:
    idx = pd.date_range("2019-01-02", periods=n, freq="B")
    return pd.DataFrame(
        {
            "Open": 100.0,
            "High": 101.0,
            "Low": 99.0,
            "Close": 100.5,
            "Volume": 1_000_000,
        },
        index=idx,
    )


def _multiindex_ohlcv(ticker: str, n: int = 260) -> pd.DataFrame:
    """Mimic modern yfinance: columns are MultiIndex [(OHLCV, ticker), ...]."""
    base = _flat_ohlcv(n)
    base.columns = pd.MultiIndex.from_product([base.columns, [ticker]])
    return base


class TestLoadRotationDataHandlesMultiIndex(unittest.TestCase):
    def test_flattens_multiindex_columns_from_yfinance(self):
        """Regression: yfinance returns MultiIndex columns; loader must
        flatten them before the lowercase rename, else downstream KeyError.
        """
        from alphalens.archive.rotation.data_loader import load_rotation_data
        from alphalens.data.macro.signals import SignalSet

        def fake_download(ticker, **kwargs):
            return _multiindex_ohlcv(ticker)

        fake_fred = MagicMock()
        # fred returns plain Series for DGS10/DGS2/VIXCLS
        idx = pd.date_range("2019-01-02", periods=260, freq="B")
        fake_fred.fetch_series = MagicMock(
            side_effect=[
                pd.Series(2.5, index=idx, name="DGS10"),
                pd.Series(1.5, index=idx, name="DGS2"),
                pd.Series(18.0, index=idx, name="VIXCLS"),
            ]
        )

        with (
            patch("yfinance.download", side_effect=fake_download) as mock_yf,
            patch(
                "alphalens.archive.rotation.data_loader.FREDClient.from_env",
                return_value=fake_fred,
            ),
        ):
            store, signals = load_rotation_data(start="2019-01-02", end="2019-12-31")

        self.assertEqual(mock_yf.call_count, 3)
        for ticker in ("SPY", "QQQ", "IWM"):
            df = store.full(ticker)
            # Post-fix: lowercase flat columns, no MultiIndex
            self.assertFalse(isinstance(df.columns, pd.MultiIndex))
            self.assertListEqual(list(df.columns), ["open", "high", "low", "close", "volume"])
        self.assertIsInstance(signals, SignalSet)

    def test_handles_flat_columns_too(self):
        """Some yfinance versions / flags return flat columns; also OK."""
        from alphalens.archive.rotation.data_loader import load_rotation_data

        def fake_download(ticker, **kwargs):
            return _flat_ohlcv()

        fake_fred = MagicMock()
        idx = pd.date_range("2019-01-02", periods=260, freq="B")
        fake_fred.fetch_series = MagicMock(
            side_effect=[
                pd.Series(2.5, index=idx),
                pd.Series(1.5, index=idx),
                pd.Series(18.0, index=idx),
            ]
        )

        with (
            patch("yfinance.download", side_effect=fake_download),
            patch(
                "alphalens.archive.rotation.data_loader.FREDClient.from_env",
                return_value=fake_fred,
            ),
        ):
            store, _ = load_rotation_data(start="2019-01-02", end="2019-12-31")

        for ticker in ("SPY", "QQQ", "IWM"):
            df = store.full(ticker)
            self.assertListEqual(list(df.columns), ["open", "high", "low", "close", "volume"])

    def test_raises_when_yfinance_returns_empty(self):
        from alphalens.archive.rotation.data_loader import load_rotation_data

        with (
            patch("yfinance.download", return_value=pd.DataFrame()),
            patch("alphalens.archive.rotation.data_loader.FREDClient.from_env"),
            self.assertRaises(RuntimeError) as ctx,
        ):
            load_rotation_data(start="2019-01-02", end="2019-12-31")
        self.assertIn("no data", str(ctx.exception).lower())


if __name__ == "__main__":
    unittest.main()
