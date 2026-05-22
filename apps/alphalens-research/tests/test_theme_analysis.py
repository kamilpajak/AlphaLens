import unittest

import pandas as pd


class TestSnapshotThemes(unittest.TestCase):
    def test_single_theme_per_ticker(self):
        from alphalens_research.backtest.theme_analysis import snapshot_themes

        themes_map = {"A": ["quantum"], "B": ["quantum"], "C": ["ai"]}
        snap = snapshot_themes(["A", "B", "C"], themes_map)

        self.assertAlmostEqual(snap.theme_weights["quantum"], 2 / 3)
        self.assertAlmostEqual(snap.theme_weights["ai"], 1 / 3)
        self.assertEqual(snap.dominant_theme, "quantum")
        self.assertEqual(snap.unclassified_fraction, 0.0)

    def test_multi_theme_ticker_splits_weight(self):
        from alphalens_research.backtest.theme_analysis import snapshot_themes

        themes_map = {"X": ["quantum", "ai"]}  # ticker w 2 tematach
        snap = snapshot_themes(["X"], themes_map)

        # X dostaje 1.0 wagi, splitujemy 50/50 na quantum i ai
        self.assertAlmostEqual(snap.theme_weights["quantum"], 0.5)
        self.assertAlmostEqual(snap.theme_weights["ai"], 0.5)

    def test_unclassified_ticker(self):
        from alphalens_research.backtest.theme_analysis import snapshot_themes

        themes_map = {"A": ["quantum"]}  # B nie jest w mapie
        snap = snapshot_themes(["A", "B"], themes_map)

        self.assertAlmostEqual(snap.theme_weights["quantum"], 0.5)
        self.assertAlmostEqual(snap.unclassified_fraction, 0.5)

    def test_empty_input(self):
        from alphalens_research.backtest.theme_analysis import snapshot_themes

        snap = snapshot_themes([], {})
        self.assertEqual(snap.theme_weights, {})
        self.assertIsNone(snap.dominant_theme)
        self.assertEqual(snap.hhi, 0.0)

    def test_position_weights_honor(self):
        from alphalens_research.backtest.theme_analysis import snapshot_themes

        themes_map = {"A": ["quantum"], "B": ["ai"], "C": ["biotech"]}
        # Top-heavy: pierwszy ticker dominuje.
        weights = [0.7, 0.2, 0.1]
        snap = snapshot_themes(["A", "B", "C"], themes_map, position_weights=weights)

        self.assertAlmostEqual(snap.theme_weights["quantum"], 0.7)
        self.assertAlmostEqual(snap.theme_weights["ai"], 0.2)
        self.assertAlmostEqual(snap.theme_weights["biotech"], 0.1)
        self.assertEqual(snap.dominant_theme, "quantum")

    def test_hhi_diversification(self):
        from alphalens_research.backtest.theme_analysis import snapshot_themes

        # Perfect diversification — 3 equal themes → HHI = 3 × (1/3)² = 1/3
        themes_map = {"A": ["quantum"], "B": ["ai"], "C": ["biotech"]}
        snap = snapshot_themes(["A", "B", "C"], themes_map)
        self.assertAlmostEqual(snap.hhi, 1 / 3, places=4)

        # Full concentration: all in one theme → HHI = 1.0
        themes_map = {"A": ["quantum"], "B": ["quantum"], "C": ["quantum"]}
        snap = snapshot_themes(["A", "B", "C"], themes_map)
        self.assertAlmostEqual(snap.hhi, 1.0)

    def test_position_weights_length_mismatch_raises(self):
        from alphalens_research.backtest.theme_analysis import snapshot_themes

        with self.assertRaises(ValueError):
            snapshot_themes(["A", "B"], {"A": ["q"]}, position_weights=[1.0])


class TestThemeSeries(unittest.TestCase):
    def _make_snaps(self, rows):
        """rows: list of (date_str, themes_map, top_n_tickers)."""
        from alphalens_research.backtest.theme_analysis import snapshot_themes

        return [
            snapshot_themes(tickers, themes_map, date=pd.Timestamp(d))
            for d, themes_map, tickers in rows
        ]

    def test_series_aggregates(self):
        from alphalens_research.backtest.theme_analysis import theme_series

        themes_map = {"A": ["quantum"], "B": ["ai"]}
        snaps = self._make_snaps(
            [
                ("2024-01-02", themes_map, ["A", "A", "A"]),  # all quantum
                ("2024-01-03", themes_map, ["A", "B"]),  # 50/50
                ("2024-01-04", themes_map, ["B", "B", "B"]),  # all ai
            ]
        )
        df, stats = theme_series(snaps, concentration_threshold=0.9)

        self.assertEqual(len(df), 3)
        # Mean weights: quantum (1 + 0.5 + 0) / 3 = 0.5
        self.assertAlmostEqual(stats.mean_weights["quantum"], 0.5)
        self.assertAlmostEqual(stats.mean_weights["ai"], 0.5)
        # Concentration alerts at threshold 0.9: 2 dni (all-quantum + all-ai)
        self.assertEqual(stats.concentration_alert_days, 2)

    def test_empty_snapshots(self):
        from alphalens_research.backtest.theme_analysis import theme_series

        df, stats = theme_series([])
        self.assertTrue(df.empty)
        self.assertEqual(stats.concentration_alert_days, 0)

    def test_days_dominant_count(self):
        from alphalens_research.backtest.theme_analysis import theme_series

        themes_map = {"A": ["quantum"], "B": ["ai"]}
        snaps = self._make_snaps(
            [
                ("2024-01-01", themes_map, ["A"]),  # quantum
                ("2024-01-02", themes_map, ["A", "A"]),  # quantum
                ("2024-01-03", themes_map, ["B"]),  # ai
            ]
        )
        _, stats = theme_series(snaps)
        self.assertEqual(stats.days_dominant["quantum"], 2)
        self.assertEqual(stats.days_dominant["ai"], 1)


class TestFormatThemeSummary(unittest.TestCase):
    def test_output_contains_key_sections(self):
        from alphalens_research.backtest.theme_analysis import (
            ThemeSeriesStats,
            format_theme_summary,
        )

        stats = ThemeSeriesStats(
            all_themes=("quantum", "ai"),
            mean_weights={"quantum": 0.6, "ai": 0.4},
            days_dominant={"quantum": 150, "ai": 100},
            mean_hhi=0.45,
            concentration_alert_days=30,
            concentration_threshold=0.70,
        )
        out = format_theme_summary(stats, n_total_days=250)
        self.assertIn("250 days", out)
        self.assertIn("HHI", out)
        self.assertIn("quantum", out)
        self.assertIn("ai", out)


class TestSnapshotsFromBacktest(unittest.TestCase):
    def test_from_rebalance_results(self):
        from alphalens_research.backtest.engine import RebalanceSnapshot
        from alphalens_research.backtest.theme_analysis import snapshots_from_backtest

        daily = [
            RebalanceSnapshot(
                date=pd.Timestamp("2024-01-02"),
                scored_count=10,
                top_n_tickers=["A", "B"],
                top_n_scores=[1.0, 0.9],
                top_n_forward_returns=[0.01, 0.02],
                portfolio_return=0.015,
                portfolio_return_holding=0.03,
                universe_median_return=0.005,
                ic=0.05,
            ),
        ]
        snaps = snapshots_from_backtest(daily, {"A": ["quantum"], "B": ["ai"]})
        self.assertEqual(len(snaps), 1)
        self.assertAlmostEqual(snaps[0].theme_weights["quantum"], 0.5)


if __name__ == "__main__":
    unittest.main()
