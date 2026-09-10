"""CLI tests for ``alphalens broker stream-status`` (rearm design memo §6 INC-6).

A read-only one-shot probe over the stream-state gauges the daemon writes each
tick to ``alphalens_domain_broker-manager-<env>-stream.prom`` (resolved through
the existing ``ALPHALENS_TEXTFILE_DIR`` seam). No broker call, no auth, no
mutation — safe while the daemon runs. Repo CLI doctrine: stdout carries the
result only (exactly one JSON value in JSON mode), errors go to stderr with a
stable machine-readable ``code``, and a missing textfile exits 4 (not found).
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from typer.testing import CliRunner

# Every gauge base name the daemon emits (rearm design memo §4.6) — the JSON
# envelope must carry each one.
_ALL_GAUGE_BASES = (
    "alphalens_broker_manager_stream_reader_up",
    "alphalens_broker_manager_stream_breaker_open",
    "alphalens_broker_manager_stream_last_message_age_seconds",
    "alphalens_broker_manager_stream_consecutive_failures",
    "alphalens_broker_manager_stream_trips_total",
    "alphalens_broker_manager_stream_in_session",
)

_GAUGE_VALUES = {
    "alphalens_broker_manager_stream_reader_up": 0.0,
    "alphalens_broker_manager_stream_breaker_open": 1.0,
    "alphalens_broker_manager_stream_last_message_age_seconds": 731.5,
    "alphalens_broker_manager_stream_consecutive_failures": 6.0,
    "alphalens_broker_manager_stream_trips_total": 3.0,
    "alphalens_broker_manager_stream_in_session": 1.0,
}


class TestStreamStatusEnvDefault(unittest.TestCase):
    """#1377: a bare invocation reads the instance the shell names.

    Before this the default was a hardcoded ``sim`` while ``picks`` and
    ``watches`` followed ``$ALPHALENS_BROKER_ENVIRONMENT``, so in a live shell
    the three commands described different instances.
    """

    def setUp(self) -> None:
        self.runner = CliRunner()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.textfile_dir = Path(self._tmp.name)
        patcher = mock.patch.dict(
            "os.environ", {"ALPHALENS_TEXTFILE_DIR": str(self.textfile_dir)}, clear=False
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _seed(self, env: str) -> None:
        path = self.textfile_dir / f"alphalens_domain_broker-manager-{env}-stream.prom"
        lines = [
            f'{name}{{job="broker-manager-{env}"}} {value}' for name, value in _GAUGE_VALUES.items()
        ]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _invoke(self, *args: str):
        from alphalens_cli.commands.broker import broker_app

        return self.runner.invoke(broker_app, ["stream-status", "--format", "json", *args])

    def test_the_environment_variable_selects_the_instance(self) -> None:
        self._seed("live")
        with mock.patch.dict("os.environ", {"ALPHALENS_BROKER_ENVIRONMENT": "live"}):
            result = self._invoke()
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(json.loads(result.stdout)["env"], "live")

    def test_the_option_still_wins_over_the_variable(self) -> None:
        self._seed("sim")
        with mock.patch.dict("os.environ", {"ALPHALENS_BROKER_ENVIRONMENT": "live"}):
            result = self._invoke("--env", "sim")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(json.loads(result.stdout)["env"], "sim")

    def test_an_unset_variable_still_means_sim(self) -> None:
        self._seed("sim")
        with mock.patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop("ALPHALENS_BROKER_ENVIRONMENT", None)
            result = self._invoke()
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(json.loads(result.stdout)["env"], "sim")


class TestStreamStatusReadsTheUnitsDirectory(unittest.TestCase):
    """`--env <x>` reads the metrics directory of THAT instance's unit (#1394).

    Measured on the VPS: in a plain shell both `--env sim` and `--env live`
    looked in `~/.alphalens/metrics` while the files sit in the units'
    `/var/lib/node_exporter/textfile`. The SIM case is the bad one — that file
    exists, so the command reported "not found" about it. `_apply_env_option`
    cannot serve this: it never shells out for sim by design (#1377).
    """

    _SHOW_SEAM = "alphalens_pipeline.brokers.automanager.unit_env._systemctl_show"

    def setUp(self) -> None:
        self.runner = CliRunner()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.unit_dir = Path(self._tmp.name) / "unit"
        self.process_dir = Path(self._tmp.name) / "process"
        self.unit_dir.mkdir()
        self.process_dir.mkdir()
        # The process points somewhere ELSE on purpose: the assertion below is
        # about PRECEDENCE, not about merely finding a file.
        patcher = mock.patch.dict(
            "os.environ", {"ALPHALENS_TEXTFILE_DIR": str(self.process_dir)}, clear=False
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _seed(self, directory: Path, env: str) -> None:
        (directory / f"alphalens_domain_broker-manager-{env}-stream.prom").write_text(
            'alphalens_broker_manager_stream_reader_up{job="x"} 1\n', encoding="utf-8"
        )

    def _show(self, payload: str):
        return lambda _unit, prop: {"LoadState": "loaded", "Environment": payload}[prop]

    def _invoke(self, *args: str):
        from alphalens_cli.commands.broker import broker_app

        return self.runner.invoke(broker_app, ["stream-status", "--format", "json", *args])

    def test_the_units_directory_wins_over_the_process_for_both_instances(self) -> None:
        for env in ("sim", "live"):
            with self.subTest(env=env):
                self._seed(self.unit_dir, env)
                payload = f"ALPHALENS_TEXTFILE_DIR={self.unit_dir}"
                with mock.patch(self._SHOW_SEAM, self._show(payload)):
                    result = self._invoke("--env", env)
                self.assertEqual(result.exit_code, 0, result.output)
                self.assertEqual(
                    json.loads(result.stdout)["source"].startswith(str(self.unit_dir)), True
                )

    def test_a_bare_invocation_never_touches_systemctl(self) -> None:
        # Positive control: the seam EXPLODES if used. A bare call keeps its
        # documented meaning — whatever this process's environment says.
        self._seed(self.process_dir, "sim")

        def explode(_unit: str, _prop: str) -> str:
            raise AssertionError("a bare stream-status must not shell out")

        with mock.patch(self._SHOW_SEAM, explode):
            result = self._invoke()
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertTrue(json.loads(result.stdout)["source"].startswith(str(self.process_dir)))

    def test_an_unreadable_unit_falls_back_and_the_error_names_the_path(self) -> None:
        # No systemctl (a developer Mac): the command degrades to the process
        # env exactly as before, and the not-found envelope still says where it
        # looked — which is the fact an operator acts on.
        def missing(_unit: str, _prop: str) -> str:
            raise FileNotFoundError("systemctl")

        with mock.patch(self._SHOW_SEAM, missing):
            result = self._invoke("--env", "live")
        self.assertEqual(result.exit_code, 4)
        self.assertIn(str(self.process_dir), result.stderr)


class TestStreamStatusCommand(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = CliRunner()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.textfile_dir = Path(self._tmp.name)
        patcher = mock.patch.dict(
            "os.environ", {"ALPHALENS_TEXTFILE_DIR": str(self.textfile_dir)}, clear=False
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write_prom(self, env: str = "sim") -> Path:
        path = self.textfile_dir / f"alphalens_domain_broker-manager-{env}-stream.prom"
        lines = [
            f'{name}{{job="broker-manager-{env}"}} {value}' for name, value in _GAUGE_VALUES.items()
        ]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def _invoke(self, *args: str):
        from alphalens_cli.commands.broker import broker_app

        return self.runner.invoke(broker_app, ["stream-status", *args])

    def test_json_envelope_carries_every_gauge_and_a_schema_field(self) -> None:
        self._write_prom()
        result = self._invoke("--format", "json")
        self.assertEqual(result.exit_code, 0, result.output)
        # Exactly ONE JSON value on stdout — no preamble, no trailing prose.
        envelope = json.loads(result.stdout)
        self.assertIn("schema", envelope)
        self.assertEqual(envelope["env"], "sim")
        for name in _ALL_GAUGE_BASES:
            self.assertIn(name, envelope["gauges"], name)
        self.assertEqual(envelope["gauges"]["alphalens_broker_manager_stream_trips_total"], 3.0)
        self.assertEqual(
            envelope["gauges"]["alphalens_broker_manager_stream_last_message_age_seconds"],
            731.5,
        )

    def test_human_output_names_the_breaker_state_and_rearm_count(self) -> None:
        self._write_prom()
        result = self._invoke()
        self.assertEqual(result.exit_code, 0, result.output)
        out = result.stdout
        # The breaker state is named as a STATE (an episode is open), not a
        # bare float, and the trip count is labeled as the re-arm cycle count.
        self.assertIn("breaker", out.lower())
        self.assertIn("open", out.lower())
        self.assertRegex(out.lower(), r"(trips|re-arm)[^\n]*3")

    def test_missing_textfile_exits_four_with_a_stable_error_code(self) -> None:
        result = self._invoke("--format", "json")
        self.assertEqual(result.exit_code, 4, result.output)
        # stdout stays empty (result-only); the stable machine-readable code
        # goes to stderr.
        self.assertEqual(result.stdout.strip(), "")
        self.assertIn("stream_metrics_missing", result.stderr)


if __name__ == "__main__":
    unittest.main()
