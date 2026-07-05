"""Tests for the ``alphalens feedback backfill-shadow-returns`` operator CLI.

The command name is retained for the existing systemd unit
``alphalens-feedback-shadow-returns.service``. After the Track-A click ledger
removal (#465) the per-decision ladder replay (which read the ``decisions``
table) is gone; the command now drives ONLY the broker-free, parquet-only
population monitor. These tests pin: the population step is invoked, the command
exits 0 and prints the population summary, and a population-step failure is
swallowed (never aborts the nightly timer).
"""

from __future__ import annotations

import unittest
from unittest import mock

from alphalens_cli.main import app
from typer.testing import CliRunner


class TestFeedbackBackfillCommand(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()

    def test_command_invokes_population_monitor_and_exits_zero(self):
        # Stub the population monitor + its enrichment tail (all lazy-imported in
        # the command body) so the test never touches ~/.alphalens or Polygon.
        fake_report = mock.Mock(terminal=2, ongoing=1)
        with (
            mock.patch(
                "alphalens_pipeline.feedback.population_ladder_monitor.replay_population_ladders",
                return_value=[fake_report],
            ) as replay,
            mock.patch(
                "alphalens_pipeline.feedback.population_ladder_monitor.enrich_store_with_size_fields",
                return_value=0,
            ),
            mock.patch(
                "alphalens_pipeline.feedback.benchmark_excess.enrich_store_with_benchmark_excess",
                return_value=0,
            ),
            mock.patch(
                "alphalens_pipeline.feedback.sector_excess.enrich_store_with_sector_excess",
                return_value=0,
            ),
        ):
            result = self.runner.invoke(
                app,
                ["feedback", "backfill-shadow-returns", "--briefs-dir", "/tmp/does-not-matter"],
            )

        self.assertEqual(result.exit_code, 0, result.stdout)
        self.assertIn("population-monitor", result.stdout)
        replay.assert_called_once()

    def test_command_invokes_sector_excess_enrichment(self):
        # The sector-relative EDGE outcome (PR-2b) runs in the unconditional
        # enrichment tail alongside benchmark-excess, so the store gets its
        # sector_excess_return columns on every nightly sweep.
        fake_report = mock.Mock(terminal=2, ongoing=1)
        with (
            mock.patch(
                "alphalens_pipeline.feedback.population_ladder_monitor.replay_population_ladders",
                return_value=[fake_report],
            ),
            mock.patch(
                "alphalens_pipeline.feedback.population_ladder_monitor.enrich_store_with_size_fields",
                return_value=0,
            ),
            mock.patch(
                "alphalens_pipeline.feedback.benchmark_excess.enrich_store_with_benchmark_excess",
                return_value=0,
            ),
            mock.patch(
                "alphalens_pipeline.feedback.sector_excess.enrich_store_with_sector_excess",
                return_value=3,
            ) as sector_excess,
        ):
            result = self.runner.invoke(
                app,
                ["feedback", "backfill-shadow-returns", "--briefs-dir", "/tmp/does-not-matter"],
            )

        self.assertEqual(result.exit_code, 0, result.stdout)
        sector_excess.assert_called_once()
        self.assertIn("sector-excess", result.stdout)

    def test_population_failure_is_swallowed_command_still_exits_zero(self):
        # A Polygon outage / replay error must NOT change the command's exit
        # behaviour — the nightly timer must stay green so staleness alerting
        # (not a non-zero exit) is the signal of a stuck job.
        # The enrichment tail runs UNCONDITIONALLY after the replay try/except,
        # so stub it too — otherwise it would hit the real ~/.alphalens store.
        with (
            mock.patch(
                "alphalens_pipeline.feedback.population_ladder_monitor.replay_population_ladders",
                side_effect=RuntimeError("polygon down"),
            ),
            mock.patch(
                "alphalens_pipeline.feedback.population_ladder_monitor.enrich_store_with_size_fields",
                return_value=0,
            ),
            mock.patch(
                "alphalens_pipeline.feedback.benchmark_excess.enrich_store_with_benchmark_excess",
                return_value=0,
            ),
            mock.patch(
                "alphalens_pipeline.feedback.sector_excess.enrich_store_with_sector_excess",
                return_value=0,
            ),
        ):
            result = self.runner.invoke(
                app,
                ["feedback", "backfill-shadow-returns", "--briefs-dir", "/tmp/does-not-matter"],
            )

        self.assertEqual(result.exit_code, 0, result.stdout)


class TestFeedbackDropDecisionsTableCommand(unittest.TestCase):
    """Pins the operator teardown command that drops the dead ``decisions`` table
    from a legacy ``feedback.db`` (the schema-evolution follow-up: wires
    ``alphalens_feedback.migrate.drop_decisions_table`` to an invokable CLI so the
    orphaned host table can actually be cleaned)."""

    def setUp(self):
        self.runner = CliRunner()

    def test_command_invokes_teardown_with_path_and_exits_zero(self):
        with mock.patch(
            "alphalens_feedback.migrate.drop_decisions_table", return_value=True
        ) as drop:
            result = self.runner.invoke(
                app,
                ["feedback", "drop-decisions-table", "--feedback-db", "/tmp/legacy-feedback.db"],
            )
        self.assertEqual(result.exit_code, 0, result.stdout)
        drop.assert_called_once()
        self.assertEqual(str(drop.call_args[0][0]), "/tmp/legacy-feedback.db")

    def test_command_reports_noop_when_no_file(self):
        with mock.patch("alphalens_feedback.migrate.drop_decisions_table", return_value=False):
            result = self.runner.invoke(
                app,
                ["feedback", "drop-decisions-table", "--feedback-db", "/tmp/absent.db"],
            )
        self.assertEqual(result.exit_code, 0, result.stdout)
        self.assertIn("nothing", result.stdout.lower())


if __name__ == "__main__":
    unittest.main()
