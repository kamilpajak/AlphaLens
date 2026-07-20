"""Drift test for ``execution_config_version()`` (ADR 0013 R3 / ADR 0014 P2).

Mirrors ``test_catalyst_config_version``-style pins:

- deterministic + non-empty; shape ``execution-v2-<12 hex>`` (v2 = the
  FX-leg journal-shape bump);
- the token DRIFTS when any covered policy constant changes
  (``mock.patch.object`` + ``subTest`` over the full covered list);
- a module-level policy constant cannot be added without joining the token
  (namespace sweep vs the covered list).
"""

from __future__ import annotations

import re
import unittest
from unittest import mock

from alphalens_pipeline.brokers import execution

# Every module-level policy constant the token must cover. A new _UPPER_CASE
# constant in execution.py must be added HERE and to the canonical dict in
# execution_config_version() — the namespace sweep below fails otherwise.
_COVERED_CONSTANTS = (
    "_DECOMPOSITION_MODE",
    "_TP_ASSIGNMENT_POLICY",
    "_ZERO_QTY_TIER_POLICY",
    "_EXCESS_TRANCHE_POLICY",
    "_STOP_ORDER_TYPE",
    "_MAX_CHILD_DISTANCE_FRAC",
    "_EXIT_DURATION",
    "_ENTRY_DURATION",
    "_TTL_ZERO_SENTINEL_DAYS",
    "_PRECHECK_REQUIRED",
    "_MANUAL_ORDER",
    "_TICK_QUANTIZE_POLICY",
    "_MAX_TICK_ADJUSTMENT_BPS",
    "_MISSING_FX_RATE_POLICY",
    "_FX_RATE_MAX_AGE_S",
    "_FX_ACCEPTED_PRICE_TYPES",
    "_FX_RATE_SOURCE",
    "_FX_CONVERSION_POINT",
    "_FX_PRECHECK_RATE_DIVERGENCE_MAX_PCT",
    "_FX_SIZING_BUFFER_PCT",
)


def _drifted_value(current: object) -> object:
    """A same-type value guaranteed different from ``current``."""
    if isinstance(current, bool):
        return not current
    if isinstance(current, int | float):
        return current + 1
    if isinstance(current, tuple):
        return (*current, "drifted")
    return f"{current}-drifted"


class TestExecutionConfigVersion(unittest.TestCase):
    def test_deterministic_and_non_empty(self):
        first = execution.execution_config_version()
        second = execution.execution_config_version()
        self.assertTrue(first)
        self.assertEqual(first, second)

    def test_token_shape_prefix_and_12_hex_digest(self):
        # v2 = the FX-leg journal-shape bump (fx provenance keys ADDED).
        token = execution.execution_config_version()
        self.assertRegex(token, r"^execution-v2-[0-9a-f]{12}$")

    def test_token_changes_on_every_covered_constant(self):
        baseline = execution.execution_config_version()
        for attr in _COVERED_CONSTANTS:
            with self.subTest(constant=attr):
                current = getattr(execution, attr)
                with mock.patch.object(execution, attr, _drifted_value(current)):
                    self.assertNotEqual(
                        execution.execution_config_version(),
                        baseline,
                        f"changing {attr} must drift the execution_config_version token",
                    )
        self.assertEqual(
            execution.execution_config_version(),
            baseline,
            "token must return to baseline after the patches unwind",
        )

    def test_stamp_schema_bumps_only_shape_never_values(self):
        with mock.patch.object(execution, "_STAMP_SCHEMA", "3"):
            token = execution.execution_config_version()
        self.assertTrue(token.startswith("execution-v3-"))

    def test_no_uncovered_policy_constant_in_module_namespace(self):
        """A new ``_UPPER_CASE`` module constant must join the token.

        ``_STAMP_SCHEMA`` is the schema key itself (covered separately by the
        shape test above); everything else matching the policy-constant naming
        convention must be in ``_COVERED_CONSTANTS``.
        """
        pattern = re.compile(r"^_[A-Z][A-Z0-9_]*$")
        policy_names = {
            name
            for name, value in vars(execution).items()
            if pattern.match(name) and isinstance(value, str | int | float | bool | tuple)
        }
        uncovered = policy_names - set(_COVERED_CONSTANTS) - {"_STAMP_SCHEMA"}
        self.assertEqual(
            uncovered,
            set(),
            "policy constants missing from execution_config_version coverage: "
            f"{sorted(uncovered)} — add them to the canonical dict AND to "
            "_COVERED_CONSTANTS in this test",
        )


if __name__ == "__main__":
    unittest.main()
