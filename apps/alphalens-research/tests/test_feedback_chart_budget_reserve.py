"""The nightly feedback run must reserve wall-clock budget for the chart pass.

2026-07-13 incident: the chart enrich pass is the LAST of four consumers of one
shared latching 75-min deadline; a grown upstream backlog consumed the whole
budget for five consecutive nights ("chart-payload: enriched 0 rows"), so no
chart in the store advanced past 2026-07-02. The fix: the upstream passes
(replay, benchmark-excess, sector-excess, size) share a deadline of
``total - reserve`` while the chart pass gets its own deadline of ``total`` —
both anchored at the same start, so the chart pass always has at least the
reserve left when its turn comes and the run still fits the systemd timeout.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import alphalens_cli.commands.feedback as feedback_cmd
import alphalens_pipeline.feedback.population_ladder_monitor as plm


class _SpyDeadline:
    """Stands in for _RunDeadline; records the budget each instance was given."""

    def __init__(self, budget_s: float, *args, **kwargs) -> None:
        self.budget_s = float(budget_s)
        self.stopped_reason: str | None = None

    def should_stop(self) -> bool:
        return False

    def record_fetch_result(self, *, ok: bool) -> None:
        pass


class TestChartPassBudgetReserve(unittest.TestCase):
    def _run(self, env: dict[str, str]) -> dict[str, object]:
        """Run _refresh_population_ladders with spies; return captured deadlines."""
        captured: dict[str, object] = {}

        def _capture(name):
            def recorder(*args, **kwargs):
                captured[name] = kwargs.get("deadline")

            return recorder

        with tempfile.TemporaryDirectory() as tmp:
            with (
                mock.patch.dict(os.environ, env, clear=False),
                mock.patch.object(plm, "_RunDeadline", _SpyDeadline),
                mock.patch.object(plm, "replay_population_ladders", lambda *a, **k: []),
                mock.patch.object(
                    feedback_cmd,
                    "_enrich_population_benchmark_excess",
                    _capture("benchmark"),
                ),
                mock.patch.object(
                    feedback_cmd, "_enrich_population_sector_excess", _capture("sector")
                ),
                mock.patch.object(feedback_cmd, "_enrich_population_size_fields", _capture("size")),
                mock.patch.object(
                    feedback_cmd, "_enrich_population_chart_payloads", _capture("chart")
                ),
            ):
                feedback_cmd._refresh_population_ladders(Path(tmp))
        return captured

    def test_upstream_shares_reduced_budget_chart_gets_full_budget(self) -> None:
        """Default: upstream passes share (total - reserve); the chart pass gets a
        SEPARATE deadline carrying the full total, so it inherits the reserve."""
        captured = self._run(
            {"ALPHALENS_FEEDBACK_FETCH_DEADLINE_S": "4500"}  # 75 min, explicit for clarity
        )
        upstream = captured["benchmark"]
        chart = captured["chart"]
        assert isinstance(upstream, _SpyDeadline)
        assert isinstance(chart, _SpyDeadline)
        self.assertEqual(upstream.budget_s, 4500.0 - plm._CHART_RESERVE_S_DEFAULT)
        self.assertEqual(chart.budget_s, 4500.0)
        self.assertIsNot(chart, upstream)
        # benchmark/sector/size all share the SAME upstream instance.
        self.assertIs(captured["sector"], upstream)
        self.assertIs(captured["size"], upstream)

    def test_reserve_env_override_is_respected(self) -> None:
        captured = self._run(
            {
                "ALPHALENS_FEEDBACK_FETCH_DEADLINE_S": "4500",
                "ALPHALENS_FEEDBACK_CHART_RESERVE_S": "600",
            }
        )
        upstream = captured["benchmark"]
        assert isinstance(upstream, _SpyDeadline)
        self.assertEqual(upstream.budget_s, 3900.0)

    def test_reserve_at_least_total_clamps_upstream_to_zero(self) -> None:
        """A pathological reserve >= total must clamp (upstream budget 0), not raise."""
        captured = self._run(
            {
                "ALPHALENS_FEEDBACK_FETCH_DEADLINE_S": "600",
                "ALPHALENS_FEEDBACK_CHART_RESERVE_S": "4500",
            }
        )
        upstream = captured["benchmark"]
        chart = captured["chart"]
        assert isinstance(upstream, _SpyDeadline)
        assert isinstance(chart, _SpyDeadline)
        self.assertEqual(upstream.budget_s, 0.0)
        self.assertEqual(chart.budget_s, 600.0)


if __name__ == "__main__":
    unittest.main()
