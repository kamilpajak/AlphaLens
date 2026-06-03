"""Tests for the always-on Alpaca trade_updates daemon (PaperTradeStreamDaemon).

No real socket and no reliance on real thread timing: the daemon's building
blocks (event classification, the never-raise handler, the single-flight
worker tick, health emit, shutdown guards) are exercised directly with injected
reconcile / emit / clock fakes.
"""

import importlib.util
import sqlite3
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from alphalens_pipeline.paper import trade_stream as ts
from alphalens_pipeline.paper.trade_stream import PaperTradeStreamDaemon, classify_event


def _evt(name):
    """A minimal stand-in for an alpaca-py TradeUpdate."""
    return SimpleNamespace(event=name, order=SimpleNamespace(id="ord-1"))


def _make_daemon(*, reconcile=None, emit=None, now=None, resync=1000.0):
    return PaperTradeStreamDaemon(
        stream=mock.Mock(),
        broker=mock.Mock(),
        ledger_path=Path("/tmp/does-not-matter.db"),
        account="test",
        reconcile=reconcile or mock.Mock(),
        emit=emit or mock.Mock(),
        resync_interval_s=resync,
        now=now or (lambda: 1000.0),
    )


class TestClassifyEvent(unittest.TestCase):
    def test_trigger_events(self):
        for name in (
            "fill",
            "partial_fill",
            "canceled",
            "expired",
            "rejected",
            "replaced",
            "done_for_day",
        ):
            self.assertEqual(classify_event(name), "trigger", name)

    def test_ignore_events(self):
        for name in (
            "new",
            "accepted",
            "pending_new",
            "pending_cancel",
            "pending_replace",
            "stopped",
            "calculated",
            "suspended",
            "held",
            "restated",
        ):
            self.assertEqual(classify_event(name), "ignore", name)

    def test_action_rejected_events(self):
        for name in ("order_cancel_rejected", "order_replace_rejected"):
            self.assertEqual(classify_event(name), "action_rejected", name)

    def test_unknown_event_is_failsafe(self):
        self.assertEqual(classify_event("teleported"), "unknown")


class TestEventNameNormalisation(unittest.TestCase):
    def test_raw_string(self):
        self.assertEqual(ts._event_name(_evt("partial_fill")), "partial_fill")

    def test_enum_with_value(self):
        enum_like = SimpleNamespace(value="FILL")
        self.assertEqual(ts._event_name(SimpleNamespace(event=enum_like)), "fill")


class TestHandler(unittest.IsolatedAsyncioTestCase):
    async def test_trigger_event_requests_reconcile(self):
        d = _make_daemon()
        await d._on_trade_update(_evt("partial_fill"))
        self.assertTrue(d._pending)
        self.assertTrue(d._wake.is_set())
        self.assertEqual(d._last_event_ts, 1000.0)

    async def test_ignore_event_does_not_request_but_records_liveness(self):
        d = _make_daemon()
        await d._on_trade_update(_evt("new"))
        self.assertFalse(d._pending)
        self.assertFalse(d._wake.is_set())
        self.assertEqual(d._last_event_ts, 1000.0)

    async def test_unknown_event_failsafe_counts_no_trigger(self):
        d = _make_daemon()
        await d._on_trade_update(_evt("teleported"))
        self.assertFalse(d._pending)
        self.assertEqual(d._unknown_events, 1)

    async def test_action_rejected_increments_counter_no_trigger(self):
        d = _make_daemon()
        await d._on_trade_update(_evt("order_cancel_rejected"))
        self.assertFalse(d._pending)
        self.assertEqual(d._action_rejected, 1)

    async def test_handler_never_raises(self):
        d = _make_daemon()
        # event attribute access raises -> handler must swallow it.
        boom = mock.Mock()
        type(boom).event = mock.PropertyMock(side_effect=RuntimeError("boom"))
        try:
            await d._on_trade_update(boom)
        except Exception as exc:
            self.fail(f"handler raised: {exc!r}")
        self.assertFalse(d._pending)


