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
