"""Tests for ``/v1/market/status`` — read-only exchange-calendar projection.

Design memo: ``docs/research/paper_trading_non_trading_day_2026_05_29.md``
§5 (PR-C sequencing).

The endpoint exposes one snapshot of XNYS state used by the SPA to render
a closed-market banner with a live countdown to the next session open. The
backing helpers live in ``market.calendar`` (a thin wrapper around the
``exchange_calendars`` library, mirrored from the pipeline's
``alphalens_pipeline.paper.calendar``; both wrappers delegate to the same
underlying library so the half-day / next-open algorithms cannot drift
silently).

Determinism: tests pin the anchor via the ``?as_of=YYYY-MM-DD`` query
parameter. Without it the view defaults to ``dt.datetime.now(dt.UTC)`` and
the result depends on wall-clock — fine for the SPA, useless for CI.
"""

from __future__ import annotations

import datetime as dt

import pytest
from rest_framework.test import APIClient


@pytest.fixture
def client() -> APIClient:
    return APIClient()


class TestTradingDay:
    """Fri 2026-05-29 — full XNYS session, 09:30 → 16:00 ET."""

    def test_friday_is_trading_day(self, client: APIClient):
        res = client.get("/v1/market/status?as_of=2026-05-29")
        assert res.status_code == 200

        body = res.json()
        assert body["is_trading_day"] is True
        assert body["is_half_day"] is False
        assert body["exchange"] == "XNYS"
        # ``next_open_iso`` is a wall-clock field (keyed off now, not the
        # ``as_of`` anchor) — its value is pinned deterministically in
        # ``TestNextSessionOpen``; here we only exercise the day-level pair.


class TestWeekend:
    """Sat 2026-05-30 — NYSE closed all weekend."""

    def test_saturday_is_not_trading_day(self, client: APIClient):
        res = client.get("/v1/market/status?as_of=2026-05-30")
        assert res.status_code == 200

        body = res.json()
        assert body["is_trading_day"] is False
        assert body["is_half_day"] is False


class TestUSHoliday:
    """Mon 2026-01-19 — Martin Luther King Jr. Day, NYSE closed."""

    def test_mlk_day_is_not_trading_day(self, client: APIClient):
        res = client.get("/v1/market/status?as_of=2026-01-19")
        assert res.status_code == 200

        body = res.json()
        assert body["is_trading_day"] is False
        assert body["is_half_day"] is False


class TestHalfDay:
    """Fri 2026-11-27 — day after Thanksgiving, XNYS closes 13:00 ET (18:00 UTC)."""

    def test_black_friday_is_half_day(self, client: APIClient):
        res = client.get("/v1/market/status?as_of=2026-11-27")
        assert res.status_code == 200

        body = res.json()
        assert body["is_trading_day"] is True
        assert body["is_half_day"] is True


class TestInvalidAsOf:
    def test_malformed_as_of_returns_400(self, client: APIClient):
        """Anything that isn't strict ISO ``YYYY-MM-DD`` is rejected so a
        typo doesn't silently fall back to wall-clock and surface as a
        confusing "wrong day" banner."""
        res = client.get("/v1/market/status?as_of=not-a-date")
        assert res.status_code == 400

    def test_iso_with_time_component_returns_400(self, client: APIClient):
        """``2026-05-29T12:00:00`` is a valid ISO datetime but not the
        date-only form the view accepts — the calendar lookup is
        date-granular and ambiguous parsing would mask operator typos."""
        res = client.get("/v1/market/status?as_of=2026-05-29T12:00:00")
        assert res.status_code == 400


