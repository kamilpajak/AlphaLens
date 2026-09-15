"""Pin the names of the gauges the edge mirror publishes (#1436).

``manage.py rebuild_ladder_outcomes_cache`` writes three gauges to the textfile
collector after every run; the Prometheus rules that replace the old
``AlphalensEdgeStale`` (which only saw whether the mirror RAN) read them by name.
The Django command and the rules file cannot share a constant (research CI runs
without Django configured, and the rules file is YAML), so the names are pinned
here from the research side, the way ``test_ingest_watermark_parity.py`` pins the
sentinel filename: read the Django source as TEXT and require each name as a
string literal assigned to a module constant.

``EDGE_MIRROR_GAUGES`` is the single list the rules-side test reads too, so a
rename on either side is a red test rather than a rule that quietly never fires.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_COMMAND_PATH = (
    _REPO_ROOT
    / "apps"
    / "alphalens-django"
    / "edge"
    / "management"
    / "commands"
    / "rebuild_ladder_outcomes_cache.py"
)

# Name -> the fact the value encodes. Keep in step with the module constants in
# the Django command (each appears there as ``<CONST> = "<name>"``).
EDGE_MIRROR_GAUGES: dict[str, str] = {
    "alphalens_edge_mirror_watermark_timestamp_seconds": (
        "completed_at of the ingest watermark the mirror read this run (0 when none)"
    ),
    "alphalens_edge_mirror_unsettled_dates": "dates refused this run (parquet newer than the watermark)",
    "alphalens_edge_mirror_newest_brief_date_timestamp_seconds": (
        "newest brief date in the mirror ledger (edge_daymetaladderoutcome) after the run, midnight UTC (0 before the first ingest)"
    ),
}

_ASSIGNMENT = re.compile(r'^[A-Z_]+ = "(?P<name>alphalens_edge_mirror_[a-z_]+)"$', re.MULTILINE)


def _published_names(source: str) -> set[str]:
    return {m.group("name") for m in _ASSIGNMENT.finditer(source)}


class TestEdgeMirrorGaugeNames(unittest.TestCase):
    def setUp(self) -> None:
        self.assertTrue(_COMMAND_PATH.is_file(), f"Django command not found at {_COMMAND_PATH}")
        self.source = _COMMAND_PATH.read_text()

    def test_command_declares_every_pinned_gauge(self) -> None:
        self.assertEqual(
            _published_names(self.source),
            set(EDGE_MIRROR_GAUGES),
            "the gauge names the Django command assigns to module constants must be "
            "exactly the pinned set — the Prometheus rules read them by name.",
        )

    def test_the_grep_can_fail(self) -> None:
        # Positive control: a source that renames one gauge is caught, so the
        # assertion above is not vacuous.
        renamed = self.source.replace(
            "alphalens_edge_mirror_unsettled_dates", "alphalens_edge_mirror_refused_dates"
        )
        self.assertNotEqual(_published_names(renamed), set(EDGE_MIRROR_GAUGES))


if __name__ == "__main__":
    unittest.main()
