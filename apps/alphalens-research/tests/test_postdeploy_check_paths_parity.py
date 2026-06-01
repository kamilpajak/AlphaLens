"""Pin the django-build-trigger path list shared by two files that drift apart.

``deploy/scripts/postdeploy_check.sh`` computes the EXPECTED running-image
commit as the latest ``origin/main`` commit touching the paths that trigger a
Django image rebuild. That trigger set is defined authoritatively in
``.github/workflows/django-image.yml`` (``on.push.paths``). If the two drift,
the postdeploy check resolves the WRONG expected commit and silently
false-passes or false-fails (gotcha #1 of the Phase 1a-ii recon). This test
asserts the script's ``DJANGO_TRIGGER_PATHS`` array equals the workflow's
``on.push.paths`` (modulo the ``/**`` glob suffix, which git pathspecs imply).

Positive control: a fabricated extra path on one side must be detected — so the
parity check can never rot to a no-op.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "deploy" / "scripts" / "postdeploy_check.sh"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "django-image.yml"

_ARRAY_RE = re.compile(r"DJANGO_TRIGGER_PATHS=\((.*?)\)", re.DOTALL)
_ENTRY_RE = re.compile(r'"([^"]+)"')


def _script_paths() -> set[str]:
    """Quoted entries of the DJANGO_TRIGGER_PATHS=( ... ) array in the script."""
    m = _ARRAY_RE.search(SCRIPT.read_text())
    if m is None:
        raise AssertionError(
            "DJANGO_TRIGGER_PATHS=( ... ) array not found in postdeploy_check.sh "
            "— the array shape changed; update _ARRAY_RE."
        )
    return set(_ENTRY_RE.findall(m.group(1)))


def _workflow_trigger_paths() -> set[str]:
    """``on.push.paths`` from django-image.yml, with the ``/**`` glob stripped.

    PyYAML parses the reserved word ``on:`` as the boolean key ``True``
    (YAML 1.1), so accept either key.
    """
    doc = yaml.safe_load(WORKFLOW.read_text())
    on = doc.get("on", doc.get(True))
    paths = on["push"]["paths"]
    return {p[:-3] if p.endswith("/**") else p for p in paths}


class TestPostdeployCheckPathsParity(unittest.TestCase):
    def test_script_array_parses_nonempty(self) -> None:
        # Anti-rot: an empty parse would make the equality below vacuously pass.
        self.assertGreater(len(_script_paths()), 0, "parsed no paths from the script")

    def test_workflow_paths_parse_nonempty(self) -> None:
        self.assertGreater(
            len(_workflow_trigger_paths()), 0, "parsed no on.push.paths from django-image.yml"
        )

    def test_script_trigger_paths_match_workflow(self) -> None:
        script = _script_paths()
        workflow = _workflow_trigger_paths()
        self.assertEqual(
            script,
            workflow,
            "postdeploy_check.sh DJANGO_TRIGGER_PATHS must equal django-image.yml "
            f"on.push.paths (glob-stripped). Only in script: {sorted(script - workflow)}; "
            f"only in workflow: {sorted(workflow - script)}. A mismatch makes the "
            "postdeploy image-drift check resolve the wrong expected commit.",
        )

    def test_positive_control_extra_path_is_detected(self) -> None:
        # A fabricated divergence MUST be flagged, or the parity check is a no-op.
        script = _script_paths() | {"deploy/ghost/path"}
        self.assertNotEqual(
            script, _workflow_trigger_paths(), "positive control: divergence not detected"
        )


if __name__ == "__main__":
    unittest.main()
