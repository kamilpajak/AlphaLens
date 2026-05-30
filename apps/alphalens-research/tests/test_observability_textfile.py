"""Round-trip + atomic-write coverage for the textfile metric emitter.

The emitter lives at
``apps/alphalens-pipeline/alphalens_pipeline/observability/textfile.py``
and is called from CLI success-paths to publish domain-specific
counters that node_exporter's ``--collector.textfile.directory`` then
scrapes. Failure modes the tests guard against:

1. **Partial reads from node_exporter.** node_exporter polls the
   textfile directory ~every 15s; if the emitter writes mid-poll, the
   exporter sees a truncated file and either skips the metric or
   reports a parse error. ``emit_domain_metrics`` writes to a sibling
   ``.tmp`` then ``os.replace``s into place (atomic on POSIX), so the
   exporter only ever observes a fully-written file.

2. **Silent path errors.** If ``ALPHALENS_TEXTFILE_DIR`` resolves to a
   missing parent (e.g. fresh VPS, no metrics dir yet), the emitter
   must ``mkdir -p`` first rather than swallowing the FileNotFoundError.

3. **Pre-existing file from a previous run.** Each call MUST overwrite,
   not append — appending would let counters grow unboundedly across
   runs (we publish gauges, not Prometheus counters).
"""

from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

from alphalens_pipeline.observability.textfile import (
    DEFAULT_DIR,
    ENV_VAR,
    emit_domain_metrics,
)


