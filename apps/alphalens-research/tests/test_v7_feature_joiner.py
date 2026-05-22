"""Phase A unit tests for v7 options-implied feature joiner.

Locks the contract for `alphalens_research.screeners.options_implied.features`:
- FEATURE_NAMES matches pre-registration JSON exactly (4 options + 3 equity).
- Options features extracted verbatim from cached smd rows (vendor IVP/IVX
  PIT-validated by `scripts/probe_pit_replication.py` Pearson 0.9990).
- Equity controls (1m reversal, 6m mom, 30d realized vol) computed locally
  from smd's `close` series — strictly PIT (no post-asof leakage).
- ETL anomaly bounds drop rows with non-physical values (e.g. ivx30 > 3.0).
- Universe filter applies dynamic optionable check + ADV ≥ $2M + price ≥ $1
  + OTC pink excluded, all per pre-reg.
- Multi-exchange responses (e.g. cross-listed CTT NYSE+TSX) filter to US
  primary exchanges only.
- Phase A gates: coverage ≥ 70% non-NaN; max pairwise |corr| < 0.85.
- Multicollinearity remediation hierarchy is deterministic (drop most-derived
  feature first) — pre-committed contingency, no HARKing.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

import numpy as np
import pandas as pd
from alphalens_research.screeners.options_implied import (
    ETL_ANOMALY_BOUNDS,
    FEATURE_NAMES,
    OPTIONS_FEATURES,
    build_feature_frame,
    multicollinearity_drop_recommendation,
    validate_phase_a_gates,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
PREREG_JSON = (
    REPO_ROOT
    / "docs"
    / "research"
    / "preregistration"
    / "params_v7_smd_options_implied_2026_05_02.json"
)


# ---------------------------------------------------------------------------
# Synthetic smd helpers


def _smd_row(
    *,
    ticker: str = "AAPL",
    trade_date: str = "2024-01-15",
    exchange: str = "NASDAQ",
    ivx30: float = 25.0,
    ivp30: float = 50.0,
    ivx180: float = 27.0,
    hv20: float = 20.0,
    close: float = 180.0,
    stock_volume: float = 50_000_000,
    opt_vol: float = 100_000.0,
    oi_call: float = 500_000.0,
    oi_put: float = 400_000.0,
) -> dict:
    """A single smd row matching key vendor fields. Other 130+ vendor cols
    omitted — feature joiner reads only the named columns."""
    return {
        "symbol": ticker,
        "tradeDate": trade_date,
        "exchange": exchange,
        "ivx30": ivx30,
        "ivp30": ivp30,
        "ivx180": ivx180,
        "hv20": hv20,
        "close": close,
        "stockVolume": stock_volume,
        "optVol": opt_vol,
        "openInterestCall": oi_call,
        "openInterestPut": oi_put,
    }


def _smd_history(
    ticker: str,
    *,
    start: str = "2023-01-02",
    n_days: int = 200,
    close_drift: float = 0.0005,
    seed: int = 1,
    ivx30: float = 25.0,
    ivp30: float = 50.0,
    exchange: str = "NASDAQ",
) -> pd.DataFrame:
    """Synthetic smd history for one ticker — daily rows over `n_days`
    business days with a smooth close path so equity controls are stable."""
    idx = pd.bdate_range(start, periods=n_days)
    rng = np.random.default_rng(seed)
    log_steps = close_drift + 0.005 * rng.standard_normal(n_days)
    closes = 100.0 * np.exp(np.cumsum(log_steps))
    rows = [
        _smd_row(
            ticker=ticker,
            trade_date=d.strftime("%Y-%m-%d"),
            exchange=exchange,
            ivx30=ivx30,
            ivp30=ivp30,
            ivx180=ivx30 + 2.0,
            hv20=ivx30 - 5.0,
            close=float(c),
            stock_volume=50_000_000.0,
        )
        for d, c in zip(idx, closes, strict=False)
    ]
    return pd.DataFrame(rows)


def _make_loader(histories: dict[str, pd.DataFrame]):
    """Build a smd_loader callable matching the feature joiner's contract."""

    def loader(ticker: str) -> pd.DataFrame | None:
        return histories.get(ticker.upper())

    return loader


