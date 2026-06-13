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
from alphalens_pipeline.experts.buffett import qual_enrichment as qe
from alphalens_pipeline.experts.buffett.comparison import BuffettPanel
from alphalens_pipeline.experts.buffett.qualitative import QualitativeAssessment

ASOF = dt.date(2026, 6, 11)
NOW = dt.datetime(2026, 6, 12, 9, 0, 0, tzinfo=dt.UTC)
_CV = qe.BUFFETT_QUAL_CONFIG_VERSION  # "buffett-pre-registry-v0"

# Reference the module constant directly so the fixture cannot drift from the
# real column set (the 8th `buffett_qual_config_version` column is now covered).
_QUAL_COLUMNS = qe.QUAL_COLUMNS


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
                config_version=_CV,
            )
            qe.write_cache("AAA", ASOF, cache, rec)
            path = cache / _CV / ASOF.isoformat() / "AAA.json"
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
            self.assertFalse((cache / _CV / ASOF.isoformat() / "AAA.json").exists())
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
            self.assertFalse((cache / _CV / ASOF.isoformat() / "AAA.json").exists())

    def test_scuttlebutt_and_plain_runs_have_separate_cache(self) -> None:
        # A no-scuttlebutt cache entry must NOT short-circuit a --scuttlebutt run.
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            self._run([_panel("AAA")], {"AAA": _assessment("brand")}, cache, scuttlebutt=False)
            # Scuttlebutt run is a distinct computation -> assess_one IS called.
            _, calls = self._run(
                [_panel("AAA")], {"AAA": _assessment("network")}, cache, scuttlebutt=True
            )
            self.assertEqual(calls, ["AAA"])
            self.assertTrue((cache / _CV / ASOF.isoformat() / "AAA.json").exists())
            self.assertTrue((cache / _CV / ASOF.isoformat() / "AAA.sb.json").exists())

    def test_assess_one_raising_is_failsoft(self) -> None:
        import tempfile
        from pathlib import Path

        def boom(panel, asof, scuttle):
            raise RuntimeError("vendor hiccup")

        with tempfile.TemporaryDirectory() as tmp:
            recs = qe.enrich_qualitative(
                [_panel("AAA"), _panel("BBB")],
                asof=ASOF,
                cache_dir=Path(tmp),
                assess_one=boom,
                now_fn=lambda: NOW,
            )
            self.assertEqual(recs, [None, None])  # batch survives

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
                config_version=_CV,
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
        self.assertEqual(a["buffett_qual_config_version"], _CV)
        b = out[out["ticker"] == "BBB"].iloc[0]
        self.assertTrue(pd.isna(b["buffett_moat_type"]))
        self.assertTrue(pd.isna(b["buffett_qual_config_version"]))


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
            self.assertEqual(a["buffett_qual_config_version"], _CV)
            b = out[out["ticker"] == "BBB"].iloc[0]
            self.assertTrue(pd.isna(b["buffett_moat_type"]))
            self.assertTrue(pd.isna(b["buffett_qual_config_version"]))
            self.assertEqual(n_real, 1)  # only AAA resolved a real classification


