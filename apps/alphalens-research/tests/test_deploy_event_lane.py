"""Deploy contract of the event lane (#1296): the day script runs the detector
only behind the flag, best-effort, between shadow-map and score; the systemd
unit forwards the flag into the container."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
RUN_THEMATIC_SCRIPT = REPO_ROOT / "deploy" / "docker" / "run_thematic_day.sh"
THEMATIC_BUILD_SERVICE = REPO_ROOT / "deploy" / "systemd" / "alphalens-thematic-build.service"
README = REPO_ROOT / "deploy" / "systemd" / "README.md"


def _logical_lines(text: str) -> list[str]:
    out: list[str] = []
    buf = ""
    for raw in text.splitlines():
        if raw.rstrip().endswith("\\"):
            buf += raw.rstrip()[:-1] + " "
            continue
        out.append((buf + raw).strip())
        buf = ""
    if buf:
        out.append(buf.strip())
    return out


class TestRunThematicDayEventLane(unittest.TestCase):
    def setUp(self):
        self.text = RUN_THEMATIC_SCRIPT.read_text()
        self.logical = _logical_lines(self.text)

    def _event_line(self) -> str:
        matches = [ln for ln in self.logical if ln.startswith("alphalens events insider-clusters")]
        self.assertEqual(len(matches), 1, matches)
        return matches[0]

    def test_event_stage_is_gated_on_the_flag(self):
        self.assertIn('if [ "${ALPHALENS_EVENT_LANE:-0}" = "1" ]; then', self.text)
        gate = self.text.index('if [ "${ALPHALENS_EVENT_LANE:-0}" = "1" ]; then')
        stage = self.text.index("alphalens events insider-clusters")
        self.assertLess(gate, stage)
        self.assertLess(stage, self.text.index("fi\n", stage))

    def test_event_stage_is_best_effort(self):
        line = self._event_line()
        self.assertIn("||", line, line)
        self.assertRegex(line, r"\|\|\s*echo .*WARN")

    def test_event_stage_runs_after_shadow_map_and_before_score(self):
        shadow = self.text.index("alphalens thematic shadow-map")
        events = self.text.index("alphalens events insider-clusters")
        score = self.text.index("alphalens thematic score")
        self.assertLess(shadow, events)
        self.assertLess(events, score)


class TestThematicBuildUnitEventLane(unittest.TestCase):
    def test_docker_run_forwards_the_event_lane_flag(self):
        unit = THEMATIC_BUILD_SERVICE.read_text()
        self.assertRegex(unit, re.compile(r"^\s+-e ALPHALENS_EVENT_LANE\s*\\?\s*$", re.MULTILINE))
        # bare `-e KEY` (no `=value`): the container inherits the flag only when
        # /etc/alphalens/env sets it, so an unset host keeps the lane OFF
        self.assertNotRegex(unit, re.compile(r"-e ALPHALENS_EVENT_LANE=", re.MULTILINE))

    def test_readme_documents_the_accrual_switch(self):
        text = README.read_text()
        self.assertIn("ALPHALENS_EVENT_LANE=1", text)
        self.assertIn("#1297", text)


if __name__ == "__main__":
    unittest.main()
