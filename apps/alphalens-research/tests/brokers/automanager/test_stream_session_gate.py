"""Daemon wiring for the price stream's outside-market-hours session gate.

The stream side (``SaxoPriceStream(session_window=...)``) is fail-open by
contract and covered in ``tests/data/test_saxo_price_stream.py``; these tests
pin the composition-root half in ``control_loop``:

* the predicate is built ONLY when ``ALPHALENS_SAXO_STREAM_SESSION_GATE`` is
  exactly ``"1"`` (default unset -> None -> today's behavior);
* the window is ``[session_open - WARMUP, session_close + GRACE]`` off the
  exchange-parametrized calendar helpers (real holidays, early closes, DST) —
  never hand-rolled hours;
* a calendar exception PROPAGATES out of the predicate (the stream side fails
  OPEN on a raise; swallowing into False here would silence the stream during
  trading hours).

Every window-math assertion drives an INJECTED clock — never a real ``now()``
(the partial-mock date time-bomb doctrine).
"""

from __future__ import annotations

import datetime as dt
import unittest
from collections.abc import Mapping
from typing import Any
from unittest import mock

from alphalens_pipeline.brokers.automanager import control_loop as cl

_GATE_ENV = "ALPHALENS_SAXO_STREAM_SESSION_GATE"

# XNYS regular session on Tuesday 2026-08-18 (EDT): 13:30-20:00 UTC.
_SESSION_OPEN = dt.datetime(2026, 8, 18, 13, 30, tzinfo=dt.UTC)
_SESSION_CLOSE = dt.datetime(2026, 8, 18, 20, 0, tzinfo=dt.UTC)


class _SettableClock:
    """Deterministic injected clock — window math must never read real now()."""

    def __init__(self, now: dt.datetime) -> None:
        self.now = now

    def __call__(self) -> dt.datetime:
        return self.now


