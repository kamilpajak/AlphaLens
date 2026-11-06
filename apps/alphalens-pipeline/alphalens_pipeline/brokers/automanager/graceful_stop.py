"""A graceful stop for the broker daemon on SIGTERM / SIGINT.

systemd stops and restarts ``alphalens broker manage`` with SIGTERM. With no
handler CPython takes the default action and the process dies at whatever
instruction it was on: the tick's ``finally`` (which drains the operator pages
queued that tick) and the command's ``finally`` (which stops the stream reader)
never run. With this handler the FIRST signal only asks the loop to stop: the
tick in progress runs to completion, the wait between ticks ends at once, and
``run_daemon`` returns.

Why the handler sets one flag and nothing else. A Python handler runs on the
main thread between two bytecodes of whatever the main thread was doing,
including the middle of a placement. A handler that raised (``SystemExit``,
``KeyboardInterrupt``) would unwind from that instruction, which is exactly the
hazard a graceful stop exists to remove. So it never raises.

Why the handler cannot wake the wait itself. Every wait between ticks must end
promptly, and PEP 475 makes ``time.sleep`` and ``Event.wait`` RESUME after a
handler that returns. The waits therefore block on Events that something must
``set()``. The handler must not be that something: ``Event.set`` takes the
Event's internal lock, and the main thread HOLDS that lock for a few bytecodes
inside its own ``wait()`` and ``clear()`` on the same Event. A signal landing
there would make the handler wait for a lock its own thread holds, forever. So
the handler sets ``_requested``, an Event the main thread never waits on or
clears, and a small relay thread does the waking: it sets ``_interrupt`` (what
:meth:`GracefulStop.sleep` waits on) and the stream's ``wake_event``. The INFO
line is logged by the relay too, for the same reason: a write to a stream the
interrupted code was in the middle of writing can raise
``RuntimeError: reentrant call``.

A SECOND signal restores the default action and re-delivers itself, so the
process dies of it at once, as it did before this module existed. See
:meth:`GracefulStop.handle` for why that and not a raise.
"""

from __future__ import annotations

import logging
import math
import signal
import threading
from types import FrameType

logger = logging.getLogger(__name__)

__all__ = ["GracefulStop"]

_RELAY_THREAD_NAME = "graceful-stop-relay"


class GracefulStop:
    """The stop flag, the handler that raises it, and the waits it ends.

    ``wake_event`` is the streaming path's early-wake Event (``None`` when the
    daemon runs poll-only); the relay sets it so ``wake_event.wait`` ends too.
    Pass :meth:`handle` to ``signal.signal``, :meth:`is_running` and
    :meth:`sleep` to ``run_daemon``, and call :meth:`close` when the loop is
    done and the handlers are restored."""

    def __init__(self, *, wake_event: threading.Event | None = None) -> None:
        self._wake_event = wake_event
        # Set by the handler only. The main thread never waits on it and never
        # clears it, so the handler's set() cannot meet its own thread's lock.
        self._requested = threading.Event()
        # Set by the relay only; the main thread sleeps on it.
        self._interrupt = threading.Event()
        # Plain attributes written only by the handler, on the main thread.
        self._signalled = False
        self._signum: int | None = None
        self._closed = False
        self._relay = threading.Thread(target=self._relay_run, name=_RELAY_THREAD_NAME, daemon=True)
        self._relay.start()

    def handle(self, signum: int, _frame: FrameType | None) -> None:
        """The signal handler. Never raises and never blocks on the main thread.

        A second signal re-delivers itself under the default action instead of
        raising: systemd sends one SIGTERM and a SIGKILL 90 s later, so only a
        human sends a second one, and a human who presses Ctrl-C twice wants
        out NOW, not after the tick. The default action gives exactly that, the
        same death the daemon had before the first handler existed, with an
        honest status (killed by the signal: 130 or 143 in a shell, which
        ``Restart=on-failure`` treats as a clean stop). A raise would instead
        unwind from the interrupted instruction and run every ``finally`` and
        ``except BaseException`` on the way out in a half-done state — the
        hazard the first signal is careful to avoid."""
        if self._signalled:
            signal.signal(signum, signal.SIG_DFL)
            signal.raise_signal(signum)
            return
        # Marked BEFORE the set(): a second signal that lands inside set() takes
        # the branch above, so the handler never re-enters set() on one lock.
        # No test can pin this order: the window is a few bytecodes wide.
        self._signalled = True
        self._signum = signum
        self._requested.set()

    def is_running(self) -> bool:
        """False once a signal asked for the stop (``run_daemon``'s loop check)."""
        return not self._requested.is_set()

    def sleep(self, seconds: float) -> None:
        """The wait between ticks: ``seconds``, or less when a stop is requested.

        Refuses a negative or NaN length as ``time.sleep`` does. ``Event.wait``
        returns at once for those, which would turn a mistyped ``--poll-seconds``
        into a loop that ticks with no pause instead of a loud failure."""
        if math.isnan(seconds) or seconds < 0:
            raise ValueError(f"sleep length must be a non-negative number, got {seconds!r}")
        self._interrupt.wait(seconds)

    def close(self) -> None:
        """End the relay thread. Call it after the handlers are restored."""
        self._closed = True
        self._requested.set()
        self._relay.join(timeout=1.0)

    def _relay_run(self) -> None:
        self._requested.wait()
        if self._closed:
            return
        name = signal.Signals(self._signum).name if self._signum is not None else "signal"
        logger.info(
            "broker manage: %s received, stopping (a tick in progress runs to "
            "completion; a second signal exits at once)",
            name,
        )
        self._interrupt.set()
        if self._wake_event is not None:
            self._wake_event.set()