class TestIntradaySession:
    """Minute-resolution ``is_session_open_at`` / ``next_session_close_utc``.

    The day-level fields (``is_trading_day`` etc.) answer "what about day D";
    these two helpers answer "right now". They feed the SPA's per-exchange
    session chip (open/closed + countdown to the next close while open). The
    view passes ``datetime.now(UTC)``; here we pin explicit instants so the
    branches are deterministic without freezing the clock.

    Late-May is EDT (UTC-4): XNYS regular session 09:30→16:00 ET maps to
    13:30→20:00 UTC. Black Friday is EST (UTC-5) with an early 13:00 ET close
    (18:00 UTC).
    """

    def test_open_during_regular_session(self):
        from market.calendar import is_session_open_at

        # Fri 2026-05-29 15:00 UTC == 11:00 ET — mid-session.
        instant = dt.datetime(2026, 5, 29, 15, 0, tzinfo=dt.UTC)
        assert is_session_open_at(instant) is True

    def test_closed_before_open(self):
        from market.calendar import is_session_open_at

        # Fri 2026-05-29 09:00 UTC == 05:00 ET — pre-market.
        instant = dt.datetime(2026, 5, 29, 9, 0, tzinfo=dt.UTC)
        assert is_session_open_at(instant) is False

    def test_closed_after_close(self):
        from market.calendar import is_session_open_at

        # Fri 2026-05-29 21:00 UTC == 17:00 ET — after the 16:00 ET close.
        instant = dt.datetime(2026, 5, 29, 21, 0, tzinfo=dt.UTC)
        assert is_session_open_at(instant) is False

    def test_closed_on_weekend(self):
        from market.calendar import is_session_open_at

        # Sat 2026-05-30 15:00 UTC — no session at all.
        instant = dt.datetime(2026, 5, 30, 15, 0, tzinfo=dt.UTC)
        assert is_session_open_at(instant) is False

    def test_half_day_open_before_early_close(self):
        from market.calendar import is_session_open_at

        # Black Friday 2026-11-27 17:00 UTC == 12:00 ET — before the 13:00 ET
        # early close, so the venue is open.
        instant = dt.datetime(2026, 11, 27, 17, 0, tzinfo=dt.UTC)
        assert is_session_open_at(instant) is True

    def test_half_day_closed_after_early_close(self):
        from market.calendar import is_session_open_at

        # Black Friday 2026-11-27 19:00 UTC == 14:00 ET — after the 13:00 ET
        # early close. A naive "is it a trading day" check would miss this.
        instant = dt.datetime(2026, 11, 27, 19, 0, tzinfo=dt.UTC)
        assert is_session_open_at(instant) is False

    def test_next_close_is_todays_close_when_open(self):
        from market.calendar import next_session_close_utc

        # Mid-session Fri → next close is today's 16:00 ET (20:00 UTC).
        instant = dt.datetime(2026, 5, 29, 15, 0, tzinfo=dt.UTC)
        nxt = next_session_close_utc(instant)
        assert nxt.isoformat() == "2026-05-29T20:00:00+00:00"

    def test_next_close_is_early_close_on_half_day(self):
        from market.calendar import next_session_close_utc

        # Mid-session Black Friday → next close is the early 13:00 ET
        # (18:00 UTC), not the regular 16:00 ET.
        instant = dt.datetime(2026, 11, 27, 17, 0, tzinfo=dt.UTC)
        nxt = next_session_close_utc(instant)
        assert nxt.isoformat() == "2026-11-27T18:00:00+00:00"

    def test_naive_instant_assumed_utc(self):
        from market.calendar import is_session_open_at

        # A naive datetime must be read as UTC, not local — the view passes
        # tz-aware now() but the helper is robust either way.
        instant = dt.datetime(2026, 5, 29, 15, 0)  # noqa: DTZ001 — UTC assumed
        assert is_session_open_at(instant) is True

    def test_exact_open_minute_is_open(self):
        from market.calendar import is_session_open_at

        # Pin the library's boundary contract: the open minute (09:30 ET =
        # 13:30 UTC on this EDT date) counts as in-session, so the chip
        # flips to "open" exactly at the bell, not a minute late.
        instant = dt.datetime(2026, 5, 29, 13, 30, 0, tzinfo=dt.UTC)
        assert is_session_open_at(instant) is True

    def test_exact_close_minute_is_closed(self):
        from market.calendar import is_session_open_at

        # The close minute (16:00 ET = 20:00 UTC) counts as NOT in-session —
        # ``exchange_calendars`` treats sessions as left-closed/right-open, so
        # the chip flips to "closed" at the bell. Pinning this guards against
        # a library default-``side`` change silently shifting the boundary.
        instant = dt.datetime(2026, 5, 29, 20, 0, 0, tzinfo=dt.UTC)
        assert is_session_open_at(instant) is False