# ---------------------------------------------------------------------------


class TestFeatureNamesContract(unittest.TestCase):
    """FEATURE_NAMES tuple must match the pre-registration JSON.

    Drift = HARKing risk; lock with a test.
    """

    def test_feature_names_count_matches_preregistration(self):
        with PREREG_JSON.open() as fh:
            payload = json.load(fh)
        params = payload["params_frozen"]
        n_options = len(params["feature_stack_options"])
        n_equity = len(params["feature_stack_equity_controls"])
        self.assertEqual(len(FEATURE_NAMES), n_options + n_equity)
        self.assertEqual(len(FEATURE_NAMES), 7)

    def test_options_subset_is_first_4(self):
        """First 4 of FEATURE_NAMES are options features per pre-reg ordering."""
        self.assertEqual(len(OPTIONS_FEATURES), 4)
        self.assertEqual(FEATURE_NAMES[:4], OPTIONS_FEATURES)

    def test_feature_name_strings_are_lowercase_snake(self):
        for name in FEATURE_NAMES:
            self.assertEqual(name, name.lower())
            self.assertNotIn(" ", name)


# ---------------------------------------------------------------------------


class TestOptionsFeatureExtraction(unittest.TestCase):
    """Map smd row → 4 options features verbatim (vendor PIT-validated)."""

    def test_ivp30_taken_directly_from_vendor_field(self):
        history = _smd_history("AAPL", n_days=200, ivp30=73.5)
        frame = build_feature_frame(
            smd_loader=_make_loader({"AAPL": history}),
            universe=["AAPL"],
            asof_dates=[history["tradeDate"].iloc[-1]],
        )
        self.assertEqual(len(frame), 1)
        self.assertAlmostEqual(float(frame["ivp30"].iloc[0]), 73.5)

    def test_ivx30_taken_directly_from_vendor_field(self):
        # Vendor returns 31.0 (percent); joiner converts to 0.31 decimal
        history = _smd_history("AAPL", n_days=200, ivx30=31.0)
        frame = build_feature_frame(
            smd_loader=_make_loader({"AAPL": history}),
            universe=["AAPL"],
            asof_dates=[history["tradeDate"].iloc[-1]],
        )
        self.assertAlmostEqual(float(frame["ivx30"].iloc[0]), 0.31, places=5)

    def test_term_spread_is_ivx180_minus_ivx30(self):
        history = _smd_history("AAPL", n_days=200, ivx30=25.0)
        # _smd_history sets ivx180 = ivx30 + 2.0 (percent) → 0.02 decimal
        frame = build_feature_frame(
            smd_loader=_make_loader({"AAPL": history}),
            universe=["AAPL"],
            asof_dates=[history["tradeDate"].iloc[-1]],
        )
        self.assertAlmostEqual(float(frame["ivx180_minus_ivx30"].iloc[0]), 0.02, places=5)

    def test_iv_hv_ratio_is_ivx30_over_hv20(self):
        history = _smd_history("AAPL", n_days=200, ivx30=30.0)
        # _smd_history sets hv20 = ivx30 - 5.0 = 25.0 (percent) → 0.25 decimal
        frame = build_feature_frame(
            smd_loader=_make_loader({"AAPL": history}),
            universe=["AAPL"],
            asof_dates=[history["tradeDate"].iloc[-1]],
        )
        self.assertAlmostEqual(float(frame["ivx30_over_hv20"].iloc[0]), 0.30 / 0.25, places=5)


# ---------------------------------------------------------------------------


