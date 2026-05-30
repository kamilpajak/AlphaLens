"""Guard against regressions in the deploy/systemd unit files.

The pipeline image declares ``ENTRYPOINT ["/app/.venv/bin/alphalens"]`` so
any ``docker compose run pipeline <script>`` invocation must explicitly
override the entrypoint, otherwise typer interprets the script path as a
command name and dies before the pipeline starts. The systemd unit silently
exits non-zero in that case, so a missing override yields the symptom
"daily timer fires, briefs/ stays empty" — surfaced by zen pre-merge review.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

# Test file lives at apps/alphalens-research/tests/<name>.py; the repo root
# is three parents up. deploy/ stays at the repo root, not under the app.
REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE_PATH = REPO_ROOT / "deploy" / "systemd" / "alphalens-thematic-build.service"


class TestSystemdUnits(unittest.TestCase):
    def setUp(self) -> None:
        self.unit_text = SERVICE_PATH.read_text()

    def test_thematic_build_service_overrides_entrypoint(self):
        self.assertIn(
            "--entrypoint /bin/bash",
            self.unit_text,
            "ExecStart must override the pipeline image ENTRYPOINT or typer "
            "will refuse to run the script — see "
            "deploy/docker/Dockerfile.pipeline:ENTRYPOINT and the comment "
            "above ExecStart in alphalens-thematic-build.service.",
        )

    def test_thematic_build_service_invokes_driver_script(self):
        self.assertIn(
            "/app/deploy/docker/run_thematic_day.sh",
            self.unit_text,
        )

    def test_thematic_build_service_keeps_oneshot_type(self):
        self.assertIn("Type=oneshot", self.unit_text)

    def test_thematic_build_verify_cache_does_not_repeat_alphalens_arg(self):
        # The pipeline image's ENTRYPOINT is ``/app/.venv/bin/alphalens``
        # (Dockerfile.pipeline). The verify-cache ExecStartPost runs the
        # CLI directly (no ``--entrypoint`` override), so the args after
        # ``alphalens-pipeline:latest`` are forwarded to Typer verbatim.
        # Passing a literal ``alphalens`` as the first arg makes Typer
        # parse it as a subcommand and abort with
        # ``No such command 'alphalens'`` — observed on VPS 2026-05-29.
        # This regex pins the correct first token (``thematic``) and
        # fails loud if the leading ``alphalens`` ever returns.
        #
        # Multiline-anchored so a comment containing ``alphalens thematic
        # verify-cache`` cannot satisfy the assertion.
        bad_pattern = re.compile(
            r"^ExecStartPost=[^\n]*(?:\\\n[^\n]*)*"
            r"alphalens-pipeline:latest\s*\\\s*\n\s*alphalens\s+thematic\s+verify-cache",
            re.MULTILINE,
        )
        self.assertNotRegex(
            self.unit_text,
            bad_pattern,
            "verify-cache ExecStartPost must NOT repeat the `alphalens` "
            "token after the image name — the pipeline image ENTRYPOINT "
            "already IS alphalens. Use `thematic verify-cache ...` "
            "directly. See Dockerfile.pipeline:ENTRYPOINT.",
        )

        good_pattern = re.compile(
            r"^ExecStartPost=[^\n]*(?:\\\n[^\n]*)*"
            r"alphalens-pipeline:latest\s*\\\s*\n\s*thematic\s+verify-cache",
            re.MULTILINE,
        )
        self.assertRegex(
            self.unit_text,
            good_pattern,
            "Expected an ExecStartPost line invoking "
            "`<image> thematic verify-cache ...` (no leading `alphalens` "
            "arg). Found neither the broken nor the correct form — the "
            "gap-detection hook has been removed entirely.",
        )

    def test_thematic_build_service_rebuilds_briefs_cache_post_run(self):
        # After a successful pipeline run the new parquet output must be
        # synced into the Django Postgres-backed cache. The unit invokes
        # the ``rebuild-cache`` maintenance one-shot from the django-prod
        # compose stack. Regression here surfaces as "daily timer fires,
        # parquet refreshed, but the API still serves yesterday's briefs"
        # — silent until someone notices.
        #
        # Regex bound to a directive line (multiline ``^``) so the
        # assertion cannot pass on a comment mentioning ``rebuild-cache``.
        # Allows for the ``\`` line-continuation between the directive
        # and its args.
        self.assertRegex(
            self.unit_text,
            re.compile(r"^ExecStartPost=[^\n]*(?:\\\n[^\n]*)*rebuild-cache\b", re.MULTILINE),
            "Missing or malformed ExecStartPost — the Django briefs "
            "cache will not pick up the freshly written parquet from "
            "the daily pipeline run.",
        )


if __name__ == "__main__":
    unittest.main()
