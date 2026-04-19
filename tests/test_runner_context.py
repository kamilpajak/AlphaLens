"""Phase C: runner formats per-source trigger context and logs it before propagate.

Actual injection into the TradingAgents graph is deferred to an upstream PR
(touching vendored code would conflict with subtree pulls). For now: log only.
"""

import logging
import unittest
from unittest.mock import MagicMock


def _cand(source, payload, ticker="AAPL", priority=10):
    from alphalens.candidates import Candidate

    return Candidate.from_screener(
        ticker=ticker,
        source=source,
        priority=priority,
        payload=payload,
        discriminator="d",
    )


class TestTriggerContextFormatters(unittest.TestCase):
    def test_format_momentum_context(self):
        from alphalens.runner import build_trigger_context

        ctx = build_trigger_context(
            _cand("momentum", {"momentum_score": 0.85, "themes": ["AI", "MegaCap"]})
        )
        self.assertIn("momentum", ctx.lower())
        self.assertIn("0.85", ctx)
        self.assertIn("AI", ctx)

    def test_format_watchdog_sec_context(self):
        from alphalens.runner import build_trigger_context

        ctx = build_trigger_context(
            _cand(
                "watchdog_sec",
                {"accession": "0001-23", "form": "8-K", "url": "https://sec.gov/x"},
                priority=0,
            )
        )
        self.assertIn("8-K", ctx)
        self.assertIn("0001-23", ctx)

    def test_format_prescreener_context(self):
        from alphalens.runner import build_trigger_context

        ctx = build_trigger_context(
            _cand(
                "prescreener",
                {"rank": 3, "composite_score": 0.76},
                priority=20,
            )
        )
        self.assertIn("0.76", ctx)
        self.assertIn("3", ctx)

    def test_unknown_source_returns_empty_string(self):
        from alphalens.runner import build_trigger_context

        ctx = build_trigger_context(_cand("mystery", {}))
        self.assertEqual(ctx, "")


class TestRunnerLogsContextBeforePropagate(unittest.TestCase):
    def test_runner_logs_trigger_context(self):
        from alphalens.runner import TradingAgentsRunner

        graph = MagicMock()
        graph.propagate.return_value = ({}, "HOLD")
        runner = TradingAgentsRunner(
            config_builder=lambda: {"deep_think_llm": "gemini-3-pro-preview"},
            graph_factory=lambda _cfg: graph,
        )

        candidate = _cand(
            "momentum", {"momentum_score": 0.91, "themes": ["AI"]}, ticker="NVDA"
        )

        with self.assertLogs("alphalens.runner", level="INFO") as cm:
            runner.run(candidate, candidate_id=1)

        combined = "\n".join(cm.output)
        self.assertIn("trigger_context", combined)
        self.assertIn("NVDA", combined)
        self.assertIn("momentum", combined.lower())


if __name__ == "__main__":
    unittest.main()
