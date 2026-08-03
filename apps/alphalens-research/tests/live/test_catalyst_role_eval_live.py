"""Live catalyst-role anchor evaluation - opt-in via CATALYST_ROLE_EVAL=1.

The hermetic ``tests/test_catalyst_role_cases.py`` proves the case set is
well-formed and that the prompt stays blind. It cannot say whether the
instrument still ANSWERS the known-answer cases correctly, because that needs
the real model. This module runs the real classifier over the frozen anchor
payloads under both framings and applies the same ``anchor_report`` gate the
sweep applies.

COSTS REAL MONEY. One DeepSeek v4-pro call per anchor case per framing (6 x 2 =
12 calls per run, plus retries on empty responses), so it is opt-in and NEVER
in the blocking PR path - the default ``unittest discover`` collects it and
``@skipUnless`` skips it, exactly like the L4 vendor probes next to it::

    CATALYST_ROLE_EVAL=1 .venv/bin/python -m unittest tests.live.test_catalyst_role_eval_live -v

Two deliberate decisions about what "the gate passes" means here:

* **Parse failures are transient, role mismatches are permanent.** An empty or
  errored response says nothing about the instrument's judgement - DeepSeek's
  JSON mode intermittently returns no choices - so it is classified transient
  and tolerated by ``run_probes`` unless it dominates. An off-taxonomy or
  unparseable body IS a contract break, and a wrong role on a known-answer case
  is the result the gate exists to surface. Both fail.

* **One pre-committed strict-framing allowance, named up front.** The strict
  rubric says in so many words that a general sector tailwind is not a channel,
  which puts every solution-provider anchor on the boundary it draws; a pilot
  run duly returned "unaffected" for Varonis on the AI-agent attack story with
  confidence 0.95. That is the instrument's known bias, recorded in the script
  docstring before this test existed, not a fresh excuse for a red run. So
  under the STRICT framing only, a solution-provider anchor answered
  "unaffected" is tolerated and reported. Every other mismatch fails, under
  either framing. Loosening this further after seeing a run would turn the gate
  into a rubber stamp - the script says as much - so it stays this narrow.

* **The surviving subset must still span the roles.** Excluding transient
  losses shrinks what the gate actually grades, and a count floor alone does
  not stop it shrinking onto one role - lose both "unaffected" anchors and
  tolerate both solution-provider ones and the gate has verified subject-adverse
  only, while the number it licenses is a count of "unaffected". So
  ``gate_shortfall`` requires both a count and a role spread.
"""

from __future__ import annotations

import os
import unittest
from collections.abc import Sequence

from scripts.classify_catalyst_roles import ANCHORS, FRAMINGS, anchor_report, classify_role

from tests.live import PermanentProbeError, TransientProbeError, run_probes
from tests.test_catalyst_role_cases import anchor_cases

_LIVE = os.environ.get("CATALYST_ROLE_EVAL") == "1"

# The single pre-committed exception, stated before any run: see the module
# docstring. Read as "under STRICT only, this expected -> got pair is the
# documented rubric boundary, not an instrument failure".
_STRICT_BOUNDARY_ALLOWANCE: tuple[tuple[str, str], ...] = (("solution-provider", "unaffected"),)

# Statuses that say nothing about the model's judgement (see the classify_role
# docstring: DeepSeek's JSON mode intermittently returns no choices).
_TRANSIENT_STATUSES = frozenset({"empty_content", "error"})


# How many distinct expected roles the surviving anchors must still cover. The
# frozen anchor set spans three (subject-adverse, solution-provider,
# unaffected) and the hermetic suite pins that, so three is "all of them", not
# an arbitrary floor.
_MIN_MEASURED_ROLES = 3


def _is_documented_strict_boundary(framing: str, expected: str, got: str) -> bool:
    return framing == "strict" and (expected, got) in _STRICT_BOUNDARY_ALLOWANCE


