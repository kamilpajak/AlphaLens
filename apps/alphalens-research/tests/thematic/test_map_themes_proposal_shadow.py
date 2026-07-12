"""``map_themes`` feeds the V-forward proposal-shadow logger the FULL pre-gate
LLM proposal set — including candidates the verification gates later drop.

The retro head-to-head (design memo §3) could only use ``thematic_candidates``,
which persists ONLY gate survivors, so the ungated LLM proposal set is lost. This
test pins the fix: the shadow writer receives every proposed ticker (pre-gate),
not just the survivors that reach the candidates parquet. Side effect is
best-effort telemetry — the writer is stubbed so no real ``~/.alphalens`` write
happens.
"""

import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd
from alphalens_pipeline.thematic.mapping import orchestrator

from .test_theme_mapping import _catalyst_payload

ASOF = dt.date(2026, 6, 18)


def _survivor_row(theme: str, ticker: str) -> dict:
    return {
        "theme": theme,
        "ticker": ticker,
        "company_name": f"{ticker} Inc",
        "rationale": "stub",
        "llm_confidence": 0.7,
        "market_cap": 1_000_000_000,
        "n_gates_passed": 1,
        "verified": True,
    }


class TestMapThemesProposalShadow(unittest.TestCase):
    def test_shadow_receives_full_pregate_proposals_not_just_survivors(self):
        # Pro proposes TWO tickers for the theme; the gate keeps only ONE.
        proposed = [
            {"ticker": "KEEP", "confidence": 0.9},
            {"ticker": "DROP", "confidence": 0.4},
        ]

        def _verify(*, theme, **_kwargs):
            # Only KEEP survives the gate; DROP is rejected (dropped=1).
            return ([_survivor_row(theme, "KEEP")], 1, 0)

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            with (
                patch.object(orchestrator, "_init_pro_client", return_value=object()),
                patch.object(orchestrator, "_fetch_press_window", return_value=pd.DataFrame()),
                patch.object(orchestrator, "_resolve_catalyst", return_value=_catalyst_payload()),
                patch.object(
                    orchestrator,
                    "_propose_and_filter_candidates",
                    return_value=(proposed, {"KEEP": True, "DROP": True}, ["kw"]),
                ),
                patch.object(orchestrator, "_verify_candidates_for_theme", side_effect=_verify),
                patch.object(orchestrator.proposal_shadow, "write_proposal_shadow") as shadow,
            ):
                df = orchestrator.map_themes(
                    themes=["ai"], asof=ASOF, api_key="dummy", output_dir=out, rebuild=True
                )

            # The candidates parquet holds only the survivor.
            self.assertEqual(list(df["ticker"]), ["KEEP"])
            # The shadow writer got BOTH pre-gate proposals.
            shadow.assert_called_once()
            kwargs = shadow.call_args.kwargs
            llm_proposals = shadow.call_args.args[1]
            tickers = {p["ticker"] for p in llm_proposals}
            self.assertEqual(tickers, {"KEEP", "DROP"})
            self.assertEqual(kwargs["mapper_config_version"], df.loc[0, "mapper_config_version"])

    def test_no_proposals_skips_shadow_write(self):
        # A theme with no catalyst yields no proposals → writer must not fire.
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            with (
                patch.object(orchestrator, "_init_pro_client", return_value=object()),
                patch.object(orchestrator, "_fetch_press_window", return_value=pd.DataFrame()),
                patch.object(orchestrator, "_resolve_catalyst", return_value=None),
                patch.object(orchestrator.proposal_shadow, "write_proposal_shadow") as shadow,
            ):
                orchestrator.map_themes(
                    themes=["ai"], asof=ASOF, api_key="dummy", output_dir=out, rebuild=True
                )
            shadow.assert_not_called()

    def test_shadow_write_failure_never_aborts_map_themes(self):
        proposed = [{"ticker": "KEEP", "confidence": 0.9}]

        def _verify(*, theme, **_kwargs):
            return ([_survivor_row(theme, "KEEP")], 0, 0)

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            with (
                patch.object(orchestrator, "_init_pro_client", return_value=object()),
                patch.object(orchestrator, "_fetch_press_window", return_value=pd.DataFrame()),
                patch.object(orchestrator, "_resolve_catalyst", return_value=_catalyst_payload()),
                patch.object(
                    orchestrator,
                    "_propose_and_filter_candidates",
                    return_value=(proposed, {"KEEP": True}, ["kw"]),
                ),
                patch.object(orchestrator, "_verify_candidates_for_theme", side_effect=_verify),
                patch.object(
                    orchestrator.proposal_shadow,
                    "write_proposal_shadow",
                    side_effect=OSError("disk full"),
                ),
            ):
                # Must NOT raise — the candidates parquet is still produced.
                df = orchestrator.map_themes(
                    themes=["ai"], asof=ASOF, api_key="dummy", output_dir=out, rebuild=True
                )
            self.assertEqual(list(df["ticker"]), ["KEEP"])


if __name__ == "__main__":
    unittest.main()