class TestWorkerSingleFlight(unittest.TestCase):
    def test_burst_requests_coalesce_to_one_reconcile_per_tick(self):
        reconcile = mock.Mock()
        d = _make_daemon(reconcile=reconcile, resync=1000.0)
        for _ in range(5):  # 5 events arrive before the worker wakes
            d._request_reconcile()
        d._tick()  # one drain
        reconcile.assert_called_once()

    def test_run_reconcile_calls_writer_with_profile_args_and_advances_gauge(self):
        reconcile = mock.Mock()
        clock = mock.Mock(side_effect=[111.0, 111.0])
        d = _make_daemon(reconcile=reconcile, now=clock)
        d._run_reconcile_once()
        reconcile.assert_called_once_with(
            ledger_path=d._ledger_path, broker=d._broker, account="test"
        )
        self.assertEqual(d._last_reconcile_ts, 111.0)

    def test_periodic_resync_reconciles_even_with_no_pending(self):
        reconcile = mock.Mock()
        d = _make_daemon(reconcile=reconcile, resync=0.01)  # wake.wait times out fast
        self.assertFalse(d._pending)
        d._tick()  # timeout path = periodic safety resync
        reconcile.assert_called_once()

    def test_reconcile_db_locked_is_swallowed_and_gauge_not_advanced(self):
        reconcile = mock.Mock(side_effect=sqlite3.OperationalError("database is locked"))
        d = _make_daemon(reconcile=reconcile)
        try:
            d._run_reconcile_once()
        except Exception as exc:
            self.fail(f"reconcile error escaped: {exc!r}")
        self.assertIsNone(d._last_reconcile_ts)


class TestHealthEmit(unittest.TestCase):
    def test_emits_core_gauges_without_last_reconcile_before_first_run(self):
        emit = mock.Mock()
        d = _make_daemon(emit=emit)
        d._connected = 1
        d._emit_health()
        job, metrics = emit.call_args[0]
        self.assertEqual(job, "paper-trade-stream")
        self.assertIn('alphalens_paper_trade_stream_connected{account="test"}', metrics)
        self.assertIn(
            'alphalens_paper_trade_stream_heartbeat_timestamp_seconds{account="test"}', metrics
        )
        self.assertNotIn(
            'alphalens_paper_trade_stream_last_reconcile_timestamp_seconds{account="test"}', metrics
        )

    def test_includes_last_reconcile_after_a_successful_reconcile(self):
        emit = mock.Mock()
        d = _make_daemon(emit=emit, reconcile=mock.Mock())
        d._run_reconcile_once()
        _job, metrics = emit.call_args[0]
        self.assertIn(
            'alphalens_paper_trade_stream_last_reconcile_timestamp_seconds{account="test"}', metrics
        )

    def test_emit_failure_does_not_escape(self):
        emit = mock.Mock(side_effect=OSError("disk full"))
        d = _make_daemon(emit=emit)
        try:
            d._emit_health()
        except Exception as exc:
            self.fail(f"emit error escaped: {exc!r}")


class TestShutdownGuards(unittest.TestCase):
    def test_stop_stream_noop_when_loop_absent(self):
        d = _make_daemon()
        d._stream._loop = None  # pre-subscribe window
        d._stop_stream()
        d._stream.stop.assert_not_called()

    def test_stop_stream_calls_stop_when_loop_running(self):
        d = _make_daemon()
        d._stream._loop = SimpleNamespace(is_running=lambda: True)
        d._stop_stream()
        d._stream.stop.assert_called_once()

    def test_on_signal_sets_stop_and_wake(self):
        d = _make_daemon()
        d._stream._loop = None
        d._on_signal(15, None)
        self.assertTrue(d._stop.is_set())
        self.assertTrue(d._wake.is_set())


class TestRunOrdering(unittest.TestCase):
    def test_connect_resync_runs_before_subscribe(self):
        order = []
        reconcile = mock.Mock(side_effect=lambda **kw: order.append("reconcile"))
        stream = mock.Mock()
        stream._loop = None
        stream.subscribe_trade_updates.side_effect = lambda h: order.append("subscribe")
        stream.run.side_effect = lambda: order.append("run")
        d = PaperTradeStreamDaemon(
            stream=stream,
            broker=mock.Mock(),
            ledger_path=Path("/tmp/x.db"),
            account="test",
            reconcile=reconcile,
            emit=mock.Mock(),
            resync_interval_s=1000.0,
            now=lambda: 1.0,
        )
        d.run()
        # The connect-time resync must precede the subscribe, which must precede
        # the blocking stream.run().
        self.assertLess(order.index("reconcile"), order.index("subscribe"))
        self.assertLess(order.index("subscribe"), order.index("run"))


class TestSingleWriterNegativeControl(unittest.TestCase):
    def test_daemon_module_never_writes_the_ledger_directly(self):
        path = Path(importlib.util.find_spec("alphalens_pipeline.paper.trade_stream").origin)
        src = path.read_text()
        # The daemon is a trigger, not a writer: it must not open the ledger or
        # emit SQL itself — every write goes through reconcile_orders.
        for forbidden in (
            "open_ledger",
            "sqlite3.connect",
            "INSERT ",
            "UPDATE ",
            "DELETE ",
            "executemany(",
        ):
            self.assertNotIn(
                forbidden, src, f"daemon must not write the ledger: found {forbidden!r}"
            )
        # Positive control: it DOES delegate to the sole writer.
        self.assertIn("reconcile_orders", src)


if __name__ == "__main__":
    unittest.main()
