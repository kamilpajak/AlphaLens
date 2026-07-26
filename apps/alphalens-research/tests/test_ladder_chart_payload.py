"""Pipeline-side tests for the ladder-chart payload projection (PR-1 backend).

Runs in the research suite (``unittest discover``) where ``alphalens_pipeline``
IS importable. Pins the PURE payload builder (``build_chart_payload`` + the
daily-from-minute aggregation + the marker mapping) and the IMPURE store enricher
(``enrich_store_with_chart_payloads``) that persists the payload as a
``chart_payload_json`` column on the population-ladder parquet — mirroring
``benchmark_excess.enrich_store_with_benchmark_excess``.

The load-bearing correctness invariant is "every marker time lands on an existing
DAILY bar" (the Lightweight-Charts silent-non-render gotcha — a marker time in a
non-trading gap renders nothing). The other pins cover the SL-first ambiguity
flag, the daily high/low = union of the minute highs/lows, the TIME_STOP marker
vs price-line split, and the NO_STRUCTURE / NO_DATA shapes.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import tempfile
import unittest
from pathlib import Path

import pandas as pd
from alphalens_pipeline.feedback.ladder_chart import (
    CHART_PAYLOAD_COLUMN,
    LEAD_IN_CAP,
    LEAD_IN_FLOOR,
    TRAILING_SESSIONS,
    _horizon_session,
    _is_frozen_terminal_ok,
    _merge_bars,
    build_chart_payload,
    enrich_store_with_chart_payloads,
)
from alphalens_pipeline.feedback.ladder_replay import replay_ladder
from alphalens_pipeline.feedback.population_ladder_monitor import _RunDeadline
from alphalens_pipeline.paper.calendar import (
    advance_trading_sessions,
    previous_trading_day,
    session_open_utc,
)
from alphalens_pipeline.paper.constants import TIME_STOP_DAYS

UTC = dt.UTC

# XNYS arrival on 2026-05-01 (a Friday session) with a one-week horizon gives a
# handful of RTH sessions; 2026-05-04 is the following Monday session.
_EXCHANGE = "XNYS"
_ARRIVAL = dt.date(2026, 5, 1)
_NEXT_SESSION = dt.date(2026, 5, 4)
_HORIZON = dt.date(2026, 5, 8)

# A plannable OK setup: single dip-buy entry at 100, single TP at 110, disaster
# stop at 95 -> R = 100 - 95 = 5. Matches the e2e fixture geometry.
_OK_SETUP = {
    "status": "OK",
    "schema_version": "1.0.0",
    "suggested_size_pct": 2.0,
    "disaster_stop": 95.0,
    "atr": 2.0,
    "order_ttl_days": 7,
    "entry_tiers": [{"limit": 100.0, "alloc_pct": 100.0}],
    "tp_tranches": [{"target": 110.0, "tranche_pct": 100.0}],
}


def _session_open_ms(session: dt.date) -> int:
    return int(session_open_utc(session, _EXCHANGE).timestamp() * 1000)


def _bar(ts_ms: int, o: float, h: float, low: float, c: float, v: float = 1000.0) -> dict:
    return {"t": ts_ms, "o": o, "h": h, "l": low, "c": c, "v": v}


def _daily_bar(session: dt.date, *, o=50.0, h=51.0, low=49.0, c=50.5, v=1000.0) -> dict:
    """A Polygon DAILY aggregate bar (same OHLCV dict shape as a minute bar).

    Stamped at the session's RTH open ms so the merge dedup keys it to the right
    session date — exactly how Polygon daily aggregates land inside a session
    window.
    """
    return _bar(_session_open_ms(session), o=o, h=h, low=low, c=c, v=v)


def _sessions_before(arrival: dt.date, n: int) -> list[dt.date]:
    """The ``n`` sessions strictly before ``arrival`` (oldest first)."""
    out: list[dt.date] = []
    cursor = arrival
    for _ in range(n):
        cursor = previous_trading_day(cursor, _EXCHANGE)
        out.append(cursor)
    return list(reversed(out))


def _payload(
    bars: list[dict],
    outcome,
    *,
    setup=_OK_SETUP,
    horizon=_HORIZON,
    daily_bar_fetch=None,
    ticker="NVDA",
) -> dict:
    return build_chart_payload(
        setup,
        bars,
        outcome,
        arrival_session=_ARRIVAL,
        horizon_session=horizon,
        exchange=_EXCHANGE,
        ticker=ticker,
        daily_bar_fetch=daily_bar_fetch,
    )


class TestBuildChartPayload(unittest.TestCase):
    def test_daily_high_low_is_union_of_minute_bars(self) -> None:
        """A daily candle's high/low = union of its minute highs/lows; open=first,
        close=last, volume=sum (memo §4.5/§6 internal-consistency requirement)."""
        open_ms = _session_open_ms(_ARRIVAL)
        bars = [
            _bar(open_ms, o=1.0, h=1.2, low=0.9, c=1.1, v=100.0),
            _bar(open_ms + 60_000, o=1.1, h=1.3, low=0.85, c=1.25, v=250.0),
        ]
        payload = _payload(bars, replay_ladder(_OK_SETUP, bars))
        daily = payload["bars"]
        self.assertEqual(len(daily), 1)
        candle = daily[0]
        self.assertEqual(candle["time"], _ARRIVAL.isoformat())
        self.assertEqual(candle["high"], 1.3)
        self.assertEqual(candle["low"], 0.85)
        self.assertEqual(candle["open"], 1.0)  # first minute's open
        self.assertEqual(candle["close"], 1.25)  # last minute's close
        self.assertEqual(candle["volume"], 350.0)  # sum

    def test_daily_bars_drop_non_finite_minute_bars(self) -> None:
        """A minute bar with a NaN/Inf OHLC value is dropped from the daily fold
        (a NaN candle would fail JSON float serialisation downstream); a session
        left with no finite bar emits no candle, just like a session with no bars
        at all (zen MEDIUM, PR #496)."""
        open_ms = _session_open_ms(_ARRIVAL)
        next_open_ms = _session_open_ms(_NEXT_SESSION)
        bars = [
            # Arrival session: one good bar + one NaN-high bar -> the NaN one is
            # dropped, the daily candle stays finite and reflects only the good bar.
            _bar(open_ms, o=1.0, h=1.2, low=0.9, c=1.1, v=100.0),
            _bar(open_ms + 60_000, o=1.1, h=float("nan"), low=0.85, c=1.25, v=250.0),
            # Next session: every bar non-finite -> no candle emitted for it.
            _bar(next_open_ms, o=float("inf"), h=float("inf"), low=0.5, c=0.6, v=10.0),
        ]
        payload = _payload(bars, replay_ladder(_OK_SETUP, bars))
        daily = {b["time"]: b for b in payload["bars"]}
        self.assertIn(_ARRIVAL.isoformat(), daily)
        self.assertNotIn(_NEXT_SESSION.isoformat(), daily)  # all-non-finite session dropped
        candle = daily[_ARRIVAL.isoformat()]
        self.assertEqual(candle["high"], 1.2)  # NaN bar excluded
        self.assertEqual(candle["close"], 1.1)  # last FINITE bar's close
        self.assertEqual(candle["volume"], 100.0)  # NaN bar's volume not summed
        for value in (candle["open"], candle["high"], candle["low"], candle["close"]):
            self.assertTrue(math.isfinite(value))

    def test_every_marker_time_lands_on_an_existing_daily_bar(self) -> None:
        """A crossing whose bar_ts_ms maps to a session NOT in the emitted daily
        bars is DROPPED (never emitted with a dangling time that would silently
        fail to render)."""
        bars = [
            _bar(_session_open_ms(_ARRIVAL), o=101.0, h=102.0, low=99.0, c=100.5),  # entry 100
            _bar(_session_open_ms(_NEXT_SESSION), o=105.0, h=111.0, low=104.0, c=110.5),  # TP 110
        ]
        payload = _payload(bars, replay_ladder(_OK_SETUP, bars))
        emitted_times = {b["time"] for b in payload["bars"]}
        self.assertTrue(emitted_times)  # sanity: there ARE daily bars
        for marker in payload["markers"]:
            self.assertIn(
                marker["time"],
                emitted_times,
                f"marker {marker} time not in daily bars {sorted(emitted_times)}",
            )
        kinds = {m["kind"] for m in payload["markers"]}
        self.assertIn("ENTRY", kinds)
        self.assertIn("TP", kinds)

    def test_touched_but_unsold_tp_markers_are_distinct_kind(self) -> None:
        """Partial entry fill: TP1 sells the whole held position; TP2/TP3 are
        TOUCHED but sell nothing. Their markers must render as ``TP_TOUCHED`` (a
        distinct kind the SPA draws hollow) so three green arrows do not overstate
        capture. The SOLD TP stays kind ``TP``."""
        setup = {
            "status": "OK",
            "schema_version": "1.0.0",
            "suggested_size_pct": 2.0,
            "disaster_stop": 70.0,
            "atr": 2.0,
            "order_ttl_days": 7,
            "entry_tiers": [
                {"limit": 100.0, "alloc_pct": 33.3},
                {"limit": 90.0, "alloc_pct": 33.3},
                {"limit": 80.0, "alloc_pct": 33.3},
            ],
            "tp_tranches": [
                {"target": 110.0, "tranche_pct": 33.3},
                {"target": 120.0, "tranche_pct": 33.3},
                {"target": 130.0, "tranche_pct": 33.3},
            ],
        }
        bars = [
            _bar(_session_open_ms(_ARRIVAL), o=101.0, h=102.0, low=99.0, c=100.5),  # E1 only
            _bar(_session_open_ms(_NEXT_SESSION), o=105.0, h=131.0, low=104.0, c=130.5),  # all TPs
        ]
        outcome = replay_ladder(setup, bars)
        self.assertEqual(outcome.realized_tp_ids, ("TP1",))  # only TP1 sold
        payload = _payload(bars, outcome, setup=setup)
        by_level = {
            m["level_id"]: m["kind"] for m in payload["markers"] if m["kind"].startswith("TP")
        }
        self.assertEqual(by_level.get("TP1"), "TP")  # sold -> solid
        self.assertEqual(by_level.get("TP2"), "TP_TOUCHED")  # touched, unsold
        self.assertEqual(by_level.get("TP3"), "TP_TOUCHED")

    def test_sl_first_ambiguity_flag_plumbed(self) -> None:
        """A bar crossing both a TP high and the SL low -> SL marker
        ambiguous=true, payload ambiguous_bars>=1, intrabar_rule=='sl_first', SL
        is the resolved exit."""
        bars = [
            _bar(_session_open_ms(_ARRIVAL), o=100.0, h=99.5, low=99.0, c=99.5),  # entry only
            # Next bar straddles BOTH TP 110 (high) and SL 95 (low) -> ambiguous.
            _bar(_session_open_ms(_NEXT_SESSION), o=100.0, h=111.0, low=94.0, c=96.0),
        ]
        outcome = replay_ladder(_OK_SETUP, bars)
        payload = _payload(bars, outcome)
        self.assertEqual(payload["intrabar_rule"], "sl_first")
        self.assertGreaterEqual(payload["ambiguous_bars"], 1)
        sl_markers = [m for m in payload["markers"] if m["kind"] == "SL"]
        self.assertTrue(sl_markers, "expected an SL marker")
        self.assertIs(sl_markers[0]["ambiguous"], True)

    def test_time_stop_marker_present_but_not_a_price_line(self) -> None:
        """A TIME_STOP terminal crossing -> a markers[] entry kind=='TIME_STOP'
        exists AND price_lines carries only entry/tp/stop."""
        sessions = [_ARRIVAL, _NEXT_SESSION, dt.date(2026, 5, 5)]
        bars = [_bar(_session_open_ms(_ARRIVAL), o=100.0, h=100.5, low=99.0, c=100.0)]
        for s in sessions[1:]:
            bars.append(_bar(_session_open_ms(s), o=100.0, h=100.5, low=99.5, c=100.0))
        position_expiry_ms = _session_open_ms(sessions[-1])
        outcome = replay_ladder(_OK_SETUP, bars, position_expiry_ms=position_expiry_ms)
        payload = _payload(bars, outcome, horizon=sessions[-1])
        kinds = [m["kind"] for m in payload["markers"]]
        self.assertIn("TIME_STOP", kinds)
        self.assertEqual(set(payload["price_lines"].keys()), {"entry", "tp", "stop"})

    def test_no_structure_payload_when_setup_unparseable(self) -> None:
        """parse_ladder ok=False -> status 'NO_STRUCTURE', empty bars/markers, all
        price lines None/empty."""
        bad_setup = {"status": "NOT_PLANNABLE"}
        payload = _payload(
            [_bar(_session_open_ms(_ARRIVAL), 100.0, 101.0, 99.0, 100.0)],
            replay_ladder(bad_setup, []),
            setup=bad_setup,
        )
        self.assertEqual(payload["status"], "NO_STRUCTURE")
        self.assertEqual(payload["bars"], [])
        self.assertEqual(payload["markers"], [])
        self.assertEqual(payload["price_lines"], {"entry": None, "tp": [], "stop": None})

    def test_no_data_payload_when_bars_empty(self) -> None:
        """Valid setup, empty bars -> status 'NO_DATA', empty bars + markers."""
        payload = _payload([], replay_ladder(_OK_SETUP, []))
        self.assertEqual(payload["status"], "NO_DATA")
        self.assertEqual(payload["bars"], [])
        self.assertEqual(payload["markers"], [])


class TestContextWindow(unittest.TestCase):
    """Lead-in (pre-arrival) + trailing (post-horizon) DAILY context bars sit
    around the in-trade minute-folded candles so the entry/TP/stop levels read in
    market structure (option A: in-trade sessions keep the minute-fold, context
    sessions use fetched daily aggregates)."""

    def _in_trade_bars(self) -> list[dict]:
        """Two in-trade minute bars spanning arrival + the next session."""
        return [
            _bar(_session_open_ms(_ARRIVAL), o=101.0, h=102.0, low=99.0, c=100.5),
            _bar(_session_open_ms(_NEXT_SESSION), o=105.0, h=111.0, low=104.0, c=110.5),
        ]

    def test_context_window_adds_lead_in_bars(self) -> None:
        """An injected daily_bar_fetch returning 30 pre-arrival daily sessions adds
        lead-in bars: the payload bars start before arrival and there are more bars
        than the in-trade count."""
        in_trade = self._in_trade_bars()
        pre = _sessions_before(_ARRIVAL, 30)

        def daily_fetch(ticker, start, end):
            return [_daily_bar(s, o=40.0, h=41.0, low=39.0, c=40.5) for s in pre]

        payload = _payload(
            in_trade, replay_ladder(_OK_SETUP, in_trade), daily_bar_fetch=daily_fetch
        )
        bars = payload["bars"]
        times = [b["time"] for b in bars]
        self.assertEqual(times, sorted(times))  # date-ordered
        # In-trade alone is 2 sessions; lead-in adds >= LEAD_IN_FLOOR sessions.
        self.assertGreaterEqual(len(bars), 2 + LEAD_IN_FLOOR)
        self.assertLess(bars[0]["time"], _ARRIVAL.isoformat())  # first bar before arrival
        # The FIRST emitted lead-in bar is the boundary (oldest kept) session — a
        # date-based fetch window must not drop the boundary daily bar (Fix 2).
        expected_oldest = _sessions_before(_ARRIVAL, LEAD_IN_FLOOR)[0]
        self.assertEqual(bars[0]["time"], expected_oldest.isoformat())

    def test_short_hold_gets_minimum_20_session_lead_in(self) -> None:
        """A ~2-session hold floors the lead-in at LEAD_IN_FLOOR (20) sessions, so
        when the fetch supplies them they are all emitted."""
        in_trade = self._in_trade_bars()  # arrival -> next session == 1 elapsed session
        pre = _sessions_before(_ARRIVAL, 40)  # supply more than the floor

        captured: dict[str, dt.datetime] = {}

        def daily_fetch(ticker, start, end):
            captured["start"] = start
            return [_daily_bar(s, o=40.0, h=41.0, low=39.0, c=40.5) for s in pre]

        payload = _payload(
            in_trade, replay_ladder(_OK_SETUP, in_trade), daily_bar_fetch=daily_fetch
        )
        lead_in_times = [b["time"] for b in payload["bars"] if b["time"] < _ARRIVAL.isoformat()]
        # Floor is 20; a short hold must not collapse the lead-in below it.
        self.assertGreaterEqual(len(lead_in_times), LEAD_IN_FLOOR)
        # The earliest kept lead-in session is exactly LEAD_IN_FLOOR sessions back
        # (the floor governs, not 2 x hold).
        expected_oldest = _sessions_before(_ARRIVAL, LEAD_IN_FLOOR)[0]
        self.assertEqual(min(lead_in_times), expected_oldest.isoformat())

    def test_long_hold_lead_in_is_2x_capped_at_90(self) -> None:
        """A 50-session hold would request 2x = 100 lead-in sessions but the cap
        clamps it to LEAD_IN_CAP (90)."""
        long_horizon = advance_trading_sessions(_ARRIVAL, 50, _EXCHANGE)
        # In-trade minute bars on arrival + the 50th session (hold == 50 sessions).
        in_trade = [
            _bar(_session_open_ms(_ARRIVAL), o=100.0, h=101.0, low=99.0, c=100.0),
            _bar(_session_open_ms(long_horizon), o=100.0, h=101.0, low=99.0, c=100.0),
        ]
        pre = _sessions_before(_ARRIVAL, 120)  # supply more than the cap

        def daily_fetch(ticker, start, end):
            return [_daily_bar(s, o=40.0, h=41.0, low=39.0, c=40.5) for s in pre]

        payload = build_chart_payload(
            _OK_SETUP,
            in_trade,
            replay_ladder(_OK_SETUP, in_trade),
            arrival_session=_ARRIVAL,
            horizon_session=long_horizon,
            exchange=_EXCHANGE,
            ticker="NVDA",
            daily_bar_fetch=daily_fetch,
        )
        lead_in_times = [b["time"] for b in payload["bars"] if b["time"] < _ARRIVAL.isoformat()]
        self.assertEqual(len(lead_in_times), LEAD_IN_CAP)  # 90, not 100
        expected_oldest = _sessions_before(_ARRIVAL, LEAD_IN_CAP)[0]
        self.assertEqual(min(lead_in_times), expected_oldest.isoformat())

    def test_markers_still_land_on_existing_bars_with_context(self) -> None:
        """With context bars added, every marker.time is still an emitted in-trade
        daily bar (in-trade dates preserved + ordered)."""
        in_trade = self._in_trade_bars()
        pre = _sessions_before(_ARRIVAL, 10)
        post = [advance_trading_sessions(_NEXT_SESSION, i, _EXCHANGE) for i in range(1, 6)]

        def daily_fetch(ticker, start, end):
            return [_daily_bar(s) for s in pre] + [_daily_bar(s) for s in post]

        payload = _payload(
            in_trade, replay_ladder(_OK_SETUP, in_trade), daily_bar_fetch=daily_fetch
        )
        emitted = {b["time"] for b in payload["bars"]}
        self.assertTrue(payload["markers"])
        for marker in payload["markers"]:
            self.assertIn(marker["time"], emitted)
        kinds = {m["kind"] for m in payload["markers"]}
        self.assertIn("ENTRY", kinds)
        self.assertIn("TP", kinds)

    def test_daily_high_low_is_union_of_minute_bars_with_adjacent_context(self) -> None:
        """An in-trade session keeps its minute-fold high/low even when a context
        daily bar for an ADJACENT session is present (in-trade wins on overlap; no
        context bar overwrites an in-trade session)."""
        open_ms = _session_open_ms(_ARRIVAL)
        in_trade = [
            _bar(open_ms, o=1.0, h=1.2, low=0.9, c=1.1, v=100.0),
            _bar(open_ms + 60_000, o=1.1, h=1.3, low=0.85, c=1.25, v=250.0),
        ]
        pre = _sessions_before(_ARRIVAL, 3)

        def daily_fetch(ticker, start, end):
            # Includes a context bar for the session immediately before arrival AND
            # (defensively) a daily bar stamped on the arrival session itself with
            # DIFFERENT highs/lows — the in-trade minute fold must win.
            ctx = [_daily_bar(s, o=5.0, h=6.0, low=4.0, c=5.5) for s in pre]
            ctx.append(_daily_bar(_ARRIVAL, o=9.0, h=9.9, low=8.0, c=9.0))
            return ctx

        payload = _payload(
            in_trade, replay_ladder(_OK_SETUP, in_trade), daily_bar_fetch=daily_fetch
        )
        by_time = {b["time"]: b for b in payload["bars"]}
        arrival_candle = by_time[_ARRIVAL.isoformat()]
        self.assertEqual(arrival_candle["high"], 1.3)  # minute union, NOT the 9.9 context
        self.assertEqual(arrival_candle["low"], 0.85)
        self.assertEqual(arrival_candle["open"], 1.0)
        self.assertEqual(arrival_candle["close"], 1.25)
        self.assertEqual(arrival_candle["volume"], 350.0)

    def test_context_fetch_failure_degrades_to_in_trade_bars(self) -> None:
        """When daily_bar_fetch raises, build_chart_payload still returns status OK
        with the in-trade bars only (no crash, no context), markers intact."""
        in_trade = self._in_trade_bars()

        def boom(ticker, start, end):
            raise RuntimeError("polygon down")

        payload = _payload(in_trade, replay_ladder(_OK_SETUP, in_trade), daily_bar_fetch=boom)
        self.assertEqual(payload["status"], "OK")
        times = [b["time"] for b in payload["bars"]]
        # No bar before arrival (no context survived the failure).
        self.assertTrue(all(t >= _ARRIVAL.isoformat() for t in times))
        self.assertEqual(len(payload["bars"]), 2)  # exactly the two in-trade sessions
        self.assertTrue(payload["markers"])

    def test_closed_trade_trailing_capped_at_15(self) -> None:
        """A closed trade keeps at most TRAILING_SESSIONS (15) post-horizon context
        bars even when the fetch supplies more."""
        in_trade = self._in_trade_bars()  # spans arrival -> next session (the horizon)
        post = [advance_trading_sessions(_NEXT_SESSION, i, _EXCHANGE) for i in range(1, 30)]

        def daily_fetch(ticker, start, end):
            return [_daily_bar(s) for s in post]

        payload = _payload(
            in_trade,
            replay_ladder(_OK_SETUP, in_trade),
            horizon=_NEXT_SESSION,
            daily_bar_fetch=daily_fetch,
        )
        trailing = [b["time"] for b in payload["bars"] if b["time"] > _NEXT_SESSION.isoformat()]
        self.assertEqual(len(trailing), TRAILING_SESSIONS)  # 15, not 29

    def test_plan_preview_when_no_in_trade_bars_but_context_exists(self) -> None:
        """A freshly-started OPEN position with NO in-trade minute bars yet but a
        lead-in band of daily context bars renders its PLAN (status OK, context
        bars only, the entry/TP/stop price lines, NO markers — no fills yet)."""
        pre = _sessions_before(_ARRIVAL, 25)

        def daily_fetch(ticker, start, end):
            return [_daily_bar(s, o=40.0, h=41.0, low=39.0, c=40.5) for s in pre]

        payload = _payload(
            [],  # no in-trade minute bars yet
            replay_ladder(_OK_SETUP, []),
            daily_bar_fetch=daily_fetch,
        )
        self.assertEqual(payload["status"], "OK")
        bars = payload["bars"]
        times = [b["time"] for b in bars]
        self.assertEqual(times, sorted(times))  # date-ordered
        # All bars are context (before arrival) — the floor (20) still applies.
        self.assertGreaterEqual(len(bars), LEAD_IN_FLOOR)
        self.assertTrue(all(t < _ARRIVAL.isoformat() for t in times))
        # No fills yet -> no markers, but the plan IS drawn.
        self.assertEqual(payload["markers"], [])
        self.assertEqual(payload["price_lines"]["entry"], 100.0)
        self.assertEqual(payload["price_lines"]["tp"], [110.0])
        self.assertEqual(payload["price_lines"]["stop"], 95.0)

    def test_no_data_when_no_in_trade_and_no_context(self) -> None:
        """Empty in-trade bars AND an empty daily context fetch -> NO_DATA (no plan
        preview without any bars to anchor it)."""

        def daily_fetch(ticker, start, end):
            return []

        payload = _payload([], replay_ladder(_OK_SETUP, []), daily_bar_fetch=daily_fetch)
        self.assertEqual(payload["status"], "NO_DATA")
        self.assertEqual(payload["bars"], [])
        self.assertEqual(payload["markers"], [])

    def test_open_trade_trailing_only_up_to_available(self) -> None:
        """An open trade emits trailing context only for sessions the fetch
        actually supplies (no synthetic future bars)."""
        in_trade = self._in_trade_bars()  # spans arrival -> next session (the horizon)
        # Only TWO post-horizon sessions are available (the rest are future).
        post = [advance_trading_sessions(_NEXT_SESSION, i, _EXCHANGE) for i in range(1, 3)]

        def daily_fetch(ticker, start, end):
            return [_daily_bar(s) for s in post]

        payload = _payload(
            in_trade,
            replay_ladder(_OK_SETUP, in_trade),
            horizon=_NEXT_SESSION,
            daily_bar_fetch=daily_fetch,
        )
        trailing = [b["time"] for b in payload["bars"] if b["time"] > _NEXT_SESSION.isoformat()]
        self.assertEqual(len(trailing), 2)  # only the supplied ones, never padded


class TestHorizonSession(unittest.TestCase):
    def test_horizon_session_empty_bars_falls_back_to_arrival(self) -> None:
        """``_horizon_session`` over an empty bar list returns the arrival session
        (defensive fallback — no ``max()`` over an empty sequence)."""
        self.assertEqual(_horizon_session(_ARRIVAL, [], _EXCHANGE), _ARRIVAL)


def _write_store_row(store_dir: Path, brief_date: dt.date, ticker: str) -> None:
    store_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([{"brief_date": brief_date, "ticker": ticker, "forward_return": 0.05}]).to_parquet(
        store_dir / f"{brief_date.isoformat()}.parquet"
    )


def _write_brief(briefs_dir: Path, brief_date: dt.date, ticker: str, setup: dict) -> None:
    briefs_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        [
            {
                "ticker": ticker,
                "theme": "ai",
                "verified": True,
                "brief_trade_setup": json.dumps(setup),
            }
        ]
    ).to_parquet(briefs_dir / f"{brief_date.isoformat()}.parquet")


