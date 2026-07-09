"""Tests for the options retro pilot helpers (options_retro_ivol_smd_v1).

Covers the four pieces the pilot memo pins:
- smd feature extraction as-of a brief date (weekend padding + exchange filter),
- ticker-episode dedup (chained rolling 5-session window, keep-first),
- restricted wild cluster bootstrap p-values,
- CR2 cluster-robust SEs + VIF diagnostics.
"""

from __future__ import annotations

import datetime as dt
import unittest

import numpy as np
import pandas as pd
from alphalens_research.diagnostics.options_retro import (
    OPTIONS_RETRO_VERSION,
    cluster_ols,
    smd_features_asof,
    ticker_episode_dedup,
    vif_table,
    wild_cluster_bootstrap_p,
)


def _smd_row(
    trade_date: str,
    *,
    exchange: str = "NYSE",
    ivx30: float | None = 45.0,
    ivx180: float | None = 40.0,
    hv20: float | None = 38.0,
    ivp30: float | None = 72.0,
) -> dict:
    return {
        "tradeDate": trade_date,
        "exchange": exchange,
        "ivx30": ivx30,
        "ivx180": ivx180,
        "hv20": hv20,
        "ivp30": ivp30,
    }


class TestSmdFeaturesAsof(unittest.TestCase):
    def test_picks_last_trading_row_at_or_before_asof(self):
        history = pd.DataFrame(
            [
                _smd_row("2026-06-10", ivx30=30.0),
                _smd_row("2026-06-11", ivx30=45.0),
                _smd_row("2026-06-12", ivx30=60.0),
            ]
        )
        feats = smd_features_asof(history, dt.date(2026, 6, 11))
        self.assertIsNotNone(feats)
        self.assertAlmostEqual(feats["ivx30"], 0.45)

    def test_weekend_padding_rows_with_nan_ivp30_are_skipped(self):
        history = pd.DataFrame(
            [
                _smd_row("2026-06-12", ivx30=45.0),
                _smd_row("2026-06-13", ivx30=99.0, ivp30=None),  # Saturday carry-forward
                _smd_row("2026-06-14", ivx30=99.0, ivp30=None),  # Sunday carry-forward
            ]
        )
        feats = smd_features_asof(history, dt.date(2026, 6, 14))
        self.assertIsNotNone(feats)
        self.assertAlmostEqual(feats["ivx30"], 0.45)

    def test_non_us_exchange_rows_are_filtered_out(self):
        history = pd.DataFrame(
            [
                _smd_row("2026-06-11", exchange="NYSE", ivx30=45.0),
                _smd_row("2026-06-12", exchange="TSX", ivx30=99.0),
            ]
        )
        feats = smd_features_asof(history, dt.date(2026, 6, 12))
        self.assertIsNotNone(feats)
        self.assertAlmostEqual(feats["ivx30"], 0.45)

    def test_percent_to_decimal_conversion_and_ivp30_scale(self):
        history = pd.DataFrame(
            [_smd_row("2026-06-12", ivx30=45.0, ivx180=40.0, hv20=38.0, ivp30=72.0)]
        )
        feats = smd_features_asof(history, dt.date(2026, 6, 12))
        self.assertAlmostEqual(feats["ivx30"], 0.45)
        self.assertAlmostEqual(feats["ivx180_minus_ivx30"], -0.05)
        self.assertAlmostEqual(feats["hv20"], 0.38)
        self.assertAlmostEqual(feats["ivp30"], 72.0)  # stays 0-100 per vendor convention

    def test_missing_any_of_four_fields_returns_none(self):
        for field in ("ivx30", "ivx180", "hv20", "ivp30"):
            row = _smd_row("2026-06-12")
            row[field] = None
            history = pd.DataFrame([row])
            self.assertIsNone(smd_features_asof(history, dt.date(2026, 6, 12)), field)

    def test_no_rows_at_or_before_asof_returns_none(self):
        history = pd.DataFrame([_smd_row("2026-06-12")])
        self.assertIsNone(smd_features_asof(history, dt.date(2026, 6, 10)))

    def test_empty_history_returns_none(self):
        self.assertIsNone(smd_features_asof(pd.DataFrame(), dt.date(2026, 6, 10)))
        self.assertIsNone(smd_features_asof(None, dt.date(2026, 6, 10)))


