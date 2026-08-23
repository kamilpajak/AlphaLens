"""Tests for the corporate-actions lookup behind the implausible-move guard (#1090).

The 0.60 threshold is demoted from a verdict to a TRIGGER (design memo
``docs/research/implausible_guard_redesign_2026_08_23.md``, Amendment 1). On a
trip the guard asks the corporate-actions source of record and then an
independent vendor, instead of guessing:

* action found            -> ``split_invalidated`` (terminal quarantine)
* lookup failed           -> ``lookup_failed`` (carry, counted)
* none found + yf agrees  -> ``extreme_validated`` (accept)
* none found + disagrees / no data -> ``data_quality`` (carry, counted)

Fixtures are the three REAL measured production cases (the session's standing
lesson — never invented shapes): MRNA +142.5% (real move, no actions), CRSR
+61.6% (real move, barely over the trigger), MQ +342.5% (4:1 reverse split
executed 2026-07-01 inside the window).
"""

from __future__ import annotations

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd
from alphalens_pipeline.feedback.corporate_actions import (
    CROSS_CHECK_AGREEMENT_PP,
    DISPOSITION_DATA_QUALITY,
    DISPOSITION_EXTREME_VALIDATED,
    DISPOSITION_LOOKUP_FAILED,
    DISPOSITION_SPLIT_INVALIDATED,
    GUARD_CONFIG_VERSION,
    NONE_FOUND_CACHE_TTL_DAYS,
    SPECIAL_DIVIDEND_PRE_EX_CLOSE_FRACTION,
    CachedCorporateActionsLookup,
    CorporateActionsAnswer,
    CorporateActionsLookupError,
    PolygonCorporateActionsLookup,
    adjusted_window_return,
    resolve_guard_disposition,
)

# ---- the three REAL measured cases (production, last 21 days before 2026-08-23) --

# Coordinates grounded 2026-08-23 from the VPS journals + stores: the forward
# returns are the logged rejection values; the windows are the parked rows'
# own (MRNA: the bracket-arm brief 2026-08-06; MQ: the production brief
# 2026-05-29); the closes below are yfinance adjusted values measured live.

# MRNA: +142.5% rejected by the old guard; NO corporate actions in the window
# (volume 4.3M -> 199M on 2026-08-19 — a real move, not a split artifact).
_MRNA_FORWARD_RETURN = 1.425
_MRNA_ARRIVAL = dt.date(2026, 8, 7)
_MRNA_HORIZON = dt.date(2026, 8, 21)

# CRSR: +61.6%, barely over the 0.60 trigger; no actions. Its window predates
# MRNA's, so its resolver test passes these bounds explicitly.
_CRSR_FORWARD_RETURN = 0.616
_CRSR_ARRIVAL = dt.date(2026, 7, 7)
_CRSR_HORIZON = dt.date(2026, 8, 8)

# MQ: +342.5% raw-bar artifact; 4:1 reverse split EXECUTED 2026-07-01 in-window.
_MQ_FORWARD_RETURN = 3.425
_MQ_ARRIVAL = dt.date(2026, 6, 1)
_MQ_HORIZON = dt.date(2026, 8, 21)
_MQ_SPLIT_RECORD = {
    "ticker": "MQ",
    "execution_date": "2026-07-01",
    "split_from": 4.0,
    "split_to": 1.0,
}


def _closes(points: dict[str, float]) -> pd.Series:
    """A tz-naive adjusted-daily-closes Series from {iso_date: close}."""
    return pd.Series(
        list(points.values()), index=pd.DatetimeIndex(list(points.keys())), dtype=float
    )


# MRNA adjusted closes measured live from yfinance for the real window
# (+145.3%, a 2.8pp gap vs the raw forward — inside the agreement band).
_MRNA_CLOSES = _closes({"2026-08-07": 59.17, "2026-08-21": 145.13})
# CRSR adjusted closes measured live (+61.4%, a 0.2pp gap).
_CRSR_CLOSES = _closes({"2026-07-07": 8.89, "2026-08-07": 14.35})


class _StubLookup:
    """Injectable lookup port: canned answer or raised exception, call-counted."""

    def __init__(self, answer: CorporateActionsAnswer | None = None, exc: Exception | None = None):
        self.answer = answer
        self.exc = exc
        self.calls: list[tuple[str, dt.date, dt.date]] = []

    def lookup(self, ticker: str, start: dt.date, end: dt.date) -> CorporateActionsAnswer:
        self.calls.append((ticker, start, end))
        if self.exc is not None:
            raise self.exc
        assert self.answer is not None
        return self.answer


