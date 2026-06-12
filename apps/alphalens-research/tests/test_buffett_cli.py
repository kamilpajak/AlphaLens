"""CLI tests for `alphalens buffett lens` (Mode-A lens, #511).

Exercises the command body via typer's CliRunner against the ROOT app
(`alphalens buffett lens ...`, mirroring the real `add_typer` registration)
with `build_comparison` monkeypatched, so no network / store fetch happens:
the store + yfinance singleton are constructed (cheap, no I/O) but the patched
assembler returns fixed panels. Covers the table render, the `--out` parquet
write, the no-candidates path, and the bad-date guard.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import alphalens_pipeline.buffett.comparison as comparison_mod
import alphalens_pipeline.buffett.qualitative as qualitative_mod
import alphalens_pipeline.buffett.scuttlebutt as scuttlebutt_mod
import alphalens_pipeline.thematic.verification.tenk_grep as tenk_grep_mod
from alphalens_cli.commands.buffett import _fmt_num, _format_rationale_block, _format_table
from alphalens_cli.main import app
from alphalens_pipeline.buffett.comparison import BuffettPanel
from alphalens_pipeline.buffett.qualitative import QualitativeAssessment
from alphalens_pipeline.buffett.scuttlebutt import Scuttlebutt
from typer.testing import CliRunner


def _panel(ticker: str, **kw) -> BuffettPanel:
    base: dict = {
        "ticker": ticker,
        "theme": "AI infrastructure",
        "market_cap": 1.0e9,
        "owner_earnings_latest": 5.0e7,
        "owner_earnings_yield_pct": 5.0,
        "roic_latest": 18.0,
        "roic_3y_avg": 16.0,
        "op_margin_latest": 22.0,
        "op_margin_3y_avg": 20.0,
        "intrinsic_value_per_share": 120.0,
        "margin_of_safety_pct": 12.0,
        "buyback_pct": -1.5,
        "net_buyback": True,
        "dividend_yield_pct": 1.2,
        "data_coverage": 1.0,
    }
    base.update(kw)
    return BuffettPanel(**base)


class TestFormatTable(unittest.TestCase):
    def test_headers_and_none_dash(self):
        table = _format_table([_panel("AAPL", roic_latest=None)])
        self.assertIn("TICKER", table)
        self.assertIn("AAPL", table)
        # A None metric renders as the dash sentinel, never a fabricated 0.
        self.assertIn("-", table)

    def test_fmt_num_none_and_decimals(self):
        self.assertEqual(_fmt_num(None), "-")
        self.assertEqual(_fmt_num(3.14159, decimals=2), "3.14")


class TestFormatRationaleBlock(unittest.TestCase):
    def test_renders_present_rationales_skips_missing(self):
        panels = [_panel("AAPL"), _panel("MSFT")]
        assessments = [
            QualitativeAssessment(
                understandable=True,
                moat_type="brand",
                moat_trend="stable",
                management_candor="candid",
                rationale="Durable ecosystem and pricing power.",
            ),
            QualitativeAssessment(
                understandable=None,
                moat_type=None,
                moat_trend=None,
                management_candor=None,
                rationale=None,  # no fetchable 10-K -> skipped
            ),
        ]
        block = _format_rationale_block(panels, assessments)
        assert block is not None
        self.assertIn("Why (qualitative rationale)", block)
        self.assertIn("AAPL", block)
        self.assertIn("Durable ecosystem", block)
        self.assertNotIn("MSFT", block)  # the None-rationale candidate is omitted

    def test_returns_none_when_no_rationale(self):
        panels = [_panel("AAPL")]
        assessments = [
            QualitativeAssessment(
                understandable=None,
                moat_type=None,
                moat_trend=None,
                management_candor=None,
                rationale=None,
            )
        ]
        self.assertIsNone(_format_rationale_block(panels, assessments))


class TestLensCommand(unittest.TestCase):
    def setUp(self):
        self._runner = CliRunner()
        self._original = comparison_mod.build_comparison

    def tearDown(self):
        comparison_mod.build_comparison = self._original  # type: ignore[assignment]

    def _patch_panels(self, panels: list[BuffettPanel]) -> None:
        comparison_mod.build_comparison = lambda *_a, **_k: panels  # type: ignore[assignment]

    def test_prints_table_and_writes_parquet(self):
        self._patch_panels([_panel("AAPL"), _panel("MSFT", data_coverage=0.5)])
        with TemporaryDirectory() as tmp:
            out = Path(tmp) / "lens.parquet"
            result = self._runner.invoke(
                app,
                ["buffett", "lens", "2026-06-10", "--briefs-dir", tmp, "--out", str(out)],
            )
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertIn("Buffett lens (Mode A)", result.output)
            self.assertIn("AAPL", result.output)
            self.assertTrue(out.exists())

    def test_no_candidates_message(self):
        self._patch_panels([])
        with TemporaryDirectory() as tmp:
            result = self._runner.invoke(
                app, ["buffett", "lens", "2026-06-10", "--briefs-dir", tmp]
            )
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertIn("No candidates", result.output)

    def test_bad_date_is_rejected(self):
        result = self._runner.invoke(app, ["buffett", "lens", "not-a-date"])
        self.assertNotEqual(result.exit_code, 0)


class TestQualitativeFlag(unittest.TestCase):
    """The opt-in `--qualitative` flag adds MOAT/TREND/CANDOR/UNDERSTOOD columns.

    All network / LLM seams are monkeypatched: `build_comparison` returns fixed
    panels, `fetch_10k_text` returns synthetic text, and `assess_qualitative`
    returns a fixed assessment — so the test is fully hermetic.
    """

    def setUp(self):
        self._runner = CliRunner()
        self._orig_build = comparison_mod.build_comparison
        self._orig_fetch = tenk_grep_mod.fetch_multi_year_10k_texts
        self._orig_assess = qualitative_mod.assess_qualitative

    def tearDown(self):
        comparison_mod.build_comparison = self._orig_build  # type: ignore[assignment]
        tenk_grep_mod.fetch_multi_year_10k_texts = self._orig_fetch  # type: ignore[assignment]
        qualitative_mod.assess_qualitative = self._orig_assess  # type: ignore[assignment]

    def test_qualitative_flag_adds_columns_and_writes_parquet(self):
        comparison_mod.build_comparison = lambda *_a, **_k: [_panel("AAPL")]  # type: ignore[assignment]
        tenk_grep_mod.fetch_multi_year_10k_texts = lambda **_k: [  # type: ignore[assignment]
            (
                "2026-03-27",
                "Item 1. Business We make things. Item 1A. Risk Factors Risky. "
                "Item 7. MD&A We discuss. Item 8. End",
            )
        ]
        qualitative_mod.assess_qualitative = lambda **_k: QualitativeAssessment(  # type: ignore[assignment]
            understandable=True,
            moat_type="brand",
            moat_trend="widening",
            management_candor="candid",
            rationale="strong brand",
        )
        with TemporaryDirectory() as tmp:
            out = Path(tmp) / "lens.parquet"
            result = self._runner.invoke(
                app,
                [
                    "buffett",
                    "lens",
                    "2026-06-10",
                    "--briefs-dir",
                    tmp,
                    "--qualitative",
                    "--out",
                    str(out),
                ],
            )
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertIn("MOAT", result.output)
            self.assertIn("brand", result.output)
            # The rationale (too long for a table cell) prints in a 'Why' block.
            self.assertIn("Why (qualitative rationale)", result.output)
            self.assertIn("strong brand", result.output)
            self.assertTrue(out.exists())
            import pandas as pd

            df = pd.read_parquet(out)
            for col in ("moat_type", "moat_trend", "management_candor", "understandable"):
                self.assertIn(col, df.columns)

    def test_no_qualitative_flag_does_not_call_llm(self):
        comparison_mod.build_comparison = lambda *_a, **_k: [_panel("AAPL")]  # type: ignore[assignment]
        calls: list[str] = []
        tenk_grep_mod.fetch_multi_year_10k_texts = lambda **_k: calls.append("fetch") or []  # type: ignore[assignment]
        qualitative_mod.assess_qualitative = lambda **_k: calls.append("assess")  # type: ignore[assignment]
        with TemporaryDirectory() as tmp:
            result = self._runner.invoke(
                app, ["buffett", "lens", "2026-06-10", "--briefs-dir", tmp]
            )
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertNotIn("MOAT", result.output)
            self.assertEqual(calls, [])

    def test_qualitative_feeds_prior_year_risk_factors(self):
        # #505 wiring: the multi-year fetch's prior years become the
        # prior_year_risk_factors (Item 1A per year) passed to assess_qualitative.
        comparison_mod.build_comparison = lambda *_a, **_k: [_panel("AAPL")]  # type: ignore[assignment]
        tenk_grep_mod.fetch_multi_year_10k_texts = lambda **_k: [  # type: ignore[assignment]
            (
                "2026-03-27",
                "Item 1. Business Now. Item 1A. Risk Factors Newest risk wording. Item 8. End",
            ),
            (
                "2025-03-21",
                "Item 1. Business Then. Item 1A. Risk Factors Middle risk wording. Item 8. End",
            ),
            (
                "2024-03-22",
                "Item 1. Business Past. Item 1A. Risk Factors Oldest risk wording. Item 8. End",
            ),
        ]
        captured: dict = {}

        def _capture_assess(**kwargs):
            captured.update(kwargs)
            return QualitativeAssessment(
                understandable=True,
                moat_type="brand",
                moat_trend="narrowing",
                management_candor="mixed",
                rationale="risks intensified across years",
            )

        qualitative_mod.assess_qualitative = _capture_assess  # type: ignore[assignment]
        with TemporaryDirectory() as tmp:
            result = self._runner.invoke(
                app, ["buffett", "lens", "2026-06-10", "--briefs-dir", tmp, "--qualitative"]
            )
            self.assertEqual(result.exit_code, 0, result.output)
        priors = captured.get("prior_year_risk_factors")
        self.assertIsNotNone(priors)
        # This asserts the SET of prior years handed to assess_qualitative (the
        # latest filing is the primary sections, not a prior). The chronological
        # oldest-first ordering inside the prompt is validated separately in
        # test_buffett_qualitative.test_prior_year_risk_factors_rendered_oldest_first.
        dates = [d for d, _ in priors]
        self.assertEqual(sorted(dates), ["2024-03-22", "2025-03-21"])
        texts = " ".join(t or "" for _, t in priors)
        self.assertIn("Oldest risk wording", texts)
        self.assertIn("Middle risk wording", texts)
        self.assertNotIn("Newest risk wording", texts)  # latest year is the primary sections


class TestScuttlebuttFlag(unittest.TestCase):
    """#507 PR-7a: `--scuttlebutt` (sub-flag of `--qualitative`) fetches
    web-grounded context per candidate and feeds it to assess_qualitative."""

    def setUp(self):
        self._runner = CliRunner()
        self._orig_build = comparison_mod.build_comparison
        self._orig_fetch = tenk_grep_mod.fetch_multi_year_10k_texts
        self._orig_assess = qualitative_mod.assess_qualitative
        self._orig_sb = scuttlebutt_mod.fetch_scuttlebutt
        comparison_mod.build_comparison = lambda *_a, **_k: [_panel("AAPL")]  # type: ignore[assignment]
        tenk_grep_mod.fetch_multi_year_10k_texts = lambda **_k: [  # type: ignore[assignment]
            ("2026-03-27", "Item 1. Business X. Item 1A. Risk Factors Y. Item 8. End")
        ]

    def tearDown(self):
        comparison_mod.build_comparison = self._orig_build  # type: ignore[assignment]
        tenk_grep_mod.fetch_multi_year_10k_texts = self._orig_fetch  # type: ignore[assignment]
        qualitative_mod.assess_qualitative = self._orig_assess  # type: ignore[assignment]
        scuttlebutt_mod.fetch_scuttlebutt = self._orig_sb  # type: ignore[assignment]

    def _capture(self) -> dict:
        captured: dict = {}

        def _assess(**kwargs):
            captured.update(kwargs)
            return QualitativeAssessment(
                understandable=True,
                moat_type="brand",
                moat_trend="stable",
                management_candor="candid",
                rationale="ok",
            )

        qualitative_mod.assess_qualitative = _assess  # type: ignore[assignment]
        return captured

    def test_scuttlebutt_text_passed_to_assess(self):
        captured = self._capture()
        scuttlebutt_mod.fetch_scuttlebutt = lambda ticker, **_k: Scuttlebutt(  # type: ignore[assignment]
            ticker=ticker, text="Rivals undercut on price.", ok=True
        )
        with (
            TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"PERPLEXITY_API_KEY": "test-key"}),
        ):
            result = self._runner.invoke(
                app,
                [
                    "buffett",
                    "lens",
                    "2026-06-10",
                    "--briefs-dir",
                    tmp,
                    "--qualitative",
                    "--scuttlebutt",
                ],
            )
            self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(captured.get("scuttlebutt"), "Rivals undercut on price.")

    def test_scuttlebutt_failsoft_passes_none(self):
        captured = self._capture()
        scuttlebutt_mod.fetch_scuttlebutt = lambda ticker, **_k: Scuttlebutt(  # type: ignore[assignment]
            ticker=ticker, text="", ok=False
        )
        with (
            TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"PERPLEXITY_API_KEY": "test-key"}),
        ):
            result = self._runner.invoke(
                app,
                [
                    "buffett",
                    "lens",
                    "2026-06-10",
                    "--briefs-dir",
                    tmp,
                    "--qualitative",
                    "--scuttlebutt",
                ],
            )
            self.assertEqual(result.exit_code, 0, result.output)
        # ok=False → scuttlebutt passed as None (section omitted).
        self.assertIsNone(captured.get("scuttlebutt"))

    def test_missing_key_skips_scuttlebutt_no_crash(self):
        captured = self._capture()
        scuttlebutt_mod.fetch_scuttlebutt = lambda *_a, **_k: (_ for _ in ()).throw(  # type: ignore[assignment]
            AssertionError("must not fetch without a key")
        )
        with TemporaryDirectory() as tmp, patch.dict(os.environ, {"PERPLEXITY_API_KEY": ""}):
            result = self._runner.invoke(
                app,
                [
                    "buffett",
                    "lens",
                    "2026-06-10",
                    "--briefs-dir",
                    tmp,
                    "--qualitative",
                    "--scuttlebutt",
                ],
            )
            self.assertEqual(result.exit_code, 0, result.output)
        self.assertIsNone(captured.get("scuttlebutt"))

    def test_scuttlebutt_without_qualitative_is_noop(self):
        called: list[str] = []
        scuttlebutt_mod.fetch_scuttlebutt = lambda *_a, **_k: called.append("fetch")  # type: ignore[assignment]
        with TemporaryDirectory() as tmp:
            result = self._runner.invoke(
                app, ["buffett", "lens", "2026-06-10", "--briefs-dir", tmp, "--scuttlebutt"]
            )
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertIn("requires --qualitative", result.output)
            self.assertEqual(called, [])


class TestBuildExecCompFn(unittest.TestCase):
    """#507 PR-7b: the CLI helper resolves ticker -> CIK and calls exec_comp_as_of,
    returning a NOT_DISCLOSED facts object when the CIK can't be resolved."""

    def test_resolves_cik_and_calls_exec_comp(self):
        import datetime as _dt

        from alphalens_cli.commands.buffett import _build_exec_comp_fn
        from alphalens_pipeline.buffett.exec_comp import ExecCompCoverage, ExecCompFacts

        sentinel = ExecCompFacts(
            cik="1321655", coverage=ExecCompCoverage.PRESENT, peo_to_neo_ratio=4.0
        )
        with (
            patch("alphalens_pipeline.data.alt_data.sec_edgar_client.get_default_sec_client"),
            patch(
                "alphalens_pipeline.thematic.verification.tenk_grep._resolve_cik",
                return_value="0001321655",
            ),
            patch(
                "alphalens_pipeline.buffett.exec_comp.exec_comp_as_of", return_value=sentinel
            ) as ec,
        ):
            fn = _build_exec_comp_fn()
            result = fn("ACME", _dt.date(2026, 6, 1))
        self.assertIs(result, sentinel)
        self.assertEqual(ec.call_args.args[0], "0001321655")

    def test_unresolvable_cik_returns_not_disclosed(self):
        import datetime as _dt

        from alphalens_cli.commands.buffett import _build_exec_comp_fn
        from alphalens_pipeline.buffett.exec_comp import ExecCompCoverage

        with (
            patch("alphalens_pipeline.data.alt_data.sec_edgar_client.get_default_sec_client"),
            patch(
                "alphalens_pipeline.thematic.verification.tenk_grep._resolve_cik", return_value=None
            ),
        ):
            fn = _build_exec_comp_fn()
            result = fn("XYZ", _dt.date(2026, 6, 1))
        self.assertEqual(result.coverage, ExecCompCoverage.NOT_DISCLOSED)
        self.assertIsNone(result.peo_to_neo_ratio)


