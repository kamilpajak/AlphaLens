import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock


def _cand(ticker="AAPL", source="momentum", priority=10, payload=None, discriminator="D"):
    from alphalens.candidates import Candidate

    return Candidate.from_screener(
        ticker=ticker,
        source=source,
        priority=priority,
        payload=payload or {},
        discriminator=discriminator,
    )


class TestTradingAgentsRunner(unittest.TestCase):
    def test_runner_builds_graph_with_gemini_config(self):
        from alphalens.runner import TradingAgentsRunner

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
        from alphalens.runner import TradingAgentsRunner

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
        from alphalens.runner import TradingAgentsRunner

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
        today = datetime.now(timezone.utc).date().isoformat()
        self.assertEqual(args[1], today)

    def test_runner_propagates_exception(self):
        from alphalens.runner import TradingAgentsRunner

        graph = MagicMock()
        graph.propagate.side_effect = RuntimeError("429")
        runner = TradingAgentsRunner(
            config_builder=lambda: {"deep_think_llm": "x"},
            graph_factory=lambda _cfg: graph,
        )

        with self.assertRaises(RuntimeError):
            runner.run(_cand(ticker="AAPL"), candidate_id=1)


if __name__ == "__main__":
    unittest.main()