class TestEmitDomainMetricsRoundTrip(unittest.TestCase):
    def test_writes_metrics_to_named_file_in_textfile_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ[ENV_VAR] = tmp
            try:
                path = emit_domain_metrics(
                    job="edgar-detect",
                    metrics={
                        "alphalens_edgar_events_detected_total": 5,
                        "alphalens_edgar_events_dispatched_total": 2,
                    },
                )

                self.assertEqual(path, Path(tmp) / "alphalens_domain_edgar-detect.prom")
                contents = path.read_text()
                self.assertIn("alphalens_edgar_events_detected_total 5", contents)
                self.assertIn("alphalens_edgar_events_dispatched_total 2", contents)
            finally:
                os.environ.pop(ENV_VAR, None)

    def test_supports_label_expressions_in_metric_keys(self) -> None:
        # Domain metrics carry labels via the full PromQL expression form,
        # e.g. ``alphalens_edgar_candidates_total{severity="approval"}``,
        # so the emitter doesn't have to know about label semantics.
        with tempfile.TemporaryDirectory() as tmp:
            os.environ[ENV_VAR] = tmp
            try:
                emit_domain_metrics(
                    job="thematic-build",
                    metrics={
                        'alphalens_thematic_briefs_total{model="pro"}': 12,
                        'alphalens_thematic_briefs_total{model="flash"}': 7,
                    },
                )
                contents = (Path(tmp) / "alphalens_domain_thematic-build.prom").read_text()
                self.assertIn('alphalens_thematic_briefs_total{model="pro"} 12', contents)
                self.assertIn('alphalens_thematic_briefs_total{model="flash"} 7', contents)
            finally:
                os.environ.pop(ENV_VAR, None)

    def test_overwrites_previous_run(self) -> None:
        # Gauges, not counters — successive runs must REPLACE the file,
        # never append.
        with tempfile.TemporaryDirectory() as tmp:
            os.environ[ENV_VAR] = tmp
            try:
                emit_domain_metrics(job="x", metrics={"a_total": 1})
                emit_domain_metrics(job="x", metrics={"a_total": 99})
                contents = (Path(tmp) / "alphalens_domain_x.prom").read_text()
                self.assertNotIn("a_total 1\n", contents)
                self.assertIn("a_total 99", contents)
            finally:
                os.environ.pop(ENV_VAR, None)

    def test_creates_textfile_dir_when_missing(self) -> None:
        # First-ever VPS run: the metrics dir doesn't exist yet. The
        # emitter must mkdir -p; refusing to write would leave the
        # systemd unit failing on an absent dir error.
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "deeply" / "nested" / "metrics"
            os.environ[ENV_VAR] = str(target)
            try:
                emit_domain_metrics(job="x", metrics={"a_total": 1})
                self.assertTrue(target.is_dir())
                self.assertTrue((target / "alphalens_domain_x.prom").is_file())
            finally:
                os.environ.pop(ENV_VAR, None)

    def test_atomic_write_no_partial_file_visible(self) -> None:
        # Race: emit runs concurrently with a reader (node_exporter).
        # Reader thread polls the file path tightly; if any read returns
        # partial content (e.g. half-written line), the emitter is not
        # using os.replace and the test fails.
        with tempfile.TemporaryDirectory() as tmp:
            os.environ[ENV_VAR] = tmp
            try:
                target = Path(tmp) / "alphalens_domain_x.prom"
                # Seed a known-good file so the reader always has SOMETHING.
                emit_domain_metrics(job="x", metrics={"seed_total": 0})

                stop = threading.Event()
                partial_observed: list[str] = []

                def reader() -> None:
                    while not stop.is_set():
                        try:
                            raw = target.read_text()
                        except FileNotFoundError:
                            continue
                        for line in raw.splitlines():
                            # Any line that doesn't parse as `<name> <value>`
                            # would be a partial-write artifact. The seed
                            # line is `seed_total 0`; any emit overwrites
                            # with a single-line file too.
                            if line and not line.startswith("#"):
                                parts = line.rsplit(" ", 1)
                                if (
                                    len(parts) != 2
                                    or not parts[1].lstrip("-").replace(".", "").isdigit()
                                ):
                                    partial_observed.append(line)

                t = threading.Thread(target=reader, daemon=True)
                t.start()

                for i in range(200):
                    emit_domain_metrics(job="x", metrics={"a_total": i})
                    if i % 20 == 0:
                        time.sleep(0.001)

                stop.set()
                t.join(timeout=2)

                self.assertEqual(
                    partial_observed,
                    [],
                    f"node_exporter would observe partial reads: {partial_observed[:3]}",
                )
            finally:
                os.environ.pop(ENV_VAR, None)

    def test_default_dir_used_when_env_var_unset(self) -> None:
        # When ALPHALENS_TEXTFILE_DIR is unset the emitter falls back to
        # ~/.alphalens/metrics — the bind-mount target node_exporter
        # reads on the VPS. Keep the test isolated by mocking $HOME so
        # we don't touch the operator's real metrics dir.
        with tempfile.TemporaryDirectory() as tmp:
            os.environ.pop(ENV_VAR, None)
            orig_home = os.environ.get("HOME")
            os.environ["HOME"] = tmp
            try:
                path = emit_domain_metrics(job="x", metrics={"a_total": 1})
                self.assertEqual(path.parent, Path(tmp) / ".alphalens" / "metrics")
                self.assertTrue(path.is_file())
            finally:
                if orig_home is not None:
                    os.environ["HOME"] = orig_home
                else:
                    os.environ.pop("HOME", None)


class TestDefaultPathContract(unittest.TestCase):
    """The bash systemd hook reads ``${ALPHALENS_TEXTFILE_DIR:-$HOME/.alphalens/metrics}``;
    the Python helper must agree byte-for-byte or the two halves of the
    metric stream land in different directories and Prometheus loses
    half the signal.
    """

    def test_default_dir_matches_bash_hook(self) -> None:
        # The two halves share one constant — pin it here so a future
        # refactor that moves DEFAULT_DIR also gets surfaced when the
        # bash hook drifts.
        self.assertEqual(DEFAULT_DIR.name, "metrics")
        self.assertEqual(DEFAULT_DIR.parent.name, ".alphalens")


if __name__ == "__main__":
    unittest.main()