class TestPolygonCorporateActionsLookup(unittest.TestCase):
    def _client(self, *, splits=None, dividends=None):
        client = mock.Mock()
        client.get_splits.return_value = splits or []
        client.get_dividends.return_value = dividends or []
        return client

    def test_mq_reverse_split_in_window_is_found(self):
        # GIVEN the real MQ 4:1 reverse split record. WHEN looked up over the
        # replay window. THEN the answer is FOUND (-> SPLIT_INVALIDATED).
        client = self._client(splits=[_MQ_SPLIT_RECORD])
        lookup = PolygonCorporateActionsLookup(pre_ex_close=lambda t, d: None, client=client)
        answer = lookup.lookup("MQ", _MQ_ARRIVAL - dt.timedelta(days=3), _MQ_HORIZON)
        self.assertTrue(answer.found)
        self.assertIn("split", (answer.detail or "").lower())
        # The window is passed through to the client as execution-date bounds.
        _, kwargs = client.get_splits.call_args
        self.assertEqual(kwargs["execution_date_gte"], _MQ_ARRIVAL - dt.timedelta(days=3))
        self.assertEqual(kwargs["execution_date_lte"], _MQ_HORIZON)

    def test_no_actions_returns_none_found(self):
        client = self._client()
        lookup = PolygonCorporateActionsLookup(pre_ex_close=lambda t, d: None, client=client)
        answer = lookup.lookup("MRNA", _MRNA_ARRIVAL, _MRNA_HORIZON)
        self.assertFalse(answer.found)

    def test_dividend_at_9pct_of_pre_ex_close_does_not_invalidate(self):
        # 9% of the pre-ex-date raw close is below the 10% materiality floor —
        # an ordinary (large) dividend, not a split-class gap.
        client = self._client(
            dividends=[{"ticker": "XYZ", "ex_dividend_date": "2026-07-10", "cash_amount": 4.5}]
        )
        lookup = PolygonCorporateActionsLookup(pre_ex_close=lambda t, d: 50.0, client=client)
        answer = lookup.lookup("XYZ", dt.date(2026, 7, 1), dt.date(2026, 8, 21))
        self.assertFalse(answer.found)

    def test_dividend_at_11pct_of_pre_ex_close_invalidates(self):
        client = self._client(
            dividends=[{"ticker": "XYZ", "ex_dividend_date": "2026-07-10", "cash_amount": 5.5}]
        )
        captured: list[tuple[str, dt.date]] = []

        def pre_ex_close(ticker: str, ex_date: dt.date) -> float:
            captured.append((ticker, ex_date))
            return 50.0

        lookup = PolygonCorporateActionsLookup(pre_ex_close=pre_ex_close, client=client)
        answer = lookup.lookup("XYZ", dt.date(2026, 7, 1), dt.date(2026, 8, 21))
        self.assertTrue(answer.found)
        self.assertIn("dividend", (answer.detail or "").lower())
        # Materiality denominator is the close BEFORE the ex-date (Amendment 3).
        self.assertEqual(captured, [("XYZ", dt.date(2026, 7, 10))])

    def test_materiality_constant_is_ten_percent(self):
        self.assertEqual(SPECIAL_DIVIDEND_PRE_EX_CLOSE_FRACTION, 0.10)

    def test_dividend_at_exactly_the_floor_does_not_invalidate(self):
        # Boundary pin: materiality is STRICTLY above the floor. 5.0/50.0
        # divides to exactly the float literal 0.1, so the comparison is
        # float-exact here and a `>` -> `>=` flip goes red.
        client = self._client(
            dividends=[{"ticker": "XYZ", "ex_dividend_date": "2026-07-10", "cash_amount": 5.0}]
        )
        lookup = PolygonCorporateActionsLookup(pre_ex_close=lambda t, d: 50.0, client=client)
        answer = lookup.lookup("XYZ", dt.date(2026, 7, 1), dt.date(2026, 8, 21))
        self.assertFalse(answer.found)

    def test_dividend_with_missing_pre_ex_close_raises_lookup_error(self):
        # Fail-closed: without the denominator the materiality cannot be
        # assessed, so the lookup fails (-> carry + lookup_failed) rather than
        # silently passing a possibly-material dividend.
        client = self._client(
            dividends=[{"ticker": "XYZ", "ex_dividend_date": "2026-07-10", "cash_amount": 5.5}]
        )
        lookup = PolygonCorporateActionsLookup(pre_ex_close=lambda t, d: None, client=client)
        with self.assertRaises(CorporateActionsLookupError):
            lookup.lookup("XYZ", dt.date(2026, 7, 1), dt.date(2026, 8, 21))

    def test_client_error_raises_lookup_error(self):
        client = mock.Mock()
        client.get_splits.side_effect = RuntimeError("polygon down")
        lookup = PolygonCorporateActionsLookup(pre_ex_close=lambda t, d: None, client=client)
        with self.assertRaises(CorporateActionsLookupError):
            lookup.lookup("MRNA", _MRNA_ARRIVAL, _MRNA_HORIZON)


