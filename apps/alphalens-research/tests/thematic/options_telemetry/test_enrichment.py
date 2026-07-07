"""Enricher contract tests: window rule, freeze/carry-forward, NaN discipline."""

from __future__ import annotations

import datetime as dt
import math
import unittest

import pandas as pd
from alphalens_pipeline.thematic.options_telemetry import enrichment as en
from alphalens_pipeline.thematic.options_telemetry import features as f

ASOF = dt.date(2026, 7, 6)  # Monday, regular XNYS session (close 20:00 UTC)
IN_WINDOW = dt.datetime(2026, 7, 7, 0, 30, tzinfo=dt.UTC)  # 00:30 UTC slot
OUT_OF_WINDOW = dt.datetime(2026, 7, 7, 16, 30, tzinfo=dt.UTC)  # during next session


def _frame(tickers=("QUBT", "IONQ")) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "theme": ["quantum"] * len(tickers),
            "ticker": list(tickers),
            "company_name": [f"{t} Corp" for t in tickers],
        }
    )


def _chain_frame(iv=0.5, oi=100, vol=10, bid=3.0, ask=3.2) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "strike": [100.0],
            "bid": [bid],
            "ask": [ask],
            "impliedVolatility": [iv],
            "openInterest": [oi],
            "volume": [vol],
        }
    )


def _good_snapshot(ticker: str, asof: dt.date) -> en.TickerSnapshot:
    near = asof + dt.timedelta(days=18)
    far = asof + dt.timedelta(days=46)
    term = asof + dt.timedelta(days=186)
    return en.TickerSnapshot(
        spot=100.0,
        expiries=[near, far, term],
        chains={
            near: (_chain_frame(iv=0.60), _chain_frame(iv=0.62)),
            far: (_chain_frame(iv=0.55), _chain_frame(iv=0.57)),
            term: (_chain_frame(iv=0.45), _chain_frame(iv=0.47)),
        },
        fetch_failed=False,
    )


def _no_options_snapshot(ticker: str, asof: dt.date) -> en.TickerSnapshot:
    return en.TickerSnapshot(spot=100.0, expiries=[], chains={}, fetch_failed=False)


def _failed_snapshot(ticker: str, asof: dt.date) -> en.TickerSnapshot:
    return en.TickerSnapshot(spot=None, expiries=[], chains={}, fetch_failed=True)


class TestStampWindow(unittest.TestCase):
    def test_window_for_monday_session(self):
        close, nxt_open = en.stamp_window_utc(ASOF)
        self.assertEqual(close, dt.datetime(2026, 7, 6, 20, 0, tzinfo=dt.UTC))
        self.assertEqual(nxt_open, dt.datetime(2026, 7, 7, 13, 30, tzinfo=dt.UTC))

    def test_weekend_asof_rolls_to_prior_session(self):
        # Sunday asof -> Friday 2026-07-03 session window... 2026-07-03 is the
        # Independence Day observed holiday; the prior session is Thu 07-02.
        close, _ = en.stamp_window_utc(dt.date(2026, 7, 5))
        self.assertEqual(close.date(), dt.date(2026, 7, 2))


class TestEnrichInWindow(unittest.TestCase):
    def test_stamps_all_columns(self):
        out = en.enrich(_frame(), asof=ASOF, now_utc=IN_WINDOW, snapshot_fn=_good_snapshot)
        for col in en.OPTIONS_COLUMNS:
            self.assertIn(col, out.columns)
        row = out.iloc[0]
        # near 18 DTE @ 0.61 mid, far 46 DTE @ 0.56 mid -> 30 DTE interp
        self.assertAlmostEqual(
            row["options_ivx30"], 0.61 + (30 - 18) / (46 - 18) * (0.56 - 0.61), places=6
        )
        self.assertAlmostEqual(row["options_term_slope"], 0.46 - row["options_ivx30"], places=6)
        self.assertEqual(row["options_chain_quality"], f.CHAIN_QUALITY_OK)
        self.assertEqual(row["options_snapshot_utc"], IN_WINDOW.isoformat())
        self.assertEqual(row["options_config_version"], f.OPTIONS_CONFIG_VERSION)
        self.assertEqual(row["options_put_vol"], 20.0)  # near+far puts: 10+10
        self.assertEqual(row["options_call_oi"], 200.0)  # near+far calls: 100+100
        self.assertEqual(
            row["options_asof_expiry_near"], (ASOF + dt.timedelta(days=18)).isoformat()
        )

    def test_no_listed_options_is_quality_none(self):
        out = en.enrich(_frame(), asof=ASOF, now_utc=IN_WINDOW, snapshot_fn=_no_options_snapshot)
        row = out.iloc[0]
        self.assertEqual(row["options_chain_quality"], f.CHAIN_QUALITY_NONE)
        self.assertTrue(math.isnan(row["options_ivx30"]))
        self.assertTrue(math.isnan(row["options_put_vol"]))
        self.assertIsNotNone(out.iloc[0]["options_snapshot_utc"])

    def test_fetch_failure_is_quality_none_and_never_raises(self):
        out = en.enrich(_frame(), asof=ASOF, now_utc=IN_WINDOW, snapshot_fn=_failed_snapshot)
        self.assertEqual(out.iloc[0]["options_chain_quality"], f.CHAIN_QUALITY_NONE)
        self.assertIsNotNone(out.iloc[0]["options_snapshot_utc"])

    def test_feature_exception_degrades_to_none_quality(self):
        def _raising(ticker: str, asof: dt.date):
            raise RuntimeError("boom")

        out = en.enrich(_frame(), asof=ASOF, now_utc=IN_WINDOW, snapshot_fn=_raising)
        self.assertEqual(out.iloc[0]["options_chain_quality"], f.CHAIN_QUALITY_NONE)

    def test_duplicate_ticker_rows_get_identical_values_one_fetch(self):
        calls = {"n": 0}

        def _counting(ticker: str, asof: dt.date):
            calls["n"] += 1
            return _good_snapshot(ticker, asof)

        frame = _frame(tickers=("QUBT", "QUBT"))
        out = en.enrich(frame, asof=ASOF, now_utc=IN_WINDOW, snapshot_fn=_counting)
        self.assertEqual(calls["n"], 1)
        self.assertEqual(out.iloc[0]["options_ivx30"], out.iloc[1]["options_ivx30"])


