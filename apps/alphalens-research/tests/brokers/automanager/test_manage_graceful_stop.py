"""SIGTERM and SIGINT stop ``alphalens broker manage`` GRACEFULLY.

systemd stops and restarts both broker daemons with SIGTERM (``KillSignal=15``,
then SIGKILL after ``TimeoutStopUSec=90s``). Before this change the command
installed no handler, so CPython took the default action and the process died at
the interrupted instruction: the tick's ``finally: _drain_pages(report)`` did not
run (a page queued that tick was lost), and the ``manage`` ``finally`` that stops
the stream reader did not run either. Measured on main with the first test below:
exit status -15 (143 in a shell) and no ``TICK-FINALLY`` marker.

These are SUBPROCESS tests on purpose, like ``tests/data/test_price_reader_shutdown.py``:
what they guard is the disposition of a real signal delivered to a real process,
which no in-process test can reproduce. The child runs the REAL ``manage``
command and the REAL ``run_daemon``; only the broker-facing parts are fakes —
``build_default_deps``, and ``run_once``, whose tick blocks in a ``time.sleep``
the way a real tick blocks in a broker call, and prints a marker from its own
``finally``.

The in-process tests at the end pin the pieces a subprocess cannot isolate: the
handler itself, the fill-probe wait, and the restore of the previous handlers.
"""

from __future__ import annotations

import os
import queue
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from typing import Any
from unittest import mock

from alphalens_pipeline.brokers.automanager import control_loop as cl
from alphalens_pipeline.brokers.automanager import graceful_stop
from typer.testing import CliRunner

from tests.brokers.automanager.home_isolation import IsolatedHomeTestCase

# The tick blocks this long, so a signal sent right after TICK-START lands mid-tick.
_TICK_S = 1.5
# Long enough that a sleep the signal does NOT wake is unmistakable against
# _STOP_BOUND_S, and above the fill probe's 5 s chunk.
_POLL_S = 30.0
# The bound on signal-to-exit while the daemon sleeps. Measured well under 1 s;
# the slack is for a loaded CI runner, and it stays far below _POLL_S.
_STOP_BOUND_S = 5.0
# How long after the post-tick marker the signal is sent: the daemon is then
# inside the wait between ticks.
_INTO_SLEEP_S = 0.5
# A line the child prints before anything else, so the harness knows it booted.
_BOOT_TIMEOUT_S = 30.0

_CHILD = textwrap.dedent(
    """
    import sys, threading, time, types
    from unittest import mock

    mode, tick_s, poll_s, once = sys.argv[1], float(sys.argv[2]), sys.argv[3], sys.argv[4] == "1"

    from alphalens_cli.commands import broker as cli
    from alphalens_pipeline.brokers.automanager import control_loop as cl
    from broker_contract.contract import OrderState, OrderStatus

    def say(line):
        print(line, flush=True)

    ticks = {"n": 0}

    def fake_run_once(deps, *, sweep_orphans=False, keep_placing=None):
        ticks["n"] += 1
        n = ticks["n"]
        say(f"TICK-START {n}")
        try:
            time.sleep(tick_s)  # stands in for a broker call inside the tick
            say(f"TICK-COMPLETED {n}")
        finally:
            say(f"TICK-FINALLY {n}")

    class Trigger:
        def stop(self):
            say("STREAM-STOPPED")

    probe = None

    def build_probe(_deps):
        if mode == "slowboot":  # widens the gap between the handlers and the tick
            say("PROBE-BUILD")
            time.sleep(1.0)
        return probe

    if mode == "probe":
        resting = OrderState(
            order_id="o1", status=OrderStatus.WORKING, instrument=None,
            filled_quantity=0.0, raw_status="Working", side="BUY",
            order_type="TrailingStopIfTraded",
        )
        probe = cl.EntryFillProbe(
            armed_orders=lambda: {"o1": cl.ArmedEntryOrder(crid="crid-t0", uic=1)},
            list_open_orders=lambda: [resting],
            seen={},
            append=lambda record: None,
        )

    deps = types.SimpleNamespace(
        wake_event=threading.Event() if mode == "stream" else None,
        stream_tick=None,
        stream_trigger=Trigger(),
        chain_state={},
    )

    argv = ["manage", "--poll-seconds", poll_s] + (["--once"] if once else [])
    with (
        mock.patch.object(cli, "_telegram_daemon_notify", return_value=lambda m: None),
        mock.patch.object(cli, "_telegram_chain_loss_notify", return_value=lambda m: None),
        mock.patch.object(cl, "build_default_deps", side_effect=lambda **_: deps),
        mock.patch.object(cl, "build_entry_fill_probe", side_effect=build_probe),
        mock.patch.object(cl, "run_once", side_effect=fake_run_once),
        mock.patch.object(cl, "_kill_active", return_value=False),
        mock.patch.object(
            cl, "_emit_price_reader_client_gauges", side_effect=lambda _s: say("POST-TICK")
        ),
    ):
        say("BOOTED")
        cli.broker_app(argv, prog_name="alphalens broker")
    """
)


