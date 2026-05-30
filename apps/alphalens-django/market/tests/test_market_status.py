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
        # next_open is the NEXT session open relative to the anchor (the
        # following Monday's 13:30 UTC), not the current day's open.
        assert body["next_open_iso"] == "2026-06-01T13:30:00+00:00"


class TestWeekend:
    """Sat 2026-05-30 — NYSE closed all weekend."""

    def test_saturday_is_not_trading_day(self, client: APIClient):
        res = client.get("/v1/market/status?as_of=2026-05-30")
        assert res.status_code == 200

        body = res.json()
        assert body["is_trading_day"] is False
        assert body["is_half_day"] is False
        # Next session is Monday 2026-06-01.
        assert body["next_open_iso"] == "2026-06-01T13:30:00+00:00"


class TestUSHoliday:
    """Mon 2026-01-19 — Martin Luther King Jr. Day, NYSE closed."""

    def test_mlk_day_is_not_trading_day(self, client: APIClient):
        res = client.get("/v1/market/status?as_of=2026-01-19")
        assert res.status_code == 200

        body = res.json()
        assert body["is_trading_day"] is False
        assert body["is_half_day"] is False
        # Next session is Tue 2026-01-20.
        assert body["next_open_iso"] == "2026-01-20T14:30:00+00:00"


class TestHalfDay:
    """Fri 2026-11-27 — day after Thanksgiving, XNYS closes 13:00 ET (18:00 UTC)."""

    def test_black_friday_is_half_day(self, client: APIClient):
        res = client.get("/v1/market/status?as_of=2026-11-27")
        assert res.status_code == 200

        body = res.json()
        assert body["is_trading_day"] is True
        assert body["is_half_day"] is True
        # Half-day still has a regular 09:30 ET open; next_open refers to
        # the FOLLOWING session (Mon 2026-11-30).
        assert body["next_open_iso"] == "2026-11-30T14:30:00+00:00"


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


class TestSchema:
    def test_response_shape_keys(self, client: APIClient):
        """Stable contract for the SPA banner: exactly these four keys."""
        res = client.get("/v1/market/status?as_of=2026-05-29")
        body = res.json()

        assert set(body.keys()) == {
            "is_trading_day",
            "is_half_day",
            "next_open_iso",
            "exchange",
        }
        # next_open is a tz-aware ISO 8601 string the SPA can hand
        # straight to ``new Date(...)``. We assert parseability rather
        # than a literal because half-day vs full-day open semantics
        # are covered by the dedicated tests above.
        parsed = dt.datetime.fromisoformat(body["next_open_iso"])
        assert parsed.tzinfo is not None


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
