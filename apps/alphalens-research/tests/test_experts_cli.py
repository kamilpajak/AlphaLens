"""CLI tests for the ``experts`` app (enrich + migrate-qual-cache).

Replaces the deleted ``buffett qual-enrich`` / ``buffett migrate-qual-cache`` tests.
The network seams (build_comparison, 10-K fetch, LLM classify) are patched, so no
vendor is touched; the store / yfinance / SEC clients construct harmlessly.
"""

from __future__ import annotations

import json as _json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import alphalens_pipeline.experts.buffett.comparison as comparison_mod
import alphalens_pipeline.experts.buffett.qualitative as qualitative_mod
import alphalens_pipeline.thematic.verification.tenk_grep as tenk_grep_mod
import pandas as pd
from alphalens_cli.main import app
from alphalens_pipeline.experts.buffett.comparison import BuffettPanel
from alphalens_pipeline.experts.buffett.qual_enrichment import BUFFETT_QUAL_CONFIG_VERSION as _CV
from alphalens_pipeline.experts.buffett.qualitative import QualitativeAssessment
from typer.testing import CliRunner

_LEGACY_BODY = {
    "moat_type": "brand",
    "moat_trend": "stable",
    "management_candor": "candid",
    "understandable": True,
    "rationale": "x",
    "used_scuttlebutt": False,
    "computed_at": "2026-06-11T00:00:00+00:00",
}


