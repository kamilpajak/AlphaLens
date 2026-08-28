"""Consumption-side gauges for the shared price reader (#1172 PR-4).

The reader publishes its own liveness; these gauges answer the OTHER question,
which is the one that matters to a daemon: is THIS instance actually getting
prices from it? A reader that is up while a daemon cannot reach its socket
(wrong path in a drop-in, permissions, a restart race) is invisible from the
reader's side alone.
"""

from __future__ import annotations

import unittest
from unittest import mock

from alphalens_pipeline.brokers.automanager import control_loop as cl


class _FakeRemote:
    def __init__(self, *, connected: bool, attempts: int, failures: int) -> None:
        self.is_connected = connected
        self.connect_attempts = attempts
        self.failures = failures


class TestPriceReaderClientGauges(unittest.TestCase):
    def _emit(self, remote) -> tuple[str, dict[str, float]]:
        captured: dict[str, object] = {}

        def _fake_emit(job, metrics):
            captured["job"] = job
            captured["metrics"] = dict(metrics)

        with mock.patch(
            "alphalens_pipeline.observability.textfile.emit_domain_metrics", _fake_emit
        ):
            cl._emit_price_reader_client_gauges(remote)
        return str(captured.get("job")), dict(captured.get("metrics") or {})

    def test_a_connected_client_reports_up_with_its_counters(self):
        job, metrics = self._emit(_FakeRemote(connected=True, attempts=3, failures=1))
        self.assertEqual(job, "price-reader-client-sim")
        label = '{job="price-reader-client-sim"}'
        self.assertEqual(metrics[f"alphalens_price_reader_client_up{label}"], 1)
        self.assertEqual(metrics[f"alphalens_price_reader_client_connect_attempts_total{label}"], 3)
        self.assertEqual(metrics[f"alphalens_price_reader_client_failures_total{label}"], 1)

    def test_a_disconnected_client_reports_down(self):
        """The signature the alert keys on: the daemon has a reader configured
        and cannot talk to it."""
        _, metrics = self._emit(_FakeRemote(connected=False, attempts=9, failures=9))
        self.assertEqual(
            metrics['alphalens_price_reader_client_up{job="price-reader-client-sim"}'], 0
        )

    def test_the_job_label_follows_the_instance(self):
        """SIM and LIVE must not share a job: emit_domain_metrics rewrites a
        whole per-job file, so one instance would erase the other's series."""
        with mock.patch.dict("os.environ", {"ALPHALENS_BROKER_ENVIRONMENT": "live"}):
            job, _ = self._emit(_FakeRemote(connected=True, attempts=1, failures=0))
        self.assertEqual(job, "price-reader-client-live")

    def test_emitting_without_a_remote_source_is_a_no_op(self):
        """A daemon on the in-process path has no client to describe; writing
        up=0 there would page for a reader it was never meant to use."""
        captured: list[object] = []
        with mock.patch(
            "alphalens_pipeline.observability.textfile.emit_domain_metrics",
            lambda job, metrics: captured.append(job),
        ):
            cl._emit_price_reader_client_gauges(None)
        self.assertEqual(captured, [])

    def test_the_tick_actually_emits_them(self):
        """Anti-rot: a gauge nobody writes is a file, not a signal. Pins that
        the daemon loop calls the emitter each pass, next to the heartbeat."""
        import ast
        import inspect

        source = inspect.getsource(cl.run_daemon)
        called = {
            node.func.id
            for node in ast.walk(ast.parse(inspect.cleandoc(source)))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        self.assertIn("_emit_price_reader_client_gauges", called)

    def test_a_failing_emit_never_reaches_the_tick(self):
        """Same doctrine as the daemon heartbeat: a textfile-dir hiccup must
        not take prices down."""
        with mock.patch(
            "alphalens_pipeline.observability.textfile.emit_domain_metrics",
            side_effect=OSError("no dir"),
        ):
            cl._emit_price_reader_client_gauges(_FakeRemote(connected=True, attempts=1, failures=0))


if __name__ == "__main__":
    unittest.main()
