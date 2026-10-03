"""Tests for the broker-agnostic execution layer (``alphalens_pipeline.brokers``).

Importing this package installs the operator-state guard: no test in it may
resolve the real ``~/.alphalens`` tree. Without the guard a test silently reads
the journals the live SIM daemon is writing, and its verdict depends on the
time of day -- 49 tests behaved that way until #1696.
"""

from tests.brokers.automanager.home_isolation import install_operator_state_guard

install_operator_state_guard()
