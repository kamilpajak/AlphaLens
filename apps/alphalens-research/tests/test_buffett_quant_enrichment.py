"""Unit tests for the Buffett cheap-numerics enrichment step (PR-1).

``enrich`` stamps six Buffett columns onto the Layer-4 scored frame — the four
cheap numerics, the coverage fraction, and the quality score — reusing the
fundamentals already fetched in the scoring pass. It runs right after
``score_candidates`` in the ``score`` stage; the columns then ride the existing
merge chain into the brief parquet, Django, and the card.

These tests inject a fake ``panel_fn`` so NO store / network is touched.

Covered:

- columns are stamped from the panel, one row per ticker, preserving order and
  every pre-existing column;
- a ticker whose ``panel_fn`` returns ``None`` (or raises) gets all-``None``
  Buffett columns and never aborts the batch (fail-soft);
- the score column equals ``compute_quality_score`` of that ticker's panel;
- a duplicated ticker (pre-dedup frame) is computed once and mapped to both rows;
- an empty frame returns the six columns with zero rows (stable schema).
"""

from __future__ import annotations

import datetime as dt
import unittest

import pandas as pd
from alphalens_pipeline.buffett import quant_enrichment
from alphalens_pipeline.buffett.comparison import BuffettPanel
from alphalens_pipeline.buffett.quality_score import compute_quality_score

ASOF = dt.date(2026, 6, 11)

_BUFFETT_COLUMNS = (
    "buffett_owner_earnings_yield_pct",
    "buffett_roic_latest",
    "buffett_roic_3y_avg",
    "buffett_margin_of_safety_pct",
    "buffett_data_coverage",
    "buffett_quality_score",
)


def _panel(ticker: str, **overrides) -> BuffettPanel:
    base: dict = {
        "ticker": ticker,
        "theme": "t",
        "market_cap": None,
        "owner_earnings_latest": None,
        "owner_earnings_yield_pct": None,
        "roic_latest": None,
        "roic_3y_avg": None,
        "op_margin_latest": None,
        "op_margin_3y_avg": None,
        "intrinsic_value_per_share": None,
        "margin_of_safety_pct": None,
        "buyback_pct": None,
        "net_buyback": None,
        "dividend_yield_pct": None,
        "data_coverage": 0.0,
    }
    base.update(overrides)
    return BuffettPanel(**base)


class TestEnrich(unittest.TestCase):
    def test_stamps_columns_from_panel_preserving_order_and_columns(self) -> None:
        frame = pd.DataFrame(
            {"ticker": ["AAA", "BBB"], "theme": ["t1", "t2"], "layer4_weighted_score": [5, 3]}
        )
        panels = {
            "AAA": _panel(
                "AAA",
                owner_earnings_yield_pct=8.0,
                roic_latest=22.0,
                roic_3y_avg=20.0,
                margin_of_safety_pct=10.0,
                data_coverage=1.0,
            ),
            "BBB": _panel("BBB", roic_3y_avg=30.0, data_coverage=0.5),
        }

        out = quant_enrichment.enrich(frame, asof=ASOF, panel_fn=lambda t, theme, asof: panels[t])

        # Pre-existing columns + order preserved.
        self.assertEqual(list(out["ticker"]), ["AAA", "BBB"])
        self.assertEqual(list(out["layer4_weighted_score"]), [5, 3])
        for col in _BUFFETT_COLUMNS:
            self.assertIn(col, out.columns)
        # AAA numerics surfaced verbatim from the panel.
        row = out[out["ticker"] == "AAA"].iloc[0]
        self.assertAlmostEqual(row["buffett_owner_earnings_yield_pct"], 8.0)
        self.assertAlmostEqual(row["buffett_roic_latest"], 22.0)
        self.assertAlmostEqual(row["buffett_roic_3y_avg"], 20.0)
        self.assertAlmostEqual(row["buffett_margin_of_safety_pct"], 10.0)
        self.assertAlmostEqual(row["buffett_data_coverage"], 1.0)
        self.assertAlmostEqual(row["buffett_quality_score"], compute_quality_score(panels["AAA"]))

    def test_score_matches_quality_score_helper(self) -> None:
        frame = pd.DataFrame({"ticker": ["BBB"], "theme": ["t2"]})
        panel = _panel("BBB", roic_3y_avg=30.0, data_coverage=0.5)
        out = quant_enrichment.enrich(frame, asof=ASOF, panel_fn=lambda t, theme, asof: panel)
        self.assertAlmostEqual(out.iloc[0]["buffett_quality_score"], compute_quality_score(panel))

    def test_none_panel_yields_all_none_columns(self) -> None:
        frame = pd.DataFrame({"ticker": ["AAA"], "theme": ["t1"]})
        out = quant_enrichment.enrich(frame, asof=ASOF, panel_fn=lambda t, theme, asof: None)
        row = out.iloc[0]
        for col in _BUFFETT_COLUMNS:
            self.assertTrue(pd.isna(row[col]), f"{col} should be NA for an absent panel")

    def test_panel_fn_raising_is_failsoft(self) -> None:
        frame = pd.DataFrame({"ticker": ["AAA", "BBB"], "theme": ["t1", "t2"]})

        def boom(ticker: str, theme: str, asof: dt.date) -> BuffettPanel:
            raise RuntimeError("vendor hiccup")

        out = quant_enrichment.enrich(frame, asof=ASOF, panel_fn=boom)
        self.assertEqual(len(out), 2)
        for col in _BUFFETT_COLUMNS:
            self.assertTrue(out[col].isna().all())

    def test_duplicate_ticker_computed_once_mapped_to_all_rows(self) -> None:
        # Pre-dedup frame: the same ticker appears under two themes.
        frame = pd.DataFrame(
            {"ticker": ["AAA", "AAA"], "theme": ["t1", "t2"], "layer4_weighted_score": [5, 5]}
        )
        calls: list[str] = []

        def counting_fn(ticker: str, theme: str, asof: dt.date) -> BuffettPanel:
            calls.append(ticker)
            return _panel("AAA", owner_earnings_yield_pct=10.0, data_coverage=1.0)

        out = quant_enrichment.enrich(frame, asof=ASOF, panel_fn=counting_fn)
        self.assertEqual(calls, ["AAA"])  # computed once for the unique ticker
        self.assertEqual(len(out), 2)
        self.assertTrue((out["buffett_owner_earnings_yield_pct"] == 10.0).all())

    def test_empty_frame_returns_columns_with_zero_rows(self) -> None:
        frame = pd.DataFrame({"ticker": pd.Series(dtype="object")})
        out = quant_enrichment.enrich(frame, asof=ASOF, panel_fn=lambda t, theme, asof: None)
        self.assertEqual(len(out), 0)
        for col in _BUFFETT_COLUMNS:
            self.assertIn(col, out.columns)


if __name__ == "__main__":
    unittest.main()
