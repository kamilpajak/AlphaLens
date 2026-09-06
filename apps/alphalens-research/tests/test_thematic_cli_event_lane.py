"""``alphalens thematic score`` behind the event-lane flag (#1296).

Flag OFF must be byte-identical to the pre-lane behaviour even when an event
parquet exists; flag ON merges the eligible event rows (and stamps ``source``
even when the event parquet is missing).
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd
from alphalens_cli.main import app
from alphalens_pipeline.events import EVENT_LANE_ENV
from typer.testing import CliRunner

from tests.events.test_merge import event_frame, thematic_frame

DATE = "2026-03-04"


def _fake_score(df, *, asof):
    return df.assign(layer4_weighted_score=1, technical_atr_pct=2.0, selection_score=1.0)


class TestScoreEventLane(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()

    def _run(self, *, flag: bool, events: pd.DataFrame | None) -> pd.DataFrame:
        with (
            tempfile.TemporaryDirectory() as cdir,
            tempfile.TemporaryDirectory() as edir,
            tempfile.TemporaryDirectory() as out,
        ):
            thematic_frame().to_parquet(Path(cdir) / f"{DATE}.parquet", index=False)
            if events is not None:
                events.to_parquet(Path(edir) / f"{DATE}.parquet", index=False)
            env = {EVENT_LANE_ENV: "1"} if flag else {}
            with (
                patch.dict(os.environ, env, clear=False),
                patch(
                    "alphalens_cli.commands.thematic.screening_scorer.score_candidates",
                    side_effect=_fake_score,
                ),
                patch(
                    "alphalens_pipeline.experts.buffett.quant_enrichment.enrich",
                    side_effect=lambda df, **kw: df,
                ),
                patch(
                    "alphalens_pipeline.experts.oneil.quant_enrichment.enrich",
                    side_effect=lambda df, **kw: df,
                ),
            ):
                if not flag:
                    os.environ.pop(EVENT_LANE_ENV, None)
                result = self.runner.invoke(
                    app,
                    [
                        "thematic",
                        "score",
                        "--date",
                        DATE,
                        "--candidates-dir",
                        cdir,
                        "--output-dir",
                        out,
                        "--event-candidates-dir",
                        edir,
                    ],
                )
            self.assertEqual(result.exit_code, 0, msg=result.output)
            self.output = result.output
            return pd.read_parquet(Path(out) / f"{DATE}.parquet")

    def test_flag_off_ignores_event_parquet_and_output_is_identical(self):
        without = self._run(flag=False, events=None)
        with_events = self._run(flag=False, events=event_frame(("AAA",)))
        pd.testing.assert_frame_equal(with_events, without)
        self.assertNotIn("source", without.columns)
        self.assertNotIn("event_overlap", without.columns)
        self.assertNotIn("Event lane ON", self.output)

    def test_flag_on_appends_eligible_event_rows_with_source(self):
        out = self._run(flag=True, events=event_frame(("AAA",)))
        self.assertEqual(list(out.ticker), ["QUBT", "IONQ", "AAA"])
        self.assertEqual(list(out.source), ["thematic", "thematic", "insider_cluster"])
        self.assertIn("Event lane ON: 1 event row(s) appended, 0 overlap(s)", self.output)
        self.assertIn("layer4_weighted_score", out.columns)

    def test_flag_on_missing_event_parquet_still_stamps_source_thematic(self):
        out = self._run(flag=True, events=None)
        self.assertEqual(list(out.source), ["thematic", "thematic"])
        self.assertFalse(out.event_overlap.any())
        self.assertIn("Event lane ON: 0 event row(s) appended", self.output)

    def test_flag_on_overlap_collapses_to_one_row_per_ticker(self):
        out = self._run(flag=True, events=event_frame(("QUBT", "BBB")))
        self.assertEqual(list(out.ticker), ["QUBT", "IONQ", "BBB"])
        self.assertEqual(out.ticker.is_unique, True)
        q = out[out.ticker == "QUBT"].iloc[0]
        self.assertEqual(q.source, "thematic")
        self.assertTrue(q.event_overlap)
        self.assertIn("1 overlap(s)", self.output)


if __name__ == "__main__":
    unittest.main()