class TestAdjustedWindowReturn(unittest.TestCase):
    def test_mrna_window_return_matches_measured_move(self):
        value = adjusted_window_return(_MRNA_CLOSES, _MRNA_ARRIVAL, _MRNA_HORIZON)
        self.assertIsNotNone(value)
        self.assertAlmostEqual(value, 145.13 / 59.17 - 1.0)
        self.assertLess(abs(value - _MRNA_FORWARD_RETURN), CROSS_CHECK_AGREEMENT_PP)

    def test_single_point_returns_none(self):
        closes = _closes({"2026-08-07": 59.17})
        self.assertIsNone(adjusted_window_return(closes, _MRNA_ARRIVAL, _MRNA_HORIZON))

    def test_none_or_empty_returns_none(self):
        self.assertIsNone(adjusted_window_return(None, _MRNA_ARRIVAL, _MRNA_HORIZON))
        self.assertIsNone(
            adjusted_window_return(pd.Series(dtype=float), _MRNA_ARRIVAL, _MRNA_HORIZON)
        )

    def test_closes_outside_window_are_ignored(self):
        closes = _closes({"2026-08-01": 10.0, "2026-08-07": 59.17, "2026-08-21": 145.13})
        value = adjusted_window_return(closes, _MRNA_ARRIVAL, _MRNA_HORIZON)
        self.assertAlmostEqual(value, 145.13 / 59.17 - 1.0)