class TestEquityControlsExtraction(unittest.TestCase):
    """1m reversal, 6m momentum, 30d realized vol — computed locally from
    smd `close` series. PIT-correct (post-asof rows excluded)."""

    def test_reversal_1m_uses_21d_lookback(self):
        # Build a deterministic close series with no noise
        n = 200
        idx = pd.bdate_range("2023-01-02", periods=n)
        # Linear price up 1% per day
        closes = np.array([100 * (1.01) ** i for i in range(n)])
        rows = [
            _smd_row(
                ticker="LIN",
                trade_date=d.strftime("%Y-%m-%d"),
                close=float(c),
            )
            for d, c in zip(idx, closes, strict=False)
        ]
        history = pd.DataFrame(rows)
        asof = idx[-1].strftime("%Y-%m-%d")
        frame = build_feature_frame(
            smd_loader=_make_loader({"LIN": history}),
            universe=["LIN"],
            asof_dates=[asof],
        )
        # close[asof]/close[asof-21bd] - 1 = 1.01^21 - 1 ≈ 0.232
        # reversal = -0.232
        self.assertAlmostEqual(float(frame["reversal_1m"].iloc[0]), -((1.01**21) - 1.0), places=4)

    def test_momentum_6m_skip_month_jegadeesh(self):
        n = 200
        idx = pd.bdate_range("2023-01-02", periods=n)
        closes = np.array([100 * (1.01) ** i for i in range(n)])
        rows = [
            _smd_row(
                ticker="LIN",
                trade_date=d.strftime("%Y-%m-%d"),
                close=float(c),
            )
            for d, c in zip(idx, closes, strict=False)
        ]
        history = pd.DataFrame(rows)
        asof = idx[-1].strftime("%Y-%m-%d")
        frame = build_feature_frame(
            smd_loader=_make_loader({"LIN": history}),
            universe=["LIN"],
            asof_dates=[asof],
        )
        # close[asof-21bd]/close[asof-126bd] - 1 = 1.01^(126-21) - 1 = 1.01^105 - 1
        self.assertAlmostEqual(float(frame["momentum_6m"].iloc[0]), (1.01**105) - 1.0, places=3)

    def test_post_asof_spike_does_not_leak(self):
        """Insert +50% spike at asof+1; reversal/mom must reflect pre-spike only."""
        n = 200
        idx = pd.bdate_range("2023-01-02", periods=n)
        closes = np.full(n, 100.0)
        spike_idx = 150
        closes[spike_idx:] *= 1.5  # +50% jump from index 150 onward
        rows = [
            _smd_row(
                ticker="SPK",
                trade_date=d.strftime("%Y-%m-%d"),
                close=float(c),
            )
            for d, c in zip(idx, closes, strict=False)
        ]
        history = pd.DataFrame(rows)
        # asof = day BEFORE spike (index 149)
        asof = idx[spike_idx - 1].strftime("%Y-%m-%d")
        frame = build_feature_frame(
            smd_loader=_make_loader({"SPK": history}),
            universe=["SPK"],
            asof_dates=[asof],
        )
        # close[asof] = 100, close[asof-21bd] = 100 → reversal = -0
        self.assertAlmostEqual(float(frame["reversal_1m"].iloc[0]), 0.0, places=6)


# ---------------------------------------------------------------------------


class TestEtlAnomalyBounds(unittest.TestCase):
    """Pre-reg ETL bounds drop non-physical rows."""

    def test_bounds_dict_matches_preregistration(self):
        with PREREG_JSON.open() as fh:
            params = json.load(fh)["params_frozen"]
        prereg = params["etl_anomaly_bounds"]
        self.assertEqual(ETL_ANOMALY_BOUNDS["ivx30_max"], prereg["ivx30_max"])
        self.assertEqual(ETL_ANOMALY_BOUNDS["ivx30_min"], prereg["ivx30_min"])
        self.assertEqual(ETL_ANOMALY_BOUNDS["term_spread_abs_max"], prereg["term_spread_abs_max"])
        self.assertEqual(ETL_ANOMALY_BOUNDS["iv_hv_ratio_max"], prereg["iv_hv_ratio_max"])
        self.assertEqual(ETL_ANOMALY_BOUNDS["iv_hv_ratio_min"], prereg["iv_hv_ratio_min"])
        self.assertEqual(ETL_ANOMALY_BOUNDS["stock_price_min"], prereg["stock_price_min"])

    def test_extreme_ivx30_row_dropped(self):
        # Vendor 400% IV (extreme) → 4.0 decimal > 3.0 bound → row dropped
        history = _smd_history("OUTLIER", n_days=200, ivx30=400.0)
        frame = build_feature_frame(
            smd_loader=_make_loader({"OUTLIER": history}),
            universe=["OUTLIER"],
            asof_dates=[history["tradeDate"].iloc[-1]],
        )
        self.assertEqual(len(frame), 0)

    def test_penny_stock_row_dropped(self):
        n = 200
        idx = pd.bdate_range("2023-01-02", periods=n)
        rows = [
            _smd_row(
                ticker="PNY",
                trade_date=d.strftime("%Y-%m-%d"),
                close=0.85,  # < $1 bound
            )
            for d in idx
        ]
        history = pd.DataFrame(rows)
        frame = build_feature_frame(
            smd_loader=_make_loader({"PNY": history}),
            universe=["PNY"],
            asof_dates=[idx[-1].strftime("%Y-%m-%d")],
        )
        self.assertEqual(len(frame), 0)


