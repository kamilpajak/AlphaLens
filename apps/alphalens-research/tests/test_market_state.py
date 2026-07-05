"""Unit tests for the pure market-state classifier (PR-1).

``classify_state`` composes the PR-0 primitives into the 4-state trend×vol label
(+ ``unknown``) at the last bar, per the design memo
(docs/research/market_state_signal_design_2026_07_05.md §1.3). It is a pure
function of assembled trailing SPY OHLC + VIX series — no store, no network. The
I/O wrapper (``classify`` / ``enrich``) is tested separately once added.

Fixtures build ~300-bar trailing series with a SHRINKING intraday range so the
ATR% quantile stays low: that pins the vol axis on VIX (the OR's other leg),
letting each state be selected deterministically.
"""

import datetime as dt
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd


def _ohlc(
    n: int = 300,
    *,
    drift: float = 0.0005,
    spread_start: float = 4.0,
    spread_end: float = 0.3,
    base: float = 100.0,
):
    """Trending close with a shrinking high/low range → recent ATR% is low."""
    idx = pd.date_range("2019-01-01", periods=n, freq="B")
    close = base * (1.0 + drift) ** np.arange(n)
    spread = np.linspace(spread_start, spread_end, n)
    high = close + spread / 2.0
    low = close - spread / 2.0
    return (
        pd.Series(close, index=idx),
        pd.Series(high, index=idx),
        pd.Series(low, index=idx),
    )


def _vix(n: int = 300, *, last: float = 10.0, body: float = 15.0):
    idx = pd.date_range("2019-01-01", periods=n, freq="B")
    v = np.full(n, body)
    v[-1] = last
    return pd.Series(v, index=idx)


class TestClassifyStateFourStates(unittest.TestCase):
    def test_uptrend_low_vol_is_bull_quiet(self):
        from alphalens_pipeline.market.market_state import classify_state

        close, high, low = _ohlc(drift=0.0008)
        out = classify_state(close=close, high=high, low=low, vix=_vix(last=10.0))

        self.assertEqual(out["market_state"], "bull_quiet")

    def test_uptrend_high_vix_is_bull_volatile(self):
        from alphalens_pipeline.market.market_state import classify_state

        close, high, low = _ohlc(drift=0.0008)
        out = classify_state(close=close, high=high, low=low, vix=_vix(last=30.0))

        self.assertEqual(out["market_state"], "bull_volatile")

    def test_downtrend_low_vol_is_bear_quiet(self):
        from alphalens_pipeline.market.market_state import classify_state

        close, high, low = _ohlc(drift=-0.0008)
        out = classify_state(close=close, high=high, low=low, vix=_vix(last=10.0))

        self.assertEqual(out["market_state"], "bear_quiet")

    def test_downtrend_high_vix_is_bear_volatile(self):
        from alphalens_pipeline.market.market_state import classify_state

        close, high, low = _ohlc(drift=-0.0008)
        out = classify_state(close=close, high=high, low=low, vix=_vix(last=30.0))

        self.assertEqual(out["market_state"], "bear_volatile")


class TestClassifyStateVolBoundary(unittest.TestCase):
    def test_vix_just_below_25_is_low_vol(self):
        from alphalens_pipeline.market.market_state import classify_state

        close, high, low = _ohlc(drift=0.0008)
        out = classify_state(close=close, high=high, low=low, vix=_vix(last=24.99))

        self.assertEqual(out["market_state"], "bull_quiet")

    def test_vix_just_above_25_is_high_vol(self):
        from alphalens_pipeline.market.market_state import classify_state

        close, high, low = _ohlc(drift=0.0008)
        out = classify_state(close=close, high=high, low=low, vix=_vix(last=25.01))

        self.assertEqual(out["market_state"], "bull_volatile")


