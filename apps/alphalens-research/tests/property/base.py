"""Shared base for property tests: profile load AT IMPORT + float comparison.

``@given`` reuses one ``TestCase`` instance across many examples, so property
methods must be STATELESS on ``self`` (never stash generated data on the
instance). ``assert_close`` uses a MIXED relative/absolute tolerance
(``math.isclose``) -- an absolute-only epsilon is too tight for large prices and
too loose for tiny ones, and would let sign/formula mutants slip through.

THE PROFILE IS LOADED AT IMPORT, AND IT HAS TO BE. Hypothesis binds
``settings.default`` to a test when the ``@given`` decorator is APPLIED, not
when the test runs, and a class body runs at import time. This module used to
load the profile in ``setUpClass`` instead, which is strictly too late: every
property test in this package ran at Hypothesis's own default of 100 examples
and ``HYPOTHESIS_PROFILE`` did nothing. Measured before and after the move, on
one trivially-passing property:

    profile     max_examples   examples actually run
    setUpClass  300/2000/30    100 / 100 / 100
    at import   300/2000/30    300 / 2000 /  30

Any module defining property tests therefore has to import this one first,
which it already does for :class:`PropertyTestCase`. The pin lives in
``test_profile_is_in_force.py`` -- a silent return to 100 would weaken every
property in the package without failing anything.
"""

from __future__ import annotations

import math
import os
import unittest

from hypothesis import settings

from .profile import register_profiles

register_profiles()
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "ci"))


class PropertyTestCase(unittest.TestCase):
    """Base for the property suite. See the module docstring for the profile."""

    def assert_close(
        self, a: float, b: float, *, rel_tol: float = 1e-9, abs_tol: float = 1e-9
    ) -> None:
        self.assertTrue(
            math.isclose(a, b, rel_tol=rel_tol, abs_tol=abs_tol),
            msg=f"{a!r} not close to {b!r} (rel_tol={rel_tol}, abs_tol={abs_tol})",
        )