class TestEnrichWindowAndFreeze(unittest.TestCase):
    def test_out_of_window_no_previous_leaves_nulls(self):
        out = en.enrich(_frame(), asof=ASOF, now_utc=OUT_OF_WINDOW, snapshot_fn=_good_snapshot)
        row = out.iloc[0]
        self.assertTrue(math.isnan(row["options_ivx30"]))
        self.assertIsNone(row["options_snapshot_utc"])
        self.assertIsNone(row["options_chain_quality"])
        self.assertEqual(row["options_config_version"], f.OPTIONS_CONFIG_VERSION)

    def test_out_of_window_carries_previous_stamp(self):
        first = en.enrich(_frame(), asof=ASOF, now_utc=IN_WINDOW, snapshot_fn=_good_snapshot)
        out = en.enrich(
            _frame(),
            asof=ASOF,
            now_utc=OUT_OF_WINDOW,
            previous=first,
            snapshot_fn=_failed_snapshot,  # must not matter: no fetch out of window
        )
        pd.testing.assert_series_equal(
            out["options_ivx30"], first["options_ivx30"], check_names=False
        )
        self.assertEqual(out.iloc[0]["options_snapshot_utc"], IN_WINDOW.isoformat())

    def test_in_window_previous_stamp_freezes_no_refetch(self):
        first = en.enrich(_frame(), asof=ASOF, now_utc=IN_WINDOW, snapshot_fn=_good_snapshot)
        calls = {"n": 0}

        def _counting(ticker: str, asof: dt.date):
            calls["n"] += 1
            return _good_snapshot(ticker, asof)

        later = dt.datetime(2026, 7, 7, 4, 30, tzinfo=dt.UTC)  # still in window
        out = en.enrich(_frame(), asof=ASOF, now_utc=later, previous=first, snapshot_fn=_counting)
        self.assertEqual(calls["n"], 0)
        self.assertEqual(out.iloc[0]["options_snapshot_utc"], IN_WINDOW.isoformat())

    def test_previous_unstamped_row_refetches_in_window(self):
        # Previous run was out-of-window (nulls) -> this in-window run stamps.
        unstamped = en.enrich(
            _frame(), asof=ASOF, now_utc=OUT_OF_WINDOW, snapshot_fn=_good_snapshot
        )
        out = en.enrich(
            _frame(),
            asof=ASOF,
            now_utc=IN_WINDOW,
            previous=unstamped,
            snapshot_fn=_good_snapshot,
        )
        self.assertEqual(out.iloc[0]["options_chain_quality"], f.CHAIN_QUALITY_OK)

    def test_previous_with_nan_marker_is_not_frozen(self):
        # A parquet round-trip can turn None markers into NaN/pd.NA; such a
        # row was never stamped and must NOT freeze the ticker.
        import tempfile
        from pathlib import Path

        unstamped = en.enrich(
            _frame(), asof=ASOF, now_utc=OUT_OF_WINDOW, snapshot_fn=_good_snapshot
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "prev.parquet"
            unstamped.to_parquet(path, index=False)
            roundtripped = pd.read_parquet(path)
        out = en.enrich(
            _frame(),
            asof=ASOF,
            now_utc=IN_WINDOW,
            previous=roundtripped,
            snapshot_fn=_good_snapshot,
        )
        self.assertEqual(out.iloc[0]["options_chain_quality"], f.CHAIN_QUALITY_OK)


if __name__ == "__main__":
    unittest.main()
