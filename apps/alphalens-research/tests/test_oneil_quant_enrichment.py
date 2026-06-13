"""The O'Neil score-stage enrichment — 8 always-present columns, technical reuse,
tri-state bool-as-float, no-new-network preload, split gating, fail-soft.
"""

from __future__ import annotations

import datetime as dt
import unittest

import pandas as pd
from alphalens_pipeline.experts.oneil.comparison import ONeilPanel
from alphalens_pipeline.experts.oneil.quant_enrichment import ONEIL_COLUMNS, enrich

ASOF = dt.date(2026, 5, 1)


def _frame(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows)


def _panel(ticker="AAA", **overrides) -> ONeilPanel:
    base: dict = {
        "ticker": ticker,
        "theme": "t",
        "pct_off_52w_high": -3.0,
        "ma200_slope_pct_per_day": 0.05,
        "ma200_distance_pct": 8.0,
        "earnings_growth_yoy_pct": 20.0,
        "earnings_growth_near_zero_base": False,
        "new_high_split_suspected": False,
        "data_coverage": 1.0,
    }
    base.update(overrides)
    return ONeilPanel(**base)


class TestSchemaStability(unittest.TestCase):
    def test_seven_columns_always_added_empty_frame(self):
        out = enrich(_frame([]), asof=ASOF)
        for col in ONEIL_COLUMNS:
            self.assertIn(col, out.columns)
            self.assertEqual(out[col].dtype, "float64")
        self.assertEqual(len(out), 0)

    def test_all_none_degraded_path_keeps_dtype(self):
        frame = _frame([{"ticker": "AAA", "theme": "t"}, {"ticker": "BBB", "theme": "t"}])
        out = enrich(frame, asof=ASOF, panel_fn=lambda t, th, a, tech: None)
        for col in ONEIL_COLUMNS:
            self.assertEqual(out[col].dtype, "float64")
            self.assertTrue(out[col].isna().all())

    def test_columns_count_is_eight(self):
        self.assertEqual(len(ONEIL_COLUMNS), 8)


class TestBoolAsFloat(unittest.TestCase):
    def test_bool_columns_emit_float_tristate(self):
        frame = _frame(
            [
                {"ticker": "TRU", "theme": "t"},
                {"ticker": "FAL", "theme": "t"},
                {"ticker": "NON", "theme": "t"},
            ]
        )

        def panel_fn(ticker, theme, asof, tech):
            flag = {"TRU": True, "FAL": False, "NON": None}[ticker]
            return _panel(ticker=ticker, earnings_growth_near_zero_base=flag)

        out = enrich(frame, asof=ASOF, panel_fn=panel_fn)
        by = {
            row["ticker"]: row["oneil_earnings_growth_near_zero_base"] for _, row in out.iterrows()
        }
        self.assertEqual(by["TRU"], 1.0)
        self.assertEqual(by["FAL"], 0.0)
        self.assertTrue(pd.isna(by["NON"]))


class TestTechnicalReuse(unittest.TestCase):
    def test_reuses_technical_columns_off_frame(self):
        # The panel_fn receives the technicals read off the frame — assert the
        # exact values are passed through (no recompute, no vendor call).
        frame = _frame(
            [
                {
                    "ticker": "AAA",
                    "theme": "t",
                    "technical_pct_off_52w_high": -4.5,
                    "technical_ma200_slope_pct_per_day": 0.07,
                    "technical_ma200_distance_pct": 11.0,
                }
            ]
        )
        seen: dict[str, float | None] = {}

        def panel_fn(ticker, theme, asof, tech):
            seen.update(tech)
            return _panel(ticker=ticker)

        enrich(frame, asof=ASOF, panel_fn=panel_fn)
        # Pass-through values (no arithmetic) compare exactly.
        self.assertEqual(seen["pct_off_52w_high"], -4.5)
        self.assertEqual(seen["ma200_slope_pct_per_day"], 0.07)
        self.assertEqual(seen["ma200_distance_pct"], 11.0)

    def test_missing_technical_column_passes_none(self):
        # Frame without the technical columns => the panel_fn gets None terms.
        frame = _frame([{"ticker": "AAA", "theme": "t"}])
        seen: dict[str, object] = {}

        def panel_fn(ticker, theme, asof, tech):
            seen.update(tech)
            return _panel(ticker=ticker)

        enrich(frame, asof=ASOF, panel_fn=panel_fn)
        self.assertIsNone(seen["pct_off_52w_high"])
        self.assertIsNone(seen["ma200_slope_pct_per_day"])


