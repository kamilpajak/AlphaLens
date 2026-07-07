"""Score-CLI wiring: options enrichment is called with carry-forward previous."""

from __future__ import annotations

import datetime as dt
import unittest
from unittest.mock import patch

import pandas as pd


class TestScoreCliWiresOptionsEnrichment(unittest.TestCase):
    def test_score_command_source_wires_options_enrichment(self):
        # Static wiring pin (cheap, catches accidental removal): the score
        # command body must import + call the options enricher with previous=.
        import inspect

        from alphalens_cli.commands import thematic

        src = inspect.getsource(thematic.score)
        self.assertIn("options_telemetry", src)
        self.assertIn("previous=", src)

    def test_enrich_receives_previous_frame_when_output_exists(self):
        # Behavior pin through the helper the CLI calls (keeps the CLI thin).
        import tempfile
        from pathlib import Path

        from alphalens_cli.commands import thematic

        frame = pd.DataFrame({"theme": ["q"], "ticker": ["QUBT"], "company_name": ["Q"]})
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "2026-07-06.parquet"
            prev = frame.copy()
            prev["options_snapshot_utc"] = ["2026-07-07T00:30:00+00:00"]
            prev.to_parquet(out_path, index=False)

            captured = {}

            def _fake_enrich(fr, *, asof, previous=None, **kw):
                captured["previous"] = previous
                return fr

            with patch(
                "alphalens_pipeline.thematic.options_telemetry.enrichment.enrich",
                side_effect=_fake_enrich,
            ):
                thematic._apply_options_telemetry(
                    frame, target=dt.date(2026, 7, 6), out_path=out_path
                )

        self.assertIsNotNone(captured["previous"])
        self.assertIn("options_snapshot_utc", captured["previous"].columns)

    def test_helper_is_fail_soft(self):
        import tempfile
        from pathlib import Path

        from alphalens_cli.commands import thematic

        frame = pd.DataFrame({"theme": ["q"], "ticker": ["QUBT"], "company_name": ["Q"]})
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch(
                "alphalens_pipeline.thematic.options_telemetry.enrichment.enrich",
                side_effect=RuntimeError("boom"),
            ),
        ):
            out = thematic._apply_options_telemetry(
                frame, target=dt.date(2026, 7, 6), out_path=Path(tmp) / "x.parquet"
            )
        pd.testing.assert_frame_equal(out, frame)  # unchanged, no raise


if __name__ == "__main__":
    unittest.main()
