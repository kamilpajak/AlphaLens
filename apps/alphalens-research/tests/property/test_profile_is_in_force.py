"""The profile has to actually reach the tests, and nothing else says so.

A property suite that quietly runs 100 examples instead of 300 stays green,
reports the same test count, and finds less. That is exactly what happened here:
the profile was loaded in ``setUpClass``, while Hypothesis binds
``settings.default`` when ``@given`` is APPLIED -- at import, before any
``setUpClass`` runs. Nothing failed, so nothing said so for as long as it lasted.

So this counts. The probe below is decorated AT IMPORT, which is the moment the
binding happens, and a test then runs it and compares the number of examples it
saw against the number the loaded profile asks for.
"""

from __future__ import annotations

import unittest

from hypothesis import given, settings
from hypothesis import strategies as st

from .base import PropertyTestCase

# Hypothesis's own default when no profile is in force. The failure this module
# exists to catch is the suite silently falling back to it.
HYPOTHESIS_LIBRARY_DEFAULT_EXAMPLES = 100

_EXAMPLES_SEEN = {"n": 0}


class TheLoadedProfileReachesTheTests(PropertyTestCase):
    @given(x=st.floats(min_value=0.0, max_value=1e9))
    def a_counting_probe(self, x: float) -> None:
        """Deliberately NOT named ``test_*``: the loader must not collect it, and
        the test below drives it directly. A float range is used rather than a
        small integer one so the engine cannot exhaust the domain and stop early.
        """
        _EXAMPLES_SEEN["n"] += 1

    def test_the_suite_runs_as_many_examples_as_the_profile_declares(self) -> None:
        declared = settings.default.max_examples
        _EXAMPLES_SEEN["n"] = 0
        self.a_counting_probe()
        self.assertGreaterEqual(
            _EXAMPLES_SEEN["n"],
            declared,
            "the loaded profile is not reaching @given: it asks for "
            f"{declared} examples and the probe saw {_EXAMPLES_SEEN['n']}. "
            f"Hypothesis's own default is {HYPOTHESIS_LIBRARY_DEFAULT_EXAMPLES}; "
            "seeing that number means the profile is being loaded after the "
            "decorators are applied. Load it at import in `base.py`.",
        )

    def test_the_profile_asks_for_more_than_the_library_default(self) -> None:
        # Anti-tautology: the comparison above proves nothing if the profile
        # happens to declare the same number the library would have used.
        # `mutation` deliberately declares fewer, and is exempt.
        if settings.default.max_examples < HYPOTHESIS_LIBRARY_DEFAULT_EXAMPLES:
            self.skipTest("the mutation profile runs fewer examples on purpose")
        self.assertGreater(settings.default.max_examples, HYPOTHESIS_LIBRARY_DEFAULT_EXAMPLES)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
