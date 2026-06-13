"""Unit tests for the registry-driven enrichment driver ``enrich_briefs``.

Mechanics only (read-once / write-once / capability-skip / fail-soft) with fake
experts so no network or LLM is touched. The Buffett expert's actual column set is
pinned in ``test_buffett_qual_enrichment.TestBuffettEnrichBriefFrame``.
"""

from __future__ import annotations

import datetime as dt
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pandas as pd
from alphalens_pipeline.experts import enrich as enrich_mod
from alphalens_pipeline.experts.enrich import enrich_briefs

ASOF = dt.date(2026, 6, 11)


class _FakeQualExpert:
    """A qual-capable expert (implements the QualEnrichExpert capability)."""

    def __init__(self, expert_id: str = "fake", *, raises: bool = False) -> None:
        self.id = expert_id
        self._raises = raises

    def enrich_brief_frame(self, df, brief_date, **_kw):
        if self._raises:
            raise RuntimeError("expert boom")
        out = df.copy()
        out[f"{self.id}_col"] = "stamped"
        return out, len(out)

    def migrate_qual_cache(self, cache_dir=None) -> int:
        return 0


class _FakeNumericExpert:
    """A numeric-only expert — no enrich_brief_frame, so NOT a QualEnrichExpert."""

    id = "numeric"


def _write_brief(briefs: Path) -> None:
    briefs.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"ticker": ["AAA", "BBB"], "theme": ["t1", "t2"]}).to_parquet(
        briefs / f"{ASOF.isoformat()}.parquet", index=False
    )


def _kwargs(briefs: Path) -> dict:
    return {
        "briefs_dir": briefs,
        "store": object(),
        "mcap_fn": lambda *_a, **_k: None,
        "dividends_fn": lambda *_a, **_k: None,
    }


class TestEnrichBriefs(unittest.TestCase):
    def test_reads_once_writes_once_atomic_index_false(self) -> None:
        with TemporaryDirectory() as tmp:
            briefs = Path(tmp) / "briefs"
            _write_brief(briefs)
            with (
                patch.object(enrich_mod.pd, "read_parquet", wraps=pd.read_parquet) as read_spy,
                patch.object(enrich_mod, "write_parquet_atomic") as write_spy,
            ):
                counts = enrich_briefs(
                    ASOF, experts=[_FakeQualExpert("buffett")], **_kwargs(briefs)
                )
            self.assertEqual(counts, {"buffett": 2})
            self.assertEqual(read_spy.call_count, 1)  # read ONCE
            self.assertEqual(write_spy.call_count, 1)  # write ONCE
            self.assertFalse(write_spy.call_args.kwargs["index"])  # index=False

    def test_skips_expert_without_qual_capability(self) -> None:
        with TemporaryDirectory() as tmp:
            briefs = Path(tmp) / "briefs"
            _write_brief(briefs)
            counts = enrich_briefs(
                ASOF, experts=[_FakeNumericExpert(), _FakeQualExpert("buffett")], **_kwargs(briefs)
            )
            self.assertEqual(counts, {"buffett": 2})  # numeric skipped, not in counts
            out = pd.read_parquet(briefs / f"{ASOF.isoformat()}.parquet")
            self.assertIn("buffett_col", out.columns)
            self.assertNotIn("numeric_col", out.columns)

    def test_one_expert_failure_does_not_abort_others(self) -> None:
        with TemporaryDirectory() as tmp:
            briefs = Path(tmp) / "briefs"
            _write_brief(briefs)
            counts = enrich_briefs(
                ASOF,
                experts=[_FakeQualExpert("boom", raises=True), _FakeQualExpert("ok")],
                **_kwargs(briefs),
            )
            self.assertEqual(counts, {"boom": 0, "ok": 2})  # failing expert -> 0, other stamps
            out = pd.read_parquet(briefs / f"{ASOF.isoformat()}.parquet")
            self.assertIn("ok_col", out.columns)  # write happened once after both ran
            self.assertNotIn("boom_col", out.columns)

    def test_no_write_when_no_qual_capable_expert(self) -> None:
        with TemporaryDirectory() as tmp:
            briefs = Path(tmp) / "briefs"
            _write_brief(briefs)
            with patch.object(enrich_mod, "write_parquet_atomic") as write_spy:
                counts = enrich_briefs(ASOF, experts=[_FakeNumericExpert()], **_kwargs(briefs))
            self.assertEqual(counts, {})  # nobody stamped
            self.assertEqual(write_spy.call_count, 0)  # so no rewrite of the brief

    def test_missing_brief_raises_filenotfound(self) -> None:
        with TemporaryDirectory() as tmp:
            briefs = Path(tmp) / "briefs"
            briefs.mkdir()
            with self.assertRaises(FileNotFoundError):
                enrich_briefs(ASOF, experts=[_FakeQualExpert()], **_kwargs(briefs))


if __name__ == "__main__":
    unittest.main()