class TestQualEnrichCommand(unittest.TestCase):
    """`alphalens buffett qual-enrich <date>` stamps the seven qual columns into
    the brief parquet and caches each result. All network seams patched: fixed
    panels, synthetic 10-K, fixed assessment."""

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

    def test_stamps_columns_and_caches(self):
        import pandas as pd

        with TemporaryDirectory() as tmp:
            briefs = Path(tmp) / "briefs"
            briefs.mkdir()
            cache = Path(tmp) / "cache"
            pd.DataFrame({"ticker": ["AAPL"], "theme": ["x"]}).to_parquet(
                briefs / "2026-06-10.parquet", index=False
            )
            result = self._runner.invoke(
                app,
                [
                    "buffett",
                    "qual-enrich",
                    "2026-06-10",
                    "--briefs-dir",
                    str(briefs),
                    "--cache-dir",
                    str(cache),
                ],
            )
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertIn("classified 1", result.output)
            out = pd.read_parquet(briefs / "2026-06-10.parquet")
            self.assertEqual(out.iloc[0]["buffett_moat_type"], "brand")
            self.assertEqual(out.iloc[0]["buffett_understandable"], True)
            # The result was cached immutably for (date, ticker).
            self.assertTrue((cache / "2026-06-10" / "AAPL.json").exists())

    def test_bad_date_guard(self):
        result = self._runner.invoke(app, ["buffett", "qual-enrich", "nope"])
        self.assertNotEqual(result.exit_code, 0)


if __name__ == "__main__":
    unittest.main()
