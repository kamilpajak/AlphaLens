"""Tests for ``alphalens events insider-clusters`` (event lane, epic #1293)."""

from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd
from alphalens_cli.main import app
from typer.testing import CliRunner


def _frame(n: int) -> pd.DataFrame:
    from alphalens_pipeline.events.insider_cluster_detect import EVENT_CANDIDATE_COLUMNS

    df = pd.DataFrame(columns=EVENT_CANDIDATE_COLUMNS)
    for i in range(n):
        df.loc[i, "ticker"] = f"T{i}"
        df.loc[i, "event_n_insiders"] = 2
        df.loc[i, "event_cluster_usd"] = 150_000.0
        df.loc[i, "eligible"] = i == 0
        df.loc[i, "exclusion_reason"] = "" if i == 0 else "mcap_unknown"
    return df


class TestEventsInsiderClusters(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()

    def test_events_group_is_registered(self):
        result = self.runner.invoke(app, ["--help"])
        self.assertEqual(result.exit_code, 0, result.stdout)
        self.assertIn("events", result.stdout)
        result = self.runner.invoke(app, ["events", "--help"])
        self.assertEqual(result.exit_code, 0, result.stdout)
        self.assertIn("insider-clusters", result.stdout)

    def test_insider_clusters_writes_parquet_and_prints_summary(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch(
                "alphalens_pipeline.events.insider_cluster_detect.build_event_candidates",
                return_value=_frame(2),
            ) as build,
        ):
            result = self.runner.invoke(
                app,
                ["events", "insider-clusters", "--date", "2026-03-04", "--output-dir", tmp],
            )
            self.assertEqual(result.exit_code, 0, result.stdout)
            self.assertEqual(build.call_args.kwargs["asof"], dt.date(2026, 3, 4))
            written = pd.read_parquet(Path(tmp) / "2026-03-04.parquet")
            self.assertEqual(len(written), 2)
            self.assertIn("2 cluster(s), 1 eligible", result.stdout)
            self.assertIn("T1", result.stdout)
            self.assertIn("mcap_unknown", result.stdout)

    def test_insider_clusters_defaults_to_yesterday_utc(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch(
                "alphalens_pipeline.events.insider_cluster_detect.build_event_candidates",
                return_value=_frame(0),
            ) as build,
        ):
            result = self.runner.invoke(app, ["events", "insider-clusters", "--output-dir", tmp])
            self.assertEqual(result.exit_code, 0, result.stdout)
            expected = dt.datetime.now(dt.UTC).date() - dt.timedelta(days=1)
            self.assertEqual(build.call_args.kwargs["asof"], expected)

    def test_insider_clusters_empty_result_writes_empty_parquet_and_exits_zero(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch(
                "alphalens_pipeline.events.insider_cluster_detect.build_event_candidates",
                return_value=_frame(0),
            ),
        ):
            result = self.runner.invoke(
                app, ["events", "insider-clusters", "--date", "2026-03-04", "--output-dir", tmp]
            )
            self.assertEqual(result.exit_code, 0, result.stdout)
            written = pd.read_parquet(Path(tmp) / "2026-03-04.parquet")
            self.assertTrue(written.empty)
            self.assertIn("0 cluster(s), 0 eligible", result.stdout)


if __name__ == "__main__":
    unittest.main()
