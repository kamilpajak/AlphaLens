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

import datetime as dt
import unittest
from unittest import mock

from alphalens_cli.main import app
from alphalens_pipeline.feedback.selection_label import SelectionLabelReport
from typer.testing import CliRunner

_EMPTY_LABEL_REPORT = SelectionLabelReport()


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
            mock.patch(
                "alphalens_pipeline.feedback.event_car.enrich_store_with_event_car",
                return_value=0,
            ),
            mock.patch(
                "alphalens_pipeline.feedback.selection_label.enrich_selection_labels",
                return_value=_EMPTY_LABEL_REPORT,
            ),
        ):
            result = self.runner.invoke(
                app,
                ["feedback", "backfill-shadow-returns", "--briefs-dir", "/tmp/does-not-matter"],
            )

        self.assertEqual(result.exit_code, 0, result.stdout)
        self.assertIn("population-monitor", result.stdout)
        replay.assert_called_once()

    def _replay_kwargs(self, extra_args: list[str]) -> dict:
        with (
            mock.patch(
                "alphalens_pipeline.feedback.population_ladder_monitor.replay_population_ladders",
                return_value=[],
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
            mock.patch(
                "alphalens_pipeline.feedback.event_car.enrich_store_with_event_car",
                return_value=0,
            ),
            mock.patch(
                "alphalens_pipeline.feedback.selection_label.enrich_selection_labels",
                return_value=_EMPTY_LABEL_REPORT,
            ),
        ):
            result = self.runner.invoke(
                app,
                [
                    "feedback",
                    "backfill-shadow-returns",
                    "--briefs-dir",
                    "/tmp/does-not-matter",
                    *extra_args,
                ],
            )
        self.assertEqual(result.exit_code, 0, result.stdout)
        return dict(replay.call_args.kwargs)

    def test_lookback_defaults_to_the_monitor_window(self):
        from alphalens_pipeline.feedback.population_ladder_monitor import MONITOR_LOOKBACK_DAYS

        self.assertEqual(self._replay_kwargs([])["lookback_days"], MONITOR_LOOKBACK_DAYS)

    def test_lookback_days_option_reaches_the_replay(self):
        # A from-scratch store rebuild (#1416) must reach briefs older than the
        # nightly window.
        self.assertEqual(self._replay_kwargs(["--lookback-days", "120"])["lookback_days"], 120)

    def _replay_calls(self, extra_args: list[str]) -> list:
        with (
            mock.patch(
                "alphalens_pipeline.feedback.population_ladder_monitor.replay_population_ladders",
                return_value=[],
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
            mock.patch(
                "alphalens_pipeline.feedback.event_car.enrich_store_with_event_car",
                return_value=0,
            ),
            mock.patch(
                "alphalens_pipeline.feedback.selection_label.enrich_selection_labels",
                return_value=_EMPTY_LABEL_REPORT,
            ),
        ):
            result = self.runner.invoke(
                app,
                [
                    "feedback",
                    "backfill-shadow-returns",
                    "--briefs-dir",
                    "/tmp/does-not-matter",
                    *extra_args,
                ],
            )
        self.assertEqual(result.exit_code, 0, result.stdout)
        return list(replay.call_args_list)

    def test_named_dates_are_replayed_one_at_a_time(self):
        # The nightly sweep reaches 75 days back; a date older than that (the June
        # pre-open rebuilds, #1494) can only be reached by naming it.
        calls = self._replay_calls(["--date", "2026-06-04", "--date", "2026-06-15"])
        self.assertEqual(len(calls), 2)
        self.assertEqual(
            [c.kwargs["end_date"] for c in calls],
            [dt.date(2026, 6, 4), dt.date(2026, 6, 15)],
        )
        for call in calls:
            self.assertEqual(call.kwargs["lookback_days"], 0)

    def test_without_named_dates_the_sweep_is_one_call_over_the_window(self):
        calls = self._replay_calls([])
        self.assertEqual(len(calls), 1)
        self.assertIsNone(calls[0].kwargs.get("end_date"))

    def test_naming_a_date_and_a_lookback_is_refused(self):
        # The two describe different date sets; silently honouring one would make the
        # command's own help a lie about what it just recomputed.
        result = self.runner.invoke(
            app,
            [
                "feedback",
                "backfill-shadow-returns",
                "--date",
                "2026-06-04",
                "--lookback-days",
                "120",
            ],
        )
        self.assertNotEqual(result.exit_code, 0)

    def test_named_dates_still_settle_the_store_for_the_mirror(self):
        # The mirror only ingests dates at or below the settled watermark, so a run
        # that skipped it would leave /edge on the pre-rebuild rows.
        from alphalens_cli.commands import feedback as feedback_cmd

        with (
            mock.patch(
                "alphalens_pipeline.feedback.population_ladder_monitor.replay_population_ladders",
                return_value=[],
            ),
            mock.patch.object(feedback_cmd, "_enrich_population_benchmark_excess"),
            mock.patch.object(feedback_cmd, "_enrich_population_event_car"),
            mock.patch.object(feedback_cmd, "_enrich_population_sector_excess"),
            mock.patch.object(feedback_cmd, "_enrich_population_size_fields"),
            mock.patch.object(feedback_cmd, "_enrich_selection_labels"),
            mock.patch.object(feedback_cmd, "_enrich_population_chart_payloads"),
            mock.patch.object(feedback_cmd, "_write_ingest_watermark") as watermark,
        ):
            result = self.runner.invoke(
                app,
                ["feedback", "backfill-shadow-returns", "--date", "2026-06-04"],
            )
        self.assertEqual(result.exit_code, 0, result.stdout)
        watermark.assert_called_once()

    def test_a_named_date_that_produced_nothing_is_reported(self):
        # The monitor skips a date with no brief parquet and returns no report, so a
        # silent run would tell the operator "0 across 0 dates" for a date they asked
        # for by name.
        from alphalens_cli.commands import feedback as feedback_cmd

        with (
            mock.patch(
                "alphalens_pipeline.feedback.population_ladder_monitor.replay_population_ladders",
                return_value=[],
            ),
            mock.patch.object(feedback_cmd, "_enrich_population_benchmark_excess"),
            mock.patch.object(feedback_cmd, "_enrich_population_event_car"),
            mock.patch.object(feedback_cmd, "_enrich_population_sector_excess"),
            mock.patch.object(feedback_cmd, "_enrich_population_size_fields"),
            mock.patch.object(feedback_cmd, "_enrich_selection_labels"),
            mock.patch.object(feedback_cmd, "_enrich_population_chart_payloads"),
            mock.patch.object(feedback_cmd, "_write_ingest_watermark"),
            mock.patch.object(feedback_cmd.logger, "warning") as warned,
        ):
            result = self.runner.invoke(
                app,
                ["feedback", "backfill-shadow-returns", "--date", "2026-06-04"],
            )
        self.assertEqual(result.exit_code, 0, result.stdout)
        warned.assert_called()
        self.assertIn("2026-06-04", str(warned.call_args))

    def test_the_enrichment_tail_runs_once_however_many_dates_were_named(self):
        # The enrichment passes sweep the WHOLE store, so running them per date would
        # repeat the same work N times inside one wall-clock deadline.
        from alphalens_cli.commands import feedback as feedback_cmd

        with (
            mock.patch(
                "alphalens_pipeline.feedback.population_ladder_monitor.replay_population_ladders",
                return_value=[],
            ),
            mock.patch.object(feedback_cmd, "_enrich_population_benchmark_excess") as bench,
            mock.patch.object(feedback_cmd, "_enrich_population_event_car"),
            mock.patch.object(feedback_cmd, "_enrich_population_sector_excess"),
            mock.patch.object(feedback_cmd, "_enrich_population_size_fields"),
            mock.patch.object(feedback_cmd, "_enrich_selection_labels"),
            mock.patch.object(feedback_cmd, "_enrich_population_chart_payloads"),
            mock.patch.object(feedback_cmd, "_write_ingest_watermark"),
        ):
            result = self.runner.invoke(
                app,
                [
                    "feedback",
                    "backfill-shadow-returns",
                    "--date",
                    "2026-06-04",
                    "--date",
                    "2026-06-15",
                ],
            )
        self.assertEqual(result.exit_code, 0, result.stdout)
        bench.assert_called_once()

    def test_a_store_from_the_old_arrival_rule_stops_the_whole_chain(self):
        # Fail closed (#1416): no enrichment pass and no ingest watermark run on a
        # store that must be rebuilt, so /edge keeps the last complete state and
        # the staleness alerts surface the pending rebuild.
        from alphalens_cli.commands import feedback as feedback_cmd
        from alphalens_pipeline.feedback.population_ladder_monitor import LegacyArrivalStoreError

        with (
            mock.patch(
                "alphalens_pipeline.feedback.population_ladder_monitor.replay_population_ladders",
                side_effect=LegacyArrivalStoreError(3),
            ),
            mock.patch.object(feedback_cmd, "_enrich_population_benchmark_excess") as bench,
            mock.patch.object(feedback_cmd, "_enrich_population_chart_payloads") as chart,
            mock.patch.object(feedback_cmd, "_write_ingest_watermark") as watermark,
        ):
            result = self.runner.invoke(
                app,
                ["feedback", "backfill-shadow-returns", "--briefs-dir", "/tmp/does-not-matter"],
            )
        self.assertEqual(result.exit_code, 0, result.stdout)
        bench.assert_not_called()
        chart.assert_not_called()
        watermark.assert_not_called()

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
            mock.patch(
                "alphalens_pipeline.feedback.event_car.enrich_store_with_event_car",
                return_value=0,
            ),
            mock.patch(
                "alphalens_pipeline.feedback.selection_label.enrich_selection_labels",
                return_value=_EMPTY_LABEL_REPORT,
            ),
        ):
            result = self.runner.invoke(
                app,
                ["feedback", "backfill-shadow-returns", "--briefs-dir", "/tmp/does-not-matter"],
            )

        self.assertEqual(result.exit_code, 0, result.stdout)
        sector_excess.assert_called_once()
        self.assertIn("sector-excess", result.stdout)

    def test_command_invokes_event_car_enrichment(self):
        # The event-lane outcome pass (epic #1293, #1297) runs in the unconditional
        # enrichment tail, right after benchmark-excess.
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
                return_value=0,
            ),
            mock.patch(
                "alphalens_pipeline.feedback.event_car.enrich_store_with_event_car",
                return_value=2,
            ) as event_car,
            mock.patch(
                "alphalens_pipeline.feedback.selection_label.enrich_selection_labels",
                return_value=_EMPTY_LABEL_REPORT,
            ),
        ):
            result = self.runner.invoke(
                app,
                ["feedback", "backfill-shadow-returns", "--briefs-dir", "/tmp/does-not-matter"],
            )

        self.assertEqual(result.exit_code, 0, result.stdout)
        event_car.assert_called_once()
        self.assertIn("event-car: 2 event row(s)", result.stdout)

    def _run_with_selection_label(self, **label_patch):
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
                return_value=0,
            ),
            mock.patch(
                "alphalens_pipeline.feedback.event_car.enrich_store_with_event_car",
                return_value=0,
            ),
            mock.patch(
                "alphalens_pipeline.feedback.selection_label.enrich_selection_labels",
                **label_patch,
            ) as labels,
        ):
            result = self.runner.invoke(
                app,
                ["feedback", "backfill-shadow-returns", "--briefs-dir", "/tmp/briefs-under-test"],
            )
        self.assertEqual(result.exit_code, 0, result.stdout)
        return result, labels

    def test_command_invokes_selection_label_enrichment_and_prints_counts_only(self):
        # The ML selection label (memo section 5.1) is stamped in the enrichment tail
        # from the same briefs directory; the echo carries status counts, never values.
        report = SelectionLabelReport(
            dates_written=3, rows_stamped=7, status_counts_h20={"ok": 5, "immature": 2}
        )
        result, labels = self._run_with_selection_label(return_value=report)
        labels.assert_called_once()
        self.assertEqual(str(labels.call_args.kwargs["briefs_dir"]), "/tmp/briefs-under-test")
        self.assertIn("selection-labels: 3 date(s) written, 7 row(s) stamped", result.stdout)
        self.assertIn("immature=2", result.stdout)

    def test_selection_label_reads_the_stores_where_the_pipeline_writes_them(self):
        # The map-themes run writes the shadow under the candidates directory
        # (orchestrator: ``out_dir / "proposal_shadow"``), not under the home root.
        from alphalens_cli.commands import feedback as feedback_cmd
        from alphalens_pipeline.thematic.mapping.orchestrator import DEFAULT_OUTPUT_DIR

        _, labels = self._run_with_selection_label(return_value=_EMPTY_LABEL_REPORT)
        kwargs = labels.call_args.kwargs
        home = feedback_cmd._ALPHALENS_HOME
        self.assertEqual(kwargs["shadow_dir"], home / DEFAULT_OUTPUT_DIR.name / "proposal_shadow")
        self.assertEqual(kwargs["grouped_root"], home / "grouped_daily_history")

    def test_selection_label_failure_is_swallowed(self):
        result, labels = self._run_with_selection_label(side_effect=RuntimeError("boom"))
        labels.assert_called_once()
        self.assertNotIn("selection-labels:", result.stdout)

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
            mock.patch(
                "alphalens_pipeline.feedback.event_car.enrich_store_with_event_car",
                return_value=0,
            ),
            mock.patch(
                "alphalens_pipeline.feedback.selection_label.enrich_selection_labels",
                return_value=_EMPTY_LABEL_REPORT,
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
