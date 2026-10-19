"""Tests for the account cash book (``alphalens_pipeline.account_cash``, #1689).

Importing this package installs the operator-state guard: no test in it may
resolve the real ``~/.alphalens`` tree. The guard installed by
``tests/brokers/__init__.py`` does NOT reach a new test package -- it runs on
that package's import, and nothing imports it from here -- so this package
installs its own. Without it a test would read, and the write-side tests would
APPEND TO, the operator's own money store: 109 broker tests read the real tree
until #1696 fixed exactly this shape one package over.

The guard covers ``account_cash.paths._alphalens_home`` because
``home_isolation._GUARDED_BUILDERS`` names it, and
``test_home_isolation_gate.py`` reads that set off the source of the guarded
packages, so a second home builder in this package cannot stay unguarded
quietly.

Installed at package import rather than from a ``load_tests`` hook: ``load_tests``
runs under DISCOVERY only, so ``python -m unittest tests.account_cash.test_store``
-- how a single module is normally run -- would carry no guard at all.
"""

from tests.brokers.automanager.home_isolation import install_operator_state_guard

install_operator_state_guard()
