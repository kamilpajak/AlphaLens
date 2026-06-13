"""Net for the WIRED thematic score stage after the buffett -> experts move.

``alphalens_cli/commands/thematic.py`` (the daily score stage) imports
``alphalens_pipeline.experts.buffett.quant_enrichment`` and calls ``enrich`` to
stamp the six BUFFETT_COLUMNS onto the scored frame. If the move broke that import
path this test fails at import; the stamping assertion pins the behaviour itself.
"""

from __future__ import annotations

import datetime as dt
import unittest

import pandas as pd
from alphalens_pipeline.experts.buffett import quant_enrichment as qe
from alphalens_pipeline.experts.buffett.comparison import BuffettPanel

ASOF = dt.date(2026, 6, 11)


def _panel(ticker: str, theme: str, asof: dt.date) -> BuffettPanel:
    return BuffettPanel(
        ticker=ticker,
        theme=theme,
        market_cap=1.0e9,
        owner_earnings_latest=5.0e7,
        owner_earnings_yield_pct=5.0,
        roic_latest=18.0,
        roic_3y_avg=16.0,
        op_margin_latest=22.0,
        op_margin_3y_avg=20.0,
        intrinsic_value_per_share=120.0,
        margin_of_safety_pct=12.0,
        buyback_pct=-1.5,
        net_buyback=True,
        dividend_yield_pct=1.2,
    )


class TestScoreStageWiring(unittest.TestCase):
    def test_enrich_stamps_buffett_columns_via_moved_path(self) -> None:
        frame = pd.DataFrame({"ticker": ["AAA"], "theme": ["t"]})
        out = qe.enrich(frame, asof=ASOF, panel_fn=_panel)
        self.assertEqual(len(qe.BUFFETT_COLUMNS), 6)
        for col in qe.BUFFETT_COLUMNS:
            self.assertIn(col, out.columns)


if __name__ == "__main__":
    unittest.main()
