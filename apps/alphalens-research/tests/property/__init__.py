"""Property-based tests (hypothesis) for AlphaLens' money-relevant pure core.

Complements the mutation-testing work (#852-#856): mutation shows WHERE tests are
weak; these property tests KILL those weaknesses by asserting invariants over
generated inputs. Everything here is ``unittest.TestCase`` + ``@given`` so it is
collected by the repo's ``unittest discover`` CI (pytest-style is silently
skipped). Shared strategies live in ``strategies.py``; the CI/dev/mutation
settings profiles live in ``profile.py`` and are loaded explicitly by the
``PropertyTestCase`` base in ``base.py``.
"""
