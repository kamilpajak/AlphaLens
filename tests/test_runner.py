import unittest
from datetime import UTC, datetime
from unittest.mock import MagicMock


def _cand(ticker="AAPL", source="momentum", priority=10, payload=None, discriminator="D"):
    from alphalens.core.candidates import Candidate

    return Candidate.from_screener(
        ticker=ticker,
        source=source,
        priority=priority,
        payload=payload or {},
        discriminator=discriminator,
    )


class TestTradingAgentsRunner(unittest.TestCase):
    def test_runner_builds_graph_with_gemini_config(self):
        from alphalens.core.runner import TradingAgentsRunner

        config = {"llm_provider": "google", "deep_think_llm": "gemini-3-pro"}
        graph = MagicMock()
        graph.propagate.return_value = ({}, "HOLD")
        graph_factory = MagicMock(return_value=graph)

        runner = TradingAgentsRunner(
            config_builder=lambda: config,
            graph_factory=graph_factory,
        )
        runner.run(_cand(ticker="AAPL"), candidate_id=1)

        graph_factory.assert_called_once_with(config)

    def test_runner_returns_analysis_result_with_duration(self):
        from alphalens.core.runner import TradingAgentsRunner

        graph = MagicMock()
        graph.propagate.return_value = ({"foo": "bar"}, "BUY")
        runner = TradingAgentsRunner(
            config_builder=lambda: {"deep_think_llm": "gemini-3-pro-preview"},
            graph_factory=lambda _cfg: graph,
        )

        result = runner.run(_cand(ticker="AAPL"), candidate_id=7)
        self.assertEqual(result.candidate_id, 7)
        self.assertEqual(result.ticker, "AAPL")
        self.assertEqual(result.source, "momentum")
        self.assertEqual(result.rating, "BUY")
        self.assertGreaterEqual(result.duration_sec, 0.0)
        self.assertEqual(result.model_used, "gemini-3-pro-preview")
        self.assertIsNone(result.cost_usd)
        self.assertIsNotNone(result.completed_at.tzinfo)
        self.assertEqual(result.final_state, {"foo": "bar"})

    def test_runner_passes_ticker_and_today_to_propagate(self):
        from alphalens.core.runner import TradingAgentsRunner

        graph = MagicMock()
        graph.propagate.return_value = ({}, "HOLD")
        runner = TradingAgentsRunner(
            config_builder=lambda: {"deep_think_llm": "x"},
            graph_factory=lambda _cfg: graph,
        )
        runner.run(_cand(ticker="NVDA"), candidate_id=1)

        graph.propagate.assert_called_once()
        args, _ = graph.propagate.call_args
        self.assertEqual(args[0], "NVDA")
        # second arg is today's date in ISO format
        today = datetime.now(UTC).date().isoformat()
        self.assertEqual(args[1], today)

    def test_runner_propagates_exception(self):
        from alphalens.core.runner import TradingAgentsRunner

        graph = MagicMock()
        graph.propagate.side_effect = RuntimeError("429")
        runner = TradingAgentsRunner(
            config_builder=lambda: {"deep_think_llm": "x"},
            graph_factory=lambda _cfg: graph,
        )

        with self.assertRaises(RuntimeError):
            runner.run(_cand(ticker="AAPL"), candidate_id=1)


class TestTradingAgentsRunnerPITReplay(unittest.TestCase):
    """curr_date override + selected_analysts for point-in-time historical replay."""

    def test_runner_forwards_curr_date_to_propagate(self):
        from datetime import date

        from alphalens.core.runner import TradingAgentsRunner

        graph = MagicMock()
        graph.propagate.return_value = ({}, "BUY")
        runner = TradingAgentsRunner(
            config_builder=lambda: {"deep_think_llm": "x"},
            graph_factory=lambda _cfg: graph,
        )

        runner.run(_cand(ticker="NVDA"), candidate_id=1, curr_date=date(2023, 6, 15))

        args, _ = graph.propagate.call_args
        self.assertEqual(args[0], "NVDA")
        self.assertEqual(args[1], "2023-06-15")

    def test_runner_forwards_selected_analysts_to_graph_factory(self):
        from alphalens.core.runner import TradingAgentsRunner

        graph = MagicMock()
        graph.propagate.return_value = ({}, "HOLD")
        factory = MagicMock(return_value=graph)
        runner = TradingAgentsRunner(
            config_builder=lambda: {"deep_think_llm": "x"},
            graph_factory=factory,
        )

        runner.run(
            _cand(ticker="AAPL"),
            candidate_id=1,
            selected_analysts=["market", "news", "fundamentals"],
        )

        factory.assert_called_once()
        _args, kwargs = factory.call_args
        self.assertEqual(
            kwargs.get("selected_analysts"),
            ["market", "news", "fundamentals"],
        )

    def test_runner_skips_selected_analysts_kwarg_when_none(self):
        """Backward compat: existing 1-arg graph_factory callables must still work."""
        from alphalens.core.runner import TradingAgentsRunner

        graph = MagicMock()
        graph.propagate.return_value = ({}, "HOLD")
        factory = MagicMock(return_value=graph)
        runner = TradingAgentsRunner(
            config_builder=lambda: {"deep_think_llm": "x"},
            graph_factory=factory,
        )
        runner.run(_cand(ticker="MSFT"), candidate_id=1)

        _args, kwargs = factory.call_args
        self.assertNotIn("selected_analysts", kwargs)


if __name__ == "__main__":
    unittest.main()
