"""L3 golden-master replay of the extract stage (test-strategy Phase 3b).

Drives the REAL ``event_extractor.extract_daily`` deterministically and offline
over a frozen 6-row news slice (``fixtures/extract_day/``): the 3 synthetic
press-release rows take the deterministic template path (no LLM), and the 3
real rows fall through to DeepSeek Flash whose real bytes were recorded once by
``scripts/record_golden_extract.py`` and are replayed via ``ReplayOpenRouter``.

The point is to assert SIDE EFFECTS, not exit codes: a template that stops
firing (predicate / entity regression) flips ``extraction_method`` template→
flash in the projection; a Flash model that returns empty extractions flips
``themes_nonempty``; a schema drift shows in ``columns``. A cassette miss is
fail-loud (re-record), never a silent live call.

Refresh the fixtures with ``scripts/record_golden_extract.py`` (needs a live
key) and review the diff in the PR.
"""

from __future__ import annotations

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd
from alphalens_pipeline.thematic.extraction import event_extractor
from alphalens_pipeline.thematic.extraction.templates.entity_resolver import EntityResolver

from tests.golden.projection import extract_projection
from tests.golden.replay_client import ReplayOpenRouter

_ASOF = dt.date(2026, 5, 24)
_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "extract_day"
_CASSETTES = _FIXTURES / "cassettes"
_COMPANY_TICKERS = _FIXTURES / "company_tickers.json"
_GOLDEN = _FIXTURES / "golden" / "projection.json"


def _replay_extract(out_dir: Path) -> pd.DataFrame:
    """Run extract_daily off the frozen fixtures with the LLM replayed."""
    news_path = _FIXTURES / f"{_ASOF.isoformat()}.parquet"
    if not news_path.exists() or not any(_CASSETTES.glob("*.json")):
        raise FileNotFoundError(
            f"golden fixtures missing under {_FIXTURES} — run "
            "scripts/record_golden_extract.py (one-time live capture) to record them"
        )
    replay = ReplayOpenRouter(_CASSETTES)
    resolver = EntityResolver(company_tickers_path=_COMPANY_TICKERS)
    # engine defaults to the shipped DEFAULT_TEMPLATES_DIR — the golden then
    # locks "current templates x current engine", and a template change is a
    # reviewable projection diff.
    return event_extractor.extract_daily(
        date=_ASOF,
        news_dir=_FIXTURES,
        events_dir=out_dir,
        llm_client=replay,
        resolver=resolver,
    )


class TestGoldenExtractReplay(unittest.TestCase):
    def test_replay_matches_golden_projection(self):
        with tempfile.TemporaryDirectory() as td:
            events = _replay_extract(Path(td))
        got = extract_projection(events)
        golden = json.loads(_GOLDEN.read_text())
        self.assertEqual(got, golden)

    def test_both_extraction_paths_present(self):
        # The headline behaviour PR-2/PR-3 added: template path AND Flash
        # fallback both fire in one run. A regression that collapses one path
        # (e.g. engine never matches → all Flash) shows up here.
        with tempfile.TemporaryDirectory() as td:
            events = _replay_extract(Path(td))
        counts = events["extraction_method"].value_counts().to_dict()
        self.assertEqual(counts.get("template"), 3)
        self.assertEqual(counts.get("flash"), 3)

    def test_template_rows_carry_typed_fields(self):
        # Template rows must persist parseable typed fields (the PR-3 contract
        # the brief generator cites). Each synthetic row maps to its template.
        with tempfile.TemporaryDirectory() as td:
            events = _replay_extract(Path(td))
        tmpl = events[events["extraction_method"] == "template"].set_index("news_id")
        expected = {
            "syn_mna": ("m_and_a_press_release", "acquirer_ticker"),
            "syn_earn": ("earnings_surprise", "reporter_ticker"),
            "syn_guid": ("guidance_update", "issuer_ticker"),
        }
        for news_id, (template_id, key) in expected.items():
            row = tmpl.loc[news_id]
            self.assertEqual(row["template_id"], template_id)
            fields = json.loads(row["template_fields_json"])
            self.assertIn(key, fields)
            self.assertEqual(row["confidence"], 1.0)

    def test_flash_rows_have_no_template_id(self):
        with tempfile.TemporaryDirectory() as td:
            events = _replay_extract(Path(td))
        flash = events[events["extraction_method"] == "flash"]
        self.assertTrue(flash["template_id"].isna().all())
        self.assertTrue(flash["template_fields_json"].isna().all())

    def test_replay_is_deterministic(self):
        with tempfile.TemporaryDirectory() as td1, tempfile.TemporaryDirectory() as td2:
            a = extract_projection(_replay_extract(Path(td1)))
            b = extract_projection(_replay_extract(Path(td2)))
        self.assertEqual(a, b)

    def test_events_parquet_written_to_output_dir(self):
        with tempfile.TemporaryDirectory() as td:
            _replay_extract(Path(td))
            self.assertTrue((Path(td) / f"{_ASOF.isoformat()}.parquet").exists())


if __name__ == "__main__":
    unittest.main()
