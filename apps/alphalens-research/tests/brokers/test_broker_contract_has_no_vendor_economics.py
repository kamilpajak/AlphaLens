"""``broker_contract`` must not define one broker's fee schedule (issue #1122).

The package states its own rule in ``fx.py``: "the ADAPTER reports, never the
contract decides." An earlier revision of #1116 moved the Saxo LIVE Polish fee
card into ``broker_contract.costs``, where a second adapter would have
inherited it silently — not with an error, but with confidently wrong
thresholds on gates that refuse real orders. The decision on #1122 (2026-08-25)
was to keep those numbers on the pipeline side, next to the three modules that
actually consume them.

DELIBERATELY NARROW. This does NOT try to be a general "no vendor facts in the
contract" lint: such a test has to tell a documented provenance note (the
package legitimately carries comments like ``source: "saxo-live-l1"`` and a
latency threshold measured on the Saxo stream) apart from a decision the
contract is making, and getting that wrong makes it either useless or
unbearable. It pins exactly the constants this decision moved, by name.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
BROKER_CONTRACT = WORKSPACE_ROOT / "alphalens-broker-contract" / "broker_contract"
PIPELINE_COSTS = (
    WORKSPACE_ROOT
    / "alphalens-pipeline"
    / "alphalens_pipeline"
    / "brokers"
    / "automanager"
    / "costs.py"
)

# The vendor-economics constants moved off the contract side by #1122, plus the
# declared strategy parameter that rode along with them. Named individually
# rather than matched by pattern: a pattern would drift into the provenance
# comments this test is explicitly not policing.
VENDOR_ECONOMICS_NAMES = frozenset(
    {
        "MIN_COMMISSION_USD",
        "COMMISSION_RATE",
        "FX_ROUND_TRIP_RATE",
        "COST_GATE_FX_APPLIES",
        "COST_GATE_MIN_COMMISSION_APPLIES",
        "EXIT_EDGE_MIN_BPS",
    }
)


def _module_level_assignments(path: Path) -> set[str]:
    """Every name bound at module level in ``path`` by a plain or annotated
    assignment. Parsed rather than grepped, so a mention inside a docstring or
    a comment is not mistaken for a definition."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


class TestBrokerContractHasNoVendorEconomics(unittest.TestCase):
    def test_scanner_finds_the_constants_where_they_do_live(self) -> None:
        # POSITIVE CONTROL. Without this, a broken path or a parser change
        # would make the real assertion below pass over an empty scan and
        # report success forever.
        self.assertTrue(PIPELINE_COSTS.is_file(), f"missing {PIPELINE_COSTS}")
        defined = _module_level_assignments(PIPELINE_COSTS)
        self.assertEqual(
            VENDOR_ECONOMICS_NAMES & defined,
            VENDOR_ECONOMICS_NAMES,
            "the pipeline-side cost model should define every moved constant",
        )

    def test_no_vendor_economics_constant_is_defined_in_broker_contract(self) -> None:
        self.assertTrue(BROKER_CONTRACT.is_dir(), f"missing {BROKER_CONTRACT}")
        offenders: list[str] = []
        for path in sorted(BROKER_CONTRACT.rglob("*.py")):
            for name in sorted(VENDOR_ECONOMICS_NAMES & _module_level_assignments(path)):
                offenders.append(f"{path.relative_to(WORKSPACE_ROOT)}: {name}")
        self.assertEqual(
            offenders,
            [],
            "one broker's fee schedule is back in the shared contract layer "
            "(issue #1122) — it belongs beside the adapter that reports it",
        )


if __name__ == "__main__":
    unittest.main()
