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
    LEAD_IN_CAP,
    LEAD_IN_FLOOR,
    TRAILING_SESSIONS,
    build_chart_payload,
    enrich_store_with_chart_payloads,
)
from alphalens_pipeline.feedback.ladder_replay import replay_ladder
from alphalens_pipeline.paper.calendar import (
    advance_trading_sessions,
    previous_trading_day,
    session_open_utc,
)

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
        OK payload, never crashing the row."""
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

            # A raising daily_bar_fetch must still produce a valid in-trade-only OK
            # payload (never crash the row).
            def boom(ticker, start, end):
                raise RuntimeError("polygon down")

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
            times = [b["time"] for b in payload["bars"]]
            self.assertTrue(all(t >= _ARRIVAL.isoformat() for t in times))  # no context survived


if __name__ == "__main__":
    unittest.main()
