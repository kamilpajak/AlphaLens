"""L3 golden-master replay of the brief-generation stage (test-strategy Phase 3).

Drives the REAL ``generate_briefs`` deterministically and offline over a frozen
slice of REAL scored candidates (``fixtures/brief_day/``): the LLM goes through
``ReplayOpenRouter`` (real DeepSeek bytes recorded once by
``scripts/record_golden_brief.py``), OHLCV is the frozen cache fixture, and the
earnings lookup is stubbed. The whole point is to assert SIDE EFFECTS, not exit
codes — a model retirement that returns empty briefs (the #3 escape: run exits
0, brief is empty) shows up here as ``has_tldr=False`` breaking the golden, and
a column rename / row drop shows up as a projection diff.

Refresh the fixtures with ``scripts/record_golden_brief.py`` (needs a live key)
and review the diff in the PR.
"""

from __future__ import annotations

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd
from alphalens_pipeline.thematic.argumentation import orchestrator as brief_orch

from tests.golden.projection import brief_projection
from tests.golden.replay_client import ReplayOpenRouter

_ASOF = dt.date(2026, 5, 24)
_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "brief_day"
_CASSETTES = _FIXTURES / "cassettes"
_OHLCV = _FIXTURES / "ohlcv"
_GOLDEN = _FIXTURES / "golden" / "projection.json"

_PRO = "deepseek/deepseek-v4-pro"
_FLASH = "deepseek/deepseek-v4-flash"


def _ohlcv_loader(ticker: str, asof: dt.date) -> pd.DataFrame:
    path = _OHLCV / f"{ticker}_{asof.isoformat()}.parquet"
    return pd.read_parquet(path) if path.exists() else pd.DataFrame()


def _replay_briefs(out_dir: Path) -> pd.DataFrame:
    """Run generate_briefs off the frozen fixtures with the LLM replayed."""
    scored_path = _FIXTURES / "scored.parquet"
    if not scored_path.exists() or not any(_CASSETTES.glob("*.json")):
        raise FileNotFoundError(
            f"golden fixtures missing under {_FIXTURES} — run "
            "scripts/record_golden_brief.py (one-time live capture) to record them"
        )
    scored = pd.read_parquet(scored_path)
    replay = ReplayOpenRouter(_CASSETTES)
    with (
        mock.patch.object(brief_orch, "_build_clients", return_value=(replay, replay)),
        mock.patch(
            "alphalens_pipeline.thematic.sources.earnings_calendar.fetch_next_earnings",
            lambda *, ticker, asof: None,
        ),
    ):
        return brief_orch.generate_briefs(
            scored, asof=_ASOF, output_dir=out_dir, ohlcv_loader=_ohlcv_loader
        )


class TestGoldenBriefReplay(unittest.TestCase):
    def test_replay_matches_golden_projection(self):
        with tempfile.TemporaryDirectory() as td:
            brief = _replay_briefs(Path(td))
        got = brief_projection(brief)
        golden = json.loads(_GOLDEN.read_text())
        self.assertEqual(got, golden)

    def test_briefs_are_non_empty(self):
        # The primary L3 target (#3): a silently-empty brief (model retired →
        # 404 → empty) would still exit 0. Assert the side effect: rows exist
        # AND every row got a tldr back from its (replayed) LLM call.
        with tempfile.TemporaryDirectory() as td:
            brief = _replay_briefs(Path(td))
        self.assertGreater(len(brief), 0)
        self.assertTrue(brief["brief_tldr"].notna().all())
        self.assertTrue((brief["brief_tldr"].astype(str).str.strip() != "").all())

    def test_model_routing_by_score(self):
        # Score >=4 routes to Pro, <4 to Flash. Pins the deterministic
        # choose_model boundary end-to-end.
        with tempfile.TemporaryDirectory() as td:
            brief = _replay_briefs(Path(td))
        routed = dict(zip(brief["ticker"], brief["brief_model_used"], strict=True))
        self.assertEqual(routed["DFIN"], _PRO)
        self.assertEqual(routed["QLYS"], _PRO)
        self.assertEqual(routed["MANH"], _FLASH)
        self.assertEqual(routed["QUBT"], _FLASH)

    def test_replay_is_deterministic(self):
        # Two replays of the cassettes produce the identical projection
        # (excludes the volatile brief_generated_at). If this flaps, there is a
        # hidden non-determinism source in the prompt or ordering.
        with tempfile.TemporaryDirectory() as td1, tempfile.TemporaryDirectory() as td2:
            a = brief_projection(_replay_briefs(Path(td1)))
            b = brief_projection(_replay_briefs(Path(td2)))
        self.assertEqual(a, b)

    def test_brief_parquet_written_to_output_dir(self):
        # Side-effect contract: the stage writes {asof}.parquet (the file the
        # paper chain + Django ingest read).
        with tempfile.TemporaryDirectory() as td:
            _replay_briefs(Path(td))
            self.assertTrue((Path(td) / f"{_ASOF.isoformat()}.parquet").exists())


if __name__ == "__main__":
    unittest.main()
