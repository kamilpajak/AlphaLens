"""Fail any test that reaches the internet (#1179).

WHY THIS EXISTS. Five tests reached real vendors, by three unrelated routes:

* ``replay_population_ladders`` takes four fetch seams; two tests injected
  ``bar_fetch`` and left ``adjusted_closes_fetch`` on its default, which is
  yfinance — NO API KEY REQUIRED — so they hit Yahoo on every run;
* two tests put a PLACEHOLDER ``POLYGON_API_KEY`` in ``os.environ`` via
  ``patch.dict``, and ``get_default_polygon_client`` caches a PROCESS-WIDE
  singleton, so the placeholder-keyed client outlived the patch and served
  LATER, unrelated tests — whose calls came back 429 and slept 13 s per
  ``Retry-After``, for real;
* one test mocked ``pd.read_html`` but not the ``requests.get`` in front of it,
  so it fetched a Wikipedia page every run and discarded it.

The cost was not theoretical: minutes of backoff per run, and one run wedged
for 35 minutes in an SSL read that never returned. The suite's result depended
on vendor quota state rather than on the code.

WHY NOBODY NOTICED. The CI test step carries no secrets, so a vendor client
raises before a socket is opened — CI was protected by an ABSENT SECRET, an
accident of configuration rather than a boundary. Locally the keyless routes
ran regardless, and the placeholder-key route manufactured its own client.

WHAT IT DOES. Patches ``socket.socket.connect`` / ``connect_ex`` / ``sendto``
to refuse any address that is not loopback or AF_UNIX. Local transports stay
allowed on purpose: several tests exercise real code over them (the
price-reader server speaks AF_UNIX), and forcing those into fiction would trade
one blind spot for another.

COVERAGE, measured rather than assumed — ``socket.create_connection``,
``requests`` (urllib3), ``urllib.request``, ``asyncio``'s connector and
connectionless ``sendto`` all funnel through these primitives and are refused.

KNOWN LIMITS, both deliberate:

* name resolution — ``getaddrinfo`` resolves in C and never touches a Python
  socket, so a test CAN still perform a DNS lookup. That is a lookup, not a
  vendor call: no payload, no rate limit, no quota, and no response to wait on
  beyond the resolver's own timeout. Blocking it would also break the loopback
  check this guard depends on;
* a separate PROCESS — a test shelling out (``subprocess``, ``curl``) or a C
  extension opening its own socket never passes through Python's socket layer.
  Nothing in this suite does either; a future test that needs to would have to
  carry its own boundary.

ESCAPE HATCHES. The opt-in live probes (``tests/live/*`` and
``tests/thematic/test_gdelt_live``) exist to hit real vendors and are gated
behind ``*_LIVE_TEST`` flags; the guard stands down whenever any of those is
set. ``ALPHALENS_ALLOW_TEST_NETWORK=1`` is the explicit hatch for an ad-hoc run.

Installed from ``tests/__init__``, so it covers the whole research suite the
moment discovery imports the package.
"""

from __future__ import annotations

import ipaddress
import logging
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

logger = logging.getLogger(__name__)

# Flags already announced, so the warning fires once per process rather than
# on every socket call.
_ANNOUNCED: set[str] = set()


class LiveNetworkInTestError(RuntimeError):
    """A test tried to open a connection to something other than localhost."""


def network_guard_disabled() -> bool:
    """True when live traffic is sanctioned: any ``*_LIVE_TEST`` flag set to
    ``1``, or the explicit escape hatch. Read at CALL time so a test can flip
    it with ``mock.patch.dict``.

    The flag disarms the guard for the whole PROCESS, not just for the live
    tests — so a developer who EXPORTS one and then runs the full suite loses
    the boundary everywhere. That is bounded by how the probes are invoked
    (a per-command ``FLAG=1 python -m unittest tests.live.x`` prefix, and an
    exported flag also un-skips the probes themselves, which is loud and costs
    money on the paid ones) — but bounded is not silent, so standing down is
    announced once per process, naming the flag that did it."""
    if os.environ.get(_ALLOW_ENV) == "1":
        _announce_stand_down(_ALLOW_ENV)
        return True
    for name, value in os.environ.items():
        if name.endswith(_LIVE_FLAG_SUFFIX) and value == "1":
            _announce_stand_down(name)
            return True
    return False


def _announce_stand_down(flag: str) -> None:
    """Log the disarm ONCE per process (this runs on every socket call)."""
    if flag in _ANNOUNCED:
        return
    _ANNOUNCED.add(flag)
    logger.warning(
        "no-live-network guard STOOD DOWN for this process: %s=1. Live traffic is now "
        "permitted for EVERY test in this run, not only the opt-in probes.",
        flag,
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
    real_sendto = socket.socket.sendto

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

    def guarded_sendto(self: socket.socket, *args: Any) -> Any:
        """A datagram send carries its destination inline and never touches
        ``connect``, so a guard on the connect pair alone has a hole (measured:
        ``sendto`` to a public resolver went straight out). Signature is
        ``sendto(data, address)`` or ``sendto(data, flags, address)`` — the
        address is always LAST."""
        address = args[-1] if args else None
        if network_guard_disabled() or _is_local(address):
            return real_sendto(self, *args)
        _refuse(address)

    for wrapper in (guarded_connect, guarded_connect_ex, guarded_sendto):
        wrapper._alphalens_net_guard = True  # type: ignore[attr-defined]
    socket.socket.connect = guarded_connect  # type: ignore[method-assign]
    socket.socket.connect_ex = guarded_connect_ex  # type: ignore[method-assign]
    socket.socket.sendto = guarded_sendto  # type: ignore[method-assign]