class TestEnrichStoreWithChartPayloads(unittest.TestCase):
    def test_enrich_store_writes_chart_payload_json_column(self) -> None:
        """Given a store row + a matching cached bars source, the enricher rewrites
        the store parquet with a ``chart_payload_json`` column whose value
        json.loads to a dict with the expected keys."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store_dir = root / "population_ladders"
            briefs_dir = root / "briefs"
            brief_date = _ARRIVAL  # arrival session == brief date (Friday session)
            _write_store_row(store_dir, brief_date, "NVDA")
            _write_brief(briefs_dir, brief_date, "NVDA", _OK_SETUP)

            synthetic = {
                ("NVDA", _ARRIVAL): [
                    _bar(_session_open_ms(_ARRIVAL), o=101.0, h=102.0, low=99.0, c=100.5),
                    _bar(_session_open_ms(_NEXT_SESSION), o=105.0, h=111.0, low=104.0, c=110.5),
                ]
            }

            def bar_fetch(ticker: str, arrival_session: dt.date) -> list[dict]:
                return synthetic.get((ticker.upper(), arrival_session), [])

            n = enrich_store_with_chart_payloads(
                store_dir,
                briefs_dir,
                bar_fetch=bar_fetch,
                daily_bar_fetch=lambda *_a, **_k: [],  # hermetic: no context, no real Polygon
                exchange=_EXCHANGE,
            )
            self.assertGreaterEqual(n, 1)

            df = pd.read_parquet(store_dir / f"{brief_date.isoformat()}.parquet")
            self.assertIn("chart_payload_json", df.columns)
            payload = json.loads(df.set_index("ticker").loc["NVDA", "chart_payload_json"])
            self.assertGreaterEqual(
                set(payload),
                {
                    "status",
                    "bars",
                    "price_lines",
                    "markers",
                    "ambiguous_bars",
                    "intrabar_rule",
                    "rth_only",
                },
            )
            self.assertEqual(payload["status"], "OK")
            self.assertTrue(payload["bars"])

    def test_enrich_no_bars_row_gets_no_data_payload(self) -> None:
        """A store row whose ticker has no cached bars persists a NO_DATA payload,
        never crashing the sweep."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store_dir = root / "population_ladders"
            briefs_dir = root / "briefs"
            brief_date = _ARRIVAL
            _write_store_row(store_dir, brief_date, "MISS")
            _write_brief(briefs_dir, brief_date, "MISS", _OK_SETUP)

            enrich_store_with_chart_payloads(
                store_dir,
                briefs_dir,
                bar_fetch=lambda *_a, **_k: [],
                daily_bar_fetch=lambda *_a, **_k: [],
                exchange=_EXCHANGE,
            )
            df = pd.read_parquet(store_dir / f"{brief_date.isoformat()}.parquet")
            payload = json.loads(df.set_index("ticker").loc["MISS", "chart_payload_json"])
            self.assertEqual(payload["status"], "NO_DATA")
            self.assertEqual(payload["bars"], [])

    def test_enrich_passes_daily_fetch_and_never_raises(self) -> None:
        """enrich with an injected daily_bar_fetch writes context bars into the
        payload; a daily_bar_fetch that raises still yields a valid (in-trade-only)
        OK payload, never crashing the row — for a row that genuinely needs a
        fetch (no usable prior context yet).

        PR-2 (reuse-first): once a row's context is established as OK, a SECOND
        enrich never re-attempts the daily fetch at all (needs_fetch is False),
        so a raising fetch on an ALREADY-established row is never even invoked —
        pinned below on the same NVDA row rather than degrading its context."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store_dir = root / "population_ladders"
            briefs_dir = root / "briefs"
            brief_date = _ARRIVAL
            _write_store_row(store_dir, brief_date, "NVDA")
            _write_brief(briefs_dir, brief_date, "NVDA", _OK_SETUP)

            minute_bars = {
                ("NVDA", _ARRIVAL): [
                    _bar(_session_open_ms(_ARRIVAL), o=101.0, h=102.0, low=99.0, c=100.5),
                    _bar(_session_open_ms(_NEXT_SESSION), o=105.0, h=111.0, low=104.0, c=110.5),
                ]
            }

            def bar_fetch(ticker, arrival_session):
                return minute_bars.get((ticker.upper(), arrival_session), [])

            pre = _sessions_before(_ARRIVAL, 25)

            def daily_fetch(ticker, start, end):
                return [_daily_bar(s, o=40.0, h=41.0, low=39.0, c=40.5) for s in pre]

            enrich_store_with_chart_payloads(
                store_dir,
                briefs_dir,
                bar_fetch=bar_fetch,
                daily_bar_fetch=daily_fetch,
                exchange=_EXCHANGE,
            )
            df = pd.read_parquet(store_dir / f"{brief_date.isoformat()}.parquet")
            payload = json.loads(df.set_index("ticker").loc["NVDA", "chart_payload_json"])
            self.assertEqual(payload["status"], "OK")
            times = [b["time"] for b in payload["bars"]]
            self.assertTrue(any(t < _ARRIVAL.isoformat() for t in times))  # context present

            # Reuse-first: NVDA's context is now established OK, so a second
            # enrich must NEVER call the (raising) daily fetch for it — the
            # reuse path splices the prior lead-in back in instead.
            def boom(ticker, start, end):
                raise AssertionError("established context must not re-fetch")

            enrich_store_with_chart_payloads(
                store_dir,
                briefs_dir,
                bar_fetch=bar_fetch,
                daily_bar_fetch=boom,
                exchange=_EXCHANGE,
            )
            df = pd.read_parquet(store_dir / f"{brief_date.isoformat()}.parquet")
            payload = json.loads(df.set_index("ticker").loc["NVDA", "chart_payload_json"])
            self.assertEqual(payload["status"], "OK")
            self.assertEqual(payload.get("context"), "reused")
            times = [b["time"] for b in payload["bars"]]
            self.assertTrue(any(t < _ARRIVAL.isoformat() for t in times))  # lead-in reused

        # A genuinely NEW row (no prior context yet, separate store) still
        # degrades gracefully to in-trade-only when its daily fetch raises.
        with tempfile.TemporaryDirectory() as tmp2:
            root2 = Path(tmp2)
            store_dir2, briefs_dir2 = root2 / "population_ladders", root2 / "briefs"
            _write_store_row(store_dir2, _ARRIVAL, "FRESH")
            _write_brief(briefs_dir2, _ARRIVAL, "FRESH", _OK_SETUP)

            def bar_fetch2(ticker, arrival_session):
                return [
                    _bar(_session_open_ms(_ARRIVAL), o=101.0, h=102.0, low=99.0, c=100.5),
                    _bar(_session_open_ms(_NEXT_SESSION), o=105.0, h=111.0, low=104.0, c=110.5),
                ]

            def boom2(ticker, start, end):
                raise RuntimeError("polygon down")

            enrich_store_with_chart_payloads(
                store_dir2,
                briefs_dir2,
                bar_fetch=bar_fetch2,
                daily_bar_fetch=boom2,
                exchange=_EXCHANGE,
            )
            df2 = pd.read_parquet(store_dir2 / f"{_ARRIVAL.isoformat()}.parquet")
            fresh_payload = json.loads(df2.set_index("ticker").loc["FRESH", "chart_payload_json"])
            self.assertEqual(fresh_payload["status"], "OK")
            fresh_times = [b["time"] for b in fresh_payload["bars"]]
            self.assertTrue(all(t >= _ARRIVAL.isoformat() for t in fresh_times))  # no context

    def test_enrich_caches_daily_fetch_per_ticker_window(self) -> None:
        """Two store rows with the SAME ticker + arrival/horizon share ONE daily
        context fetch (per-run memo cache); two DIFFERENT tickers fetch twice."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store_dir = root / "population_ladders"
            briefs_dir = root / "briefs"
            brief_date = _ARRIVAL

            # Two store rows for the SAME ticker on the same date (e.g. surfaced
            # under two themes) -> one (ticker, start, end) Polygon call, not two.
            store_dir.mkdir(parents=True, exist_ok=True)
            pd.DataFrame(
                [
                    {"brief_date": brief_date, "ticker": "NVDA", "forward_return": 0.05},
                    {"brief_date": brief_date, "ticker": "NVDA", "forward_return": 0.07},
                ]
            ).to_parquet(store_dir / f"{brief_date.isoformat()}.parquet")
            _write_brief(briefs_dir, brief_date, "NVDA", _OK_SETUP)

            minute_bars = {
                ("NVDA", _ARRIVAL): [
                    _bar(_session_open_ms(_ARRIVAL), o=101.0, h=102.0, low=99.0, c=100.5),
                    _bar(_session_open_ms(_NEXT_SESSION), o=105.0, h=111.0, low=104.0, c=110.5),
                ]
            }

            def bar_fetch(ticker, arrival_session):
                return minute_bars.get((ticker.upper(), arrival_session), [])

            pre = _sessions_before(_ARRIVAL, 25)
            calls: list[tuple] = []

            def daily_fetch(ticker, start, end):
                calls.append((ticker, start, end))
                return [_daily_bar(s, o=40.0, h=41.0, low=39.0, c=40.5) for s in pre]

            enrich_store_with_chart_payloads(
                store_dir,
                briefs_dir,
                bar_fetch=bar_fetch,
                daily_bar_fetch=daily_fetch,
                exchange=_EXCHANGE,
            )
            # Same ticker + identical (start, end) across both rows -> ONE fetch.
            self.assertEqual(len(calls), 1)

    def test_enrich_caches_daily_fetch_distinct_per_ticker(self) -> None:
        """Two DIFFERENT tickers each get their own daily context fetch (cache is
        keyed by (ticker, start, end), not shared across tickers)."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store_dir = root / "population_ladders"
            briefs_dir = root / "briefs"
            brief_date = _ARRIVAL

            store_dir.mkdir(parents=True, exist_ok=True)
            pd.DataFrame(
                [
                    {"brief_date": brief_date, "ticker": "NVDA", "forward_return": 0.05},
                    {"brief_date": brief_date, "ticker": "AMD", "forward_return": 0.07},
                ]
            ).to_parquet(store_dir / f"{brief_date.isoformat()}.parquet")
            briefs_dir.mkdir(parents=True, exist_ok=True)
            pd.DataFrame(
                [
                    {
                        "ticker": t,
                        "theme": "ai",
                        "verified": True,
                        "brief_trade_setup": json.dumps(_OK_SETUP),
                    }
                    for t in ("NVDA", "AMD")
                ]
            ).to_parquet(briefs_dir / f"{brief_date.isoformat()}.parquet")

            minute_bars = {
                (t, _ARRIVAL): [
                    _bar(_session_open_ms(_ARRIVAL), o=101.0, h=102.0, low=99.0, c=100.5),
                    _bar(_session_open_ms(_NEXT_SESSION), o=105.0, h=111.0, low=104.0, c=110.5),
                ]
                for t in ("NVDA", "AMD")
            }

            def bar_fetch(ticker, arrival_session):
                return minute_bars.get((ticker.upper(), arrival_session), [])

            pre = _sessions_before(_ARRIVAL, 25)
            tickers_fetched: list[str] = []

            def daily_fetch(ticker, start, end):
                tickers_fetched.append(ticker)
                return [_daily_bar(s, o=40.0, h=41.0, low=39.0, c=40.5) for s in pre]

            enrich_store_with_chart_payloads(
                store_dir,
                briefs_dir,
                bar_fetch=bar_fetch,
                daily_bar_fetch=daily_fetch,
                exchange=_EXCHANGE,
            )
            self.assertEqual(sorted(tickers_fetched), ["AMD", "NVDA"])


def _write_terminal_store_row(
    store_dir: Path,
    brief_date: dt.date,
    ticker: str,
    *,
    terminal: bool,
    chart_payload: dict | None,
    matured_at: dt.date | None = None,
) -> None:
    """Write a one-row store parquet carrying a ``terminal`` flag and (optionally)
    a pre-existing ``chart_payload_json`` — the shape the monitor's ``_terminal_row``
    / ``_ongoing_row`` produce, so the enricher's skip predicate has real inputs.
    ``matured_at`` (the close session the monitor stamps on terminal rows) is
    omitted from the frame when None, matching old-format rows."""
    store_dir.mkdir(parents=True, exist_ok=True)
    row: dict = {"brief_date": brief_date, "ticker": ticker, "forward_return": 0.05}
    row["terminal"] = terminal
    if matured_at is not None:
        row["matured_at"] = matured_at
    if chart_payload is not None:
        row["chart_payload_json"] = json.dumps(chart_payload)
    pd.DataFrame([row]).to_parquet(store_dir / f"{brief_date.isoformat()}.parquet")


class TestEnrichSkipsFrozenTerminalRows(unittest.TestCase):
    """The Polygon-429 fast-follow: a resolved (terminal) row whose chart is already
    OK never changes, so the nightly enrich pass must NOT re-fetch it. Cutting those
    fetches is what stops the pass starving the Polygon budget and hanging."""

    def _two_bars(self, arrival: dt.date, nxt: dt.date) -> list[dict]:
        return [
            _bar(_session_open_ms(arrival), o=101.0, h=102.0, low=99.0, c=100.5),
            _bar(_session_open_ms(nxt), o=105.0, h=111.0, low=104.0, c=110.5),
        ]

    def test_frozen_terminal_ok_row_is_not_refetched(self) -> None:
        """A terminal row that already carries an OK chart payload is preserved
        verbatim and its bars are NEVER fetched again."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store_dir = root / "population_ladders"
            briefs_dir = root / "briefs"
            frozen = {"status": "OK", "bars": [{"time": _ARRIVAL.isoformat()}], "markers": []}
            _write_terminal_store_row(
                store_dir, _ARRIVAL, "OLD", terminal=True, chart_payload=frozen
            )
            _write_brief(briefs_dir, _ARRIVAL, "OLD", _OK_SETUP)

            calls: list[str] = []

            def bar_fetch(ticker: str, arrival_session: dt.date) -> list[dict]:
                calls.append(ticker)
                return self._two_bars(_ARRIVAL, _NEXT_SESSION)

            n = enrich_store_with_chart_payloads(
                store_dir,
                briefs_dir,
                bar_fetch=bar_fetch,
                daily_bar_fetch=lambda *_a, **_k: [],
                exchange=_EXCHANGE,
            )
            self.assertEqual(calls, [])  # frozen -> no Polygon fetch at all
            df = pd.read_parquet(store_dir / f"{_ARRIVAL.isoformat()}.parquet")
            # Preserved byte-for-byte, and still counted as an OK chart.
            self.assertEqual(json.loads(df.loc[0, "chart_payload_json"]), frozen)
            self.assertGreaterEqual(n, 1)

    def test_terminal_row_missing_ok_chart_is_retried(self) -> None:
        """A terminal row whose existing chart is NO_DATA (a prior transient gap)
        is NOT frozen — it is re-priced so it can self-heal to OK."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store_dir = root / "population_ladders"
            briefs_dir = root / "briefs"
            _write_terminal_store_row(
                store_dir,
                _ARRIVAL,
                "HEAL",
                terminal=True,
                chart_payload={"status": "NO_DATA", "bars": []},
            )
            _write_brief(briefs_dir, _ARRIVAL, "HEAL", _OK_SETUP)

            calls: list[str] = []

            def bar_fetch(ticker: str, arrival_session: dt.date) -> list[dict]:
                calls.append(ticker)
                return self._two_bars(_ARRIVAL, _NEXT_SESSION)

            enrich_store_with_chart_payloads(
                store_dir,
                briefs_dir,
                bar_fetch=bar_fetch,
                daily_bar_fetch=lambda *_a, **_k: [],
                exchange=_EXCHANGE,
            )
            self.assertEqual(calls, ["HEAL"])  # retried
            df = pd.read_parquet(store_dir / f"{_ARRIVAL.isoformat()}.parquet")
            self.assertEqual(json.loads(df.loc[0, "chart_payload_json"])["status"], "OK")

    def test_ongoing_row_is_reprocessed_despite_ok_chart(self) -> None:
        """An ongoing (terminal=False) row is always re-priced even with an existing
        OK chart, because its price path can still extend the next night."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store_dir = root / "population_ladders"
            briefs_dir = root / "briefs"
            ok = {"status": "OK", "bars": [{"time": _ARRIVAL.isoformat()}], "markers": []}
            _write_terminal_store_row(store_dir, _ARRIVAL, "OPEN", terminal=False, chart_payload=ok)
            _write_brief(briefs_dir, _ARRIVAL, "OPEN", _OK_SETUP)

            calls: list[str] = []

            def bar_fetch(ticker: str, arrival_session: dt.date) -> list[dict]:
                calls.append(ticker)
                return self._two_bars(_ARRIVAL, _NEXT_SESSION)

            enrich_store_with_chart_payloads(
                store_dir,
                briefs_dir,
                bar_fetch=bar_fetch,
                daily_bar_fetch=lambda *_a, **_k: [],
                exchange=_EXCHANGE,
            )
            self.assertEqual(calls, ["OPEN"])  # ongoing -> always re-priced

    def test_free_tier_unconditional_every_parquet_visited(self) -> None:
        """REPLACES the removed newest-first heuristic (memo §5 item 14).

        The Polygon-free marker tier is UNCONDITIONAL: even with the deadline
        already exhausted, EVERY store parquet is still visited and every
        non-frozen row's cached minute bars are folded (no file-walk early
        break). So both files' tickers are fetched from the disk minute cache
        regardless of budget — the exit marker can never be starved out by
        queue position or maturation timing."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store_dir = root / "population_ladders"
            briefs_dir = root / "briefs"
            d_old, d_new = _ARRIVAL, _NEXT_SESSION  # 2026-05-01 < 2026-05-04
            _write_store_row(store_dir, d_old, "AAA")
            _write_store_row(store_dir, d_new, "BBB")
            _write_brief(briefs_dir, d_old, "AAA", _OK_SETUP)
            _write_brief(briefs_dir, d_new, "BBB", _OK_SETUP)

            fetched: list[str] = []

            def bar_fetch(ticker: str, arrival_session: dt.date) -> list[dict]:
                fetched.append(ticker)
                return [
                    _bar(_session_open_ms(_ARRIVAL), o=101.0, h=102.0, low=99.0, c=100.5),
                    _bar(_session_open_ms(_NEXT_SESSION), o=105.0, h=111.0, low=104.0, c=110.5),
                ]

            enrich_store_with_chart_payloads(
                store_dir,
                briefs_dir,
                bar_fetch=bar_fetch,
                daily_bar_fetch=lambda *_a, **_k: [],
                exchange=_EXCHANGE,
                deadline=_StubDeadline(stop=True),  # budget already exhausted
            )
            # Every parquet visited despite the tripped deadline (no early break).
            self.assertEqual(sorted(fetched), ["AAA", "BBB"])

    def test_all_frozen_file_is_not_rewritten(self) -> None:
        """A store parquet whose every row is frozen terminal-OK is NOT rewritten:
        the pass does zero I/O (not just zero fetches) on the fully-resolved tail,
        instead of re-persisting a byte-identical column every night."""
        import alphalens_pipeline.feedback.ladder_chart as lc

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store_dir = root / "population_ladders"
            briefs_dir = root / "briefs"
            frozen = {"status": "OK", "bars": [{"time": _ARRIVAL.isoformat()}], "markers": []}
            _write_terminal_store_row(
                store_dir, _ARRIVAL, "OLD", terminal=True, chart_payload=frozen
            )
            _write_brief(briefs_dir, _ARRIVAL, "OLD", _OK_SETUP)

            writes: list[str] = []
            orig_write = lc._write_atomic

            def spy(path, df):
                writes.append(Path(path).name)
                return orig_write(path, df)

            lc._write_atomic = spy
            try:
                enrich_store_with_chart_payloads(
                    store_dir,
                    briefs_dir,
                    bar_fetch=lambda *_a, **_k: [],
                    daily_bar_fetch=lambda *_a, **_k: [],
                    exchange=_EXCHANGE,
                )
            finally:
                lc._write_atomic = orig_write
            self.assertEqual(writes, [])  # fully-resolved file untouched


class TestFrozenChartStalenessAgainstMaturedAt(unittest.TestCase):
    """A terminal-OK chart is final ONLY when its last bar reaches the row's close
    (``matured_at``). The 2026-07-13 incident: a row that terminalized AFTER the
    last successful chart pass froze a chart ending 6 sessions before its close
    (BAH 2026-06-30: chart ends 07-02, CLOSED 07-10) — permanently, because the
    freeze predicate never compared the payload against ``matured_at``."""

    def _run_enrich(self, store_dir: Path, briefs_dir: Path) -> list[str]:
        calls: list[str] = []

        def bar_fetch(ticker: str, arrival_session: dt.date) -> list[dict]:
            calls.append(ticker)
            return [
                _bar(_session_open_ms(_ARRIVAL), o=101.0, h=102.0, low=99.0, c=100.5),
                _bar(_session_open_ms(_NEXT_SESSION), o=105.0, h=111.0, low=104.0, c=110.5),
            ]

        enrich_store_with_chart_payloads(
            store_dir,
            briefs_dir,
            bar_fetch=bar_fetch,
            daily_bar_fetch=lambda *_a, **_k: [],
            exchange=_EXCHANGE,
        )
        return calls

    def test_chart_ending_before_matured_at_is_repriced(self) -> None:
        """Terminal + OK but the last bar predates the close: the chart is STALE,
        not final — it must be rebuilt (the BAH regression)."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store_dir, briefs_dir = root / "population_ladders", root / "briefs"
            stale = {"status": "OK", "bars": [{"time": _ARRIVAL.isoformat()}], "markers": []}
            _write_terminal_store_row(
                store_dir,
                _ARRIVAL,
                "BAH",
                terminal=True,
                chart_payload=stale,
                matured_at=_NEXT_SESSION,
            )
            _write_brief(briefs_dir, _ARRIVAL, "BAH", _OK_SETUP)
            calls = self._run_enrich(store_dir, briefs_dir)
            self.assertEqual(calls, ["BAH"])  # stale terminal chart -> rebuilt
            df = pd.read_parquet(store_dir / f"{_ARRIVAL.isoformat()}.parquet")
            payload = json.loads(df.loc[0, "chart_payload_json"])
            self.assertEqual(payload["status"], "OK")
            self.assertEqual(payload["bars"][-1]["time"], _NEXT_SESSION.isoformat())

    def test_chart_reaching_matured_at_stays_frozen(self) -> None:
        """Terminal + OK with the last bar AT the close: genuinely final, frozen."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store_dir, briefs_dir = root / "population_ladders", root / "briefs"
            final = {
                "status": "OK",
                "bars": [{"time": _ARRIVAL.isoformat()}, {"time": _NEXT_SESSION.isoformat()}],
                "markers": [],
            }
            _write_terminal_store_row(
                store_dir,
                _ARRIVAL,
                "DONE",
                terminal=True,
                chart_payload=final,
                matured_at=_NEXT_SESSION,
            )
            _write_brief(briefs_dir, _ARRIVAL, "DONE", _OK_SETUP)
            calls = self._run_enrich(store_dir, briefs_dir)
            self.assertEqual(calls, [])  # final -> no fetch
            df = pd.read_parquet(store_dir / f"{_ARRIVAL.isoformat()}.parquet")
            self.assertEqual(json.loads(df.loc[0, "chart_payload_json"]), final)

    def test_terminal_ok_without_matured_at_stays_frozen(self) -> None:
        """An old-format terminal row with no ``matured_at`` column keeps the
        legacy behavior: frozen (no mass unfreeze of the pre-column tail)."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store_dir, briefs_dir = root / "population_ladders", root / "briefs"
            frozen = {"status": "OK", "bars": [{"time": _ARRIVAL.isoformat()}], "markers": []}
            _write_terminal_store_row(
                store_dir, _ARRIVAL, "OLD", terminal=True, chart_payload=frozen
            )
            _write_brief(briefs_dir, _ARRIVAL, "OLD", _OK_SETUP)
            calls = self._run_enrich(store_dir, briefs_dir)
            self.assertEqual(calls, [])

    def test_terminal_ok_with_empty_bars_is_repriced(self) -> None:
        """status OK with an empty bars list is inconsistent — re-price it."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store_dir, briefs_dir = root / "population_ladders", root / "briefs"
            broken = {"status": "OK", "bars": [], "markers": []}
            _write_terminal_store_row(
                store_dir,
                _ARRIVAL,
                "FIXME",
                terminal=True,
                chart_payload=broken,
                matured_at=_NEXT_SESSION,
            )
            _write_brief(briefs_dir, _ARRIVAL, "FIXME", _OK_SETUP)
            calls = self._run_enrich(store_dir, briefs_dir)
            self.assertEqual(calls, ["FIXME"])


def _entry_expiry_ms(arrival: dt.date, order_ttl_days: int) -> int:
    """The engine entry cutoff: open ms of the session ``order_ttl_days`` after arrival.

    Recomputed here from the public calendar helpers (NOT imported from the monitor)
    so the test independently pins the same math the classification path uses in
    ``population_ladder_monitor._engine_cutoffs``.
    """
    expiry_session = advance_trading_sessions(arrival, order_ttl_days, _EXCHANGE)
    return int(session_open_utc(expiry_session, _EXCHANGE).timestamp() * 1000)


def _position_expiry_ms(arrival: dt.date, position_ttl_days: int) -> int:
    expiry_session = advance_trading_sessions(arrival, position_ttl_days, _EXCHANGE)
    return int(session_open_utc(expiry_session, _EXCHANGE).timestamp() * 1000)


def _enrich_and_load(store_dir: Path, briefs_dir: Path, ticker: str, brief_date: dt.date, bars):
    """Run the store enricher with an injected (hermetic) bar source and return the
    persisted payload dict for ``ticker``."""

    def bar_fetch(t: str, arrival_session: dt.date) -> list[dict]:
        return bars if t.upper() == ticker and arrival_session == brief_date else []

    enrich_store_with_chart_payloads(
        store_dir,
        briefs_dir,
        bar_fetch=bar_fetch,
        daily_bar_fetch=lambda *_a, **_k: [],  # hermetic: no context, no real Polygon
        exchange=_EXCHANGE,
    )
    df = pd.read_parquet(store_dir / f"{brief_date.isoformat()}.parquet")
    return json.loads(df.set_index("ticker").loc[ticker, "chart_payload_json"])


class TestChartReplayHonoursTtlCutoffs(unittest.TestCase):
    """The chart payload's replay MUST use the same entry-TTL / position-TTL cutoffs
    as the classification (``population_ladder_monitor``). Otherwise the chart draws
    fills the status never recorded — the KVYO bug: status ``NO_FILL`` but an ``E1``
    entry marker, because the chart re-replayed without the entry cutoff so a limit
    touched only AFTER the order expired filled on the chart.

    These are the cross-cutting guards for chart-vs-status divergence: each first
    asserts the TTL-aware and TTL-less replays actually DISAGREE on the scenario
    (so the test cannot rot to a no-op), then pins the chart to the TTL-aware one.
    """

    def test_stale_entry_touched_after_ttl_draws_no_entry_marker(self) -> None:
        """Entry limit first touched AFTER the entry-TTL expiry -> NO_FILL, so the
        chart must carry NO entry (E*) marker (the KVYO regression)."""
        order_ttl = _OK_SETUP["order_ttl_days"]  # 7
        # arrival bar stays above the 100 entry (no touch); a much-later bar, well
        # past the entry-TTL expiry session, finally dips to the entry.
        stale_session = advance_trading_sessions(_ARRIVAL, order_ttl + 2, _EXCHANGE)
        bars = [
            _bar(_session_open_ms(_ARRIVAL), o=105.0, h=106.0, low=101.0, c=104.0),
            _bar(_session_open_ms(stale_session), o=101.0, h=102.0, low=99.0, c=100.5),
        ]

        # Anti-rot: the bug-path (TTL-less) replay DOES fill; the TTL-aware one does not.
        ttl_less = replay_ladder(_OK_SETUP, bars)
        ttl_aware = replay_ladder(
            _OK_SETUP,
            bars,
            entry_expiry_ms=_entry_expiry_ms(_ARRIVAL, order_ttl),
            position_expiry_ms=_position_expiry_ms(_ARRIVAL, TIME_STOP_DAYS),
        )
        self.assertIn("E1", [c.level_id for c in ttl_less.sequence])  # bug-path fills
        self.assertEqual(ttl_aware.classification, "NO_FILL")  # cutoff-aware does not

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store_dir, briefs_dir = root / "population_ladders", root / "briefs"
            _write_store_row(store_dir, _ARRIVAL, "KVYO")
            _write_brief(briefs_dir, _ARRIVAL, "KVYO", _OK_SETUP)
            payload = _enrich_and_load(store_dir, briefs_dir, "KVYO", _ARRIVAL, bars)

        self.assertEqual(payload["status"], "OK")  # bars exist -> a plan chart still renders
        entry_markers = [m for m in payload["markers"] if m["kind"] == "ENTRY"]
        self.assertEqual(entry_markers, [], "NO_FILL status must not draw an entry marker")

    def test_entry_touched_within_ttl_keeps_entry_marker(self) -> None:
        """Positive control: an entry touched INSIDE the entry-TTL window still draws
        its E1 marker (the fix must not suppress legitimate fills)."""
        bars = [
            _bar(_session_open_ms(_ARRIVAL), o=101.0, h=102.0, low=99.0, c=100.5),
            _bar(_session_open_ms(_NEXT_SESSION), o=100.0, h=101.0, low=99.5, c=100.0),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store_dir, briefs_dir = root / "population_ladders", root / "briefs"
            _write_store_row(store_dir, _ARRIVAL, "NVDA")
            _write_brief(briefs_dir, _ARRIVAL, "NVDA", _OK_SETUP)
            payload = _enrich_and_load(store_dir, briefs_dir, "NVDA", _ARRIVAL, bars)

        entry_markers = [m for m in payload["markers"] if m["kind"] == "ENTRY"]
        self.assertEqual([m["label"] for m in entry_markers], ["E1"])

    def test_open_position_past_position_ttl_draws_time_stop_marker(self) -> None:
        """An entry fills, then never hits TP/SL: at the position-TTL the engine forces
        a TIME_STOP. The chart must honour the position cutoff and draw that marker;
        without it the chart would show an open position that never resolves."""
        sessions = [_ARRIVAL]
        for _ in range(TIME_STOP_DAYS + 2):
            sessions.append(advance_trading_sessions(sessions[-1], 1, _EXCHANGE))
        # Arrival bar fills the entry (low 99 <= 100); every later bar drifts between
        # the stop (95) and the TP (110) so neither exit ever fires.
        bars = [_bar(_session_open_ms(_ARRIVAL), o=101.0, h=102.0, low=99.0, c=100.0)]
        bars += [
            _bar(_session_open_ms(s), o=100.0, h=101.0, low=99.0, c=100.0) for s in sessions[1:]
        ]

        # Anti-rot: TTL-less replay never time-stops; the TTL-aware one does.
        ttl_less = replay_ladder(_OK_SETUP, bars)
        ttl_aware = replay_ladder(
            _OK_SETUP,
            bars,
            entry_expiry_ms=_entry_expiry_ms(_ARRIVAL, _OK_SETUP["order_ttl_days"]),
            position_expiry_ms=_position_expiry_ms(_ARRIVAL, TIME_STOP_DAYS),
        )
        self.assertNotIn("TIME_STOP", [c.level_id for c in ttl_less.sequence])
        self.assertEqual(ttl_aware.classification, "TIME_STOP")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store_dir, briefs_dir = root / "population_ladders", root / "briefs"
            _write_store_row(store_dir, _ARRIVAL, "TSLA")
            _write_brief(briefs_dir, _ARRIVAL, "TSLA", _OK_SETUP)
            payload = _enrich_and_load(store_dir, briefs_dir, "TSLA", _ARRIVAL, bars)

        time_stops = [m for m in payload["markers"] if m["kind"] == "TIME_STOP"]
        self.assertEqual(len(time_stops), 1, "position-TTL must draw exactly one TIME_STOP marker")


class _StubDeadline:
    """A ``_RunDeadline``-shaped stub: ``should_stop()`` returns a fixed flag.

    The enrich pass only calls ``should_stop()`` on the deadline; a constant
    stub lets a test simulate an already-exhausted Polygon budget
    (``stop=True``) or an unbounded one (``stop=False``) without touching the
    monotonic clock.
    """

    def __init__(self, *, stop: bool) -> None:
        self._stop = stop

    def should_stop(self) -> bool:
        return self._stop


class _BudgetDeadline:
    """Allows the paid context band for the first ``allow`` rows, then stops.

    Each ``should_stop()`` call that returns ``False`` consumes one unit of the
    residual context budget; once exhausted every later call returns ``True``.
    Used to pin WHICH row spends the scarce context budget first (the free
    marker tier is unconditional and never consults the deadline in the fixed
    design)."""

    def __init__(self, *, allow: int) -> None:
        self._remaining = allow

    def should_stop(self) -> bool:
        if self._remaining > 0:
            self._remaining -= 1
            return False
        return True


# The FTRE regression geometry (memo §5 test 5): brief 2026-06-12, arrival
# 2026-06-16, matured 2026-07-23, sequence E1 -> TP1 (TP1 sold). These are the
# exact levels the live store row carried.
_FTRE_ARRIVAL = dt.date(2026, 6, 16)
_FTRE_MATURED = dt.date(2026, 7, 23)
_FTRE_ENTRY = 15.988738606366711
_FTRE_STOP = 10.855612341290005
_FTRE_TP1 = 18.670000076293945
_FTRE_SETUP = {
    "status": "OK",
    "schema_version": "1.0.0",
    "suggested_size_pct": 2.0,
    "disaster_stop": _FTRE_STOP,
    "atr": 1.0,
    "order_ttl_days": 7,
    "entry_tiers": [{"limit": _FTRE_ENTRY, "alloc_pct": 100.0}],
    "tp_tranches": [{"target": _FTRE_TP1, "tranche_pct": 100.0}],
}


def _lead_in_payload_bars(arrival: dt.date, n: int) -> list[dict]:
    """``n`` daily-candle dicts on the sessions strictly before ``arrival``.

    Shaped like the ``bars`` a prior ``chart_payload_json`` carries so a test
    can plant a reusable lead-in context band on an existing payload."""
    return [
        {
            "time": s.isoformat(),
            "open": 40.0,
            "high": 41.0,
            "low": 39.0,
            "close": 40.5,
            "volume": 1.0,
        }
        for s in _sessions_before(arrival, n)
    ]


class TestBuildChartContextStamp(unittest.TestCase):
    """``build_chart_payload`` stamps a ``context`` state on the payload so the
    freeze gate + anti-downgrade guard can tell a full-context chart from a
    marker-core-only one (memo §3a)."""

    def _in_trade(self) -> list[dict]:
        return [
            _bar(_session_open_ms(_ARRIVAL), o=101.0, h=102.0, low=99.0, c=100.5),
            _bar(_session_open_ms(_NEXT_SESSION), o=105.0, h=111.0, low=104.0, c=110.5),
        ]

    def test_context_ok_when_daily_fetch_supplied(self) -> None:
        """A supplied ``daily_bar_fetch`` that returns a lead-in bar -> the
        payload is stamped ``context == 'OK'`` and a pre-arrival bar is
        present."""
        in_trade = self._in_trade()
        pre = _sessions_before(_ARRIVAL, 25)

        def daily_fetch(ticker, start, end):
            return [_daily_bar(s, o=40.0, h=41.0, low=39.0, c=40.5) for s in pre]

        payload = _payload(
            in_trade, replay_ladder(_OK_SETUP, in_trade), daily_bar_fetch=daily_fetch
        )
        self.assertEqual(payload.get("context"), "OK")
        times = [b["time"] for b in payload["bars"]]
        self.assertTrue(any(t < _ARRIVAL.isoformat() for t in times))

    def test_context_in_trade_only_when_no_fetch_no_reuse(self) -> None:
        """No ``daily_bar_fetch`` and no reused context -> ``context ==
        'in_trade_only'`` with the markers still folded from the minute
        bars."""
        in_trade = self._in_trade()
        payload = _payload(in_trade, replay_ladder(_OK_SETUP, in_trade), daily_bar_fetch=None)
        self.assertEqual(payload.get("context"), "in_trade_only")
        self.assertTrue(payload["markers"])

    def test_context_reused_when_prior_bars_spliced(self) -> None:
        """With ``daily_bar_fetch=None`` but a ``reused_context_bars`` lead-in
        band supplied, the band is spliced in and stamped ``context ==
        'reused'`` while the fresh in-trade fold + exit marker stay."""
        in_trade = self._in_trade()
        reused = _lead_in_payload_bars(_ARRIVAL, 10)
        payload = build_chart_payload(
            _OK_SETUP,
            in_trade,
            replay_ladder(_OK_SETUP, in_trade),
            arrival_session=_ARRIVAL,
            horizon_session=_HORIZON,
            exchange=_EXCHANGE,
            ticker="NVDA",
            daily_bar_fetch=None,
            reused_context_bars=reused,
        )
        self.assertEqual(payload.get("context"), "reused")
        times = [b["time"] for b in payload["bars"]]
        self.assertTrue(any(t < _ARRIVAL.isoformat() for t in times))  # lead-in kept
        kinds = {m["kind"] for m in payload["markers"]}
        self.assertIn("ENTRY", kinds)
        self.assertIn("TP", kinds)  # fresh in-trade exit marker


class TestMergeBarsInTradeWinsOnOverlap(unittest.TestCase):
    """Direct pin of the ``_merge_bars`` tie-break: on any date overlap the
    in-trade minute-fold candle wins over a lead-in / trailing context bar so a
    daily [low, high] is never overwritten by a coarser context candle."""

    def test_in_trade_bar_overwrites_overlapping_context_bar(self) -> None:
        overlap = _NEXT_SESSION.isoformat()
        lead_in = [{"time": overlap, "open": 9.0, "high": 9.9, "low": 8.0, "close": 9.0}]
        in_trade = [{"time": overlap, "open": 1.1, "high": 1.3, "low": 0.85, "close": 1.25}]
        trailing = [{"time": overlap, "open": 5.0, "high": 5.5, "low": 4.5, "close": 5.0}]
        merged = {b["time"]: b for b in _merge_bars(lead_in, in_trade, trailing)}
        self.assertEqual(merged[overlap]["high"], 1.3)  # minute union wins
        self.assertEqual(merged[overlap]["low"], 0.85)


class TestReusedContextMergeInTradeWins(unittest.TestCase):
    """A reused prior-payload bar that now falls INSIDE the (extended) hold is
    dropped by ``_split_reused_context`` before it can reach ``_merge_bars`` — the
    fresh in-trade minute fold owns those sessions, so the daily candle stays the
    minute union (memo §5 test 4). The reuse path never hands ``_merge_bars`` an
    overlapping bar; the split owns that guarantee (the ``_merge_bars`` tie-break
    itself is pinned directly by :class:`TestMergeBarsInTradeWinsOnOverlap`)."""

    def test_reused_trailing_bar_inside_extended_hold_overwritten(self) -> None:
        open_ms = _session_open_ms(_ARRIVAL)
        next_open_ms = _session_open_ms(_NEXT_SESSION)
        # The hold now extends to _NEXT_SESSION (two in-trade minute bars).
        in_trade = [
            _bar(open_ms, o=1.0, h=1.2, low=0.9, c=1.1, v=100.0),
            _bar(next_open_ms, o=1.1, h=1.3, low=0.85, c=1.25, v=250.0),
        ]
        # A reused context bar is stamped ON _NEXT_SESSION with DIFFERENT
        # highs/lows — once that session is inside the extended hold
        # (arrival..horizon) _split_reused_context drops it, so the fresh minute
        # fold is the only candle for that session.
        reused = [
            {
                "time": _NEXT_SESSION.isoformat(),
                "open": 9.0,
                "high": 9.9,
                "low": 8.0,
                "close": 9.0,
                "volume": 1.0,
            }
        ]
        payload = build_chart_payload(
            _OK_SETUP,
            in_trade,
            replay_ladder(_OK_SETUP, in_trade),
            arrival_session=_ARRIVAL,
            horizon_session=_NEXT_SESSION,
            exchange=_EXCHANGE,
            ticker="NVDA",
            daily_bar_fetch=None,
            reused_context_bars=reused,
        )
        by_time = {b["time"]: b for b in payload["bars"]}
        candle = by_time[_NEXT_SESSION.isoformat()]
        self.assertEqual(candle["high"], 1.3)  # minute union, NOT the 9.9 reused bar
        self.assertEqual(candle["low"], 0.85)


class TestFreeTierAlwaysRebuiltPastDeadline(unittest.TestCase):
    """The Polygon-free marker tier is rebuilt UNCONDITIONALLY — an exhausted
    deadline gates only the cosmetic context band, never the exit marker (memo
    §3c). This is the class the FTRE staleness bug lives in."""

    def test_stale_terminal_gets_exit_marker_when_deadline_exhausted(self) -> None:
        """THE FTRE REGRESSION. A long-held terminal (brief 2026-06-16, matured
        2026-07-23) whose stored chart froze days early, an already-tripped
        deadline, and a ``daily_bar_fetch`` that raises if touched -> the
        rebuilt payload STILL carries the TP1 sell marker at maturity, status
        OK, and the daily fetch is never called."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store_dir, briefs_dir = root / "population_ladders", root / "briefs"
            stale = {
                "status": "OK",
                "context": "in_trade_only",
                "bars": [{"time": _FTRE_ARRIVAL.isoformat()}, {"time": "2026-07-14"}],
                "markers": [
                    {
                        "kind": "ENTRY",
                        "level_id": "E1",
                        "label": "E1",
                        "time": _FTRE_ARRIVAL.isoformat(),
                    }
                ],
            }
            _write_terminal_store_row(
                store_dir,
                _FTRE_ARRIVAL,
                "FTRE",
                terminal=True,
                chart_payload=stale,
                matured_at=_FTRE_MATURED,
            )
            _write_brief(briefs_dir, _FTRE_ARRIVAL, "FTRE", _FTRE_SETUP)

            ftre_bars = [
                # Arrival session: dips to the entry limit (low <= 15.9887) -> E1 fills.
                _bar(_session_open_ms(_FTRE_ARRIVAL), o=16.0, h=16.2, low=15.9, c=16.0),
                # Maturity session: rallies through the TP target (high >= 18.67) -> TP1 sells.
                _bar(_session_open_ms(_FTRE_MATURED), o=18.0, h=18.7, low=17.9, c=18.6),
            ]

            def bar_fetch(ticker: str, arrival_session: dt.date) -> list[dict]:
                if ticker.upper() == "FTRE" and arrival_session == _FTRE_ARRIVAL:
                    return ftre_bars
                return []

            daily_calls: list[tuple] = []

            def daily_boom(ticker, start, end):
                daily_calls.append((ticker, start, end))
                raise AssertionError("daily context fetch must not run past the deadline")

            enrich_store_with_chart_payloads(
                store_dir,
                briefs_dir,
                bar_fetch=bar_fetch,
                daily_bar_fetch=daily_boom,
                exchange=_EXCHANGE,
                deadline=_StubDeadline(stop=True),
            )

            df = pd.read_parquet(store_dir / f"{_FTRE_ARRIVAL.isoformat()}.parquet")
            payload = json.loads(df.set_index("ticker").loc["FTRE", "chart_payload_json"])
            self.assertEqual(payload["status"], "OK")
            self.assertEqual(daily_calls, [])  # marker core needs zero Polygon
            tp_markers = [m for m in payload["markers"] if m["kind"] == "TP"]
            self.assertTrue(tp_markers, "the TP1 sell marker must survive the tripped deadline")
            self.assertEqual(tp_markers[0]["label"], "TP1")
            self.assertEqual(tp_markers[0]["time"], _FTRE_MATURED.isoformat())
            self.assertAlmostEqual(tp_markers[0]["price"], _FTRE_TP1, places=4)

    def test_context_skipped_reuses_prior_lead_in(self) -> None:
        """Deadline exhausted, the prior payload carries a lead-in band -> the
        rebuilt payload keeps that lead-in, adds the fresh in-trade fold + exit
        marker, is stamped ``context == 'reused'``, and never fetches."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store_dir, briefs_dir = root / "population_ladders", root / "briefs"
            prior = {
                "status": "OK",
                "context": "OK",
                "bars": [
                    *_lead_in_payload_bars(_ARRIVAL, 5),
                    {"time": _ARRIVAL.isoformat(), "open": 1, "high": 1, "low": 1, "close": 1},
                ],
                "markers": [],
            }
            # Ongoing row so it is always reprocessed (not frozen).
            _write_terminal_store_row(
                store_dir, _ARRIVAL, "OPEN", terminal=False, chart_payload=prior
            )
            _write_brief(briefs_dir, _ARRIVAL, "OPEN", _OK_SETUP)

            def bar_fetch(ticker, arrival_session):
                return [
                    _bar(_session_open_ms(_ARRIVAL), o=101.0, h=102.0, low=99.0, c=100.5),
                    _bar(_session_open_ms(_NEXT_SESSION), o=105.0, h=111.0, low=104.0, c=110.5),
                ]

            def daily_boom(ticker, start, end):
                raise AssertionError("must not fetch when the deadline is exhausted")

            enrich_store_with_chart_payloads(
                store_dir,
                briefs_dir,
                bar_fetch=bar_fetch,
                daily_bar_fetch=daily_boom,
                exchange=_EXCHANGE,
                deadline=_StubDeadline(stop=True),
            )
            df = pd.read_parquet(store_dir / f"{_ARRIVAL.isoformat()}.parquet")
            payload = json.loads(df.loc[0, "chart_payload_json"])
            self.assertEqual(payload["status"], "OK")
            self.assertEqual(payload.get("context"), "reused")
            times = [b["time"] for b in payload["bars"]]
            self.assertTrue(any(t < _ARRIVAL.isoformat() for t in times))  # lead-in reused
            self.assertIn("TP", {m["kind"] for m in payload["markers"]})  # fresh exit marker

    def test_every_parquet_visited_no_early_break(self) -> None:
        """Two dated store files, deadline exhausted -> the OLDER file's
        non-frozen row still gets a free-tier OK payload with its exit marker
        (the file-walk early break is gone)."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store_dir, briefs_dir = root / "population_ladders", root / "briefs"
            d_old, d_new = _ARRIVAL, _NEXT_SESSION  # 2026-05-01 < 2026-05-04
            _write_store_row(store_dir, d_old, "AAA")
            _write_store_row(store_dir, d_new, "BBB")
            _write_brief(briefs_dir, d_old, "AAA", _OK_SETUP)
            _write_brief(briefs_dir, d_new, "BBB", _OK_SETUP)

            def bar_fetch(ticker, arrival_session):
                return [
                    _bar(_session_open_ms(_ARRIVAL), o=101.0, h=102.0, low=99.0, c=100.5),
                    _bar(_session_open_ms(_NEXT_SESSION), o=105.0, h=111.0, low=104.0, c=110.5),
                ]

            enrich_store_with_chart_payloads(
                store_dir,
                briefs_dir,
                bar_fetch=bar_fetch,
                daily_bar_fetch=lambda *_a, **_k: [],
                exchange=_EXCHANGE,
                deadline=_StubDeadline(stop=True),
            )
            df_old = pd.read_parquet(store_dir / f"{d_old.isoformat()}.parquet")
            # Defensive read: today the early break leaves the older file with no
            # chart_payload_json column at all, so surface it as a clean assertion
            # (older file was still visited -> its row carries an OK marker chart).
            raw = (
                df_old.set_index("ticker").loc["AAA", "chart_payload_json"]
                if "chart_payload_json" in df_old.columns
                else None
            )
            payload = json.loads(raw) if isinstance(raw, str) else {}
            self.assertEqual(payload.get("status"), "OK")  # older file was still visited
            self.assertTrue(payload.get("markers"))