class TestSplitGatesScore(unittest.TestCase):
    def test_split_suspected_nulls_score_keeps_display_value(self):
        frame = _frame([{"ticker": "AAA", "theme": "t", "technical_pct_off_52w_high": -2.0}])

        def panel_fn(ticker, theme, asof, tech):
            return _panel(
                ticker=ticker,
                pct_off_52w_high=tech["pct_off_52w_high"],
                new_high_split_suspected=True,
            )

        out = enrich(frame, asof=ASOF, panel_fn=panel_fn)
        row = out.iloc[0]
        self.assertEqual(row["oneil_new_high_split_suspected"], 1.0)
        self.assertAlmostEqual(row["oneil_pct_off_52w_high"], -2.0)  # raw still stamped
        self.assertTrue(pd.isna(row["oneil_score"]))  # but scoring-excluded => None


class TestFailSoft(unittest.TestCase):
    def test_single_bad_ticker_does_not_abort(self):
        frame = _frame([{"ticker": "BAD", "theme": "t"}, {"ticker": "OK", "theme": "t"}])

        def panel_fn(ticker, theme, asof, tech):
            if ticker == "BAD":
                raise RuntimeError("boom")
            return _panel(ticker=ticker)

        out = enrich(frame, asof=ASOF, panel_fn=panel_fn)
        by = {row["ticker"]: row for _, row in out.iterrows()}
        self.assertTrue(pd.isna(by["BAD"]["oneil_score"]))
        self.assertFalse(pd.isna(by["OK"]["oneil_score"]))

    def test_preserves_existing_columns_and_order(self):
        frame = _frame([{"ticker": "AAA", "theme": "t", "rationale": "keep me"}])
        out = enrich(frame, asof=ASOF, panel_fn=lambda t, th, a, tech: _panel())
        self.assertEqual(out.iloc[0]["rationale"], "keep me")
        self.assertEqual(list(out.columns)[:3], ["ticker", "theme", "rationale"])


class TestNoNewNetwork(unittest.TestCase):
    def test_default_panel_fn_preloads_once_no_extra_fetch(self):
        # build_default_panel_fn wires EdgarFundamentalsStore(with_prices=False) and
        # preloads exactly the candidate list ONCE; panel computation must not fire a
        # second preload (a per-ticker preload would be a live SEC fetch).
        import alphalens_pipeline.experts.oneil.quant_enrichment as qe

        preload_calls: list[list[str]] = []

        class _Store:
            def __init__(self, *a, **k):
                pass

            def preload(self, tickers):
                preload_calls.append(list(tickers))

            def annual_series_as_of(self, ticker, asof, *, max_years=10):
                return []

        class _Yf:
            def cached_daily_ohlcv(self, ticker, *, asof):
                return pd.DataFrame({"close": [1.0, 1.0]})

        import alphalens_pipeline.data.alt_data.yfinance_client as yc
        import alphalens_pipeline.data.store.edgar_fundamentals as ef

        def _fake_get_yf():
            return _Yf()

        orig_store = ef.EdgarFundamentalsStore
        orig_get_yf = yc.get_default_yfinance_client
        ef.EdgarFundamentalsStore = _Store  # type: ignore[misc,assignment]
        yc.get_default_yfinance_client = _fake_get_yf  # type: ignore[assignment]
        try:
            fn = qe.build_default_panel_fn(["AAA", "BBB"])
            # one preload with exactly the candidate list
            self.assertEqual(preload_calls, [["AAA", "BBB"]])
            # computing a panel triggers NO further preload
            fn("AAA", "t", ASOF, dict.fromkeys(qe._TECHNICAL_SOURCE))
            self.assertEqual(len(preload_calls), 1)
        finally:
            ef.EdgarFundamentalsStore = orig_store  # type: ignore[misc]
            yc.get_default_yfinance_client = orig_get_yf


if __name__ == "__main__":
    unittest.main()
