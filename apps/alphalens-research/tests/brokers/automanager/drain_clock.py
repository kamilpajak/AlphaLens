"""Hold the placement drain's clock inside every fixture pick's window (#1734).

The drain expires an armed pick that is still unplaced when its validity
window ends (``spec.order_ttl_days`` sessions after ``meta.trade_date``), and
gives a late placement only what is left of the window. The placement tests
in these modules are about MECHANICS — routing, journaling, the money gates —
and their fixture picks are dated in 2026, so on the real clock most of them
would simply expire. Holding ``control_loop._utc_now`` before every fixture's
trade date keeps each pick inside its window; the window rules themselves are
tested against explicit clocks in ``test_capital_wait.py``.

Use it as a module fixture:

    from tests.brokers.automanager.drain_clock import hold_drain_clock

    def setUpModule() -> None:
        hold_drain_clock()

``tearDownModule`` is not needed: the patch is undone with ``unittest``'s
module cleanups.
"""

from __future__ import annotations

import datetime as dt
import unittest
from unittest import mock

from alphalens_pipeline.brokers.automanager import control_loop

# Earlier than every fixture trade date in the broker tests (the oldest is
# 2026-07-20), so `now < window_end` for every fixture pick.
INSIDE_EVERY_FIXTURE_WINDOW = dt.datetime(2026, 1, 2, 15, 0, tzinfo=dt.UTC)


def hold_drain_clock(at: dt.datetime = INSIDE_EVERY_FIXTURE_WINDOW) -> None:
    patcher = mock.patch.object(control_loop, "_utc_now", lambda: at)
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)