class TestResolveGuardDisposition(unittest.TestCase):
    def _resolve(self, *, forward_return, lookup, closes, arrival=None, horizon=None):
        return resolve_guard_disposition(
            ticker="MRNA",
            forward_return=forward_return,
            arrival_session=arrival or _MRNA_ARRIVAL,
            horizon_session=horizon or _MRNA_HORIZON,
            lookup=lookup,
            adjusted_closes=lambda t, s, e: closes,
        )

    def test_mrna_no_actions_yf_agrees_is_extreme_validated(self):
        lookup = _StubLookup(CorporateActionsAnswer(found=False))
        disposition = self._resolve(
            forward_return=_MRNA_FORWARD_RETURN, lookup=lookup, closes=_MRNA_CLOSES
        )
        self.assertEqual(disposition, DISPOSITION_EXTREME_VALIDATED)

    def test_crsr_no_actions_yf_agrees_is_extreme_validated(self):
        lookup = _StubLookup(CorporateActionsAnswer(found=False))
        disposition = self._resolve(
            forward_return=_CRSR_FORWARD_RETURN,
            lookup=lookup,
            closes=_CRSR_CLOSES,
            arrival=_CRSR_ARRIVAL,
            horizon=_CRSR_HORIZON,
        )
        self.assertEqual(disposition, DISPOSITION_EXTREME_VALIDATED)

    def test_mq_split_found_is_split_invalidated(self):
        lookup = _StubLookup(CorporateActionsAnswer(found=True, detail="split 4:1 2026-07-01"))
        disposition = self._resolve(
            forward_return=_MQ_FORWARD_RETURN,
            lookup=lookup,
            closes=None,
            arrival=_MQ_ARRIVAL,
            horizon=_MQ_HORIZON,
        )
        self.assertEqual(disposition, DISPOSITION_SPLIT_INVALIDATED)

    def test_action_lookup_window_is_padded_minus_3_plus_1_calendar_days(self):
        lookup = _StubLookup(CorporateActionsAnswer(found=False))
        self._resolve(forward_return=_MRNA_FORWARD_RETURN, lookup=lookup, closes=_MRNA_CLOSES)
        (ticker, start, end) = lookup.calls[0]
        self.assertEqual(ticker, "MRNA")
        self.assertEqual(start, _MRNA_ARRIVAL - dt.timedelta(days=3))
        self.assertEqual(end, _MRNA_HORIZON + dt.timedelta(days=1))

    def test_lookup_exception_is_lookup_failed(self):
        lookup = _StubLookup(exc=CorporateActionsLookupError("polygon down"))
        disposition = self._resolve(
            forward_return=_MRNA_FORWARD_RETURN, lookup=lookup, closes=_MRNA_CLOSES
        )
        self.assertEqual(disposition, DISPOSITION_LOOKUP_FAILED)

    def test_yf_disagreement_beyond_10pp_is_data_quality(self):
        # yfinance sees a flat window while the raw bars claim +142.5%: the raw
        # data is suspect (bad vendor bar / halt-reopen / lineage break).
        lookup = _StubLookup(CorporateActionsAnswer(found=False))
        flat = _closes({"2026-08-07": 34.2, "2026-08-21": 35.0})
        disposition = self._resolve(forward_return=_MRNA_FORWARD_RETURN, lookup=lookup, closes=flat)
        self.assertEqual(disposition, DISPOSITION_DATA_QUALITY)

    def test_cross_check_just_inside_the_band_agrees(self):
        # Band-width pin from the inside: raw +142.5% vs adjusted +132.6% is a
        # 9.9pp gap — inside CROSS_CHECK_AGREEMENT_PP. (The exact-0.10 edge is
        # deliberately unpinned: at float precision a strict-vs-inclusive flip
        # there is unobservable, so the pair 9.9/10.1 pins the band's width.)
        lookup = _StubLookup(CorporateActionsAnswer(found=False))
        inside = _closes({"2026-08-07": 100.0, "2026-08-21": 232.6})
        disposition = self._resolve(
            forward_return=_MRNA_FORWARD_RETURN, lookup=lookup, closes=inside
        )
        self.assertEqual(disposition, DISPOSITION_EXTREME_VALIDATED)

    def test_cross_check_just_outside_the_band_is_data_quality(self):
        # Band-width pin from the outside: a 10.1pp gap must NOT validate.
        lookup = _StubLookup(CorporateActionsAnswer(found=False))
        outside = _closes({"2026-08-07": 100.0, "2026-08-21": 232.4})
        disposition = self._resolve(
            forward_return=_MRNA_FORWARD_RETURN, lookup=lookup, closes=outside
        )
        self.assertEqual(disposition, DISPOSITION_DATA_QUALITY)

    def test_yf_no_data_is_data_quality(self):
        lookup = _StubLookup(CorporateActionsAnswer(found=False))
        disposition = self._resolve(forward_return=_MRNA_FORWARD_RETURN, lookup=lookup, closes=None)
        self.assertEqual(disposition, DISPOSITION_DATA_QUALITY)

    def test_cross_check_fetch_exception_is_data_quality(self):
        lookup = _StubLookup(CorporateActionsAnswer(found=False))

        def broken_fetch(t, s, e):
            raise RuntimeError("yahoo down")

        disposition = resolve_guard_disposition(
            ticker="MRNA",
            forward_return=_MRNA_FORWARD_RETURN,
            arrival_session=_MRNA_ARRIVAL,
            horizon_session=_MRNA_HORIZON,
            lookup=lookup,
            adjusted_closes=broken_fetch,
        )
        self.assertEqual(disposition, DISPOSITION_DATA_QUALITY)