class TestAntiDowngradeGuard(unittest.TestCase):
    """A fresh non-OK build never overwrites a stored OK payload (memo §3c) —
    a transient empty minute cache must not blank a chart that already shows the
    trade."""

    def test_empty_minute_cache_keeps_prior_ok_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store_dir, briefs_dir = root / "population_ladders", root / "briefs"
            prior = {
                "status": "OK",
                "context": "OK",
                "bars": [{"time": _ARRIVAL.isoformat()}, {"time": _NEXT_SESSION.isoformat()}],
                "markers": [
                    {"kind": "ENTRY", "level_id": "E1", "label": "E1", "time": _ARRIVAL.isoformat()}
                ],
            }
            # Ongoing row so the pass reprocesses it (frozen rows are a separate path).
            _write_terminal_store_row(
                store_dir, _ARRIVAL, "GAP", terminal=False, chart_payload=prior
            )
            _write_brief(briefs_dir, _ARRIVAL, "GAP", _OK_SETUP)
            before = pd.read_parquet(store_dir / f"{_ARRIVAL.isoformat()}.parquet").loc[
                0, "chart_payload_json"
            ]

            enrich_store_with_chart_payloads(
                store_dir,
                briefs_dir,
                bar_fetch=lambda *_a, **_k: [],  # empty minute cache -> a fresh NO_DATA build
                daily_bar_fetch=lambda *_a, **_k: [],
                exchange=_EXCHANGE,
            )
            after = pd.read_parquet(store_dir / f"{_ARRIVAL.isoformat()}.parquet").loc[
                0, "chart_payload_json"
            ]
            self.assertEqual(after, before)  # prior OK payload preserved byte-identical