class TestConfigVersionCache(unittest.TestCase):
    """The config_version tier keeps distinct rubrics from colliding and tags
    every verdict, while a legacy untagged body still loads as v0."""

    def _real_body(self, **over) -> dict:
        body = {
            "moat_type": "brand",
            "moat_trend": "stable",
            "management_candor": "candid",
            "understandable": True,
            "rationale": "x",
            "used_scuttlebutt": False,
            "computed_at": NOW.isoformat(),
        }
        body.update(over)
        return body

    def test_cache_path_has_config_version_tier(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            rec = qe.QualRecord(**self._real_body(config_version=_CV))
            qe.write_cache("AAA", ASOF, cache, rec)
            self.assertTrue((cache / _CV / ASOF.isoformat() / "AAA.json").exists())
            # NEVER the legacy un-tiered path.
            self.assertFalse((cache / ASOF.isoformat() / "AAA.json").exists())

    def test_distinct_config_versions_do_not_collide(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            v0 = qe.QualRecord(**self._real_body(config_version="buffett-pre-registry-v0"))
            v1 = qe.QualRecord(
                **self._real_body(moat_type="network", rationale="y", config_version="buffett-v1")
            )
            qe.write_cache("AAA", ASOF, cache, v0, config_version="buffett-pre-registry-v0")
            qe.write_cache("AAA", ASOF, cache, v1, config_version="buffett-v1")
            # Same (date, ticker), different rubric -> two files, neither overwritten.
            self.assertEqual(
                qe.load_cache("AAA", ASOF, cache, config_version="buffett-pre-registry-v0"), v0
            )
            self.assertEqual(qe.load_cache("AAA", ASOF, cache, config_version="buffett-v1"), v1)

    def test_legacy_body_without_version_back_stamps_v0_on_load(self) -> None:
        import json as _json
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            # A pre-retrofit body (no config_version) sitting under the v0 tier.
            p = cache / _CV / ASOF.isoformat() / "AAA.json"
            p.parent.mkdir(parents=True)
            p.write_text(_json.dumps(self._real_body(rationale="legacy")))
            rec = qe.load_cache("AAA", ASOF, cache)
            self.assertIsNotNone(rec)
            self.assertEqual(rec.config_version, _CV)
            self.assertEqual(rec.moat_type, "brand")

    def test_load_at_wrong_tier_is_miss_not_misattributed(self) -> None:
        import json as _json
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            # A buffett-v1 body physically mis-placed under the v0 tier dir.
            p = cache / _CV / ASOF.isoformat() / "AAA.json"
            p.parent.mkdir(parents=True)
            p.write_text(_json.dumps(self._real_body(config_version="buffett-v1")))
            # Reading at the v0 tier must MISS (guard), never mis-attribute to v0.
            self.assertIsNone(qe.load_cache("AAA", ASOF, cache, config_version=_CV))

    def test_pre_migration_legacy_path_is_a_miss(self) -> None:
        import json as _json
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            # The TRUE legacy path (the real VPS pre-migration state).
            legacy = cache / ASOF.isoformat()
            legacy.mkdir(parents=True)
            (legacy / "AAA.json").write_text(_json.dumps(self._real_body()))
            # load_cache looks under the v0 tier, so this is a miss until migrated.
            self.assertIsNone(qe.load_cache("AAA", ASOF, cache))

    def test_enrich_qualitative_threads_version_end_to_end(self) -> None:
        # Pins that the constant is RESOLVED + THREADED automatically (catches a
        # dropped config_version in _resolve_one that explicit-arg unit tests miss).
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            recs = qe.enrich_qualitative(
                [_panel("AAA")],
                asof=ASOF,
                cache_dir=cache,
                assess_one=lambda panel, asof, scuttle: _assessment("brand"),
                now_fn=lambda: NOW,
            )
            self.assertEqual(recs[0].config_version, _CV)
            self.assertTrue((cache / _CV / ASOF.isoformat() / "AAA.json").exists())
            self.assertFalse((cache / ASOF.isoformat() / "AAA.json").exists())
            # A warm second pass short-circuits via the versioned tier.
            calls: list[str] = []
            qe.enrich_qualitative(
                [_panel("AAA")],
                asof=ASOF,
                cache_dir=cache,
                assess_one=lambda p, a, s: calls.append(p.ticker) or _assessment("network"),
                now_fn=lambda: NOW,
            )
            self.assertEqual(calls, [])


class TestMigrateLegacyCache(unittest.TestCase):
    """The one-shot move relocates the untagged corpus into the v0 tier exactly
    once (no overwrite, no double-count) and is idempotent."""

    def _body(self, **over) -> dict:
        body = {
            "moat_type": "brand",
            "moat_trend": "stable",
            "management_candor": "candid",
            "understandable": True,
            "rationale": "z",
            "used_scuttlebutt": False,
            "computed_at": NOW.isoformat(),
        }
        body.update(over)
        return body

    def test_migrate_moves_to_version_tier_and_is_idempotent(self) -> None:
        import json as _json
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            legacy_dir = cache / ASOF.isoformat()
            legacy_dir.mkdir(parents=True)
            (legacy_dir / "AAA.json").write_text(_json.dumps(self._body()))
            (legacy_dir / "AAA.sb.json").write_text(_json.dumps(self._body(used_scuttlebutt=True)))
            n = qe.migrate_legacy_qual_cache(cache)
            self.assertEqual(n, 2)
            v = cache / _CV / ASOF.isoformat()
            self.assertEqual(qe.load_cache("AAA", ASOF, cache).config_version, _CV)
            self.assertTrue((v / "AAA.sb.json").exists())
            # MOVED, not copied — the legacy files are gone.
            self.assertFalse((legacy_dir / "AAA.json").exists())
            self.assertFalse((legacy_dir / "AAA.sb.json").exists())
            # Idempotent: a second run finds no legacy date tiers.
            self.assertEqual(qe.migrate_legacy_qual_cache(cache), 0)

    def test_migrated_corpus_has_no_double_count(self) -> None:
        import json as _json
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            legacy_dir = cache / ASOF.isoformat()
            legacy_dir.mkdir(parents=True)
            (legacy_dir / "AAA.json").write_text(_json.dumps(self._body()))
            qe.migrate_legacy_qual_cache(cache)
            # A naive corpus walk keyed by (config_version, date, ticker, scuttle):
            keys: list[tuple[str, str, str, bool]] = []
            for f in cache.rglob("*.json"):
                rel = f.relative_to(cache)
                cv, date, name = rel.parts[0], rel.parts[1], rel.parts[2]
                sb = name.endswith(".sb.json")
                ticker = name[:-8] if sb else name[:-5]
                keys.append((cv, date, ticker, sb))
            self.assertEqual(len(keys), 1)
            self.assertEqual(len(set(keys)), 1)

    def test_migrate_skips_version_tier_and_missing_dir(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            # Already-migrated layout only -> migrate finds no legacy date tier.
            rec = qe.QualRecord(
                moat_type="brand",
                moat_trend="stable",
                management_candor="candid",
                understandable=True,
                rationale="x",
                used_scuttlebutt=False,
                computed_at=NOW.isoformat(),
                config_version=_CV,
            )
            qe.write_cache("AAA", ASOF, cache, rec)
            self.assertEqual(qe.migrate_legacy_qual_cache(cache), 0)
            # A non-existent cache dir is a no-op, not a crash.
            self.assertEqual(qe.migrate_legacy_qual_cache(cache / "nope"), 0)


if __name__ == "__main__":
    unittest.main()
