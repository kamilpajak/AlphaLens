import json
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from alphalens_cli.commands.feedback import _write_ingest_watermark

_WATERMARK_NAME = ".ingest_watermark.json"


class WriteIngestWatermarkTest(unittest.TestCase):
    def test_writes_completed_at_greater_than_all_parquet_mtimes(self):
        with TemporaryDirectory() as d:
            store = Path(d)
            # two parquet-shaped files written "earlier"
            p1 = store / "2026-07-17.parquet"
            p2 = store / "2026-07-18.parquet"
            p1.write_bytes(b"x")
            p2.write_bytes(b"y")
            before = time.time()

            _write_ingest_watermark(store)

            sentinel = store / _WATERMARK_NAME
            self.assertTrue(sentinel.exists())
            payload = json.loads(sentinel.read_text())
            completed_at = payload["completed_at"]
            self.assertIsInstance(completed_at, float)
            self.assertGreaterEqual(completed_at, before)
            self.assertGreater(completed_at, p1.stat().st_mtime)
            self.assertGreater(completed_at, p2.stat().st_mtime)

    def test_overwrites_previous_sentinel(self):
        with TemporaryDirectory() as d:
            store = Path(d)
            _write_ingest_watermark(store)
            first = json.loads((store / _WATERMARK_NAME).read_text())["completed_at"]
            _write_ingest_watermark(store)
            second = json.loads((store / _WATERMARK_NAME).read_text())["completed_at"]
            self.assertGreaterEqual(second, first)

    def test_missing_store_dir_does_not_raise(self):
        with TemporaryDirectory() as d:
            _write_ingest_watermark(Path(d) / "nonexistent")  # must not raise


class RefreshStampsWatermarkTest(unittest.TestCase):
    """C1 regression: a completed-but-degraded run (all passes swallow their own
    errors) still advances the watermark. Only a process-kill skips it."""

    def test_watermark_written_even_when_replay_and_passes_degrade(self):
        from unittest import mock

        from alphalens_cli.commands import feedback as fb

        with TemporaryDirectory() as d:
            home = Path(d)
            (home / "population_ladders").mkdir()
            with (
                mock.patch.object(fb, "_ALPHALENS_HOME", home),
                # replay raises inside _refresh's own try/except (swallowed);
                # the four enrich passes are patched to no-op (their real bodies
                # "never raise", i.e. a Polygon outage returns normally).
                mock.patch.object(fb, "_enrich_population_benchmark_excess", lambda *a, **k: None),
                mock.patch.object(fb, "_enrich_population_sector_excess", lambda *a, **k: None),
                mock.patch.object(fb, "_enrich_population_size_fields", lambda *a, **k: None),
                mock.patch.object(fb, "_enrich_population_chart_payloads", lambda *a, **k: None),
                mock.patch(
                    "alphalens_pipeline.feedback.population_ladder_monitor."
                    "replay_population_ladders",
                    side_effect=RuntimeError("polygon down"),
                ),
            ):
                fb._refresh_population_ladders(home / "thematic_briefs")
            self.assertTrue((home / "population_ladders" / _WATERMARK_NAME).exists())


if __name__ == "__main__":
    unittest.main()