class TestFrozenGateRequiresFullContext(unittest.TestCase):
    """``_is_frozen_terminal_ok`` freezes a terminal only when the chart reaches
    ``matured_at`` AND its context band is complete; a MISSING ``context`` key
    defaults to complete so the legacy DONE tail stays frozen (memo §3e)."""

    def _row(
        self, payload: dict, *, matured: dt.date | None = _NEXT_SESSION, terminal: bool = True
    ) -> dict:
        row: dict = {"terminal": terminal, CHART_PAYLOAD_COLUMN: json.dumps(payload)}
        if matured is not None:
            row["matured_at"] = matured
        return row

    def test_in_trade_only_terminal_not_frozen_and_reattempts_context(self) -> None:
        """context 'in_trade_only', last bar AT the close -> NOT frozen; a later
        budgeted run stamps context 'OK' then it freezes."""
        in_trade_only = {
            "status": "OK",
            "context": "in_trade_only",
            "bars": [{"time": _ARRIVAL.isoformat()}, {"time": _NEXT_SESSION.isoformat()}],
            "markers": [],
        }
        self.assertFalse(_is_frozen_terminal_ok(self._row(in_trade_only)))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store_dir, briefs_dir = root / "population_ladders", root / "briefs"
            _write_terminal_store_row(
                store_dir,
                _ARRIVAL,
                "REDO",
                terminal=True,
                chart_payload=in_trade_only,
                matured_at=_NEXT_SESSION,
            )
            _write_brief(briefs_dir, _ARRIVAL, "REDO", _OK_SETUP)

            def bar_fetch(ticker, arrival_session):
                return [
                    _bar(_session_open_ms(_ARRIVAL), o=101.0, h=102.0, low=99.0, c=100.5),
                    _bar(_session_open_ms(_NEXT_SESSION), o=105.0, h=111.0, low=104.0, c=110.5),
                ]

            pre = _sessions_before(_ARRIVAL, 25)

            def daily_fetch(ticker, start, end):
                return [_daily_bar(s, o=40.0, h=41.0, low=39.0, c=40.5) for s in pre]

            # Budgeted run: the in-trade-only terminal is re-priced and its
            # context band completed to OK.
            enrich_store_with_chart_payloads(
                store_dir,
                briefs_dir,
                bar_fetch=bar_fetch,
                daily_bar_fetch=daily_fetch,
                exchange=_EXCHANGE,
            )
            df = pd.read_parquet(store_dir / f"{_ARRIVAL.isoformat()}.parquet")
            payload = json.loads(df.loc[0, "chart_payload_json"])
            self.assertEqual(payload.get("context"), "OK")

            # Now it is context-complete + reaches the close -> a second run
            # freezes it (no fetch).
            calls: list[str] = []

            def track_bar_fetch(ticker, arrival_session):
                calls.append(ticker)
                return bar_fetch(ticker, arrival_session)

            enrich_store_with_chart_payloads(
                store_dir,
                briefs_dir,
                bar_fetch=track_bar_fetch,
                daily_bar_fetch=daily_fetch,
                exchange=_EXCHANGE,
            )
            self.assertEqual(calls, [])  # frozen once context is complete

    def test_full_context_terminal_freezes(self) -> None:
        """context 'OK', last bar AT the close -> frozen."""
        full = {
            "status": "OK",
            "context": "OK",
            "bars": [{"time": _ARRIVAL.isoformat()}, {"time": _NEXT_SESSION.isoformat()}],
            "markers": [],
        }
        self.assertTrue(_is_frozen_terminal_ok(self._row(full)))

    def test_legacy_payload_missing_context_key_stays_frozen(self) -> None:
        """An OK payload with NO ``context`` key, last bar AT the close -> frozen
        (the missing key defaults to complete; no re-fetch storm on the pre-fix
        DONE tail)."""
        legacy = {
            "status": "OK",
            "bars": [{"time": _ARRIVAL.isoformat()}, {"time": _NEXT_SESSION.isoformat()}],
            "markers": [],
        }
        self.assertTrue(_is_frozen_terminal_ok(self._row(legacy)))


