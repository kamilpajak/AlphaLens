"""Tests for `alphalens analyze TICKER` subcommand."""

import datetime as dt
import unittest
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner


class TestAnalyzeCommand(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()

    def test_analyze_subcommand_registered(self):
        from alphalens_cli.main import app

        result = self.runner.invoke(app, ["--help"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("analyze", result.stdout)

    @patch("alphalens_cli.commands.analyze.TradingAgentsGraph")
    @patch("alphalens_cli.commands.analyze.build_gemini_config")
    def test_analyze_invokes_graph_with_ticker_and_today_by_default(
        self, mock_config, mock_graph_cls
    ):
        from alphalens_cli.main import app

        mock_config.return_value = {"llm_provider": "google"}
        fake_graph = MagicMock()
        fake_graph.propagate.return_value = (None, "BUY rating: strong momentum")
        mock_graph_cls.return_value = fake_graph

        result = self.runner.invoke(app, ["analyze", "TSHA"])

        self.assertEqual(result.exit_code, 0, msg=result.stdout)
        ticker_arg, date_arg = fake_graph.propagate.call_args[0]
        self.assertEqual(ticker_arg, "TSHA")
        self.assertEqual(date_arg, dt.date.today().isoformat())
        self.assertIn("BUY rating", result.stdout)

    @patch("alphalens_cli.commands.analyze.TradingAgentsGraph")
    @patch("alphalens_cli.commands.analyze.build_gemini_config")
    def test_analyze_honors_explicit_date(self, mock_config, mock_graph_cls):
        from alphalens_cli.main import app

        mock_config.return_value = {}
        fake_graph = MagicMock()
        fake_graph.propagate.return_value = (None, "HOLD")
        mock_graph_cls.return_value = fake_graph

        result = self.runner.invoke(app, ["analyze", "NVDA", "--date", "2026-01-15"])

        self.assertEqual(result.exit_code, 0, msg=result.stdout)
        _, date_arg = fake_graph.propagate.call_args[0]
        self.assertEqual(date_arg, "2026-01-15")

    @patch("alphalens_cli.commands.analyze.TradingAgentsGraph")
    @patch("alphalens_cli.commands.analyze.build_gemini_config")
    def test_analyze_passes_gemini_config_to_graph(self, mock_config, mock_graph_cls):
        from alphalens_cli.main import app

        sentinel = {
            "llm_provider": "google",
            "deep_think_llm": "gemini-3.1-pro-preview",
        }
        mock_config.return_value = sentinel
        mock_graph_cls.return_value.propagate.return_value = (None, "")

        self.runner.invoke(app, ["analyze", "TSHA"])

        mock_graph_cls.assert_called_once()
        kwargs = mock_graph_cls.call_args.kwargs
        self.assertEqual(kwargs["config"], sentinel)
        self.assertFalse(kwargs["debug"])


if __name__ == "__main__":
    unittest.main()
