"""Fail any test that reaches the internet (#1179).

WHY THIS EXISTS. Four tests were calling the real Polygon API through a seam
they forgot to inject (``replay_population_ladders(grouped_fetch=...)``). The
cost was not theoretical: on the free tier those calls return 429, the client
sleeps 13 s per ``Retry-After`` for real, and one run wedged for 35 minutes in
an SSL read that never returned. The suite's result depended on vendor quota
state rather than on the code.

WHY NOBODY NOTICED. The CI test step carries no secrets, so
``PolygonClient.from_env()`` raises before a socket is opened. CI was protected
by an ABSENT SECRET — an accident of configuration, not a boundary. Any machine
with ``POLYGON_API_KEY`` exported (every developer machine, per ``.env``) ran
the calls for real.

WHAT IT DOES. Patches ``socket.socket.connect`` / ``connect_ex`` to refuse any
address that is not loopback or AF_UNIX. Local transports stay allowed on
purpose: several tests exercise real code over them (the price-reader server
speaks AF_UNIX), and forcing those into fiction would trade one blind spot for
another.

ESCAPE HATCHES. The opt-in live probes (``tests/live/*`` and
``tests/thematic/test_gdelt_live``) exist to hit real vendors and are gated
behind ``*_LIVE_TEST`` flags; the guard stands down whenever any of those is
set. ``ALPHALENS_ALLOW_TEST_NETWORK=1`` is the explicit hatch for an ad-hoc run.

Installed from ``tests/__init__``, so it covers the whole research suite the
moment discovery imports the package.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from typing import Any

# The env flag every opt-in live probe already uses (SEC_LIVE_TEST,
# POLYGON_LIVE_TEST, GDELT_LIVE_TEST, ...). Matching the SUFFIX rather than a
# hard-coded list means a new probe cannot forget to register itself here.
_LIVE_FLAG_SUFFIX = "_LIVE_TEST"

# Explicit, documented hatch for an ad-hoc local run against a real vendor.
_ALLOW_ENV = "ALPHALENS_ALLOW_TEST_NETWORK"

_GUARD_MARKER = "_alphalens_net_guard"


class LiveNetworkInTestError(RuntimeError):
    """A test tried to open a connection to something other than localhost."""


def network_guard_disabled() -> bool:
    """True when live traffic is sanctioned: any ``*_LIVE_TEST`` flag set to
    ``1``, or the explicit escape hatch. Read at CALL time so a test can flip
    it with ``mock.patch.dict``."""
    if os.environ.get(_ALLOW_ENV) == "1":
        return True
    return any(
        name.endswith(_LIVE_FLAG_SUFFIX) and value == "1" for name, value in os.environ.items()
    )


def _is_local(address: Any) -> bool:
    """AF_UNIX (a path) and loopback are local. Anything undecidable is treated
    as REMOTE — a guard that guesses 'probably fine' is not a guard."""
    if isinstance(address, (str, bytes, os.PathLike)):
        return True  # AF_UNIX path
    if not isinstance(address, tuple) or not address:
        return False
    host = address[0]
    if not isinstance(host, str):
        return False
    if host in {"localhost", ""}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        # A hostname that is not literally 'localhost' would need DNS to
        # resolve — which is exactly the traffic being refused.
        return False


def install_network_guard() -> None:
    """Arm the guard. Idempotent: re-installing must not stack wrappers (the
    suite imports ``tests`` once, but a test may call this to assert the
    contract)."""
    if getattr(socket.socket.connect, _GUARD_MARKER, False):
        return

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def _refuse(address: Any) -> None:
        raise LiveNetworkInTestError(
            f"a test tried to reach {address!r}. Tests must not touch the network: inject the "
            "vendor client or its fetch seam (see #1179). To run a real live probe, set its "
            f"*{_LIVE_FLAG_SUFFIX} flag, or {_ALLOW_ENV}=1 for an ad-hoc run."
        )

    def guarded_connect(self: socket.socket, address: Any) -> Any:
        if network_guard_disabled() or _is_local(address):
            return real_connect(self, address)
        _refuse(address)

    def guarded_connect_ex(self: socket.socket, address: Any) -> Any:
        if network_guard_disabled() or _is_local(address):
            return real_connect_ex(self, address)
        _refuse(address)

    guarded_connect._alphalens_net_guard = True  # type: ignore[attr-defined]
    guarded_connect_ex._alphalens_net_guard = True  # type: ignore[attr-defined]
    socket.socket.connect = guarded_connect  # type: ignore[method-assign]
    socket.socket.connect_ex = guarded_connect_ex  # type: ignore[method-assign]