class TestClassifyStateNeutralFold(unittest.TestCase):
    def _flat_then_last(self, last_close: float):
        # ~flat series (|dist200| within the flat band) → trend neutral → folds
        # by sign(dist200). Shrinking spread keeps ATR% low (vol not the point).
        close, high, low = _ohlc(n=300, drift=0.0, spread_start=4.0, spread_end=0.3)
        close.iloc[-1] = last_close
        high.iloc[-1] = last_close + 0.15
        low.iloc[-1] = last_close - 0.15
        return close, high, low

    def test_neutral_folds_up_when_above_sma200(self):
        from alphalens_pipeline.market.market_state import classify_state

        close, high, low = self._flat_then_last(100.5)  # dist200 ≈ +0.5% ≤ band
        out = classify_state(close=close, high=high, low=low, vix=_vix(last=10.0))

        self.assertIn(out["market_state"], {"bull_quiet", "bull_volatile"})

    def test_neutral_folds_down_when_below_sma200(self):
        from alphalens_pipeline.market.market_state import classify_state

        close, high, low = self._flat_then_last(99.5)  # dist200 ≈ −0.5% ≤ band
        out = classify_state(close=close, high=high, low=low, vix=_vix(last=10.0))

        self.assertIn(out["market_state"], {"bear_quiet", "bear_volatile"})


class TestClassifyStateUnknown(unittest.TestCase):
    def test_insufficient_history_is_unknown(self):
        from alphalens_pipeline.market.market_state import classify_state

        close, high, low = _ohlc(n=150)  # < 252 → ATR% quantile undefined
        out = classify_state(close=close, high=high, low=low, vix=_vix(n=150, last=10.0))

        self.assertEqual(out["market_state"], "unknown")

    def test_missing_vix_is_unknown(self):
        from alphalens_pipeline.market.market_state import classify_state

        close, high, low = _ohlc(drift=0.0008)
        vix = _vix(last=float("nan"))  # no VIX at asof
        out = classify_state(close=close, high=high, low=low, vix=vix)

        self.assertEqual(out["market_state"], "unknown")

    def test_empty_bars_with_present_vix_is_unknown(self):
        from alphalens_pipeline.market.market_state import classify_state

        empty = pd.Series([], dtype=float)
        out = classify_state(close=empty, high=empty, low=empty, vix=_vix(last=10.0))

        self.assertEqual(out["market_state"], "unknown")


class TestClassifyStateTelemetry(unittest.TestCase):
    def test_returns_all_columns_with_sane_ranges(self):
        from alphalens_pipeline.market.market_state import (
            MARKET_STATE_COLUMNS,
            classify_state,
        )

        close, high, low = _ohlc(drift=0.0008)
        out = classify_state(close=close, high=high, low=low, vix=_vix(last=10.0))

        # every stamped column except the enrich-added config_version is present
        for col in MARKET_STATE_COLUMNS:
            if col == "market_state_config_version":
                continue
            self.assertIn(col, out)

        self.assertGreaterEqual(out["market_state_atr_pct_q"], 0.0)
        self.assertLessEqual(out["market_state_atr_pct_q"], 1.0)
        self.assertLess(out["market_state_atr_pct_q"], 0.70)  # low-vol fixture
        self.assertAlmostEqual(out["market_state_vix"], 10.0, places=6)
        self.assertIsInstance(out["market_state_squeeze_on"], bool)


class TestConfigVersion(unittest.TestCase):
    def test_config_version_is_unvalidated_poolability_key(self):
        from alphalens_pipeline.market.market_state import MARKET_STATE_CONFIG_VERSION

        self.assertTrue(MARKET_STATE_CONFIG_VERSION.startswith("mstate-v1"))
        self.assertTrue(MARKET_STATE_CONFIG_VERSION.endswith("UNVALIDATED"))

    def test_columns_include_label_and_config_version(self):
        from alphalens_pipeline.market.market_state import MARKET_STATE_COLUMNS

        self.assertIn("market_state", MARKET_STATE_COLUMNS)
        self.assertIn("market_state_config_version", MARKET_STATE_COLUMNS)


def _seed_store(root: Path, *, n: int = 300, drift: float = 0.0008):
    """Populate a temp grouped-daily store with SPY (+ a NOISE ticker) bars."""
    from alphalens_pipeline.data.rs_history import write_grouped_day_atomic

    dates = pd.bdate_range("2019-01-01", periods=n)
    closes = 100.0 * (1.0 + drift) ** np.arange(n)
    spreads = np.linspace(4.0, 0.3, n)
    for d, c, sp in zip(dates, closes, spreads, strict=True):
        spy = {"t": 0, "o": c, "h": c + sp / 2, "l": c - sp / 2, "c": c, "v": 1000, "vw": c}
        noise = {"t": 0, "o": 1, "h": 1, "l": 1, "c": 1, "v": 1, "vw": 1}
        write_grouped_day_atomic(root, d.date(), {"SPY": spy, "NOISE": noise})
    return dates