def gate_shortfall(measured: Sequence[dict], total: int) -> str | None:
    """Why the measured anchor subset is too weak to gate on, or None if it holds.

    Two ways a subset stops being a gate. Too FEW anchors survived, so almost
    nothing was checked. Or enough survived but they no longer span the roles -
    losing both "unaffected" anchors to transients, with both solution-provider
    anchors answered "unaffected" and tolerated under the strict rubric, leaves
    a gate that only ever verified subject-adverse while the aggregate it
    licenses is a count of "unaffected".
    """
    if len(measured) < total // 2 + 1:
        return f"only {len(measured)}/{total} anchors were labelled"
    roles = sorted({anchor["expected_role"] for anchor in measured})
    if len(roles) < _MIN_MEASURED_ROLES:
        return (
            f"the {len(measured)} labelled anchors cover only {len(roles)} expected "
            f"role(s) {roles}, fewer than the {_MIN_MEASURED_ROLES} the aggregate rests on"
        )
    return None


@unittest.skipUnless(_LIVE, "set CATALYST_ROLE_EVAL=1 to run the live catalyst-role evaluation")
class TestCatalystRoleAnchorsLive(unittest.TestCase):
    def test_anchor_gate_passes_under_both_framings(self):
        from alphalens_pipeline.data.alt_data.openrouter_client import (
            get_default_openrouter_client,
        )

        cases = anchor_cases()
        self.assertTrue(cases, "no anchor cases in the frozen case set")

        client = get_default_openrouter_client()
        labelled: dict[str, list[dict]] = {framing: [] for framing in FRAMINGS}

        def _make(case: dict, framing: str):
            def _probe() -> None:
                result = classify_role(case["event"], client, framing=framing)
                status = result["parse_status"]
                if status in _TRANSIENT_STATUSES:
                    raise TransientProbeError(f"{case['case_id']} [{framing}]: {status}")
                if status != "ok":
                    raise PermanentProbeError(
                        f"{case['case_id']} [{framing}]: {status} (role={result['role']!r})"
                    )
                labelled[framing].append(
                    {
                        "ticker": case["event"]["ticker"],
                        "brief_date": case["event"]["brief_date"],
                        "role": result["role"],
                        "channel": result["channel"],
                    }
                )

            return _probe

        probes = {
            f"{case['case_id']}/{framing}": _make(case, framing)
            for framing in FRAMINGS
            for case in cases
        }
        run_probes(self, probes, label="catalyst-role")

        for framing in FRAMINGS:
            with self.subTest(framing=framing):
                self._assert_gate(framing, labelled[framing])

    def _assert_gate(self, framing: str, rows: list[dict]) -> None:
        """Apply ``anchor_report`` to the anchors that actually got a label.

        Anchors lost to a tolerated transient failure are excluded rather than
        counted as MISSING - otherwise one rate-limited call would fail the gate
        for a reason that has nothing to do with the instrument. The
        ``gate_shortfall`` check below stops that exclusion from hollowing out
        the gate, on count and on role coverage.
        """
        measured_keys = {(row["ticker"], row["brief_date"]) for row in rows}
        measured = [a for a in ANCHORS if (a["ticker"], a["brief_date"]) in measured_keys]
        shortfall = gate_shortfall(measured, len(ANCHORS))
        self.assertIsNone(
            shortfall,
            f"{shortfall} under {framing} - too weak to gate on; re-run after a cool-down",
        )

        report = anchor_report(rows, measured)
        print(
            f"\nANCHOR GATE [{framing}]: {'PASS' if report['passed'] else 'FAIL'} "
            f"({report['n_anchors']} anchors measured)"
        )
        for row in rows:
            print(f"  {row['ticker']} {row['brief_date']}: {row['role']} - {row['channel']}")

        tolerated, failures = [], []
        for miss in report["mismatches"]:
            target = (
                tolerated
                if _is_documented_strict_boundary(framing, miss["expected_role"], miss["got"])
                else failures
            )
            target.append(miss)

        for miss in tolerated:
            print(
                f"  TOLERATED {miss['ticker']} {miss['brief_date']}: expected "
                f"{miss['expected_role']}, got {miss['got']} (documented strict-rubric boundary)"
            )

        self.assertEqual(
            failures,
            [],
            f"anchor gate failed under {framing} on cases outside the documented "
            f"strict-rubric boundary: {failures}",
        )


if __name__ == "__main__":
    unittest.main()