class TestTickerEpisodeDedup(unittest.TestCase):
    def _panel(self, rows: list[tuple[str, str]]) -> pd.DataFrame:
        return pd.DataFrame(
            [
                {"brief_date": dt.date.fromisoformat(d), "ticker": t, "x": i}
                for i, (d, t) in enumerate(rows)
            ]
        )

    def test_repeat_within_window_collapses_to_first(self):
        panel = self._panel([("2026-06-01", "AAA"), ("2026-06-03", "AAA")])
        out = ticker_episode_dedup(panel)
        self.assertEqual(len(out), 1)
        self.assertEqual(out.iloc[0]["brief_date"], dt.date(2026, 6, 1))

    def test_chain_extends_episode_beyond_window_from_first_row(self):
        # 06-01 -> 06-03 (2 sessions) -> 06-10 (5 sessions from 06-03): one chained
        # episode even though 06-10 is more than 5 sessions after 06-01.
        panel = self._panel([("2026-06-01", "AAA"), ("2026-06-03", "AAA"), ("2026-06-10", "AAA")])
        out = ticker_episode_dedup(panel)
        self.assertEqual(len(out), 1)
        self.assertEqual(out.iloc[0]["brief_date"], dt.date(2026, 6, 1))

    def test_gap_beyond_window_starts_new_episode(self):
        # 06-10 -> 06-18 is 6 XNYS sessions (11,12,15,16,17,18) > 5: new episode.
        panel = self._panel([("2026-06-10", "AAA"), ("2026-06-18", "AAA")])
        out = ticker_episode_dedup(panel)
        self.assertEqual(len(out), 2)

    def test_different_tickers_are_independent(self):
        panel = self._panel([("2026-06-01", "AAA"), ("2026-06-02", "BBB")])
        out = ticker_episode_dedup(panel)
        self.assertEqual(len(out), 2)

    def test_weekend_brief_dates_use_next_session(self):
        # Sat 06-06 and Sun 06-07 both map to the 06-08 session: same episode as 06-05.
        panel = self._panel([("2026-06-05", "AAA"), ("2026-06-06", "AAA"), ("2026-06-07", "AAA")])
        out = ticker_episode_dedup(panel)
        self.assertEqual(len(out), 1)

    def test_all_columns_preserved(self):
        panel = self._panel([("2026-06-01", "AAA")])
        out = ticker_episode_dedup(panel)
        self.assertListEqual(sorted(out.columns), sorted(panel.columns))


def _clustered_data(
    *, n_clusters: int, per_cluster: int, beta: float, seed: int
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    g = np.repeat(np.arange(n_clusters), per_cluster)
    x = rng.normal(size=g.size)
    cluster_shock = rng.normal(scale=1.0, size=n_clusters)[g]
    y = beta * x + cluster_shock + rng.normal(scale=0.5, size=g.size)
    X = np.column_stack([np.ones_like(x), x])
    return y, X, g


class TestWildClusterBootstrap(unittest.TestCase):
    def test_strong_effect_yields_small_p(self):
        y, X, g = _clustered_data(n_clusters=25, per_cluster=8, beta=1.5, seed=1)
        p = wild_cluster_bootstrap_p(y, X, g, coef_idx=1, n_boot=999, seed=2)
        self.assertLess(p, 0.01)

    def test_null_effect_yields_large_p(self):
        y, X, g = _clustered_data(n_clusters=25, per_cluster=8, beta=0.0, seed=3)
        p = wild_cluster_bootstrap_p(y, X, g, coef_idx=1, n_boot=999, seed=4)
        self.assertGreater(p, 0.05)

    def test_deterministic_given_seed(self):
        y, X, g = _clustered_data(n_clusters=20, per_cluster=6, beta=0.3, seed=5)
        p1 = wild_cluster_bootstrap_p(y, X, g, coef_idx=1, n_boot=499, seed=6)
        p2 = wild_cluster_bootstrap_p(y, X, g, coef_idx=1, n_boot=499, seed=6)
        self.assertEqual(p1, p2)


class TestClusterOls(unittest.TestCase):
    def test_beta_matches_lstsq(self):
        y, X, g = _clustered_data(n_clusters=20, per_cluster=6, beta=0.8, seed=7)
        res = cluster_ols(y, X, g)
        expected = np.linalg.lstsq(X, y, rcond=None)[0]
        np.testing.assert_allclose(res.beta, expected, rtol=1e-10)

    def test_cr2_se_exceeds_naive_ols_se_under_cluster_shocks(self):
        y, X, g = _clustered_data(n_clusters=20, per_cluster=10, beta=0.5, seed=8)
        res = cluster_ols(y, X, g)
        n, k = X.shape
        resid = y - X @ res.beta
        naive_var = float(resid @ resid) / (n - k) * np.linalg.inv(X.T @ X)[0, 0]
        self.assertGreater(res.se_cr2[0], np.sqrt(naive_var))  # intercept absorbs cluster shock

    def test_p_values_finite_and_in_unit_interval(self):
        y, X, g = _clustered_data(n_clusters=15, per_cluster=5, beta=0.5, seed=9)
        res = cluster_ols(y, X, g)
        self.assertTrue(np.all((res.p_cr2 >= 0) & (res.p_cr2 <= 1)))


class TestVifTable(unittest.TestCase):
    def test_near_duplicate_columns_flag_high_vif(self):
        rng = np.random.default_rng(10)
        x1 = rng.normal(size=300)
        df = pd.DataFrame({"a": x1, "b": 2.0 * x1 + rng.normal(scale=0.01, size=300)})
        vifs = vif_table(df, ["a", "b"])
        self.assertGreater(vifs["a"], 10.0)
        self.assertGreater(vifs["b"], 10.0)

    def test_independent_columns_have_low_vif(self):
        rng = np.random.default_rng(11)
        df = pd.DataFrame({"a": rng.normal(size=300), "b": rng.normal(size=300)})
        vifs = vif_table(df, ["a", "b"])
        self.assertLess(vifs["a"], 2.0)
        self.assertLess(vifs["b"], 2.0)


class TestVersionLabel(unittest.TestCase):
    def test_version_constant_pinned(self):
        self.assertEqual(OPTIONS_RETRO_VERSION, "options_retro_ivol_smd_v1")


if __name__ == "__main__":
    unittest.main()
