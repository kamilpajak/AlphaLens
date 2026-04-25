import unittest

import pandas as pd


class TestMomentumReporter(unittest.TestCase):
    def test_empty_result_renders_no_matches_message(self):
        from alphalens.screeners.themed.reporter import format_telegram_report

        text = format_telegram_report(
            pd.DataFrame(columns=["ticker", "momentum_score", "themes"]),
            curr_date="2026-04-17",
        )
        self.assertIn("No momentum candidates", text)
        self.assertIn("2026-04-17", text)

    def test_rows_render_with_ticker_score_and_theme(self):
        from alphalens.screeners.themed.reporter import format_telegram_report

        df = pd.DataFrame(
            [
                {"ticker": "QUBT", "momentum_score": 0.82, "themes": ["quantum"]},
                {"ticker": "RXRX", "momentum_score": 0.71, "themes": ["ai", "biotech"]},
            ]
        )
        text = format_telegram_report(df, curr_date="2026-04-17")
        self.assertIn("QUBT", text)
        self.assertIn("0.82", text)
        self.assertIn("quantum", text)
        self.assertIn("RXRX", text)
        self.assertIn("ai", text)
        self.assertIn("biotech", text)

    def test_includes_metric_breakdown_when_columns_present(self):
        from alphalens.screeners.themed.reporter import format_telegram_report

        df = pd.DataFrame(
            [
                {
                    "ticker": "QUBT",
                    "momentum_score": 0.82,
                    "themes": ["quantum"],
                    "near_high_score": 0.95,
                    "pct_20d_score": 0.90,
                    "volume_surge_score": 0.85,
                    "rel_strength_score": 0.80,
                    "rsi_score": 0.70,
                    "adx_score": 0.75,
                    "macd_score": 0.75,
                }
            ]
        )
        text = format_telegram_report(df, curr_date="2026-04-17")
        # At least the key metrics should appear
        self.assertIn("near", text.lower())
        self.assertIn("vol", text.lower())

    def test_header_contains_date(self):
        from alphalens.screeners.themed.reporter import format_telegram_report

        df = pd.DataFrame([{"ticker": "X", "momentum_score": 0.5, "themes": ["quantum"]}])
        text = format_telegram_report(df, curr_date="2026-04-17")
        self.assertIn("2026-04-17", text)
        self.assertIn("Momentum", text)

    def test_theme_breakdown_included_when_multiple_themes(self):
        from alphalens.screeners.themed.reporter import format_telegram_report

        df = pd.DataFrame(
            [
                {"ticker": "A", "momentum_score": 0.9, "themes": ["quantum"]},
                {"ticker": "B", "momentum_score": 0.8, "themes": ["ai"]},
                {"ticker": "C", "momentum_score": 0.7, "themes": ["biotech"]},
            ]
        )
        text = format_telegram_report(df, curr_date="2026-04-17")
        self.assertIn("Themes:", text)
        # Each theme should hold ~33% weight
        self.assertIn("33%", text)

    def test_concentration_warning_when_one_theme_dominates(self):
        """When >70% of picks sit in one theme, the report emits a warning."""
        from alphalens.screeners.themed.reporter import format_telegram_report

        df = pd.DataFrame(
            [
                {"ticker": "Q1", "momentum_score": 0.9, "themes": ["quantum"]},
                {"ticker": "Q2", "momentum_score": 0.85, "themes": ["quantum"]},
                {"ticker": "Q3", "momentum_score": 0.8, "themes": ["quantum"]},
                {"ticker": "Q4", "momentum_score": 0.75, "themes": ["quantum"]},
                {"ticker": "A1", "momentum_score": 0.7, "themes": ["ai"]},
            ]
        )
        text = format_telegram_report(df, curr_date="2026-04-17")
        self.assertIn("⚠️", text)
        self.assertIn("quantum", text)
        self.assertIn("single-theme bet", text)

    def test_no_warning_when_balanced(self):
        from alphalens.screeners.themed.reporter import format_telegram_report

        df = pd.DataFrame(
            [
                {"ticker": "Q", "momentum_score": 0.9, "themes": ["quantum"]},
                {"ticker": "A", "momentum_score": 0.85, "themes": ["ai"]},
                {"ticker": "B", "momentum_score": 0.80, "themes": ["biotech"]},
            ]
        )
        text = format_telegram_report(df, curr_date="2026-04-17")
        self.assertNotIn("⚠️", text)


if __name__ == "__main__":
    unittest.main()
