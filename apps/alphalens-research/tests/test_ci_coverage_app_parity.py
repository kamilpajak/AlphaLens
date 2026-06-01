"""Pin the CI Django-coverage parity incident (PR #292, 2026-06-01).

Why this exists:
  The Django CI job measures branch coverage with an explicit
  ``coverage run --source=<app1>,<app2>,...`` allow-list (see
  ``.github/workflows/ci.yml``, the ``django`` job). ``coverage``
  only instruments modules under the ``--source`` dirs; anything
  outside that list is silently ignored, so its lines never count
  toward the denominator.

  PR #292 (feedback ledger v1) shipped a NEW Django app
  (``feedback``). If the ``--source`` list is not updated when an app
  lands, SonarCloud sees "0% coverage on new code" — a FALSE GREEN:
  pytest exercises every line, but ``coverage`` never watched the
  app, so the diff-coverage gate has nothing to fail on. The
  CLAUDE.md / MEMORY note records this exact trap:
  "new Django apps MUST join ci.yml Django coverage --source= list".

  The defensible invariant: every Django app that ships an
  ``apps.py`` (the AppConfig marker Django uses to register an app)
  MUST appear in the ``--source`` list of the django coverage job.

Scope decisions (why this is parity, not equality):
  - ``config`` legitimately appears in ``--source`` even though it has
    NO ``apps.py`` — it is the settings package (``config/settings/``,
    ``config/urls.py``, ``config/{asgi,wsgi}.py``), not a Django app.
    Coverage of the settings/url wiring is desirable, so we do NOT
    flag ``--source`` entries that lack an ``apps.py``. The invariant
    is one-directional: apps.py ⇒ in --source, NOT in --source ⇒
    apps.py.
  - We discover apps from the real filesystem glob
    (``apps/alphalens-django/*/apps.py``) so a future app addition is
    picked up automatically without editing this test.

Positive control:
  ``test_positive_control_ghost_app_would_fail`` feeds a fabricated
  app (``ghost_app``) that is NOT in the real ``--source`` list and
  asserts the parity check FAILS on it — so the assertion can never
  rot into a silent no-op.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

# tests/<name>.py -> apps/alphalens-research/tests; repo root is 3 up.
REPO_ROOT = Path(__file__).resolve().parents[3]
DJANGO_ROOT = REPO_ROOT / "apps" / "alphalens-django"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

# Matches the ``--source=a,b,c`` token inside the django coverage step.
# Captures the comma-separated app list (no spaces in the value as
# authored; we strip defensively anyway).
_SOURCE_RE = re.compile(r"coverage\s+run\s+--source=([A-Za-z0-9_,]+)")


def _django_apps_with_apps_py() -> set[str]:
    """Return the set of Django app names that ship an ``apps.py``.

    The app name is the directory name containing ``apps.py`` — that
    is the label Django registers it under (and the label
    ``coverage --source`` must match, since the source dir name equals
    the importable top-level package name).
    """
    return {p.parent.name for p in DJANGO_ROOT.glob("*/apps.py")}


def _coverage_source_apps() -> list[str]:
    """Extract the ``--source=`` app list from the django CI job.

    Returns the list as-authored (order preserved) so the duplicate
    check below sees raw entries.
    """
    text = CI_WORKFLOW.read_text()
    match = _SOURCE_RE.search(text)
    if match is None:
        raise AssertionError(
            "Could not find a `coverage run --source=<apps>` invocation in "
            f"{CI_WORKFLOW.relative_to(REPO_ROOT)}. The django coverage job's "
            "scoping flag changed shape — update _SOURCE_RE in this test."
        )
    return [tok.strip() for tok in match.group(1).split(",") if tok.strip()]


def _check_apps_in_source(apps: set[str], source_list: list[str]) -> set[str]:
    """Pure helper: return apps that are missing from the source list.

    Extracted so the positive control can drive it with a fabricated
    app set without re-reading any file.
    """
    return apps - set(source_list)


class TestCiCoverageAppParity(unittest.TestCase):
    """Every Django app with an apps.py must be in the CI --source list."""

    def test_source_list_is_parseable_and_nonempty(self) -> None:
        # Guards the regex itself: if the workflow restructures the step
        # so the pattern no longer matches, we want a loud failure here
        # rather than a silent empty list that makes the parity check
        # vacuously pass.
        source_list = _coverage_source_apps()
        self.assertGreater(
            len(source_list),
            0,
            "Parsed an empty --source list from ci.yml — regex likely stale.",
        )

    def test_every_django_app_has_apps_py_discovered(self) -> None:
        # Sanity check the glob still finds apps; an empty set would make
        # the parity assertion below vacuously pass.
        apps = _django_apps_with_apps_py()
        self.assertGreater(
            len(apps),
            0,
            f"No */apps.py found under {DJANGO_ROOT.relative_to(REPO_ROOT)} — "
            "glob is stale or the Django layout moved.",
        )

    def test_every_app_with_apps_py_in_coverage_source(self) -> None:
        # THE invariant: an app coverage forgets to scope reports 0% on
        # new code (false green) — the PR #292 trap.
        apps = _django_apps_with_apps_py()
        source_list = _coverage_source_apps()
        missing = _check_apps_in_source(apps, source_list)
        self.assertEqual(
            missing,
            set(),
            f"Django app(s) missing from the CI coverage --source list: "
            f"{sorted(missing)}. Add them to "
            f"`coverage run --source=...` in the django job of "
            f".github/workflows/ci.yml, or SonarCloud will report 0% "
            f"coverage on their new code (false green). Discovered apps: "
            f"{sorted(apps)}; --source: {source_list}.",
        )

    def test_source_list_has_no_duplicates(self) -> None:
        # A duplicate entry is harmless to coverage but signals a sloppy
        # merge (two PRs both appended the same app). Cheap to pin.
        source_list = _coverage_source_apps()
        dupes = {a for a in source_list if source_list.count(a) > 1}
        self.assertEqual(
            dupes,
            set(),
            f"Duplicate entries in coverage --source list: {sorted(dupes)}.",
        )

    def test_positive_control_ghost_app_would_fail(self) -> None:
        # MANDATORY positive control: a fabricated app not present in the
        # real --source list MUST be reported missing. If this ever
        # passes, the parity helper has rotted into a no-op.
        source_list = _coverage_source_apps()
        self.assertNotIn(
            "ghost_app",
            source_list,
            "Fixture invalid: a real app is literally named 'ghost_app'.",
        )
        fabricated_apps = set(source_list) | {"ghost_app"}
        missing = _check_apps_in_source(fabricated_apps, source_list)
        self.assertEqual(
            missing,
            {"ghost_app"},
            "Positive control failed: the parity check did not flag a "
            "fabricated app missing from --source — the assertion is a no-op.",
        )


if __name__ == "__main__":
    unittest.main()
