"""Unit tests for the Expert Protocol + the BuffettExpert adapter.

The adapter WRAPS the moved Buffett machinery; these tests pin that it satisfies
the structural Protocol, round-trips the panel losslessly, delegates the score to
``compute_quality_score``, and maps a qualitative assessment to the five prefixed
content keys (forwarding the scuttlebutt client). All LLM / network seams are
injected or patched — nothing here touches a vendor.
"""

from __future__ import annotations

import datetime as dt
import unittest
from dataclasses import asdict
from unittest.mock import patch

from alphalens_pipeline.experts.base import Expert
from alphalens_pipeline.experts.buffett import qual_enrichment as qe_mod
from alphalens_pipeline.experts.buffett.comparison import BuffettPanel
from alphalens_pipeline.experts.buffett.expert import BuffettExpert
from alphalens_pipeline.experts.buffett.qualitative import QualitativeAssessment
from alphalens_pipeline.experts.buffett.quality_score import compute_quality_score
from alphalens_pipeline.experts.buffett.quant_enrichment import BUFFETT_COLUMNS

ASOF = dt.date(2026, 6, 11)

_QUAL_KEYS = {
    "buffett_moat_type",
    "buffett_moat_trend",
    "buffett_management_candor",
    "buffett_understandable",
    "buffett_qualitative_rationale",
}


def _panel(ticker: str = "AAA") -> BuffettPanel:
    return BuffettPanel(
        ticker=ticker,
        theme="t",
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


class TestBuffettExpertProtocol(unittest.TestCase):
    def test_buffett_expert_satisfies_protocol(self) -> None:
        self.assertIsInstance(BuffettExpert(), Expert)

    def test_compute_panel_returns_asdict_via_injected_fn(self) -> None:
        bp = _panel("AAA")
        exp = BuffettExpert(panel_fn=lambda t, theme, asof: bp)
        self.assertEqual(exp.compute_panel("AAA", ASOF, context={"theme": "x"}), asdict(bp))

    def test_compute_panel_none_passes_through(self) -> None:
        exp = BuffettExpert(panel_fn=lambda t, theme, asof: None)
        self.assertIsNone(exp.compute_panel("AAA", ASOF))

    def test_compute_score_wraps_quality_score(self) -> None:
        bp = _panel("AAA")
        exp = BuffettExpert()
        self.assertEqual(exp.compute_score(asdict(bp)), compute_quality_score(bp))
        self.assertIsNone(exp.compute_score(None))

    def test_assess_qualitative_none_panel_is_none(self) -> None:
        self.assertIsNone(BuffettExpert().assess_qualitative(None, ASOF, "AAA"))

    def test_assess_qualitative_maps_to_prefixed_keys_and_forwards_scuttlebutt(self) -> None:
        bp = _panel("AAA")
        assessment = QualitativeAssessment(
            understandable=True,
            moat_type="brand",
            moat_trend="stable",
            management_candor="candid",
            rationale="durable",
        )
        with patch.object(qe_mod, "assess_panel_qualitative", return_value=assessment) as m:
            out = BuffettExpert().assess_qualitative(
                asdict(bp), ASOF, "AAA", scuttlebutt_client="SB"
            )
        self.assertEqual(
            out,
            {
                "buffett_moat_type": "brand",
                "buffett_moat_trend": "stable",
                "buffett_management_candor": "candid",
                "buffett_understandable": True,
                "buffett_qualitative_rationale": "durable",
            },
        )
        self.assertEqual(m.call_args.kwargs["scuttlebutt_client"], "SB")

    def test_assess_qualitative_none_assessment_is_none(self) -> None:
        bp = _panel("AAA")
        with patch.object(qe_mod, "assess_panel_qualitative", return_value=None):
            self.assertIsNone(BuffettExpert().assess_qualitative(asdict(bp), ASOF, "AAA"))

    def test_column_names_cover_method_outputs(self) -> None:
        cols = set(BuffettExpert().column_names)
        self.assertTrue(set(BUFFETT_COLUMNS).issubset(cols))
        self.assertTrue(_QUAL_KEYS.issubset(cols))


if __name__ == "__main__":
    unittest.main()