class TestSessionGateEnvFlag(unittest.TestCase):
    def test_env_unset_yields_none_so_todays_behavior_is_kept(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(cl._stream_session_window_if_enabled())

    def test_gate_builds_a_predicate_only_for_exactly_one(self):
        for value, expect_predicate in (("1", True), ("0", False), ("true", False), ("", False)):
            with self.subTest(value=value):
                with mock.patch.dict("os.environ", {_GATE_ENV: value}, clear=True):
                    predicate = cl._stream_session_window_if_enabled()
                if expect_predicate:
                    self.assertTrue(callable(predicate))
                else:
                    self.assertIsNone(predicate)


class TestSessionWindowMath(unittest.TestCase):
    """Fixed injected clock against a known XNYS session: WARMUP=15min before
    the open (the connection must be up and the create-subscription snapshot —
    the only carrier of DelayedByMinutes — applied BEFORE the open), GRACE=
    10min after the close."""

    def setUp(self) -> None:
        self.clock = _SettableClock(_SESSION_OPEN)
        self.predicate = cl._make_stream_session_window(clock=self.clock)

    def _at(self, moment: dt.datetime) -> bool:
        self.clock.now = moment
        return self.predicate()

    def test_out_at_sixteen_minutes_before_the_open(self):
        self.assertFalse(self._at(_SESSION_OPEN - dt.timedelta(minutes=16)))

    def test_in_at_fourteen_minutes_before_the_open(self):
        self.assertTrue(self._at(_SESSION_OPEN - dt.timedelta(minutes=14)))

    def test_in_at_nine_minutes_after_the_close(self):
        self.assertTrue(self._at(_SESSION_CLOSE + dt.timedelta(minutes=9)))

    def test_out_at_eleven_minutes_after_the_close(self):
        self.assertFalse(self._at(_SESSION_CLOSE + dt.timedelta(minutes=11)))

    def test_out_all_day_on_a_weekend(self):
        # Saturday 2026-08-22, mid-US-session hour — still out.
        self.assertFalse(self._at(dt.datetime(2026, 8, 22, 15, 0, tzinfo=dt.UTC)))

    def test_early_close_comes_from_the_calendar_not_a_fixed_2000(self):
        """Friday 2026-11-27 (day after Thanksgiving) is an XNYS half day
        closing 13:00 ET = 18:00 UTC. A hand-rolled fixed 20:00 close would
        wrongly report in-session at 19:00 UTC."""
        early_close = dt.datetime(2026, 11, 27, 18, 0, tzinfo=dt.UTC)
        self.assertTrue(self._at(early_close + dt.timedelta(minutes=9)))
        self.assertFalse(self._at(early_close + dt.timedelta(minutes=11)))
        self.assertFalse(self._at(dt.datetime(2026, 11, 27, 19, 0, tzinfo=dt.UTC)))

    def test_a_calendar_exception_propagates_out_of_the_predicate(self):
        """Do NOT swallow into False: the stream side fails OPEN on a raise
        (connect + one warning). A False here would silence the stream during
        trading hours over a calendar bug — the exact hazard the fail-open
        contract exists to prevent."""
        self.clock.now = _SESSION_OPEN
        with mock.patch(
            "alphalens_pipeline.paper.calendar.is_trading_day",
            side_effect=RuntimeError("calendar exploded"),
        ):
            with self.assertRaises(RuntimeError):
                self.predicate()


class TestMultiVenueSessionWindow(unittest.TestCase):
    """#1238 PR 5: the window is the per-day HULL over the venues named in
    ``ALPHALENS_SAXO_STREAM_SESSION_VENUES`` (unset -> ("XNYS",) -> byte-
    identical). Holiday-aware per venue; no venue trading -> False all day; an
    unknown venue RAISES out of the predicate (the stream side fails open, the
    one contract this gate must never break). Injected clock throughout."""

    _VENUES_ENV = "ALPHALENS_SAXO_STREAM_SESSION_VENUES"

    def _in_window(self, venues_env: str | None, now: dt.datetime) -> bool:
        env = {} if venues_env is None else {self._VENUES_ENV: venues_env}
        with mock.patch.dict("os.environ", env, clear=True):
            predicate = cl._make_stream_session_window(_SettableClock(now))
            return predicate()

    def test_env_unset_keeps_the_xnys_window(self):
        before_open = _SESSION_OPEN - dt.timedelta(minutes=16)
        self.assertFalse(self._in_window(None, before_open))
        self.assertTrue(self._in_window(None, _SESSION_OPEN + dt.timedelta(hours=1)))

    def test_xnys_xwar_hull_spans_gpw_warmup_to_us_grace(self):
        # Tue 2026-08-18: XWAR 07:00-15:00 UTC (CEST), XNYS 13:30-20:00 UTC.
        # Hull = [06:45, 20:10].
        day = dt.date(2026, 8, 18)
        cases = (
            (dt.datetime(2026, 8, 18, 6, 44, tzinfo=dt.UTC), False),
            (dt.datetime(2026, 8, 18, 6, 46, tzinfo=dt.UTC), True),
            (
                dt.datetime(2026, 8, 18, 12, 0, tzinfo=dt.UTC),
                True,
            ),  # between sessions: hull, not union
            (dt.datetime(2026, 8, 18, 20, 9, tzinfo=dt.UTC), True),
            (dt.datetime(2026, 8, 18, 20, 11, tzinfo=dt.UTC), False),
        )
        for now, expected in cases:
            with self.subTest(now=now):
                self.assertEqual(self._in_window("XNYS,XWAR", now), expected)

    def test_gpw_holiday_falls_back_to_the_xnys_bounds(self):
        # 2026-01-06 (Epiphany): XWAR closed, XNYS trades 14:30-21:00 UTC.
        self.assertFalse(self._in_window("XNYS,XWAR", dt.datetime(2026, 1, 6, 9, 0, tzinfo=dt.UTC)))
        self.assertTrue(self._in_window("XNYS,XWAR", dt.datetime(2026, 1, 6, 15, 0, tzinfo=dt.UTC)))

    def test_us_holiday_falls_back_to_the_xwar_bounds(self):
        # 2026-07-03 (Independence Day observed): XNYS closed, XWAR trades
        # 07:00-15:00 UTC.
        self.assertTrue(self._in_window("XNYS,XWAR", dt.datetime(2026, 7, 3, 8, 0, tzinfo=dt.UTC)))
        self.assertFalse(
            self._in_window("XNYS,XWAR", dt.datetime(2026, 7, 3, 18, 0, tzinfo=dt.UTC))
        )

    def test_no_venue_trading_is_false_all_day(self):
        self.assertFalse(
            self._in_window("XNYS,XWAR", dt.datetime(2026, 8, 16, 12, 0, tzinfo=dt.UTC))
        )

    def test_venue_tokens_are_normalized(self):
        self.assertTrue(
            self._in_window(" xwar , xnys ", dt.datetime(2026, 8, 18, 7, 30, tzinfo=dt.UTC))
        )

    def test_unknown_venue_raises_out_of_the_predicate(self):
        with self.assertRaises(Exception):
            self._in_window("XNYS,NOPE", dt.datetime(2026, 8, 18, 12, 0, tzinfo=dt.UTC))


class _FakeSharedStream:
    """Only what _default_live_exits_feed_factory calls on the singleton."""

    def live_uic_for(self, ticker: str, *, exchange_mic: str) -> int | None:
        return None

    def ensure_subscribed(self, uics, *, scope: str) -> None:
        pass


class TestFactoryForwardsSessionWindow(unittest.TestCase):
    """The factory must thread the (env-gated) predicate into
    ``get_shared_price_stream`` alongside metrics_job — it only takes effect
    on the call that actually constructs the singleton."""

    def _factory_call_kwargs(self, env: dict[str, str]) -> Mapping[str, Any]:
        with mock.patch.dict("os.environ", env, clear=True):
            with mock.patch(
                "alphalens_pipeline.data.alt_data.saxo_price_stream.get_shared_price_stream",
                return_value=_FakeSharedStream(),
            ) as getter:
                cl._default_live_exits_feed_factory({211: ("AAPL", "XNYS")}, scope="exits")
        return getter.call_args.kwargs

    def test_gate_env_unset_forwards_none(self):
        kwargs = self._factory_call_kwargs({"ALPHALENS_SAXO_LIVE_PRICES": "1"})
        self.assertIsNone(kwargs["session_window"])

    def test_gate_env_one_forwards_a_predicate(self):
        kwargs = self._factory_call_kwargs({"ALPHALENS_SAXO_LIVE_PRICES": "1", _GATE_ENV: "1"})
        self.assertTrue(callable(kwargs["session_window"]))


if __name__ == "__main__":
    unittest.main()