class TestContextBudgetOrdering(unittest.TestCase):
    """The residual context budget is spent oldest-matured-first so a long-held
    maturer's cosmetic band is not perpetually starved behind newer rows (memo
    §3d)."""

    def test_long_held_maturer_gets_context_before_recent_ongoing(self) -> None:
        old_arrival = previous_trading_day(_ARRIVAL, _EXCHANGE)  # 2026-04-30, older file
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store_dir, briefs_dir = root / "population_ladders", root / "briefs"
            # Old-dated, long-held, freshly matured terminal (no chart yet).
            _write_terminal_store_row(
                store_dir,
                old_arrival,
                "LONGHELD",
                terminal=True,
                chart_payload=None,
                matured_at=_ARRIVAL,
            )
            # Newer-dated, still-ongoing row.
            _write_store_row(store_dir, _ARRIVAL, "RECENT")
            _write_brief(briefs_dir, old_arrival, "LONGHELD", _OK_SETUP)
            _write_brief(briefs_dir, _ARRIVAL, "RECENT", _OK_SETUP)

            bars_by_arrival = {
                old_arrival: [
                    _bar(_session_open_ms(old_arrival), o=101.0, h=102.0, low=99.0, c=100.5),
                    _bar(_session_open_ms(_ARRIVAL), o=105.0, h=111.0, low=104.0, c=110.5),
                ],
                _ARRIVAL: [
                    _bar(_session_open_ms(_ARRIVAL), o=101.0, h=102.0, low=99.0, c=100.5),
                    _bar(_session_open_ms(_NEXT_SESSION), o=105.0, h=111.0, low=104.0, c=110.5),
                ],
            }

            def bar_fetch(ticker, arrival_session):
                return bars_by_arrival.get(arrival_session, [])

            daily_tickers: list[str] = []
            pre = _sessions_before(old_arrival, 40)

            def daily_fetch(ticker, start, end):
                daily_tickers.append(ticker)
                return [_daily_bar(s, o=40.0, h=41.0, low=39.0, c=40.5) for s in pre]

            enrich_store_with_chart_payloads(
                store_dir,
                briefs_dir,
                bar_fetch=bar_fetch,
                daily_bar_fetch=daily_fetch,
                exchange=_EXCHANGE,
                deadline=_BudgetDeadline(allow=1),  # room for exactly one context band
            )
            # The long-held maturer wins the single context slot; the recent
            # ongoing row's band is deferred.
            self.assertIn("LONGHELD", daily_tickers)
            self.assertNotIn("RECENT", daily_tickers)

    def test_context_budget_bounded_by_real_wall_clock_deadline(self) -> None:
        """The scarce context band is rationed by the REAL time-based
        ``_RunDeadline``, spent oldest-matured-first, and the wall-clock genuinely
        bounds the fetch phase.

        Regression for the census-does-no-I/O bug: a zero-I/O grant census calls
        ``should_stop()`` between candidates without advancing the monotonic
        clock, so the real ``_RunDeadline`` returns the same verdict for every
        candidate — all rows get context (all-or-nothing) and nothing bounds the
        Polygon fetch phase. Here a fake monotonic clock advances ONLY when a
        daily-context fetch runs, and the budget fits ~2 of the 3 fetches: only
        the two OLDEST-matured rows may fetch, the newest is deferred. The old
        census granted all three; a live per-fetch deadline check grants two."""
        # Three terminal rows, matured oldest -> newest, no chart yet (all
        # non-frozen so all must be rebuilt), one per store file.
        a_old = previous_trading_day(previous_trading_day(_ARRIVAL, _EXCHANGE), _EXCHANGE)
        a_mid = previous_trading_day(_ARRIVAL, _EXCHANGE)
        a_new = _ARRIVAL
        arrivals = {"OLD": a_old, "MID": a_mid, "NEW": a_new}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store_dir, briefs_dir = root / "population_ladders", root / "briefs"
            for tkr, arr in arrivals.items():
                _write_terminal_store_row(
                    store_dir,
                    arr,
                    tkr,
                    terminal=True,
                    chart_payload=None,
                    matured_at=advance_trading_sessions(arr, 1, _EXCHANGE),
                )
                _write_brief(briefs_dir, arr, tkr, _OK_SETUP)

            def bar_fetch(ticker, arrival_session):
                return [
                    _bar(_session_open_ms(arrival_session), o=101.0, h=102.0, low=99.0, c=100.5),
                    _bar(
                        _session_open_ms(advance_trading_sessions(arrival_session, 1, _EXCHANGE)),
                        o=105.0,
                        h=111.0,
                        low=104.0,
                        c=110.5,
                    ),
                ]

            # A fake monotonic clock that advances ONLY when a context fetch runs;
            # the free-tier build (bar_fetch + replay + fold) costs no clock time.
            clock = {"t": 0.0}
            fetch_cost_s = 1.0
            daily_tickers: list[str] = []

            def daily_fetch(ticker, start, end):
                daily_tickers.append(ticker)
                clock["t"] += fetch_cost_s
                pre = _sessions_before(arrivals[ticker], 25)
                return [_daily_bar(s, o=40.0, h=41.0, low=39.0, c=40.5) for s in pre]

            # Budget fits exactly two fetches: the deadline is checked BEFORE each
            # fetch, so 1.0 (after OLD) < 1.5 and 2.0 (after MID) >= 1.5 -> the
            # third (NEW) is deferred.
            deadline = _RunDeadline(1.5, monotonic=lambda: clock["t"])
            enrich_store_with_chart_payloads(
                store_dir,
                briefs_dir,
                bar_fetch=bar_fetch,
                daily_bar_fetch=daily_fetch,
                exchange=_EXCHANGE,
                deadline=deadline,
            )
            # Wall-clock bounded the fetch phase to the two oldest maturers; the
            # newest was deferred (the old all-or-nothing census fetched all three).
            self.assertEqual(daily_tickers, ["OLD", "MID"])