class TestTheStreamStopIsBounded(unittest.TestCase):
    """The ``manage`` ``finally`` now runs on a real stop, and the stream
    reader's ``stop()`` sends a best-effort DELETE through the Saxo client:
    30 s per phase, a 4-attempt retry ladder, a 429 Retry-After of up to 120 s,
    and no bound at all on DNS. On main a SIGTERM never reached it, so the stop
    was instant; unbounded, it would now run into systemd's SIGKILL at 90 s."""

    def test_a_stop_that_hangs_is_abandoned_within_the_budget(self) -> None:
        release = threading.Event()
        self.addCleanup(release.set)
        started = time.monotonic()
        with self.assertLogs(graceful_stop.logger, "WARNING") as logged:
            finished = graceful_stop.run_within(
                release.wait, budget_s=0.3, what="stream reader stop"
            )
        elapsed = time.monotonic() - started
        self.assertFalse(finished)
        self.assertLess(elapsed, 1.5)
        self.assertIn("stream reader stop", logged.output[0])

    def test_a_stop_that_finishes_returns_true_and_logs_nothing(self) -> None:
        calls: list[int] = []
        with self.assertNoLogs(graceful_stop.logger, "WARNING"):
            finished = graceful_stop.run_within(lambda: calls.append(1), budget_s=5.0, what="x")
        self.assertTrue(finished)
        self.assertEqual(calls, [1])

    def test_an_exception_in_the_stop_is_logged_not_raised(self) -> None:
        # Best-effort cleanup on the way out: a failure must not replace the exit.
        def _boom() -> None:
            raise RuntimeError("DELETE failed")

        with self.assertLogs(graceful_stop.logger, "WARNING") as logged:
            finished = graceful_stop.run_within(_boom, budget_s=5.0, what="stream reader stop")
        self.assertTrue(finished)
        self.assertIn("DELETE failed", "\n".join(logged.output))