class TestNextSessionOpen:
    """Minute-resolution ``next_session_open_utc`` — the wall-clock "when does
    the venue next open" that feeds the SPA's "opens in HH:MM" countdown.

    Distinct from a day-anchored "next session after day D": this answers the
    question from the *current instant*. The critical case is the pre-open
    window on a trading day — the market opens later TODAY, so the countdown
    must point to today's open, not tomorrow's. A day-anchored helper skipped
    today's still-future session and reported the next day (the bug this class
    pins). ``instant`` is normalised to UTC exactly as the close twin does.

    All instants are EDT (UTC-4): XNYS opens 09:30 ET == 13:30 UTC.
    """

    def test_preopen_on_trading_day_is_todays_open(self):
        from market.calendar import next_session_open_utc

        # Fri 2026-05-29 09:00 UTC == 05:00 ET — pre-market on a full session.
        # The venue opens later TODAY at 13:30 UTC, so THAT is the next open,
        # not the following Monday. This is the bug: a day-anchored lookup
        # skipped today's not-yet-happened session.
        instant = dt.datetime(2026, 5, 29, 9, 0, tzinfo=dt.UTC)
        nxt = next_session_open_utc(instant)
        assert nxt.isoformat() == "2026-05-29T13:30:00+00:00"

    def test_in_session_is_next_days_open(self):
        from market.calendar import next_session_open_utc

        # Mid-session Fri 15:00 UTC — today's open already passed, so the next
        # open is the following Monday's 13:30 UTC.
        instant = dt.datetime(2026, 5, 29, 15, 0, tzinfo=dt.UTC)
        nxt = next_session_open_utc(instant)
        assert nxt.isoformat() == "2026-06-01T13:30:00+00:00"

    def test_after_close_is_next_days_open(self):
        from market.calendar import next_session_open_utc

        # Fri 21:00 UTC == 17:00 ET — after the 16:00 ET close. Next open is
        # Monday 13:30 UTC.
        instant = dt.datetime(2026, 5, 29, 21, 0, tzinfo=dt.UTC)
        nxt = next_session_open_utc(instant)
        assert nxt.isoformat() == "2026-06-01T13:30:00+00:00"

    def test_weekend_is_next_sessions_open(self):
        from market.calendar import next_session_open_utc

        # Sat 2026-05-30 15:00 UTC — no session; next open is Monday 13:30 UTC.
        instant = dt.datetime(2026, 5, 30, 15, 0, tzinfo=dt.UTC)
        nxt = next_session_open_utc(instant)
        assert nxt.isoformat() == "2026-06-01T13:30:00+00:00"

    def test_naive_instant_assumed_utc(self):
        from market.calendar import next_session_open_utc

        # A naive datetime is read as UTC, mirroring the close twin — the view
        # passes tz-aware now() but the helper is robust either way.
        instant = dt.datetime(2026, 5, 29, 9, 0)  # noqa: DTZ001 — UTC assumed
        nxt = next_session_open_utc(instant)
        assert nxt.isoformat() == "2026-05-29T13:30:00+00:00"


class TestSchema:
    def test_response_shape_keys(self, client: APIClient):
        """Stable contract for the SPA session chip: exactly these six keys."""
        res = client.get("/v1/market/status?as_of=2026-05-29")
        body = res.json()

        assert set(body.keys()) == {
            "is_trading_day",
            "is_half_day",
            "is_open_now",
            "next_open_iso",
            "next_close_iso",
            "exchange",
        }
        # next_open / next_close are tz-aware ISO 8601 strings the SPA can
        # hand straight to ``new Date(...)``. We assert parseability rather
        # than a literal because half-day vs full-day semantics are covered
        # by the dedicated tests above; ``is_open_now`` depends on wall-clock
        # so we only pin its type here.
        for key in ("next_open_iso", "next_close_iso"):
            parsed = dt.datetime.fromisoformat(body[key])
            assert parsed.tzinfo is not None
        assert isinstance(body["is_open_now"], bool)


class TestDefaultAnchor:
    """When ``?as_of=`` is omitted the view consults the wall clock. We
    only assert response shape + 200 here — the calendar branch values are
    exercised by the deterministic tests above."""

    def test_no_as_of_uses_now(self, client: APIClient):
        res = client.get("/v1/market/status")
        assert res.status_code == 200

        body = res.json()
        assert isinstance(body["is_trading_day"], bool)
        assert isinstance(body["is_half_day"], bool)
        assert body["exchange"] == "XNYS"


class TestExchangeCalendarsApiContract:
    """Pin the ``exchange_calendars`` library API shape that
    ``market.calendar.is_half_day`` depends on.

    A future major-version bump that changes ``cal.close_times`` from
    ``list[(date, time)]`` to a different container would otherwise
    silently misclassify every half-day as a full day (the defensive
    ``isinstance`` guard returns False on shape mismatch — quiet wrong,
    not loud wrong). This test fails loudly on the upgrade so the
    operator knows to revisit the detection idiom. Flag surfaced by
    zen review 2026-05-30.
    """

    def test_close_times_is_list_of_date_time_tuples(self):
        import datetime as dt

        from market.calendar import _calendar

        cal = _calendar("XNYS")
        ct = cal.close_times

        assert hasattr(ct, "__iter__"), "close_times must be iterable"
        items = list(ct)
        assert len(items) >= 1, "XNYS must have at least one close_time entry"

        # Each entry must be (effective_date_or_None, datetime.time).
        for entry in items:
            assert len(entry) == 2, f"expected 2-tuple, got {entry!r}"
            _eff_date, close_time = entry
            assert isinstance(close_time, dt.time), (
                f"second tuple element must be datetime.time, got {type(close_time).__name__}"
            )
