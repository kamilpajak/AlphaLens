"""A stop of ``broker manage`` delivers the pages its last tick queued.

The stop-fill page is QUEUED by the stop-fill pass and sent by the drain in the
``finally`` of ``run_once``, after the protection pass. The pass journals its
``stop_filled`` latch BEFORE it queues the page, so no later tick pages that
fill again: a page still in the queue when the process dies is lost for good.
Without a signal handler a ``systemctl restart`` (SIGTERM) killed the process
mid-tick and the ``finally`` never ran; a Ctrl-C (SIGINT) raised
``KeyboardInterrupt`` inside the drain and took every page after it. #1864
installed ``GracefulStop``: the first signal only asks the loop to stop, and the
tick in progress runs to completion, drain included.

SUBPROCESS tests, like ``test_manage_graceful_stop.py``: what they guard is a
real signal delivered to a real process. Unlike that file, nothing in the tick
is faked: the child runs the REAL ``manage`` command, the REAL ``run_daemon``,
the REAL ``run_once`` and the REAL stop-fill pass, against a fake broker and an
isolated ``$HOME``. Only the protection view (which blocks, the way a broker
read blocks) and the page sink (which writes the page to a file) are stand-ins.
Each scenario then starts the command a second time with ``--once`` on the same
home: the latch is down, so that start must page nothing.
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
from pathlib import Path

from tests.brokers.automanager.trades_fixture import SIM_RHI_DIR

# The blocking step lasts this long, so a signal sent at its start lands inside it.
_BLOCK_S = 1.5
# Boot, and the whole second start, finish well inside this on a loaded runner.
_START_TIMEOUT_S = 30.0
# Signal to exit: the rest of the blocking step plus the drain. Measured at 1.6 s.
_STOP_BOUND_S = _BLOCK_S + 5.0
_PAGES_FILE = "pages.txt"

# The two fills the child seeds, in the order the stop-fill pass walks them
# (uic 641 before 9999). The first is the real RHI:2026-09-03 bracket stop.
_RHI_PAGE = (
    "stop-fill:5040004072\t"
    "SELL RHI SL 1796 @ 40.46 - plan stop - position closed (order 5040004072)"
)
_SECOND_PAGE = (
    "stop-fill:S-9999\tSELL uic 9999 SL 10 @ 5.00 - plan stop - position closed (order S-9999)"
)

_CHILD = textwrap.dedent(
    """
    import json, shutil, sys, time
    from pathlib import Path
    from unittest import mock

    seed, once, submissions_src, block_in, block_s = (
        sys.argv[1] == "1", sys.argv[2] == "1", sys.argv[3], sys.argv[4], float(sys.argv[5])
    )

    from alphalens_cli.commands import broker as cli
    from alphalens_pipeline.brokers.automanager import control_loop as cl
    from alphalens_pipeline.brokers.automanager import state_paths
    from alphalens_pipeline.brokers.automanager import stop_journal as sj
    from alphalens_pipeline.brokers.automanager.position_manager import ProtectionView
    from broker_contract.contract import OrderState, OrderStatus

    def say(line):
        print(line, flush=True)

    # order id -> (uic, filled qty, avg price, ref)
    STOPS = {
        "5040004072": (641, 1796.0, 40.46, "d1b6f68d-e98c-4a65-a541-74eac07b655d-stop-0"),
        "S-9999": (9999, 10.0, 5.0, None),
    }

    if seed:
        submissions = state_paths.submissions_path()
        submissions.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(submissions_src, submissions)
        journal = sj._standalone_stop_journal_path()
        journal.parent.mkdir(parents=True, exist_ok=True)
        with journal.open("a", encoding="utf-8") as fh:
            for order_id, (uic, qty, _price, ref) in STOPS.items():
                line = {"kind": "stop_placed", "order_id": order_id, "qty": qty,
                        "ref": ref, "ts": 1788442349.0, "uic": uic}
                fh.write(json.dumps(line) + "\\n")

    class Broker:
        name = "fake"

        def get_order(self, order_id):  # gone from the book
            return OrderState(order_id=order_id, status=OrderStatus.UNKNOWN, instrument=None,
                              filled_quantity=0.0, raw_status="Unknown")

        def resolve_order_outcome(self, order_id):  # the audit log says FILLED
            _uic, qty, price, _ref = STOPS[order_id]
            return OrderState(order_id=order_id, status=OrderStatus.FILLED, instrument=None,
                              filled_quantity=qty, raw_status="Filled", avg_fill_price=price)

    pages = Path.home() / "pages.txt"
    sent = {"n": 0}

    def sink(message, reason):
        sent["n"] += 1
        say(f"SINK-START {sent['n']}")
        if block_in == "sink" and sent["n"] == 1:
            time.sleep(block_s)  # the first page hangs, as a Telegram POST does
        with pages.open("a", encoding="utf-8") as fh:
            fh.write(f"{reason}\\t{message}\\n")
        return True

    def protection_view(_broker, _records):
        say("PROTECTION-START")
        if block_in == "protection":
            time.sleep(block_s)  # stands in for the broker reads of the pass
        return ProtectionView(long_positions={}, all_positions={}, sell_legs_by_uic={},
                              planned_by_uic={}, oco_unsupported=frozenset())

    deps = cl.LoopDeps(
        broker=Broker(),
        kill_file=Path("/nonexistent/KILL"),
        ensure_alive=lambda: type("C", (), {"alive": True, "reason": None})(),
        iter_picks=lambda: iter([]),
        place_pick=lambda pick: True,
        read_records=list,
        verdicts_fn=lambda records, broker: [],
        build_position_view=lambda broker, records: None,
        build_protection_view=protection_view,
        execute_protection=lambda action, kill, report: None,
        sweep_orphans_fn=lambda broker: [],
        alert=lambda message: None,
        alert_throttled=sink,
    )

    argv = ["manage", "--poll-seconds", "30"] + (["--once"] if once else [])
    with (
        mock.patch.object(cli, "_telegram_daemon_notify", return_value=lambda m: None),
        mock.patch.object(cli, "_telegram_chain_loss_notify", return_value=lambda m: None),
        mock.patch.object(cl, "build_default_deps", side_effect=lambda **_: deps),
        mock.patch.object(cl, "build_entry_fill_probe", return_value=None),
    ):
        say("BOOTED")
        cli.broker_app(argv, prog_name="alphalens broker")
    """
)


class _Start:
    """One spawned ``manage`` process on ``home``, its stdout read on a thread."""

    def __init__(self, home: Path, *, seed: bool, once: bool, block_in: str) -> None:
        env = {k: v for k, v in os.environ.items() if not k.startswith("ALPHALENS_BROKER_")}
        env["HOME"] = str(home)
        argv = [
            sys.executable,
            "-c",
            _CHILD,
            "1" if seed else "0",
            "1" if once else "0",
            str(SIM_RHI_DIR / "submissions.jsonl"),
            block_in,
            str(_BLOCK_S),
        ]
        self.proc = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env
        )
        self.lines: list[str] = []
        self._queue: queue.Queue[str | None] = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self._queue.put(line.strip())
        self._queue.put(None)

    def wait_for(self, marker: str) -> None:
        end = time.monotonic() + _START_TIMEOUT_S
        while marker not in self.lines:
            try:
                line = self._queue.get(timeout=max(0.01, end - time.monotonic()))
            except queue.Empty:
                raise AssertionError(f"no {marker!r}; got {self.lines}") from None
            if line is None:
                raise AssertionError(f"child closed stdout before {marker!r}; got {self.lines}")
            self.lines.append(line)

    def wait(self, timeout: float) -> int | None:
        try:
            return self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    def stderr_tail(self) -> str:
        if self.proc.poll() is None or self.proc.stderr is None:
            return ""
        return self.proc.stderr.read()[-2000:]

    def reap(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait(timeout=10)
        for stream in (self.proc.stdout, self.proc.stderr):
            if stream is not None:
                stream.close()


def _pages(home: Path) -> list[str]:
    path = home / _PAGES_FILE
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


class _StopScenario(unittest.TestCase):
    """One scenario per class, run in ``setUpClass``; each test checks one fact."""

    signum: signal.Signals
    marker: str
    block_in: str

    status: int | None
    pages_after_stop: list[str]
    pages_after_restart: list[str]
    restart_status: int | None
    context: str

    @classmethod
    def setUpClass(cls) -> None:
        home = Path(tempfile.mkdtemp(prefix="manage-stop-pages-"))
        cls.addClassCleanup(shutil.rmtree, home, ignore_errors=True)
        first: _Start | None = None
        second: _Start | None = None
        try:
            first = _Start(home, seed=True, once=False, block_in=cls.block_in)
            first.wait_for(cls.marker)
            first.proc.send_signal(cls.signum)
            cls.status = first.wait(_STOP_BOUND_S)
            cls.pages_after_stop = _pages(home)
            second = _Start(home, seed=False, once=True, block_in="none")
            cls.restart_status = second.wait(_START_TIMEOUT_S)
            cls.pages_after_restart = _pages(home)
            cls.context = f"lines={first.lines} stderr={first.stderr_tail()!r}"
        finally:
            for start in (first, second):
                if start is not None:
                    start.reap()

    def test_the_process_exits_with_status_zero(self) -> None:
        self.assertEqual(self.status, 0, self.context)

    def test_every_queued_page_is_delivered(self) -> None:
        self.assertEqual(self.pages_after_stop, [_RHI_PAGE, _SECOND_PAGE], self.context)

    def test_the_next_start_pages_nothing_again(self) -> None:
        self.assertEqual(self.restart_status, 0)
        self.assertEqual(self.pages_after_restart, self.pages_after_stop)


class TestSigtermDuringTheProtectionPass(_StopScenario):
    """The decisive case: ``systemctl restart`` while the pages wait in the queue.
    Measured with the handler not installed: status -15 and no page, ever."""

    signum = signal.SIGTERM
    marker = "PROTECTION-START"
    block_in = "protection"


class TestSigintDuringTheProtectionPass(_StopScenario):
    signum = signal.SIGINT
    marker = "PROTECTION-START"
    block_in = "protection"


class TestSigtermWhileAnEarlierPageIsBeingSent(_StopScenario):
    """The signal lands inside ``_drain_pages`` while the first page's POST hangs."""

    signum = signal.SIGTERM
    marker = "SINK-START 1"
    block_in = "sink"


class TestSigintWhileAnEarlierPageIsBeingSent(_StopScenario):
    """Before #1864 a ``KeyboardInterrupt`` here lost BOTH pages: the one being
    sent and the one queued behind it. Measured with the handler not installed:
    status 130 and no page."""

    signum = signal.SIGINT
    marker = "SINK-START 1"
    block_in = "sink"


# The base holds the checks; only its subclasses are cases to run.
del _StopScenario


if __name__ == "__main__":
    unittest.main()