class TestFrozenSkipPreserved(unittest.TestCase):
    """A frozen terminal-OK row (context complete, chart at the close) is still
    preserved verbatim with zero fetches under the new gate (memo §5 test 13 —
    the §3e change must not regress the frozen fast-path)."""

    def test_frozen_rows_never_fetched_and_preserved_verbatim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store_dir, briefs_dir = root / "population_ladders", root / "briefs"
            frozen = {
                "status": "OK",
                "context": "OK",
                "bars": [{"time": _ARRIVAL.isoformat()}, {"time": _NEXT_SESSION.isoformat()}],
                "markers": [
                    {"kind": "ENTRY", "level_id": "E1", "label": "E1", "time": _ARRIVAL.isoformat()}
                ],
            }
            _write_terminal_store_row(
                store_dir,
                _ARRIVAL,
                "DONE",
                terminal=True,
                chart_payload=frozen,
                matured_at=_NEXT_SESSION,
            )
            _write_brief(briefs_dir, _ARRIVAL, "DONE", _OK_SETUP)
            before = pd.read_parquet(store_dir / f"{_ARRIVAL.isoformat()}.parquet").loc[
                0, "chart_payload_json"
            ]

            bar_calls: list[str] = []
            daily_calls: list[str] = []

            def bar_fetch(ticker, arrival_session):
                bar_calls.append(ticker)
                return []

            def daily_fetch(ticker, start, end):
                daily_calls.append(ticker)
                return []

            enrich_store_with_chart_payloads(
                store_dir,
                briefs_dir,
                bar_fetch=bar_fetch,
                daily_bar_fetch=daily_fetch,
                exchange=_EXCHANGE,
            )
            after = pd.read_parquet(store_dir / f"{_ARRIVAL.isoformat()}.parquet").loc[
                0, "chart_payload_json"
            ]
            self.assertEqual(after, before)  # preserved byte-identical
            self.assertEqual(bar_calls, [])
            self.assertEqual(daily_calls, [])


