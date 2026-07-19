"""Shared base for property tests: explicit profile load + float comparison.

``@given`` reuses one ``TestCase`` instance across many examples, so property
methods must be STATELESS on ``self`` (never stash generated data on the
instance). ``assert_close`` uses a MIXED relative/absolute tolerance
(``math.isclose``) — an absolute-only epsilon is too tight for large prices and
too loose for tiny ones, and would let sign/formula mutants slip through.
"""

from __future__ import annotations

import math
import os
import unittest

from hypothesis import settings

from .profile import register_profiles


class PropertyTestCase(unittest.TestCase):
    """Base that registers + loads the selected hypothesis profile once per class."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        register_profiles()  # explicit — no import-time side effect
        settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "ci"))

    def assert_close(
        self, a: float, b: float, *, rel_tol: float = 1e-9, abs_tol: float = 1e-9
    ) -> None:
        self.assertTrue(
            math.isclose(a, b, rel_tol=rel_tol, abs_tol=abs_tol),
            msg=f"{a!r} not close to {b!r} (rel_tol={rel_tol}, abs_tol={abs_tol})",
        )
