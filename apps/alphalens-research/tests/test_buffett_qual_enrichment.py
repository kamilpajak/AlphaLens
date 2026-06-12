"""Unit tests for the eager Buffett qualitative enrichment + its result cache (PR-3).

``qual_enrichment`` runs the per-candidate qualitative LLM layer eagerly for a
brief's survivors and caches each result **immutably per (date, ticker)** so the
6x/day pipeline reruns + re-ingests never re-pay DeepSeek. The expensive op
(10-K fetch + LLM classification) is injected here as a fake ``assess_one`` so NO
network / LLM is touched.

Covered:

- cache roundtrip: a real record writes a JSON file and loads back equal;
- cache miss returns None; a second call with a warm cache does NOT recompute;
- an all-None record (LLM error / no classification) is NOT cached, so the next
  run retries it (only genuine successes are frozen);
- a panel whose ``assess_one`` returns None (no 10-K) yields a None record and is
  not cached;
- ``enrich_qualitative`` maps assessments -> records carrying used_scuttlebutt +
  computed_at, computed once per unique ticker;
- ``stamp_columns`` writes the seven qual columns onto a frame by ticker,
  preserving order + pre-existing columns, dashes (None) where no record.
"""

from __future__ import annotations

import datetime as dt
import unittest

import pandas as pd
from alphalens_pipeline.buffett import qual_enrichment as qe
from alphalens_pipeline.buffett.comparison import BuffettPanel
from alphalens_pipeline.buffett.qualitative import QualitativeAssessment

ASOF = dt.date(2026, 6, 11)
NOW = dt.datetime(2026, 6, 12, 9, 0, 0, tzinfo=dt.UTC)

_QUAL_COLUMNS = (
    "buffett_moat_type",
    "buffett_moat_trend",
    "buffett_management_candor",
    "buffett_understandable",
    "buffett_qualitative_rationale",
    "buffett_used_scuttlebutt",
    "buffett_qual_computed_at",
)


def _panel(ticker: str) -> BuffettPanel:
    return BuffettPanel(
        ticker=ticker,
        theme="t",
        market_cap=None,
        owner_earnings_latest=None,
        owner_earnings_yield_pct=None,
        roic_latest=None,
        roic_3y_avg=None,
        op_margin_latest=None,
        op_margin_3y_avg=None,
        intrinsic_value_per_share=None,
        margin_of_safety_pct=None,
        buyback_pct=None,
        net_buyback=None,
        dividend_yield_pct=None,
    )


def _assessment(moat: str | None = "brand") -> QualitativeAssessment:
    return QualitativeAssessment(
        understandable=True,
        moat_type=moat,
        moat_trend="stable",
        management_candor="candid",
        rationale="solid franchise",
    )


_ALL_NONE = QualitativeAssessment(
    understandable=None,
    moat_type=None,
    moat_trend=None,
    management_candor=None,
    rationale=None,
)


