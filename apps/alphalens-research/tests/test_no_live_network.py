"""The suite must not talk to the internet (#1179).

Found by measurement, not by suspicion — five tests reached real vendors, by
three different routes:

* the split cross-check seam defaults to yfinance, which needs NO API key, so
  it went to Yahoo on every run;
* a test injected a PLACEHOLDER ``POLYGON_API_KEY``, and the client is a
  process-wide singleton, so the placeholder-keyed client outlived the
  ``patch.dict`` and served later tests — their calls came back 429 and the
  client slept 13 s per ``Retry-After``, for real;
* one test mocked ``pd.read_html`` but not the ``requests.get`` in front of it,
  so it fetched a Wikipedia page on every run and then threw it away.

One run wedged for 35 minutes in an SSL read that never returned.

CI never caught any of it because its test step carries no secrets: without a
key the vendor client raises before opening a socket. That is protection by
ABSENT SECRET, not a boundary. This guard is the boundary.
"""

from __future__ import annotations

import contextlib
import os
import socket
import threading
import unittest
from unittest import mock

from tests._net_guard import (
    LiveNetworkInTestError,
    install_network_guard,
    network_guard_disabled,
)


def _accept_and_close(server: socket.socket) -> None:
    """Accept one connection and close it, so the test leaves no dangling fd
    (a bare ``accept()`` in a thread drops the peer socket unclosed)."""
    with contextlib.suppress(OSError):
        conn, _ = server.accept()
        conn.close()


class TestGuardBlocksTheInternet(unittest.TestCase):
    def test_a_public_address_is_refused(self):
        """The positive control. Without it this whole file could pass while
        guarding nothing."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(sock.close)
        with self.assertRaises(LiveNetworkInTestError) as caught:
            sock.connect(("api.polygon.io", 443))
        self.assertIn("api.polygon.io", str(caught.exception))

    def test_connect_ex_is_guarded_too(self):
        """``connect_ex`` is a second door into the same room — a client using
        it would slip past a guard that only covers ``connect``."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(sock.close)
        with self.assertRaises(LiveNetworkInTestError):
            sock.connect_ex(("93.184.216.34", 80))


class TestGuardAllowsLocalTransports(unittest.TestCase):
    """Local sockets are how several tests exercise real code paths (the
    price-reader server speaks AF_UNIX); blocking those would force them into
    fiction."""

    def test_loopback_tcp_is_allowed(self):
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(server.close)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        threading.Thread(target=_accept_and_close, args=(server,), daemon=True).start()

        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(client.close)
        client.settimeout(5)
        client.connect(server.getsockname())  # must not raise

    def test_unix_sockets_are_allowed(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.sock"
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.addCleanup(server.close)
            server.bind(str(path))
            server.listen(1)
            threading.Thread(target=_accept_and_close, args=(server,), daemon=True).start()

            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.addCleanup(client.close)
            client.settimeout(5)
            client.connect(str(path))  # must not raise


class TestOptInLiveProbesAreExempt(unittest.TestCase):
    """``tests/live/`` and ``test_gdelt_live`` exist to hit real vendors and are
    gated behind ``*_LIVE_TEST`` flags. The guard must stand down for them, or
    the weekly probe job would fail on its own purpose."""

    def test_a_live_flag_disables_the_guard(self):
        with mock.patch.dict(os.environ, {"POLYGON_LIVE_TEST": "1"}, clear=False):
            self.assertTrue(network_guard_disabled())

    def test_an_unset_flag_leaves_the_guard_armed(self):
        with mock.patch.dict(os.environ, {"POLYGON_LIVE_TEST": ""}, clear=False):
            self.assertFalse(network_guard_disabled())

    def test_the_explicit_escape_hatch_disables_the_guard(self):
        with mock.patch.dict(os.environ, {"ALPHALENS_ALLOW_TEST_NETWORK": "1"}, clear=False):
            self.assertTrue(network_guard_disabled())


class TestGuardIsActuallyInstalled(unittest.TestCase):
    """Anti-rot: a guard nobody wires is a file, not a boundary. This asserts
    the suite's own ``tests/__init__`` armed it."""

    def test_the_running_suite_has_the_guard_installed(self):
        self.assertTrue(getattr(socket.socket.connect, "_alphalens_net_guard", False))

    def test_installing_twice_does_not_stack_wrappers(self):
        first = socket.socket.connect
        install_network_guard()
        self.assertIs(socket.socket.connect, first)


if __name__ == "__main__":
    unittest.main()