def _fake_fred(dates, *, last: float = 10.0, body: float = 15.0):
    v = np.full(len(dates), body)
    v[-1] = last
    series = pd.Series(v, index=pd.DatetimeIndex([pd.Timestamp(d) for d in dates]))

    class _Fred:
        def fetch_series(self, series_id):
            assert series_id == "VIXCLS"
            return series

    return _Fred()


class TestEnrichBroadcast(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name)
        cls.dates = _seed_store(cls.root, n=300)
        cls.asof = cls.dates[-1].date()

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_broadcasts_same_state_to_every_row(self):
        from alphalens_pipeline.market.market_state import (
            MARKET_STATE_COLUMNS,
            MARKET_STATE_CONFIG_VERSION,
            enrich,
        )

        frame = pd.DataFrame({"ticker": ["AAA", "BBB", "CCC"]})
        out = enrich(
            frame,
            asof=self.asof,
            grouped_root=self.root,
            fred_client=_fake_fred(self.dates, last=10.0),
        )

        for col in MARKET_STATE_COLUMNS:
            self.assertIn(col, out.columns)
        self.assertEqual(len(out), 3)
        self.assertIn("ticker", out.columns)  # original columns preserved
        self.assertEqual(out["market_state"].nunique(), 1)  # index-level: same value
        self.assertEqual(out["market_state"].iloc[0], "bull_quiet")
        self.assertTrue((out["market_state_config_version"] == MARKET_STATE_CONFIG_VERSION).all())

    def test_high_vix_flips_broadcast_to_bull_volatile(self):
        from alphalens_pipeline.market.market_state import enrich

        out = enrich(
            pd.DataFrame({"ticker": ["AAA"]}),
            asof=self.asof,
            grouped_root=self.root,
            fred_client=_fake_fred(self.dates, last=30.0),
        )

        self.assertEqual(out["market_state"].iloc[0], "bull_volatile")

    def test_failsoft_on_fred_error_stamps_unknown(self):
        from alphalens_pipeline.market.market_state import MARKET_STATE_COLUMNS, enrich

        class _BoomFred:
            def fetch_series(self, series_id):
                raise RuntimeError("FRED down")

        # A store/FRED hiccup must degrade to 'unknown', never abort the score stage.
        out = enrich(
            pd.DataFrame({"ticker": ["AAA", "BBB"]}),
            asof=self.asof,
            grouped_root=self.root,
            fred_client=_BoomFred(),
        )

        for col in MARKET_STATE_COLUMNS:
            self.assertIn(col, out.columns)
        self.assertTrue((out["market_state"] == "unknown").all())

    def test_broadcast_squeeze_column_is_nullable_boolean(self):
        from alphalens_pipeline.market.market_state import enrich

        # populated-frame dtype must match the empty-frame schema (nullable bool)
        out = enrich(
            pd.DataFrame({"ticker": ["AAA"]}),
            asof=self.asof,
            grouped_root=self.root,
            fred_client=_fake_fred(self.dates, last=10.0),
        )

        self.assertEqual(out["market_state_squeeze_on"].dtype, pd.BooleanDtype())


class TestEnrichEmptyAndUnknown(unittest.TestCase):
    def test_empty_frame_adds_columns_with_stable_dtypes(self):
        from alphalens_pipeline.market.market_state import MARKET_STATE_COLUMNS, enrich

        # empty frame short-circuits before any I/O (store/fred untouched)
        out = enrich(pd.DataFrame(), asof=dt.date(2020, 1, 1), grouped_root=None, fred_client=None)

        for col in MARKET_STATE_COLUMNS:
            self.assertIn(col, out.columns)
        self.assertEqual(len(out), 0)
        self.assertEqual(out["market_state"].dtype, object)
        self.assertEqual(out["market_state_atr_pct"].dtype, np.dtype("float64"))

    def test_unknown_broadcast_when_store_too_short(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            dates = _seed_store(root, n=60)  # < 252 → ATR% quantile undefined → unknown
            from alphalens_pipeline.market.market_state import enrich

            out = enrich(
                pd.DataFrame({"ticker": ["AAA", "BBB"]}),
                asof=dates[-1].date(),
                grouped_root=root,
                fred_client=_fake_fred(dates, last=10.0),
            )

            self.assertTrue((out["market_state"] == "unknown").all())


if __name__ == "__main__":
    unittest.main()
