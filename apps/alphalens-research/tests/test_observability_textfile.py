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

import datetime as dt
import enum
import math
import os
import re
import tempfile
import threading
import time
import unittest
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd
from alphalens_pipeline.observability import textfile
from alphalens_pipeline.observability.textfile import (
    DEFAULT_DIR,
    ENV_VAR,
    INVALID_SAMPLES_METRIC,
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

    def test_emitted_file_is_world_readable(self) -> None:
        # node_exporter's container runs as ``nobody`` (UID 65534) and
        # opens textfile-collector scrape files as that user. The
        # tempfile default mode is 0o600 (owner-only) which silently
        # disables the scrape — the file appears in ``ls`` but the
        # exporter cannot read it. Pin the chmod 0o644 promotion so
        # the next "simplify the emitter" PR can't quietly drop the
        # widened mode. Caught during VPS cutover 2026-05-30.
        import stat as stat_mod

        with tempfile.TemporaryDirectory() as tmp:
            os.environ[ENV_VAR] = tmp
            try:
                target = emit_domain_metrics(job="x", metrics={"a_total": 1})
                mode = target.stat().st_mode & 0o777
                self.assertEqual(
                    mode,
                    0o644,
                    f"emitted file must be chmod 0o644 (world-readable for "
                    f"node_exporter container user), got {oct(mode)}.",
                )
                # Also assert the readable bit specifically — even if
                # someone widens the policy further (0o664 etc.), the
                # contract is "node_exporter MUST be able to read".
                self.assertTrue(mode & stat_mod.S_IROTH, "world-read bit missing")
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


# One exposition-format sample: ``name{labels} value`` or ``name value``.
_SAMPLE = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*(\{[^}]*\})? (?P<value>\S+)$")


def _assert_node_exporter_accepts(body: str) -> None:
    """Raise unless EVERY line is a sample whose value is a finite float.

    node_exporter drops the whole file on one non-float sample, and a ``nan``
    it does accept silently defeats every comparison in the alert rules. So
    the property is "a finite float", checked over the raw text.
    """
    for line in body.splitlines():
        match = _SAMPLE.match(line)
        if match is None:
            raise AssertionError(f"not a sample line: {line!r}")
        try:
            value = float(match.group("value"))
        except ValueError as exc:
            raise AssertionError(f"not a float: {line!r}") from exc
        if not math.isfinite(value):
            raise AssertionError(f"not finite: {line!r}")


class _Level(enum.IntEnum):
    HIGH = 3


class _HostileFloat(float):
    """A ``numbers.Real`` whose conversion AND repr both raise.

    The writer's no-raise contract has to cover the conversion it performs and
    the ``repr`` it logs, not only the shapes it expects.
    """

    def __float__(self) -> float:
        raise ValueError("refuses to convert")

    def __repr__(self) -> str:
        raise RuntimeError("refuses to render")


class _RuntimeErrorFloat(float):
    """Conversion raises an exception outside the arithmetic/value/type family."""

    def __float__(self) -> float:
        raise RuntimeError("refuses to convert")


class _KeyErrorInt(int):
    """An ``Integral`` whose ``__int__`` raises."""

    def __int__(self) -> int:
        raise KeyError("refuses to convert")