class TestCachedCorporateActionsLookup(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.cache_path = Path(self._td.name) / "corporate_actions_cache.json"

    def tearDown(self):
        self._td.cleanup()

    def _cached(self, inner, *, now: dt.datetime):
        return CachedCorporateActionsLookup(inner, self.cache_path, now=lambda: now)

    def test_second_call_hits_cache_without_recalling_inner(self):
        inner = _StubLookup(CorporateActionsAnswer(found=False))
        now = dt.datetime(2026, 8, 23, 6, 30, tzinfo=dt.UTC)
        cached = self._cached(inner, now=now)
        first = cached.lookup("MRNA", _MRNA_ARRIVAL, _MRNA_HORIZON)
        second = cached.lookup("MRNA", _MRNA_ARRIVAL, _MRNA_HORIZON)
        self.assertEqual(len(inner.calls), 1)
        self.assertEqual(first.found, second.found)

    def test_found_answer_is_cached_forever(self):
        inner = _StubLookup(CorporateActionsAnswer(found=True, detail="split 4:1"))
        t0 = dt.datetime(2026, 7, 2, 6, 30, tzinfo=dt.UTC)
        self._cached(inner, now=t0).lookup("MQ", _MQ_ARRIVAL, _MQ_HORIZON)
        # 400 days later the FOUND record is immutable — no re-query.
        much_later = t0 + dt.timedelta(days=400)
        answer = self._cached(inner, now=much_later).lookup("MQ", _MQ_ARRIVAL, _MQ_HORIZON)
        self.assertTrue(answer.found)
        self.assertEqual(len(inner.calls), 1)

    def test_none_found_older_than_ttl_requeries(self):
        # Corporate-action records are corrected/appended late, so a NONE-FOUND
        # answer expires after 14 days and the source is asked again.
        inner = _StubLookup(CorporateActionsAnswer(found=False))
        t0 = dt.datetime(2026, 8, 1, 6, 30, tzinfo=dt.UTC)
        self._cached(inner, now=t0).lookup("MRNA", _MRNA_ARRIVAL, _MRNA_HORIZON)
        stale = t0 + dt.timedelta(days=NONE_FOUND_CACHE_TTL_DAYS + 1)
        self._cached(inner, now=stale).lookup("MRNA", _MRNA_ARRIVAL, _MRNA_HORIZON)
        self.assertEqual(len(inner.calls), 2)

    def test_none_found_within_ttl_does_not_requery(self):
        inner = _StubLookup(CorporateActionsAnswer(found=False))
        t0 = dt.datetime(2026, 8, 1, 6, 30, tzinfo=dt.UTC)
        self._cached(inner, now=t0).lookup("MRNA", _MRNA_ARRIVAL, _MRNA_HORIZON)
        fresh = t0 + dt.timedelta(days=NONE_FOUND_CACHE_TTL_DAYS - 1)
        self._cached(inner, now=fresh).lookup("MRNA", _MRNA_ARRIVAL, _MRNA_HORIZON)
        self.assertEqual(len(inner.calls), 1)

    def test_lookup_error_is_not_cached(self):
        inner = _StubLookup(exc=CorporateActionsLookupError("down"))
        now = dt.datetime(2026, 8, 23, 6, 30, tzinfo=dt.UTC)
        cached = self._cached(inner, now=now)
        with self.assertRaises(CorporateActionsLookupError):
            cached.lookup("MRNA", _MRNA_ARRIVAL, _MRNA_HORIZON)
        inner.exc = None
        inner.answer = CorporateActionsAnswer(found=False)
        cached.lookup("MRNA", _MRNA_ARRIVAL, _MRNA_HORIZON)
        self.assertEqual(len(inner.calls), 2)  # the error night never wrote a cache entry

    def test_distinct_windows_are_distinct_cache_keys(self):
        inner = _StubLookup(CorporateActionsAnswer(found=False))
        now = dt.datetime(2026, 8, 23, 6, 30, tzinfo=dt.UTC)
        cached = self._cached(inner, now=now)
        cached.lookup("MRNA", _MRNA_ARRIVAL, _MRNA_HORIZON)
        cached.lookup("MRNA", _MRNA_ARRIVAL, _MRNA_HORIZON + dt.timedelta(days=1))
        self.assertEqual(len(inner.calls), 2)

    def test_cache_file_is_json_on_disk(self):
        inner = _StubLookup(CorporateActionsAnswer(found=True, detail="split 4:1"))
        now = dt.datetime(2026, 8, 23, 6, 30, tzinfo=dt.UTC)
        self._cached(inner, now=now).lookup("MQ", _MQ_ARRIVAL, _MQ_HORIZON)
        payload = json.loads(self.cache_path.read_text())
        self.assertTrue(any(entry.get("found") for entry in payload.values()))


class TestGuardConfigVersion(unittest.TestCase):
    def test_version_is_the_memo_date(self):
        self.assertEqual(GUARD_CONFIG_VERSION, "2026-08-23")


if __name__ == "__main__":
    unittest.main()