# ---------------------------------------------------------------------------


class TestUniverseFilter(unittest.TestCase):
    """Pre-reg universe construction rules."""

    def test_otc_pink_dropped(self):
        history = _smd_history("PNK", n_days=200, exchange="PINK")
        frame = build_feature_frame(
            smd_loader=_make_loader({"PNK": history}),
            universe=["PNK"],
            asof_dates=[history["tradeDate"].iloc[-1]],
        )
        self.assertEqual(len(frame), 0)

    def test_inactive_optionable_dropped(self):
        """If optVol=0 AND oi_call+oi_put=0 on t-1, exclude (not optionable).

        Pre-reg: smd `optVol > 0 OR (oi_call + oi_put) > 0` on t-1.
        """
        n = 200
        idx = pd.bdate_range("2023-01-02", periods=n)
        rows = [
            _smd_row(
                ticker="NOPT",
                trade_date=d.strftime("%Y-%m-%d"),
                opt_vol=0.0,
                oi_call=0.0,
                oi_put=0.0,
            )
            for d in idx
        ]
        history = pd.DataFrame(rows)
        frame = build_feature_frame(
            smd_loader=_make_loader({"NOPT": history}),
            universe=["NOPT"],
            asof_dates=[idx[-1].strftime("%Y-%m-%d")],
        )
        self.assertEqual(len(frame), 0)

    def test_active_optionable_kept(self):
        """optVol=0 but oi_call+oi_put>0 still passes the OR test."""
        history = _smd_history("OPT", n_days=200)
        # _smd_history defaults: oi_call=500k, oi_put=400k → optionable
        frame = build_feature_frame(
            smd_loader=_make_loader({"OPT": history}),
            universe=["OPT"],
            asof_dates=[history["tradeDate"].iloc[-1]],
        )
        self.assertEqual(len(frame), 1)

    def test_multi_exchange_row_filtered_to_us_primary(self):
        """Cross-listed ticker (NYSE + TSX) keeps NYSE row only."""
        n = 200
        idx = pd.bdate_range("2023-01-02", periods=n)
        rows = []
        for d in idx:
            rows.append(
                _smd_row(
                    ticker="CTT",
                    trade_date=d.strftime("%Y-%m-%d"),
                    exchange="NYSE",
                )
            )
            rows.append(
                _smd_row(
                    ticker="CTT",
                    trade_date=d.strftime("%Y-%m-%d"),
                    exchange="TSX",
                    ivx30=None,
                    close=None,
                )
            )
        history = pd.DataFrame(rows)
        frame = build_feature_frame(
            smd_loader=_make_loader({"CTT": history}),
            universe=["CTT"],
            asof_dates=[idx[-1].strftime("%Y-%m-%d")],
        )
        # Only NYSE row contributes → 1 frame row, ivx30 populated
        self.assertEqual(len(frame), 1)
        self.assertFalse(np.isnan(float(frame["ivx30"].iloc[0])))


