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
import tempfile
import unittest
from pathlib import Path

import pandas as pd
from alphalens_pipeline.feedback.ladder_chart import (
    build_chart_payload,
    enrich_store_with_chart_payloads,
)
from alphalens_pipeline.feedback.ladder_replay import replay_ladder
from alphalens_pipeline.paper.calendar import session_open_utc

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


def _payload(bars: list[dict], outcome, *, setup=_OK_SETUP, horizon=_HORIZON) -> dict:
    return build_chart_payload(
        setup,
        bars,
        outcome,
        arrival_session=_ARRIVAL,
        horizon_session=horizon,
        exchange=_EXCHANGE,
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
                store_dir, briefs_dir, bar_fetch=bar_fetch, exchange=_EXCHANGE
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
                store_dir, briefs_dir, bar_fetch=lambda *_a, **_k: [], exchange=_EXCHANGE
            )
            df = pd.read_parquet(store_dir / f"{brief_date.isoformat()}.parquet")
            payload = json.loads(df.set_index("ticker").loc["MISS", "chart_payload_json"])
            self.assertEqual(payload["status"], "NO_DATA")
            self.assertEqual(payload["bars"], [])


if __name__ == "__main__":
    unittest.main()