class TestPriorContextIsOk(unittest.TestCase):
    """``_prior_context_is_ok`` gates PR-2's reuse-first policy: a prior payload
    is reusable only when it parses, ``status == 'OK'``, AND its context tier is
    complete (``context == 'OK'``). Keyed on ``context`` — NOT on non-empty
    ``bars`` — so a delisted ongoing ticker's empty band (stamped ``CONTEXT_OK``)
    is reused, never re-fetched forever."""

    def test_ok_status_and_ok_context(self) -> None:
        from alphalens_pipeline.feedback.ladder_chart import _prior_context_is_ok

        self.assertTrue(
            _prior_context_is_ok(json.dumps({"status": "OK", "context": "OK", "bars": []}))
        )

    def test_missing_context_key_defaults_ok(self) -> None:
        from alphalens_pipeline.feedback.ladder_chart import _prior_context_is_ok

        self.assertTrue(_prior_context_is_ok(json.dumps({"status": "OK", "bars": []})))

    def test_reused_context_is_not_ok(self) -> None:
        from alphalens_pipeline.feedback.ladder_chart import _prior_context_is_ok

        self.assertFalse(
            _prior_context_is_ok(json.dumps({"status": "OK", "context": "reused", "bars": []}))
        )

    def test_in_trade_only_is_not_ok(self) -> None:
        from alphalens_pipeline.feedback.ladder_chart import _prior_context_is_ok

        self.assertFalse(
            _prior_context_is_ok(json.dumps({"status": "OK", "context": "in_trade_only"}))
        )

    def test_non_ok_status_is_not_ok(self) -> None:
        from alphalens_pipeline.feedback.ladder_chart import _prior_context_is_ok

        self.assertFalse(_prior_context_is_ok(json.dumps({"status": "NO_DATA", "context": "OK"})))

    def test_missing_or_unparseable_is_not_ok(self) -> None:
        from alphalens_pipeline.feedback.ladder_chart import _prior_context_is_ok

        self.assertFalse(_prior_context_is_ok(None))
        self.assertFalse(_prior_context_is_ok(""))
        self.assertFalse(_prior_context_is_ok("{not json"))