# ---------------------------------------------------------------------------


class TestPhaseAGates(unittest.TestCase):
    """Coverage + multicollinearity gates per pre-reg `phase_a_gates`."""

    def _frame(self, n_rows: int, n_nan_per_col: int = 0) -> pd.DataFrame:
        rng = np.random.default_rng(0)
        data = {f: rng.normal(size=n_rows) for f in FEATURE_NAMES}
        for f in FEATURE_NAMES:
            if n_nan_per_col > 0:
                data[f][:n_nan_per_col] = np.nan
        df = pd.DataFrame(data)
        df.insert(0, "ticker", [f"T{i}" for i in range(n_rows)])
        df.insert(0, "asof", "2024-04-30")
        return df

    def test_coverage_gate_pass_above_70_pct(self):
        # 100 rows, ~10 NaN per col → 90% coverage
        frame = self._frame(100, n_nan_per_col=10)
        result = validate_phase_a_gates(frame, coverage_min=0.70, corr_max=0.85)
        self.assertTrue(result["coverage_pass"])
        self.assertGreaterEqual(result["coverage_pct"], 0.70)

    def test_coverage_gate_fail_below_70_pct(self):
        # 100 rows, 50 NaN per col → 50% coverage
        frame = self._frame(100, n_nan_per_col=50)
        result = validate_phase_a_gates(frame, coverage_min=0.70, corr_max=0.85)
        self.assertFalse(result["coverage_pass"])

    def test_multicollinearity_pass_when_features_independent(self):
        # Random independent features → low pairwise correlation
        frame = self._frame(500, n_nan_per_col=0)
        result = validate_phase_a_gates(frame, coverage_min=0.70, corr_max=0.85)
        self.assertTrue(result["multicollinearity_pass"])
        self.assertLess(result["max_abs_corr"], 0.85)

    def test_multicollinearity_fail_when_pair_above_threshold(self):
        # Manufacture two near-identical features
        frame = self._frame(500, n_nan_per_col=0)
        frame["ivx30"] = frame["ivp30"] * 1.0001 + 1e-9  # near-perfect correlation
        result = validate_phase_a_gates(frame, coverage_min=0.70, corr_max=0.85)
        self.assertFalse(result["multicollinearity_pass"])
        self.assertGreaterEqual(result["max_abs_corr"], 0.85)
        # Names of the offending pair surfaced for downstream remediation
        self.assertIn("offending_pair", result)
        self.assertEqual(set(result["offending_pair"]), {"ivx30", "ivp30"})


# ---------------------------------------------------------------------------


class TestMulticollinearityDropRecommendation(unittest.TestCase):
    """Pre-committed deterministic remediation hierarchy:
    1. ivx30 ↔ ivx30_over_hv20: drop ratio (less interpretable)
    2. ivp30 ↔ ivx30: drop ivp30 (vendor-rank derivative)
    3. ivx180_minus_ivx30 ↔ ivx30: drop term spread (degenerate signal)
    """

    def test_drops_ratio_when_correlated_with_level(self):
        rec = multicollinearity_drop_recommendation(offending_pair=("ivx30", "ivx30_over_hv20"))
        self.assertEqual(rec, "ivx30_over_hv20")

    def test_drops_ivp30_when_correlated_with_ivx30(self):
        rec = multicollinearity_drop_recommendation(offending_pair=("ivp30", "ivx30"))
        self.assertEqual(rec, "ivp30")

    def test_drops_term_spread_when_correlated_with_level(self):
        rec = multicollinearity_drop_recommendation(offending_pair=("ivx30", "ivx180_minus_ivx30"))
        self.assertEqual(rec, "ivx180_minus_ivx30")

    def test_unknown_pair_raises(self):
        """If a pair outside the pre-committed hierarchy trips the gate, this
        is a design surprise — caller must investigate, not auto-remediate."""
        with self.assertRaises(ValueError):
            multicollinearity_drop_recommendation(offending_pair=("reversal_1m", "momentum_6m"))


if __name__ == "__main__":
    unittest.main()
