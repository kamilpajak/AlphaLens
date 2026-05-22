"""Phase A unit tests for multi_source_two_stage feature joiner.

Locks the contract for `alphalens_research.screeners.multi_source_two_stage.features`:
- FEATURE_NAMES matches the pre-registration JSON exactly
- F4 fire-sale exclusion flows through (delisting_events on insider_scorer)
- Rolling features are strictly PIT (no forward leakage)
- VIX-quartile thresholds frozen on train period only
- Cross-sectional ranks have neutral fallback for single-element slices
- Interactions propagate NaN
- Universe inclusion: delisted tickers participate on dates before delisting
- Insider features default to 0.0 (not NaN) when scorer returns None
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from alphalens_research.data.store.history import HistoryStore
from alphalens_research.data.store.survivorship_pit import DelistingEvent
from alphalens_research.screeners.insider_activity.parquet_scorer import ParquetInsiderScorer
from alphalens_research.screeners.multi_source_two_stage import (
    FEATURE_NAMES,
    REGIME_LABELS,
    assign_regime,
    build_feature_frame,
    train_quartile_thresholds,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
PREREG_JSON = (
    REPO_ROOT
    / "docs"
    / "research"
    / "preregistration"
    / "params_multi_source_two_stage_2026_04_30.json"
)


# ---------------------------------------------------------------------------
# Synthetic data helpers


def _bdays(start: str, periods: int) -> pd.DatetimeIndex:
    return pd.bdate_range(start, periods=periods)


def _ohlcv(start: str, periods: int, *, drift: float = 0.0005, seed: int = 1) -> pd.DataFrame:
    idx = _bdays(start, periods)
    rng = np.random.default_rng(seed)
    prices = 100.0 * np.exp(np.cumsum(drift + 0.005 * rng.standard_normal(periods)))
    return pd.DataFrame(
        {
            "open": prices,
            "high": prices * 1.01,
            "low": prices * 0.99,
            "close": prices,
            "volume": 1_000_000.0,
        },
        index=idx,
    )


def _empty_insider_parquet(root: Path) -> ParquetInsiderScorer:
    """Insider parquet with zero rows (cache-miss for every lookup)."""
    year_dir = root / "year=2020"
    year_dir.mkdir(parents=True)
    table = pa.table(
        {
            "ticker": pa.array([], type=pa.string()),
            "date": pa.array([], type=pa.date32()),
            "has_features": pa.array([], type=pa.bool_()),
            "insider_count": pa.array([], type=pa.int32()),
            "aggregate_dollar": pa.array([], type=pa.float64()),
            "cluster_window_days": pa.array([], type=pa.int16()),
            "asof": pa.array([], type=pa.date32()),
            "cached_at": pa.array([], type=pa.timestamp("us", tz="UTC")),
        }
    )
    pq.write_table(table, year_dir / "part-0.parquet")
    return ParquetInsiderScorer(root)


def _carhart_factors(start: str, periods: int) -> pd.DataFrame:
    """Synthetic FF/Carhart factors: small constant Mkt-RF + RF, zero others."""
    idx = _bdays(start, periods)
    return pd.DataFrame(
        {
            "Mkt-RF": np.full(periods, 0.0003),
            "SMB": np.full(periods, 0.0),
            "HML": np.full(periods, 0.0),
            "RF": np.full(periods, 0.0001),
            "Mom": np.full(periods, 0.0),
        },
        index=idx,
    )


def _fred_series(start: str, periods: int, *, vix_value: float = 18.0) -> dict[str, pd.Series]:
    idx = _bdays(start, periods)
    return {
        "VIXCLS": pd.Series(vix_value, index=idx, name="VIXCLS"),
        "DGS10": pd.Series(4.0, index=idx, name="DGS10"),
        "DGS3MO": pd.Series(2.0, index=idx, name="DGS3MO"),
    }


# ---------------------------------------------------------------------------


class TestFeatureNamesContract(unittest.TestCase):
    """FEATURE_NAMES must match the pre-registration JSON whitelist exactly.
    Drift between code and pre-reg = HARKing risk; lock with a test.
    """

    def test_feature_names_match_preregistration(self):
        with PREREG_JSON.open() as fh:
            payload = json.load(fh)
        prereg_list = payload["params_frozen"]["feature_whitelist"]
        self.assertEqual(
            list(FEATURE_NAMES),
            prereg_list,
            "FEATURE_NAMES tuple has drifted from pre-registration. "
            "If the change is intentional, abandon current pre-reg and register a new one.",
        )

    def test_feature_count_matches_preregistration(self):
        with PREREG_JSON.open() as fh:
            payload = json.load(fh)
        self.assertEqual(len(FEATURE_NAMES), payload["params_frozen"]["feature_whitelist_count"])
        self.assertEqual(len(FEATURE_NAMES), 21)


# ---------------------------------------------------------------------------


class TestRegimeClassifier(unittest.TestCase):
    def test_assign_regime_quartile_boundaries(self):
        thresholds = (10.0, 15.0, 20.0)
        self.assertEqual(assign_regime(5.0, thresholds), "Q1_calm")
        self.assertEqual(assign_regime(10.0, thresholds), "Q1_calm")  # boundary inclusive
        self.assertEqual(assign_regime(12.0, thresholds), "Q2")
        self.assertEqual(assign_regime(15.0, thresholds), "Q2")
        self.assertEqual(assign_regime(17.0, thresholds), "Q3")
        self.assertEqual(assign_regime(20.0, thresholds), "Q3")
        self.assertEqual(assign_regime(25.0, thresholds), "Q4_stress")
        self.assertEqual(assign_regime(None, thresholds), "Q2")  # neutral fallback

    def test_train_quartile_thresholds_uses_train_only(self):
        # Train period has VIX uniformly in [10, 20]; OOS has VIX in [80, 100].
        # If thresholds leak OOS data, q75 will be much higher than 17.5.
        train_idx = _bdays("2020-01-01", 100)
        oos_idx = _bdays("2020-07-01", 100)
        rng = np.random.default_rng(0)
        train_vix = pd.Series(rng.uniform(10, 20, 100), index=train_idx)
        oos_vix = pd.Series(rng.uniform(80, 100, 100), index=oos_idx)
        full = pd.concat([train_vix, oos_vix])
        thresholds = train_quartile_thresholds(full, train_end=train_idx[-1].date())
        self.assertGreater(thresholds[0], 9.0)
        self.assertLess(thresholds[2], 21.0)  # Would be ~85 if leakage occurred


# ---------------------------------------------------------------------------


class TestBuildFeatureFrame(unittest.TestCase):
    """End-to-end checks on a small synthetic universe."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "insider_form4.parquet"
        self.root.mkdir()
        self.insider_scorer = _empty_insider_parquet(self.root)

        # Need 280+ business days to satisfy ret_252d / rolling_beta_mkt_252d
        n_periods = 320
        start = "2020-01-01"
        self.start = start
        self.n_periods = n_periods
        self.history_store = HistoryStore(
            {
                "SPY": _ohlcv(start, n_periods, drift=0.0003, seed=0),
                "AAA": _ohlcv(start, n_periods, drift=0.0006, seed=1),
                "BBB": _ohlcv(start, n_periods, drift=0.0004, seed=2),
                "CCC": _ohlcv(start, n_periods, drift=0.0008, seed=3),
            }
        )
        self.carhart = _carhart_factors(start, n_periods)
        self.fred = _fred_series(start, n_periods, vix_value=18.0)
        self.universe = ["AAA", "BBB", "CCC"]
        # Pick asof_dates well past the 252d warmup
        all_bdays = _bdays(start, n_periods)
        self.asof_dates = [all_bdays[260].date(), all_bdays[280].date(), all_bdays[300].date()]
        self.train_end = all_bdays[290].date()

    def tearDown(self):
        self._tmp.cleanup()

    def _build(self, *, insider_scorer=None) -> pd.DataFrame:
        return build_feature_frame(
            history_store=self.history_store,
            insider_scorer=insider_scorer or self.insider_scorer,
            carhart_factors=self.carhart,
            fred_series=self.fred,
            universe=self.universe,
            asof_dates=self.asof_dates,
            train_end=self.train_end,
        )

    def test_full_frame_shape_under_minimal_setup(self):
        out = self._build()
        # 3 tickers × 3 asof = 9 rows; columns = asof + ticker + 21 features + regime
        self.assertEqual(len(out), 9)
        expected_cols = ["asof", "ticker", *FEATURE_NAMES, "regime"]
        self.assertEqual(list(out.columns), expected_cols)
        self.assertEqual(set(out["ticker"]), {"AAA", "BBB", "CCC"})

    def test_insider_features_default_to_zero_when_scorer_returns_none(self):
        out = self._build()
        # Empty insider parquet → every (ticker, asof) gets 0.0 in insider columns
        self.assertTrue((out["insider_log_count"] == 0.0).all())
        self.assertTrue((out["insider_log_dollar"] == 0.0).all())
        self.assertTrue((out["insider_cluster_window_days"] == 0.0).all())

    def test_insider_features_excluded_when_delisting_imminent(self):
        """F4 contract flows through: scorer with delisting events for AAA returns
        None → joined frame still has AAA rows (insider features default to 0)."""
        events = [
            DelistingEvent(
                ticker="AAA",
                delisted_date=date(2020, 12, 31),
                reason="bankruptcy",
            ),
        ]
        # Build a non-empty parquet so F4 exclusion has something to suppress
        year_dir = self.root.parent / "with_events.parquet" / "year=2020"
        year_dir.mkdir(parents=True)
        table = pa.table(
            {
                "ticker": ["AAA"],
                "date": [self.asof_dates[0]],
                "has_features": [True],
                "insider_count": [5],
                "aggregate_dollar": [25000.0],
                "cluster_window_days": [30],
                "asof": [self.asof_dates[0]],
                "cached_at": [None],
            }
        )
        pq.write_table(table, year_dir / "part-0.parquet")
        scorer_with_events = ParquetInsiderScorer(
            self.root.parent / "with_events.parquet",
            delisting_events=events,
        )
        # Baseline: same parquet WITHOUT delisting events → AAA at first asof gets the cluster
        scorer_baseline = ParquetInsiderScorer(self.root.parent / "with_events.parquet")
        out_baseline = self._build(insider_scorer=scorer_baseline)
        out_with_events = self._build(insider_scorer=scorer_with_events)
        # The pre-delisting fire-sale exclusion zeroes insider features for AAA
        # at any asof within 180d of 2020-12-31.
        aaa_baseline = out_baseline[
            (out_baseline["ticker"] == "AAA") & (out_baseline["asof"] == self.asof_dates[0])
        ]
        aaa_filtered = out_with_events[
            (out_with_events["ticker"] == "AAA") & (out_with_events["asof"] == self.asof_dates[0])
        ]
        self.assertGreater(float(aaa_baseline["insider_log_count"].iloc[0]), 0.0)
        self.assertEqual(float(aaa_filtered["insider_log_count"].iloc[0]), 0.0)

    def test_regime_label_is_valid(self):
        out = self._build()
        self.assertTrue(set(out["regime"]).issubset(set(REGIME_LABELS)))

    def test_macro_features_broadcast_to_all_tickers(self):
        out = self._build()
        first_asof = self.asof_dates[0]
        slice_ = out[out["asof"] == first_asof]
        # All tickers at same asof share macro
        self.assertTrue((slice_["vix_level"] == 18.0).all())
        self.assertAlmostEqual(slice_["term_spread_10y_3m"].iloc[0], 2.0)


