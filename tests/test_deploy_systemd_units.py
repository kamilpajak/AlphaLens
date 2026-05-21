"""Guard against regressions in the deploy/systemd unit files.

The pipeline image declares ``ENTRYPOINT ["/app/.venv/bin/alphalens"]`` so
any ``docker compose run pipeline <script>`` invocation must explicitly
override the entrypoint, otherwise typer interprets the script path as a
command name and dies before the pipeline starts. The systemd unit silently
exits non-zero in that case, so a missing override yields the symptom
"daily timer fires, briefs/ stays empty" — exactly the failure mode caught
during PR 2's zen pre-merge review.
"""

from __future__ import annotations

import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVICE_PATH = REPO_ROOT / "deploy" / "systemd" / "alphalens-thematic-daily.service"


class TestSystemdUnits(unittest.TestCase):
    def setUp(self) -> None:
        self.unit_text = SERVICE_PATH.read_text()

    def test_thematic_daily_service_overrides_entrypoint(self):
        self.assertIn(
            "--entrypoint /bin/bash",
            self.unit_text,
            "ExecStart must override the pipeline image ENTRYPOINT or typer "
            "will refuse to run the script — see "
            "deploy/docker/Dockerfile.pipeline:ENTRYPOINT and the comment "
            "above ExecStart in alphalens-thematic-daily.service.",
        )

    def test_thematic_daily_service_invokes_driver_script(self):
        self.assertIn(
            "/app/deploy/docker/run_thematic_day.sh",
            self.unit_text,
        )

    def test_thematic_daily_service_keeps_oneshot_type(self):
        self.assertIn("Type=oneshot", self.unit_text)


if __name__ == "__main__":
    unittest.main()
