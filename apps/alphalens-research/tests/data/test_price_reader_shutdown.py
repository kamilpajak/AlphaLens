"""SIGTERM must actually stop the price reader (#1172).

This is a SUBPROCESS test on purpose. The failure it guards only exists when
``stop()`` is called ON the thread that is inside ``serve_forever`` — which is
exactly what a signal handler does and what no same-thread unit test can
reproduce. ``BaseServer.shutdown()`` sets a flag and then BLOCKS on an event
that only the serve loop sets when it exits; called from a handler that
interrupted that very loop, it waits for a loop that can never run again.

Measured before the fix: the process hung indefinitely after SIGTERM. systemd
would have had to SIGKILL the unit at its timeout, leaving the socket file and
the venue price subscription behind — precisely what the shutdown path exists
to prevent.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_PIPELINE = _REPO_ROOT / "apps" / "alphalens-pipeline"

_SCRIPT = textwrap.dedent(
    """
    import os, signal, sys, tempfile, threading
    from pathlib import Path

    sys.path.insert(0, {pipeline!r})
    from alphalens_pipeline.data.alt_data.price_reader_server import PriceReaderServer

    class _Stream:
        def get(self, uic): return None
        def drain_running_low(self, uic, *, consumer="default"): return None
        def reseed_running_low(self, uic, low, *, consumer="default"): return None
        def live_uic_for(self, ticker, *, exchange_mic): return None
        def ensure_subscribed(self, uics, *, scope="default"): return None
        def register_latch_consumer(self, consumer): return None
        def unregister_latch_consumer(self, consumer): return None
        def set_latch_uics(self, consumer, uics): return None

    path = Path(tempfile.mkdtemp()) / "reader.sock"
    server = PriceReaderServer(_Stream(), path, heartbeat_interval_s=3600)

    signal.signal(signal.SIGTERM, lambda *_: server.request_stop())
    threading.Timer(0.5, lambda: os.kill(os.getpid(), signal.SIGTERM)).start()

    server.serve_forever()
    print("CLEAN-EXIT", flush=True)
    print("SOCKET-GONE" if not path.exists() else "SOCKET-LEFT", flush=True)
    """
)


class TestSigtermStopsTheReader(unittest.TestCase):
    def test_a_signal_handler_can_stop_a_serving_reader(self) -> None:
        with tempfile.TemporaryDirectory():
            proc = subprocess.run(
                [sys.executable, "-c", _SCRIPT.format(pipeline=str(_PIPELINE))],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        self.assertEqual(
            proc.returncode, 0, f"stdout={proc.stdout!r} stderr={proc.stderr[-2000:]!r}"
        )
        self.assertIn("CLEAN-EXIT", proc.stdout)
        # The socket file must be gone: a leftover would make the next start
        # bind onto a stale path (and an operator wonder whether a reader is
        # still holding the elevated session).
        self.assertIn("SOCKET-GONE", proc.stdout)


if __name__ == "__main__":
    unittest.main()
