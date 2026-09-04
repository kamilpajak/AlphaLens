"""Score-CLI wiring: short-interest telemetry stamped after options telemetry."""

from __future__ import annotations

import datetime as dt
import unittest
from unittest.mock import patch

import pandas as pd


class TestScoreCliWiresShortInterestTelemetry(unittest.TestCase):
    def test_score_command_source_wires_short_interest_telemetry(self):
        # Static wiring pin (cheap, catches accidental removal): score() must
        # call _apply_short_interest_telemetry; the helper must lazy-import the
        # short_interest_telemetry package inside its fail-soft boundary.
        import inspect

        from alphalens_cli.commands import thematic

        src = inspect.getsource(thematic.score)
        self.assertIn("_apply_short_interest_telemetry", src)
        helper_src = inspect.getsource(thematic._apply_short_interest_telemetry)
        self.assertIn("short_interest_telemetry", helper_src)

    def test_helper_delegates_to_enrich_with_target(self):
        from alphalens_cli.commands import thematic

        frame = pd.DataFrame({"theme": ["q"], "ticker": ["QUBT"], "company_name": ["Q"]})
        captured = {}

        def _fake_enrich(fr, *, asof, **kw):
            captured["asof"] = asof
            return fr

        with patch(
            "alphalens_pipeline.thematic.short_interest_telemetry.enrichment.enrich",
            side_effect=_fake_enrich,
        ):
            thematic._apply_short_interest_telemetry(frame, target=dt.date(2026, 9, 3))

        self.assertEqual(captured["asof"], dt.date(2026, 9, 3))

    def test_helper_is_fail_soft(self):
        from alphalens_cli.commands import thematic

        frame = pd.DataFrame({"theme": ["q"], "ticker": ["QUBT"], "company_name": ["Q"]})
        with patch(
            "alphalens_pipeline.thematic.short_interest_telemetry.enrichment.enrich",
            side_effect=RuntimeError("boom"),
        ):
            out = thematic._apply_short_interest_telemetry(frame, target=dt.date(2026, 9, 3))
        pd.testing.assert_frame_equal(out, frame)  # unchanged, no raise


if __name__ == "__main__":
    unittest.main()