class _CountingDailyFetch:
    """A ``DailyBarFetch`` fake that counts invocations (PR-2 reuse-first pins)."""

    def __init__(self, bars_by_call: list[dict] | None = None) -> None:
        self.calls = 0
        self._bars = bars_by_call or []

    def __call__(self, ticker: str, start: dt.datetime, end: dt.datetime) -> list[dict]:
        self.calls += 1
        return list(self._bars)


def _in_trade_bars() -> list[dict]:
    return [
        _bar(_session_open_ms(_ARRIVAL), o=101.0, h=102.0, low=99.0, c=100.5),
        _bar(_session_open_ms(_NEXT_SESSION), o=105.0, h=111.0, low=104.0, c=110.5),
    ]


class TestReuseFirstContextPolicy(unittest.TestCase):
    """PR-2: an ongoing row with a usable prior OK-context band REUSES it — zero
    fetch — even when the Polygon budget has room. The context band is a nightly
    re-fetch only for genuine new work: a fresh arrival (no prior context yet) or
    a terminal not yet frozen (one-time trailing band)."""

    def test_established_ongoing_makes_zero_daily_fetches_with_budget_available(self) -> None:
        """Ongoing row whose prior payload already carries context=='OK' with a
        lead-in. Deadline never stops (budget available). Reuse-first => the
        daily fetch is never called across the enrich pass."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store_dir, briefs_dir = root / "population_ladders", root / "briefs"
            prior = {
                "status": "OK",
                "context": "OK",
                "bars": [
                    *_lead_in_payload_bars(_ARRIVAL, 30),
                    {"time": _ARRIVAL.isoformat(), "open": 1, "high": 1, "low": 1, "close": 1},
                ],
                "markers": [],
            }
            _write_terminal_store_row(
                store_dir, _ARRIVAL, "AAA", terminal=False, chart_payload=prior
            )
            _write_brief(briefs_dir, _ARRIVAL, "AAA", _OK_SETUP)

            daily = _CountingDailyFetch()
            enrich_store_with_chart_payloads(
                store_dir,
                briefs_dir,
                bar_fetch=lambda ticker, arrival_session: _in_trade_bars(),
                daily_bar_fetch=daily,
                exchange=_EXCHANGE,
                deadline=_StubDeadline(stop=False),
            )
            self.assertEqual(daily.calls, 0)

    def test_brand_new_ongoing_fetches_once_then_reuses(self) -> None:
        """First enrich: no prior context -> exactly one fetch. Second enrich
        over the now-populated store: zero further fetches."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store_dir, briefs_dir = root / "population_ladders", root / "briefs"
            _write_store_row(store_dir, _ARRIVAL, "BBB")
            _write_brief(briefs_dir, _ARRIVAL, "BBB", _OK_SETUP)

            daily = _CountingDailyFetch()

            def run() -> None:
                enrich_store_with_chart_payloads(
                    store_dir,
                    briefs_dir,
                    bar_fetch=lambda ticker, arrival_session: _in_trade_bars(),
                    daily_bar_fetch=daily,
                    exchange=_EXCHANGE,
                    deadline=_StubDeadline(stop=False),
                )

            run()
            self.assertEqual(daily.calls, 1)

            run()
            self.assertEqual(daily.calls, 1)  # unchanged — reused this time

    def test_terminal_not_yet_frozen_fetches_once_then_freezes(self) -> None:
        """A terminal row without a context band yet fetches its trailing band
        exactly once, then the freeze gate reports it final."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store_dir, briefs_dir = root / "population_ladders", root / "briefs"
            _write_terminal_store_row(
                store_dir,
                _ARRIVAL,
                "CCC",
                terminal=True,
                chart_payload=None,
                matured_at=_NEXT_SESSION,
            )
            _write_brief(briefs_dir, _ARRIVAL, "CCC", _OK_SETUP)

            daily = _CountingDailyFetch()
            enrich_store_with_chart_payloads(
                store_dir,
                briefs_dir,
                bar_fetch=lambda ticker, arrival_session: _in_trade_bars(),
                daily_bar_fetch=daily,
                exchange=_EXCHANGE,
                deadline=_StubDeadline(stop=False),
            )
            self.assertEqual(daily.calls, 1)

            df = pd.read_parquet(store_dir / f"{_ARRIVAL.isoformat()}.parquet")
            row = df.set_index("ticker").loc["CCC"].to_dict()
            self.assertTrue(_is_frozen_terminal_ok(row))


if __name__ == "__main__":
    unittest.main()