class TestQualCache(unittest.TestCase):
    def test_real_record_roundtrips_through_cache(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            rec = qe.QualRecord(
                moat_type="brand",
                moat_trend="stable",
                management_candor="candid",
                understandable=True,
                rationale="solid franchise",
                used_scuttlebutt=True,
                computed_at=NOW.isoformat(),
            )
            qe.write_cache("AAA", ASOF, cache, rec)
            path = cache / ASOF.isoformat() / "AAA.json"
            self.assertTrue(path.exists())
            loaded = qe.load_cache("aaa", ASOF, cache)  # case-insensitive
            self.assertEqual(loaded, rec)

    def test_load_missing_returns_none(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(qe.load_cache("ZZZ", ASOF, Path(tmp)))


class TestEnrichQualitative(unittest.TestCase):
    def _run(self, panels, assess_map, cache_dir, scuttlebutt=False):
        calls: list[str] = []

        def assess_one(panel, asof, scuttle):
            calls.append(panel.ticker)
            return assess_map[panel.ticker]

        records = qe.enrich_qualitative(
            panels,
            asof=ASOF,
            scuttlebutt=scuttlebutt,
            cache_dir=cache_dir,
            assess_one=assess_one,
            now_fn=lambda: NOW,
        )
        return records, calls

    def test_maps_assessment_to_record_with_metadata(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            recs, calls = self._run(
                [_panel("AAA")], {"AAA": _assessment()}, Path(tmp), scuttlebutt=True
            )
            self.assertEqual(calls, ["AAA"])
            rec = recs[0]
            self.assertEqual(rec.moat_type, "brand")
            self.assertTrue(rec.understandable)
            self.assertTrue(rec.used_scuttlebutt)
            self.assertEqual(rec.computed_at, NOW.isoformat())

    def test_warm_cache_skips_recompute(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            self._run([_panel("AAA")], {"AAA": _assessment()}, cache)
            # Second pass: cache is warm, assess_one must NOT be called.
            recs2, calls2 = self._run([_panel("AAA")], {"AAA": _assessment("network")}, cache)
            self.assertEqual(calls2, [])  # no recompute
            self.assertEqual(recs2[0].moat_type, "brand")  # original cached value, not "network"

    def test_all_none_assessment_not_cached_retries(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            self._run([_panel("AAA")], {"AAA": _ALL_NONE}, cache)
            self.assertFalse((cache / ASOF.isoformat() / "AAA.json").exists())
            # next run retries (assess_one called again)
            _, calls2 = self._run([_panel("AAA")], {"AAA": _ALL_NONE}, cache)
            self.assertEqual(calls2, ["AAA"])

    def test_none_assessment_yields_none_record_not_cached(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            recs, _ = self._run([_panel("AAA")], {"AAA": None}, cache)
            self.assertIsNone(recs[0])
            self.assertFalse((cache / ASOF.isoformat() / "AAA.json").exists())

    def test_unique_ticker_computed_once(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            recs, calls = self._run(
                [_panel("AAA"), _panel("AAA")], {"AAA": _assessment()}, Path(tmp)
            )
            self.assertEqual(calls, ["AAA"])  # deduped
            self.assertEqual(len(recs), 2)
            self.assertEqual(recs[0].moat_type, recs[1].moat_type)


class TestStampColumns(unittest.TestCase):
    def test_stamps_seven_columns_by_ticker(self) -> None:
        frame = pd.DataFrame({"ticker": ["AAA", "BBB"], "layer4_weighted_score": [5, 3]})
        records = {
            "AAA": qe.QualRecord(
                moat_type="brand",
                moat_trend="stable",
                management_candor="candid",
                understandable=True,
                rationale="x",
                used_scuttlebutt=False,
                computed_at=NOW.isoformat(),
            ),
            "BBB": None,
        }
        out = qe.stamp_columns(frame, records)
        self.assertEqual(list(out["layer4_weighted_score"]), [5, 3])
        for col in _QUAL_COLUMNS:
            self.assertIn(col, out.columns)
        a = out[out["ticker"] == "AAA"].iloc[0]
        self.assertEqual(a["buffett_moat_type"], "brand")
        self.assertEqual(a["buffett_understandable"], True)
        b = out[out["ticker"] == "BBB"].iloc[0]
        self.assertTrue(pd.isna(b["buffett_moat_type"]))


class _FakeStore:
    """Minimal _FundamentalsStore — every accessor empty, so panels are all-None
    but still carry ticker/theme (the qual path only needs identity here)."""

    def ev_fcff_features_as_of(self, ticker, asof):
        return None

    def annual_series_as_of(self, ticker, asof, *, max_years=10):
        return []

    def owner_earnings_as_of(self, ticker, asof, *, max_years=10):
        return []

    def capital_allocation_as_of(self, ticker, asof, *, max_years=10):
        return []


class TestEnrichBriefParquet(unittest.TestCase):
    def test_stamps_qual_columns_into_brief_parquet_in_place(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            briefs = Path(tmp) / "briefs"
            briefs.mkdir()
            cache = Path(tmp) / "cache"
            pd.DataFrame({"ticker": ["AAA", "BBB"], "theme": ["t1", "t2"]}).to_parquet(
                briefs / f"{ASOF.isoformat()}.parquet", index=False
            )
            assess_map = {"AAA": _assessment("brand"), "BBB": None}
            n_real = qe.enrich_brief_parquet(
                ASOF,
                briefs_dir=briefs,
                store=_FakeStore(),
                mcap_fn=lambda ticker, asof=None: None,
                dividends_fn=lambda ticker, asof=None: pd.Series(dtype="float64"),
                cache_dir=cache,
                assess_one=lambda panel, asof, scuttle: assess_map[panel.ticker.upper()],
            )
            out = pd.read_parquet(briefs / f"{ASOF.isoformat()}.parquet")
            for col in _QUAL_COLUMNS:
                self.assertIn(col, out.columns)
            a = out[out["ticker"] == "AAA"].iloc[0]
            self.assertEqual(a["buffett_moat_type"], "brand")
            self.assertEqual(a["buffett_understandable"], True)
            b = out[out["ticker"] == "BBB"].iloc[0]
            self.assertTrue(pd.isna(b["buffett_moat_type"]))
            self.assertEqual(n_real, 1)  # only AAA resolved a real classification


if __name__ == "__main__":
    unittest.main()
