"""Tests for `alphalens cache companyfacts-census` (#1335).

The command is the only thing that puts the census in front of a human and in
front of node_exporter, and an adversarial review found its whole body
untested: an unconditional `raise` inside it left the rest of the suite green.

Two contracts it has to keep, both easy to break by accident:

* **It is a MEASUREMENT, not a check.** It exits 0 whenever the census
  completed, however bad the reading. Anything else and a best-effort step in
  `run_thematic_day.sh` starts logging WARN on a healthy store — or worse, a
  future caller drops the `||` and a bad reading fails a build whose brief is
  already published. Only a usage error is non-zero.
* **JSON mode prints exactly one JSON value on stdout**, with `schema` leading,
  per the repo's CLI contract.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pyarrow as pa
import pyarrow.parquet as pq
from alphalens_cli.commands import cache as cache_cmd
from alphalens_cli.main import app
from alphalens_pipeline.data.fundamentals import concept_chains as chains
from alphalens_pipeline.data.fundamentals.companyfacts_parquet import SCHEMA
from alphalens_pipeline.data.store.companyfacts_census import CENSUS_SCHEMA
from typer.testing import CliRunner


def _plant_fresh_cik(store: Path, *, age_days: int = 40) -> None:
    """One CIK with both FCFF legs ending `age_days` ago."""
    end = dt.date.today() - dt.timedelta(days=age_days)
    specs = [(chains.OPERATING_CASH_FLOW[0], end), (chains.CAPEX[0], end)]
    pq.write_table(
        pa.table(
            {
                "taxonomy": ["us-gaap"] * 2,
                "concept": [c for c, _ in specs],
                "unit": ["USD"] * 2,
                "period_start": [e - dt.timedelta(days=90) for _, e in specs],
                "period_end": [e for _, e in specs],
                "val": [1.0] * 2,
                "accn": ["0000000000-26-000001"] * 2,
                "fy": [e.year for _, e in specs],
                "fp": ["Q1"] * 2,
                "form": ["10-Q"] * 2,
                "filed_date": [e + dt.timedelta(days=30) for _, e in specs],
                "frame": [None] * 2,
            },
            schema=SCHEMA,
        ),
        store / "0000000001.parquet",
    )


class _CommandFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = CliRunner()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = Path(self._tmp.name) / "companyfacts_parquet"
        self.store.mkdir()
        # Never write into the developer's real ~/.alphalens/metrics.
        self._emit = patch.object(cache_cmd, "emit_domain_metrics")
        self.emit = self._emit.start()
        self.addCleanup(self._emit.stop)

    def invoke(self, *args: str):
        return self.runner.invoke(
            app, ["cache", "companyfacts-census", "--store-dir", str(self.store), *args]
        )


class TestJsonOutput(_CommandFixture):
    def test_stdout_is_one_json_value_carrying_the_schema(self) -> None:
        _plant_fresh_cik(self.store)

        result = self.invoke("--format", "json")

        self.assertEqual(result.exit_code, 0, result.stdout)
        payload = json.loads(result.stdout.strip())
        self.assertEqual(payload["schema"], CENSUS_SCHEMA)

    def test_the_json_carries_the_counts(self) -> None:
        _plant_fresh_cik(self.store)

        payload = json.loads(self.invoke("--format", "json").stdout.strip())

        self.assertEqual(payload["total_files"], 1)
        self.assertEqual(payload["readable"], 1)
        self.assertEqual(payload["fresh"], 1)

    def test_the_json_carries_every_census_field(self) -> None:
        # A dropped key is a silent contract break for anything parsing this.
        _plant_fresh_cik(self.store)

        payload = json.loads(self.invoke("--format", "json").stdout.strip())

        self.assertEqual(
            sorted(payload),
            sorted(
                [
                    "schema",
                    "asof",
                    "total_files",
                    "readable",
                    "empty",
                    "unreadable",
                    "fresh",
                    "refetching",
                    "past_gate",
                    "chain_absent",
                    "crossing_gate_within_horizon",
                    "refetched_recently",
                ]
            ),
        )

    def test_the_reported_asof_is_the_day_it_ran(self) -> None:
        _plant_fresh_cik(self.store)

        payload = json.loads(self.invoke("--format", "json").stdout.strip())

        self.assertEqual(payload["asof"], dt.date.today().isoformat())


class TestHumanOutput(_CommandFixture):
    def test_the_human_output_names_the_counts(self) -> None:
        _plant_fresh_cik(self.store)

        result = self.invoke()

        self.assertEqual(result.exit_code, 0, result.stdout)
        self.assertIn("1 files", result.stdout)
        self.assertIn("1 fresh", result.stdout)

    def test_the_human_output_is_the_default(self) -> None:
        # No --format at all must not produce JSON; the operator reading the
        # journal is the default audience.
        _plant_fresh_cik(self.store)

        result = self.invoke()

        with self.assertRaises(json.JSONDecodeError):
            json.loads(result.stdout.strip())


class TestExitCodes(_CommandFixture):
    def test_a_healthy_store_exits_zero(self) -> None:
        _plant_fresh_cik(self.store)

        self.assertEqual(self.invoke().exit_code, 0)

    def test_a_store_that_is_entirely_past_the_gate_still_exits_zero(self) -> None:
        # The contract that keeps this a measurement: a terrible reading is
        # still a completed census, and must not fail the build.
        _plant_fresh_cik(self.store, age_days=900)

        result = self.invoke("--format", "json")

        self.assertEqual(result.exit_code, 0, result.stdout)
        self.assertEqual(json.loads(result.stdout.strip())["past_gate"], 1)

    def test_a_missing_store_exits_zero(self) -> None:
        result = self.runner.invoke(
            app,
            ["cache", "companyfacts-census", "--store-dir", str(self.store / "gone")],
        )

        self.assertEqual(result.exit_code, 0, result.stdout)

    def test_an_unknown_format_is_a_usage_error(self) -> None:
        result = self.invoke("--format", "xml")

        self.assertEqual(result.exit_code, 2)

    def test_an_unknown_format_names_the_accepted_values(self) -> None:
        result = self.invoke("--format", "xml")

        self.assertIn("human", result.output)
        self.assertIn("json", result.output)


class TestCommandWiring(unittest.TestCase):
    """The command exists under the group the day script calls."""

    def test_the_census_is_registered_under_cache(self) -> None:
        result = CliRunner().invoke(app, ["cache", "--help"])

        self.assertEqual(result.exit_code, 0, result.stdout)
        self.assertIn("companyfacts-census", result.stdout)

    def test_the_default_store_is_the_companyfacts_parquet_dir(self) -> None:
        # Pinned because the default is what runs in production: every
        # invocation in run_thematic_day.sh passes no --store-dir, so a wrong
        # default would census an empty directory and report a healthy zero.
        from alphalens_pipeline.data.store.edgar_fundamentals import DEFAULT_PARQUET_DIR

        recorded: list[Path] = []
        with (
            patch.object(cache_cmd, "emit_domain_metrics"),
            patch(
                "alphalens_pipeline.data.store.companyfacts_census.census_companyfacts_store",
                side_effect=lambda store, **kw: recorded.append(store) or _StubCensus(),
            ),
        ):
            CliRunner().invoke(app, ["cache", "companyfacts-census"])

        self.assertEqual(recorded, [DEFAULT_PARQUET_DIR])
        self.assertEqual(DEFAULT_PARQUET_DIR.name, "companyfacts_parquet")
        self.assertEqual(DEFAULT_PARQUET_DIR.parent.name, ".alphalens")


class _StubCensus:
    """Enough of StoreCensus for the default-path test to render."""

    asof = dt.date(2026, 11, 3)
    total_files = readable = empty = unreadable = 0
    fresh = refetching = past_gate = chain_absent = 0
    crossing_gate_within_horizon = refetched_recently = 0


class TestEmitReachesTheScrapedDirectory(_CommandFixture):
    """The emit writes where node_exporter reads, not into a fallback.

    A metric written to an unscraped directory is the #1366 failure: the alert
    sat inactive for two months. The emitter honours ALPHALENS_TEXTFILE_DIR,
    which the systemd unit sets and bind-mounts, so this checks the mechanism
    the unit relies on rather than the unit's own text.
    """

    def test_the_prom_file_lands_in_the_configured_textfile_dir(self) -> None:
        _plant_fresh_cik(self.store)
        self._emit.stop()  # let the real emitter run for this one case
        self.addCleanup(self._emit.start)

        with tempfile.TemporaryDirectory() as metrics_dir:
            with patch.dict(os.environ, {"ALPHALENS_TEXTFILE_DIR": metrics_dir}):
                result = self.invoke()

            self.assertEqual(result.exit_code, 0, result.stdout)
            written = list(Path(metrics_dir).glob("*.prom"))
            self.assertEqual(
                [p.name for p in written], ["alphalens_domain_companyfacts-store.prom"]
            )
            body = written[0].read_text()
            self.assertIn('alphalens_companyfacts_cached_files{state="readable"} 1', body)


if __name__ == "__main__":
    unittest.main()