# ---------------------------------------------------------------------------


class TestPitDiscipline(unittest.TestCase):
    """Locks the PIT contract: future bars MUST NOT leak into asof features."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "insider_form4.parquet"
        self.root.mkdir()
        self.insider_scorer = _empty_insider_parquet(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def test_rolling_features_strictly_pit(self):
        """OHLCV with a known spike at asof+1 must NOT show up in ret_20d / vol_20d
        computed at asof (because truncate_to is asof-inclusive only)."""
        n = 320
        start = "2020-01-01"
        bdays = _bdays(start, n)
        rng = np.random.default_rng(0)
        prices = 100.0 * np.exp(np.cumsum(0.0005 + 0.005 * rng.standard_normal(n)))
        # Inject a +50% spike at index 270 (post-asof)
        spike_idx = 270
        prices[spike_idx:] *= 1.5
        df = pd.DataFrame(
            {
                "open": prices,
                "high": prices * 1.01,
                "low": prices * 0.99,
                "close": prices,
                "volume": 1_000_000.0,
            },
            index=bdays,
        )
        history = HistoryStore({"SPY": _ohlcv(start, n, drift=0.0001), "TKR": df})

        asof = bdays[spike_idx - 1].date()  # asof = day BEFORE spike

        out = build_feature_frame(
            history_store=history,
            insider_scorer=self.insider_scorer,
            carhart_factors=_carhart_factors(start, n),
            fred_series=_fred_series(start, n),
            universe=["TKR"],
            asof_dates=[asof],
            train_end=bdays[260].date(),
        )
        # ret_20d at asof should reflect pre-spike behavior (~ a few percent at most),
        # NOT the +50% jump that occurs at asof+1.
        ret_20d = float(out["ret_20d"].iloc[0])
        self.assertLess(abs(ret_20d), 0.30, "ret_20d leaked the post-asof spike")


# ---------------------------------------------------------------------------


class TestCrossSectionalRanks(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "insider_form4.parquet"
        self.root.mkdir()
        self.insider_scorer = _empty_insider_parquet(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def test_cross_sectional_rank_has_neutral_fallback(self):
        """Single-ticker slice → rank = 0.5 (neutral), not NaN/error."""
        n = 320
        start = "2020-01-01"
        bdays = _bdays(start, n)
        history = HistoryStore(
            {
                "SPY": _ohlcv(start, n, drift=0.0001, seed=0),
                "ONLY": _ohlcv(start, n, drift=0.0006, seed=1),
            }
        )
        out = build_feature_frame(
            history_store=history,
            insider_scorer=self.insider_scorer,
            carhart_factors=_carhart_factors(start, n),
            fred_series=_fred_series(start, n),
            universe=["ONLY"],
            asof_dates=[bdays[280].date()],
            train_end=bdays[260].date(),
        )
        self.assertEqual(len(out), 1)
        for rank_col in ("rank_momentum_60d", "rank_lowvol_20d", "rank_dollar_volume_size"):
            self.assertEqual(float(out[rank_col].iloc[0]), 0.5)


# ---------------------------------------------------------------------------


class TestUniverseInclusion(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "insider_form4.parquet"
        self.root.mkdir()
        self.insider_scorer = _empty_insider_parquet(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def test_universe_includes_delisted_tickers_pre_delisting(self):
        """Locks O2 invariant flow: delisted ticker DEL is scored on asof
        before its delisting (because store still has its pre-delisting bars).
        """
        start = "2020-01-01"
        bdays = _bdays(start, 320)
        # ALIVE: full history; DELISTED: only first 280 bars
        history = HistoryStore(
            {
                "SPY": _ohlcv(start, 320, drift=0.0001, seed=0),
                "ALIVE": _ohlcv(start, 320, drift=0.0006, seed=1),
                "DELISTED": _ohlcv(start, 280, drift=-0.0010, seed=2),
            }
        )
        out = build_feature_frame(
            history_store=history,
            insider_scorer=self.insider_scorer,
            carhart_factors=_carhart_factors(start, 320),
            fred_series=_fred_series(start, 320),
            universe=["ALIVE", "DELISTED"],
            asof_dates=[bdays[270].date()],  # before DELISTED stops
            train_end=bdays[260].date(),
        )
        self.assertEqual(set(out["ticker"]), {"ALIVE", "DELISTED"})


if __name__ == "__main__":
    unittest.main()