def _panel(ticker: str) -> BuffettPanel:
    return BuffettPanel(
        ticker=ticker,
        theme="x",
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


class TestExpertsEnrichCommand(unittest.TestCase):
    def setUp(self):
        self._runner = CliRunner()
        self._orig_build = comparison_mod.build_comparison
        self._orig_fetch = tenk_grep_mod.fetch_multi_year_10k_texts
        self._orig_assess = qualitative_mod.assess_qualitative
        comparison_mod.build_comparison = lambda *_a, **_k: [_panel("AAPL")]  # type: ignore[assignment]
        tenk_grep_mod.fetch_multi_year_10k_texts = lambda **_k: [  # type: ignore[assignment]
            ("2026-03-27", "Item 1. Business X. Item 1A. Risk Factors Y. Item 7. Z. Item 8. End")
        ]
        qualitative_mod.assess_qualitative = lambda **_k: QualitativeAssessment(  # type: ignore[assignment]
            understandable=True,
            moat_type="brand",
            moat_trend="stable",
            management_candor="candid",
            rationale="durable franchise",
        )

    def tearDown(self):
        comparison_mod.build_comparison = self._orig_build  # type: ignore[assignment]
        tenk_grep_mod.fetch_multi_year_10k_texts = self._orig_fetch  # type: ignore[assignment]
        qualitative_mod.assess_qualitative = self._orig_assess  # type: ignore[assignment]

    def _brief(self, tmp) -> Path:
        briefs = Path(tmp) / "briefs"
        briefs.mkdir()
        pd.DataFrame({"ticker": ["AAPL"], "theme": ["x"]}).to_parquet(
            briefs / "2026-06-10.parquet", index=False
        )
        return briefs

    def test_all_stamps_and_caches(self):
        with TemporaryDirectory() as tmp:
            briefs = self._brief(tmp)
            cache = Path(tmp) / "cache"
            result = self._runner.invoke(
                app,
                [
                    "experts",
                    "enrich",
                    "2026-06-10",
                    "--all",
                    "--briefs-dir",
                    str(briefs),
                    "--cache-dir",
                    str(cache),
                ],
            )
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertIn("[buffett]", result.output)
            self.assertIn("classified 1", result.output)
            out = pd.read_parquet(briefs / "2026-06-10.parquet")
            self.assertEqual(out.iloc[0]["buffett_moat_type"], "brand")
            self.assertEqual(out.iloc[0]["buffett_qual_config_version"], _CV)
            self.assertTrue((cache / _CV / "2026-06-10" / "AAPL.json").exists())

    def test_expert_buffett_runs_alone(self):
        with TemporaryDirectory() as tmp:
            briefs = self._brief(tmp)
            result = self._runner.invoke(
                app,
                [
                    "experts",
                    "enrich",
                    "2026-06-10",
                    "--expert",
                    "buffett",
                    "--briefs-dir",
                    str(briefs),
                    "--cache-dir",
                    str(Path(tmp) / "c"),
                ],
            )
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertIn("[buffett]", result.output)

    def test_expert_and_all_mutually_exclusive(self):
        both = self._runner.invoke(
            app, ["experts", "enrich", "2026-06-10", "--expert", "buffett", "--all"]
        )
        self.assertNotEqual(both.exit_code, 0)
        neither = self._runner.invoke(app, ["experts", "enrich", "2026-06-10"])
        self.assertNotEqual(neither.exit_code, 0)

    def test_unknown_expert_rejected(self):
        r = self._runner.invoke(app, ["experts", "enrich", "2026-06-10", "--expert", "graham"])
        self.assertNotEqual(r.exit_code, 0)
        self.assertIn("unknown expert", r.output)

    def test_numeric_only_expert_rejected(self):
        # O'Neil is numeric-only (stamped at the score stage) — `enrich` has no
        # qualitative layer to run for it, so a single --expert oneil is rejected.
        r = self._runner.invoke(app, ["experts", "enrich", "2026-06-10", "--expert", "oneil"])
        self.assertNotEqual(r.exit_code, 0)
        self.assertIn("numeric-only", r.output)

    def test_bad_date_rejected(self):
        r = self._runner.invoke(app, ["experts", "enrich", "nope", "--all"])
        self.assertNotEqual(r.exit_code, 0)


class TestExpertsMigrateCommand(unittest.TestCase):
    def setUp(self):
        self._runner = CliRunner()

    def test_migrate_moves_legacy(self):
        with TemporaryDirectory() as tmp:
            cache = Path(tmp)
            legacy = cache / "2026-06-10"
            legacy.mkdir(parents=True)
            (legacy / "AAPL.json").write_text(_json.dumps(_LEGACY_BODY))
            r = self._runner.invoke(
                app, ["experts", "migrate-qual-cache", "--cache-dir", str(cache)]
            )
            self.assertEqual(r.exit_code, 0, r.output)
            self.assertIn("moved 1", r.output)
            self.assertTrue((cache / _CV / "2026-06-10" / "AAPL.json").exists())
            self.assertFalse((legacy / "AAPL.json").exists())

    def test_unknown_expert_rejected(self):
        r = self._runner.invoke(app, ["experts", "migrate-qual-cache", "--expert", "graham"])
        self.assertNotEqual(r.exit_code, 0)
        self.assertIn("unknown expert", r.output)

    def test_numeric_only_expert_has_no_cache_to_migrate(self):
        # O'Neil is numeric-only — no qualitative cache to relocate.
        r = self._runner.invoke(app, ["experts", "migrate-qual-cache", "--expert", "oneil"])
        self.assertNotEqual(r.exit_code, 0)
        self.assertIn("no qualitative cache", r.output)


class TestOldBuffettSubcommandsRemoved(unittest.TestCase):
    """The rename dropped the old surface with no alias; buffett lens stays."""

    def setUp(self):
        self._runner = CliRunner()

    def test_buffett_qual_enrich_removed(self):
        r = self._runner.invoke(app, ["buffett", "qual-enrich", "2026-06-10"])
        self.assertNotEqual(r.exit_code, 0)

    def test_buffett_migrate_qual_cache_removed(self):
        r = self._runner.invoke(app, ["buffett", "migrate-qual-cache"])
        self.assertNotEqual(r.exit_code, 0)

    def test_buffett_lens_still_registered(self):
        # `lens --help` exits 0 iff the command still exists (a removed command
        # would error). Proves the rename dropped only qual-enrich / migrate.
        r = self._runner.invoke(app, ["buffett", "lens", "--help"])
        self.assertEqual(r.exit_code, 0, r.output)
        self.assertIn("lens", r.output.lower())


if __name__ == "__main__":
    unittest.main()