class _Child:
    """One spawned ``manage`` process, its stdout read line by line on a thread."""

    def __init__(self, case: unittest.TestCase, mode: str, *, once: bool = False) -> None:
        home = tempfile.mkdtemp(prefix="graceful-stop-home-")
        case.addCleanup(shutil.rmtree, home, ignore_errors=True)
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("ALPHALENS_BROKER_")
        }
        env["HOME"] = home
        self.proc = subprocess.Popen(
            [sys.executable, "-c", _CHILD, mode, str(_TICK_S), str(_POLL_S), "1" if once else "0"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        # Registered before anything can fail, so a failed assertion never
        # leaves a child behind, provided the caller runs the cleanups in a
        # ``finally`` that also covers this constructor.
        case.addCleanup(self._reap)
        self.lines: list[str] = []
        # ``None`` is the end-of-stream sentinel the pump puts after the last line.
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._eof = False
        threading.Thread(target=self._pump, daemon=True).start()
        self.wait_for("BOOTED", timeout=_BOOT_TIMEOUT_S)

    def _pump(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self._queue.put(line.strip())
        self._queue.put(None)

    def wait_for(self, marker: str, *, timeout: float = _BOOT_TIMEOUT_S) -> None:
        end = time.monotonic() + timeout
        while marker not in self.lines:
            remaining = end - time.monotonic()
            if remaining <= 0:
                raise AssertionError(f"no {marker!r} within {timeout}s; got {self.lines}")
            try:
                line = self._queue.get(timeout=remaining)
            except queue.Empty:
                continue
            if line is None:
                self._eof = True
                raise AssertionError(f"child closed stdout before {marker!r}; got {self.lines}")
            self.lines.append(line)

    def signal(self, signum: int) -> float:
        sent = time.monotonic()
        self.proc.send_signal(signum)
        return sent

    def exit_after(self, sent: float, *, timeout: float) -> tuple[int | None, float]:
        """(exit status, seconds from ``sent`` to exit); status None on timeout."""
        try:
            status = self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            elapsed = time.monotonic() - sent
            self._drain()  # what it printed so far, for the failure message
            return None, elapsed
        elapsed = time.monotonic() - sent
        self._drain()
        return status, elapsed

    def _drain(self) -> None:
        """Collect every line up to end of stream, or for at most 2 s if the
        child is still alive. Waiting for the sentinel, not for an empty queue,
        so a line still in the pipe when the child exits is never missed."""
        deadline = time.monotonic() + 2.0
        while not self._eof and (remaining := deadline - time.monotonic()) > 0:
            try:
                line = self._queue.get(timeout=remaining)
            except queue.Empty:
                break
            if line is None:
                self._eof = True
            else:
                self.lines.append(line)

    def stderr_tail(self) -> str:
        if self.proc.poll() is None or self.proc.stderr is None:
            return ""
        return self.proc.stderr.read()[-2000:]

    def _reap(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait(timeout=10)
        for stream in (self.proc.stdout, self.proc.stderr):
            if stream is not None:
                stream.close()


class _GracefulCase(unittest.TestCase):
    """Runs one scenario per class in ``setUpClass``; each test checks one fact."""

    status: int | None
    elapsed: float
    lines: list[str]
    stderr: str

    @classmethod
    def _record(cls, child: _Child, status: int | None, elapsed: float) -> None:
        cls.status, cls.elapsed, cls.lines = status, elapsed, list(child.lines)
        cls.stderr = child.stderr_tail()

    def _context(self) -> str:
        return f"status={self.status} elapsed={self.elapsed:.2f}s lines={self.lines} stderr={self.stderr!r}"


def _run_mid_tick(case: type[_GracefulCase], mode: str, signum: int, *, once: bool) -> None:
    holder = unittest.TestCase()
    try:
        # Inside the try: a child that fails to boot is still reaped.
        child = _Child(holder, mode, once=once)
        child.wait_for("TICK-START 1")
        sent = child.signal(signum)
        status, elapsed = child.exit_after(sent, timeout=_TICK_S + _STOP_BOUND_S)
        case._record(child, status, elapsed)
    finally:
        holder.doCleanups()


def _run_while_sleeping(case: type[_GracefulCase], mode: str, signum: int) -> None:
    holder = unittest.TestCase()
    try:
        child = _Child(holder, mode)
        child.wait_for("POST-TICK")
        time.sleep(_INTO_SLEEP_S)
        sent = child.signal(signum)
        status, elapsed = child.exit_after(sent, timeout=_STOP_BOUND_S)
        case._record(child, status, elapsed)
    finally:
        holder.doCleanups()


class TestSigtermMidTick(_GracefulCase):
    """The decisive case: SIGTERM lands while the tick blocks in a broker call."""

    @classmethod
    def setUpClass(cls) -> None:
        _run_mid_tick(cls, "legacy", signal.SIGTERM, once=False)

    def test_the_process_exits_with_status_zero(self) -> None:
        self.assertEqual(self.status, 0, self._context())

    def test_the_tick_in_progress_runs_its_finally(self) -> None:
        self.assertIn("TICK-FINALLY 1", self.lines, self._context())

    def test_the_tick_in_progress_runs_to_completion(self) -> None:
        # A handler that RAISED would still run the finally; only one that
        # returns lets the interrupted call finish and the tick go on.
        self.assertIn("TICK-COMPLETED 1", self.lines, self._context())

    def test_no_further_tick_starts(self) -> None:
        self.assertNotIn("TICK-START 2", self.lines, self._context())

    def test_the_manage_finally_stops_the_stream_reader(self) -> None:
        self.assertIn("STREAM-STOPPED", self.lines, self._context())

    def test_it_exits_within_the_bound_after_the_tick(self) -> None:
        self.assertLess(self.elapsed, _TICK_S + _STOP_BOUND_S, self._context())


class TestSigintMidTick(_GracefulCase):
    """Ctrl-C in a terminal: the same graceful stop as systemd's SIGTERM."""

    @classmethod
    def setUpClass(cls) -> None:
        _run_mid_tick(cls, "legacy", signal.SIGINT, once=False)

    def test_the_process_exits_with_status_zero(self) -> None:
        self.assertEqual(self.status, 0, self._context())

    def test_the_tick_in_progress_runs_to_completion(self) -> None:
        self.assertIn("TICK-COMPLETED 1", self.lines, self._context())


class TestSigtermMidTickOnce(_GracefulCase):
    """``--once`` with a signal during its tick: finish the tick, return."""

    @classmethod
    def setUpClass(cls) -> None:
        _run_mid_tick(cls, "legacy", signal.SIGTERM, once=True)

    def test_the_process_exits_with_status_zero(self) -> None:
        self.assertEqual(self.status, 0, self._context())

    def test_the_tick_runs_to_completion(self) -> None:
        self.assertIn("TICK-COMPLETED 1", self.lines, self._context())


class TestSigtermBeforeTheOnceTick(_GracefulCase):
    """``--once`` with a signal after the handlers are in but before its tick:
    the one tick still runs, so "single tick complete" is never printed for a
    run that did no tick."""

    @classmethod
    def setUpClass(cls) -> None:
        holder = unittest.TestCase()
        try:
            child = _Child(holder, "slowboot", once=True)
            child.wait_for("PROBE-BUILD")
            time.sleep(0.3)
            sent = child.signal(signal.SIGTERM)
            status, elapsed = child.exit_after(sent, timeout=_TICK_S + _STOP_BOUND_S)
            cls._record(child, status, elapsed)
        finally:
            holder.doCleanups()

    def test_the_process_exits_with_status_zero(self) -> None:
        self.assertEqual(self.status, 0, self._context())

    def test_the_one_tick_runs_to_completion(self) -> None:
        self.assertIn("TICK-COMPLETED 1", self.lines, self._context())


class TestABootFailureLeavesNoChild(unittest.TestCase):
    """Each scenario helper reaps its child even when the child never boots."""

    def _assert_reaped(self, scenario: Any) -> None:
        spawned: list[subprocess.Popen[str]] = []
        real_popen = subprocess.Popen

        def _capture(*args: Any, **kwargs: Any) -> subprocess.Popen[str]:
            proc = real_popen(*args, **kwargs)
            spawned.append(proc)
            self.addCleanup(_kill_quietly, proc)
            return proc

        with (
            mock.patch.object(sys.modules[__name__], "_BOOT_TIMEOUT_S", 0.001),
            mock.patch.object(subprocess, "Popen", side_effect=_capture),
            self.assertRaises(AssertionError),
        ):
            scenario()
        self.assertEqual(len(spawned), 1)
        self.assertIsNotNone(spawned[0].poll(), "the child outlived its scenario")

    def test_mid_tick(self) -> None:
        self._assert_reaped(
            lambda: _run_mid_tick(_GracefulCase, "legacy", signal.SIGTERM, once=False)
        )

    def test_while_sleeping(self) -> None:
        self._assert_reaped(lambda: _run_while_sleeping(_GracefulCase, "legacy", signal.SIGTERM))

    def test_second_signal(self) -> None:
        self._assert_reaped(TestSecondSignalExitsAtOnce.setUpClass)

    def test_before_the_once_tick(self) -> None:
        self._assert_reaped(TestSigtermBeforeTheOnceTick.setUpClass)


def _kill_quietly(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is None:
        proc.kill()
    proc.wait(timeout=10)


class _SleepingCase(_GracefulCase):
    mode = ""

    @classmethod
    def setUpClass(cls) -> None:
        _run_while_sleeping(cls, cls.mode, signal.SIGTERM)

    def test_the_process_exits_with_status_zero(self) -> None:
        self.assertEqual(self.status, 0, self._context())

    def test_the_sleep_ends_within_the_bound(self) -> None:
        # PEP 475: a sleep RESUMES after a handler that returns, so a flag alone
        # would leave this at the rest of the 30 s poll.
        self.assertLess(self.elapsed, _STOP_BOUND_S, self._context())

    def test_no_second_tick_runs(self) -> None:
        self.assertNotIn("TICK-START 2", self.lines, self._context())


class TestSigtermWhileSleepingOnThePlainPoll(_SleepingCase):
    """The legacy ``sleep_fn(poll_seconds)`` path: no probe, no stream."""

    mode = "legacy"


class TestSigtermWhileSleepingOnTheFillProbe(_SleepingCase):
    """The #1402 fill-probe path, which LIVE runs: an armed entry rests, so the
    wait sleeps in 5 s chunks and reads the book between them."""

    mode = "probe"


class TestSigtermWhileSleepingOnTheStream(_SleepingCase):
    """The streaming path: ``wake_event.wait`` to the next grid point."""

    mode = "stream"


# The base holds the checks; only its three subclasses are cases to run.
del _SleepingCase


class TestSecondSignalExitsAtOnce(_GracefulCase):
    """A second signal restores the default action and re-delivers itself: the
    operator who presses Ctrl-C twice gets out without waiting for the tick."""

    @classmethod
    def setUpClass(cls) -> None:
        holder = unittest.TestCase()
        try:
            child = _Child(holder, "legacy")
            child.wait_for("TICK-START 1")
            child.signal(signal.SIGTERM)
            time.sleep(0.2)
            sent = child.signal(signal.SIGTERM)
            status, elapsed = child.exit_after(sent, timeout=_STOP_BOUND_S)
            cls._record(child, status, elapsed)
        finally:
            holder.doCleanups()

    def test_the_process_dies_of_the_signal(self) -> None:
        self.assertEqual(self.status, -signal.SIGTERM, self._context())

    def test_it_does_not_wait_for_the_tick(self) -> None:
        self.assertNotIn("TICK-COMPLETED 1", self.lines, self._context())


class TestGracefulStopHandler(unittest.TestCase):
    """The handler in isolation, called directly on the main thread."""

    def setUp(self) -> None:
        # Imported here, not at module top, so the subprocess cases above still
        # run (and show the red) on a tree that has no such module.
        from alphalens_pipeline.brokers.automanager.graceful_stop import GracefulStop

        self.wake = threading.Event()
        self.stop = GracefulStop(wake_event=self.wake)
        self.addCleanup(self.stop.close)

    def test_it_is_running_until_a_signal(self) -> None:
        self.assertTrue(self.stop.is_running())

    def test_the_first_signal_returns_and_stops_the_loop(self) -> None:
        self.stop.handle(signal.SIGTERM, None)
        self.assertFalse(self.stop.is_running())

    def test_a_sleep_after_the_signal_returns_at_once(self) -> None:
        self.stop.handle(signal.SIGTERM, None)
        started = time.monotonic()
        self.stop.sleep(_POLL_S)
        self.assertLess(time.monotonic() - started, _STOP_BOUND_S)

    def test_the_signal_wakes_the_stream_wait(self) -> None:
        self.stop.handle(signal.SIGTERM, None)
        self.assertTrue(self.wake.wait(_STOP_BOUND_S))

    def test_close_without_a_signal_does_not_wake_the_stream(self) -> None:
        self.stop.close()
        self.assertFalse(self.wake.wait(0.2))

    def test_close_ends_the_relay_thread(self) -> None:
        self.stop.close()
        self.assertFalse(self.stop._relay.is_alive())

    def test_the_signal_is_logged_by_name(self) -> None:
        with self.assertLogs(
            "alphalens_pipeline.brokers.automanager.graceful_stop", "INFO"
        ) as logs:
            self.stop.handle(signal.SIGTERM, None)
            self.assertTrue(self.wake.wait(_STOP_BOUND_S))  # the relay has logged by now
        self.assertIn("SIGTERM", logs.output[0])

    def test_a_negative_sleep_fails_loud_like_time_sleep(self) -> None:
        # Event.wait(-1) returns at once; a poll loop on it would spin.
        with self.assertRaises(ValueError):
            self.stop.sleep(-1.0)

    def test_a_nan_sleep_fails_loud_like_time_sleep(self) -> None:
        with self.assertRaises(ValueError):
            self.stop.sleep(float("nan"))


class TestFillProbeWaitStopsOnRequest(IsolatedHomeTestCase):
    """While a stop is requested the probe wait reads the book no more: each
    chunk's sleep returns at once, so without the guard it would spin on the
    broker until the 45 s deadline."""

    def test_no_book_read_after_the_stop(self) -> None:
        reads: list[int] = []
        running = {"value": True}
        clock = {"now": 1000.0}

        def _sleep(seconds: float) -> None:
            clock["now"] += 0.001  # the woken sleep returns almost at once
            running["value"] = False

        probe = cl.EntryFillProbe(
            armed_orders=lambda: {"o1": cl.ArmedEntryOrder(crid="crid-t0", uic=1)},
            list_open_orders=lambda: reads.append(1) or [],
            seen={},
            append=lambda record: None,
        )
        woke = cl._wait_for_entry_fill(
            probe,
            poll_seconds=45.0,
            sleep_fn=_sleep,
            monotonic=lambda: clock["now"],
            is_running=lambda: running["value"],
        )
        self.assertEqual((woke, reads), (False, []))


class TestManageRestoresThePreviousHandlers(IsolatedHomeTestCase):
    """A test (or any caller) that runs ``manage`` in-process must get its own
    SIGTERM/SIGINT handlers back."""

    def test_the_handlers_are_restored_after_the_command(self) -> None:
        from alphalens_cli.commands.broker import broker_app

        before = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
        deps: Any = mock.Mock(wake_event=None, stream_tick=None, stream_trigger=None)
        seen_during: dict[int, Any] = {}

        def _fake_run_daemon(*_a: Any, **_k: Any) -> None:
            seen_during.update({sig: signal.getsignal(sig) for sig in before})

        with (
            mock.patch("alphalens_cli.commands.broker._telegram_daemon_notify"),
            mock.patch("alphalens_cli.commands.broker._telegram_chain_loss_notify"),
            mock.patch.object(cl, "build_default_deps", return_value=deps),
            mock.patch.object(cl, "run_daemon", side_effect=_fake_run_daemon),
        ):
            result = CliRunner().invoke(broker_app, ["manage", "--once"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertNotEqual(seen_during, before)  # installed while the loop ran
        self.assertEqual({sig: signal.getsignal(sig) for sig in before}, before)

    def test_the_handlers_are_restored_when_the_stream_stop_raises(self) -> None:
        # The stream stop is best-effort cleanup on the way out and runs through
        # ``run_within`` (bounded, #1864 review): a failure there is LOGGED and the
        # command exits normally, rather than replacing the exit with a traceback.
        # What this pins is that the handlers are restored either way.
        from alphalens_cli.commands.broker import broker_app

        before = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
        trigger = mock.Mock()
        trigger.stop.side_effect = RuntimeError("stream stop failed")
        deps: Any = mock.Mock(wake_event=None, stream_tick=None, stream_trigger=trigger)

        with (
            mock.patch("alphalens_cli.commands.broker._telegram_daemon_notify"),
            mock.patch("alphalens_cli.commands.broker._telegram_chain_loss_notify"),
            mock.patch.object(cl, "build_default_deps", return_value=deps),
            mock.patch.object(cl, "run_daemon"),
            self.assertLogs(graceful_stop.logger, "WARNING") as logged,
        ):
            result = CliRunner().invoke(broker_app, ["manage", "--once"])

        self.assertIsNone(result.exception)
        self.assertIn("stream stop failed", "\n".join(logged.output))
        self.assertEqual({sig: signal.getsignal(sig) for sig in before}, before)


if __name__ == "__main__":
    unittest.main()
