"""Hypothesis settings profiles for the property suite.

Both registration AND load are EXPLICIT (no import-time side effect): call
:func:`register_profiles` then ``settings.load_profile(...)`` — done once by
``PropertyTestCase.setUpClass``. Profiles selected via ``HYPOTHESIS_PROFILE``
(default ``ci``):

* ``ci`` — explores (``derandomize=False``) and persists found counterexamples to
  the default on-disk database, so a failing example replays first on the next
  run. ``deadline=None`` avoids timing flakes on shared CI runners.
* ``dev`` — many more examples for local hunting.
* ``mutation`` — few examples + no DB, so a cosmic-ray run (hundreds of test
  re-executions) stays tractable; we lean on the STRENGTH of the properties, not
  the example count, to kill mutants.
"""

from __future__ import annotations

from hypothesis import HealthCheck, settings


def register_profiles() -> None:
    """Register the ci/dev/mutation profiles (idempotent)."""
    settings.register_profile(
        "ci",
        max_examples=300,
        deadline=None,
        derandomize=False,
        suppress_health_check=[HealthCheck.too_slow],
    )
    settings.register_profile("dev", max_examples=2000, deadline=None)
    settings.register_profile("mutation", max_examples=30, deadline=None, database=None)
