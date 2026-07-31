"""The earnings-window gate refuses to arm entries that would rest across a
known earnings date (overnight-drift memo 2026-07-29, exposure J).

A resting pullback BUY limit held over an AMC/BMO earnings release is the
adverse-selection channel (Linnainmaa 2010): the limit is most likely to fill
exactly when the report gaps the stock down through it, with T-1-frozen
geometry at its most wrong. The gate refuses arming when the ticker's next
CONFIRMED earnings date falls inside the entry's GoodTillDate window; unknown
dates FAIL OPEN (the gate is an enhancement, never an availability rail).

Relocated from the daemon (``brokers.automanager.earnings_gate``) to the CLI
client (``alphalens_cli.commands._earnings_window``) per the broker-manager
extraction memo Revision R2 — the gate now runs once, at `arm` time, not
per-tick in the daemon. See `test_arm_cli.py` for the arm-time wiring tests.
"""

from __future__ import annotations

import datetime as dt
import os
import unittest
from unittest import mock

from alphalens_cli.commands import _earnings_window

_TODAY = dt.date(2026, 7, 29)  # Wednesday; 7 XNYS sessions end 2026-08-07


def _lookup_returning(value):
    calls = []

    def _lookup(*, ticker, asof):
        calls.append((ticker, asof))
        return value

    _lookup.calls = calls
    return _lookup


class TestEarningsWindowRefusal(unittest.TestCase):
    def setUp(self):
        _earnings_window._clear_lookup_cache_for_tests()
        # Hermetic: the opt-out env must not leak in from the host environment.
        self._env = mock.patch.dict(os.environ, {}, clear=False)
        self._env.start()
        os.environ.pop(_earnings_window.EARNINGS_GATE_OPT_OUT_ENV, None)

    def tearDown(self):
        self._env.stop()

    def test_earnings_inside_window_refuses_with_the_date(self):
        reason = _earnings_window.earnings_window_refusal(
            "KTOS", ttl_days=7, today=_TODAY, lookup=_lookup_returning(dt.date(2026, 8, 4))
        )
        self.assertIsNotNone(reason)
        self.assertIn("2026-08-04", reason)
        self.assertIn("earnings", reason)

    def test_earnings_after_window_end_allows(self):
        reason = _earnings_window.earnings_window_refusal(
            "AVAV", ttl_days=7, today=_TODAY, lookup=_lookup_returning(dt.date(2026, 9, 2))
        )
        self.assertIsNone(reason)

    def test_earnings_today_refuses(self):
        # BMO release this very morning is exactly the gap we must not rest across.
        reason = _earnings_window.earnings_window_refusal(
            "KBR", ttl_days=7, today=_TODAY, lookup=_lookup_returning(_TODAY)
        )
        self.assertIsNotNone(reason)

    def test_earnings_exactly_at_window_end_refuses(self):
        # The GTD order is live THROUGH the expiry session, so an earnings date
        # equal to the window end still rests across the release.
        window_end = _earnings_window._window_end(_TODAY, 7)
        reason = _earnings_window.earnings_window_refusal(
            "FCN", ttl_days=7, today=_TODAY, lookup=_lookup_returning(window_end)
        )
        self.assertIsNotNone(reason)

    def test_unknown_earnings_fails_open(self):
        reason = _earnings_window.earnings_window_refusal(
            "VRNS", ttl_days=7, today=_TODAY, lookup=_lookup_returning(None)
        )
        self.assertIsNone(reason)

    def test_lookup_exception_fails_open(self):
        def _boom(*, ticker, asof):
            raise RuntimeError("yfinance down")

        reason = _earnings_window.earnings_window_refusal(
            "WK", ttl_days=7, today=_TODAY, lookup=_boom
        )
        self.assertIsNone(reason)

    def test_env_opt_out_skips_the_gate_without_lookup(self):
        lookup = _lookup_returning(dt.date(2026, 7, 30))
        with mock.patch.dict(os.environ, {_earnings_window.EARNINGS_GATE_OPT_OUT_ENV: "1"}):
            reason = _earnings_window.earnings_window_refusal(
                "HTGC", ttl_days=7, today=_TODAY, lookup=lookup
            )
        self.assertIsNone(reason)
        self.assertEqual(lookup.calls, [])  # gate never even asked

    def test_default_lookup_is_cached_per_ticker_and_day(self):
        # A single arm invocation should not double-pay a network call if the
        # gate is ever consulted twice for the same (ticker, day).
        with mock.patch.object(
            _earnings_window, "_fetch_next_earnings", return_value=dt.date(2026, 8, 4)
        ) as fetch:
            first = _earnings_window.earnings_window_refusal("KTOS", ttl_days=7, today=_TODAY)
            second = _earnings_window.earnings_window_refusal("KTOS", ttl_days=7, today=_TODAY)
        self.assertIsNotNone(first)
        self.assertEqual(first, second)
        self.assertEqual(fetch.call_count, 1)


if __name__ == "__main__":
    unittest.main()