class TestEmitDomainMetricsValueContract(unittest.TestCase):
    """#1462: a bad value is DROPPED and REPORTED, never written and never raised.

    Why no raise: the broker daemon calls the writer bare on every tick and
    catches only ``OSError`` (``control_loop._default_emit_heartbeat`` and its
    siblings), so a ``ValueError`` here would stop the protective loop on SIM
    and LIVE. Why not written: a ``True`` or ``"1"`` sample makes node_exporter
    drop the whole file, and a ``nan`` / ``inf`` it accepts defeats every
    ``!= 0`` / ``> N`` rule.
    """

    REFUSED = (
        ("True", True),
        ("False", False),
        ("str", "1"),
        ("None", None),
        ("Decimal", Decimal("1")),
        ("nan", float("nan")),
        ("inf", float("inf")),
        ("-inf", float("-inf")),
        ("np.bool_", np.bool_(True)),
        ("np.float64 nan", np.float64("nan")),
        ("0-d array", np.array(2.5)),
        ("pd.NA", pd.NA),
        ("pd.NaT", pd.NaT),
        ("datetime", dt.datetime(2026, 9, 16)),
        ("huge Fraction", Fraction(10**400, 1)),
        ("hostile float", _HostileFloat(1.0)),
        ("RuntimeError float", _RuntimeErrorFloat(1.0)),
        ("KeyError int", _KeyErrorInt(3)),
    )

    ACCEPTED = (
        ("int", 3, "3"),
        ("float", 1.5, "1.5"),
        ("epoch float", 1789545634.404397, "1789545634.404397"),
        ("integral float", 15000.0, "15000.0"),
        ("negative", -2, "-2"),
        ("IntEnum", _Level.HIGH, "3"),
        ("np.int64", np.int64(3), "3"),
        ("np.uint64 max", np.uint64(2**64 - 1), "18446744073709551615"),
        ("np.float64", np.float64(1.5), "1.5"),
        ("np.float32", np.float32(0.1), "0.10000000149011612"),
        ("np.float16", np.float16(0.1), "0.0999755859375"),
        ("Fraction", Fraction(1, 4), "0.25"),
    )

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        env = mock.patch.dict(os.environ, {ENV_VAR: tmp.name})
        env.start()
        self.addCleanup(env.stop)
        # The log latch is process state; every test starts from an empty one.
        latch = mock.patch.object(textfile, "_LATCHED", set())
        latch.start()
        self.addCleanup(latch.stop)

    def emit(self, job: str, metrics: dict) -> str:
        return emit_domain_metrics(job, metrics).read_text()

    def test_a_refused_value_is_dropped_reported_and_never_raised(self) -> None:
        for name, value in self.REFUSED:
            with self.subTest(shape=name):
                job = f"refused-{name.replace(' ', '-')}"
                with self.assertLogs(textfile.logger, level="ERROR") as logs:
                    body = self.emit(job, {"bad_gauge": value, "good_gauge": 7})
                self.assertNotIn("bad_gauge", body)
                self.assertIn("good_gauge 7\n", body)
                self.assertIn(f'{INVALID_SAMPLES_METRIC}{{textfile="{job}"}} 1\n', body)
                self.assertIn("bad_gauge", "\n".join(logs.output))
                _assert_node_exporter_accepts(body)
                self.assertEqual(sorted(p.suffix for p in self.dir.iterdir()).count(".tmp"), 0)

    def test_an_accepted_value_renders_exactly(self) -> None:
        for name, value, text in self.ACCEPTED:
            with self.subTest(shape=name):
                body = self.emit("accepted", {"g": value})
                self.assertEqual(body, f"g {text}\n")

    def test_a_clean_mapping_writes_no_invalid_samples_line(self) -> None:
        # Byte-identical to the pre-#1462 output for every shape a caller passes
        # today, so no file on the VPS changes and no reader sees a new key.
        body = self.emit("clean", {'a{job="x"}': 1, "b": 2.5, "c": 1789545634.404397})
        self.assertEqual(body, 'a{job="x"} 1\nb 2.5\nc 1789545634.404397\n')

    def test_every_refused_sample_is_counted(self) -> None:
        with self.assertLogs(textfile.logger, level="ERROR"):
            body = self.emit("two-bad", {"a": True, "b": float("nan"), "c": 1})
        self.assertIn(f'{INVALID_SAMPLES_METRIC}{{textfile="two-bad"}} 2\n', body)

    def test_the_parse_check_can_refute(self) -> None:
        """Positive control for the property the tests above rely on."""
        for bad in ("g True", "g nan", "g inf", "no_value", 'g{job="x"}'):
            with self.subTest(line=bad), self.assertRaises(AssertionError):
                _assert_node_exporter_accepts(bad)


class TestInvalidValueLogLatch(unittest.TestCase):
    """One ERROR per defect, not one per call.

    The daemon emits every ~45 s and the price stream every ~15 s; a persistent
    bad value logged on every call would flood the journal. The latch is keyed
    by (job, expression), re-arms when that expression writes cleanly again,
    and is capped so it can never silence an unbounded stream of new keys.
    """

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = mock.patch.dict(os.environ, {ENV_VAR: tmp.name})
        env.start()
        self.addCleanup(env.stop)
        latch = mock.patch.object(textfile, "_LATCHED", set())
        latch.start()
        self.addCleanup(latch.stop)

    def _levels(self, calls) -> list[str]:
        with self.assertLogs(textfile.logger, level="INFO") as logs:
            for metrics in calls:
                emit_domain_metrics("latch-job", metrics)
            textfile.logger.info("sentinel")  # assertLogs needs at least one record
        return [r.levelname for r in logs.records if r.getMessage() != "sentinel"]

    def test_a_persistent_defect_logs_once(self) -> None:
        self.assertEqual(self._levels([{"g": float("nan")}] * 3), ["ERROR"])

    def test_a_recovery_logs_once_and_re_arms(self) -> None:
        levels = self._levels([{"g": True}, {"g": True}, {"g": 1}, {"g": 1}, {"g": True}])
        self.assertEqual(levels, ["ERROR", "INFO", "ERROR"])

    def test_concurrent_writers_log_one_defect_once(self) -> None:
        # The price stream emits from its own thread while the daemon ticks on
        # the main thread, in one process.
        with self.assertLogs(textfile.logger, level="ERROR") as logs:
            threads = [
                threading.Thread(
                    target=lambda: [
                        emit_domain_metrics("latch-job", {"g": float("inf")}) for _ in range(40)
                    ]
                )
                for _ in range(8)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(len(logs.records), 1)

    def test_past_the_cap_a_new_defect_still_logs_every_time(self) -> None:
        # Degrades to no latch, never to a lost log line.
        with mock.patch.object(textfile, "_LATCH_CAP", 2):
            levels = self._levels([{"a": True}, {"b": True}, {"c": True}, {"c": True}])
        self.assertEqual(levels, ["ERROR", "ERROR", "ERROR", "ERROR"])


if __name__ == "__main__":
    unittest.main()
