"""Deploy contract of the event lane (#1296): the day script runs the detector
only behind the flag, best-effort, before score; the systemd unit forwards the
flag into the container. The shadow-map collection no longer lives in the day
script (#1330: its own OnSuccess-activated unit), so this file also pins its
absence."""

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

    def test_event_stage_runs_after_map_themes_and_before_score(self):
        mapping = self.text.index("alphalens thematic map-themes")
        events = self.text.index("alphalens events insider-clusters")
        score = self.text.index("alphalens thematic score")
        self.assertLess(mapping, events)
        self.assertLess(events, score)

    def test_runner_no_longer_calls_shadow_map(self):
        # #1330: the 65-73 min collection sat before score/brief and timed
        # the 00:30 UTC slot out 12 days of 13. It runs in
        # alphalens-thematic-shadow-map.service (OnSuccess= on the build).
        calls = [ln for ln in self.logical if ln.startswith("alphalens thematic shadow-map")]
        self.assertEqual(calls, [])


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
