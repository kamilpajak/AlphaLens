"""ADR 0013 enforcement: trade-side layer contracts (T1-T8).

Three machine-checkable rules from the ADR's action item 2:
- dependency direction: T2/T3 code (thematic mapping/screening) never imports
  T5/T6/T7/T8 code (thematic.trade_setup, feedback) — R2's import-level twin;
- every layer's poolability/config-version key exists and is non-empty (R3);
- the what-if lens registry honors its contract: valid status values and the
  concurrent-lens cap (R4).
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

_PIPELINE_ROOT = Path(__file__).resolve().parents[2] / "alphalens-pipeline" / "alphalens_pipeline"
_FORBIDDEN_PREFIXES = (
    "alphalens_pipeline.thematic.trade_setup",
    "alphalens_pipeline.feedback",
)


def _imports_of(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.append(node.module)
    return names


class TestSelectionNeverImportsSetupOrFeedback(unittest.TestCase):
    """R2 (import-level): SETUP/replay outputs never feed SELECTION/ORDERING."""

    def test_mapping_and_screening_are_clean(self):
        offenders: list[str] = []
        for pkg in ("thematic/mapping", "thematic/screening"):
            for path in sorted((_PIPELINE_ROOT / pkg).rglob("*.py")):
                for name in _imports_of(path):
                    if name.startswith(_FORBIDDEN_PREFIXES):
                        offenders.append(f"{path.relative_to(_PIPELINE_ROOT)} -> {name}")
        self.assertEqual(offenders, [])

    def test_positive_control_walker_sees_imports(self):
        # The AST walk must not rot to an empty scan: builder.py legitimately
        # imports from its own package and the walker must see SOME import.
        sample = _PIPELINE_ROOT / "thematic" / "trade_setup" / "builder.py"
        self.assertTrue(any("trade_setup" in n for n in _imports_of(sample)))


class TestLayerVersionKeysExist(unittest.TestCase):
    """R3: each layer's key constant exists, is a non-empty string."""

    def test_all_named_keys(self):
        from alphalens_pipeline.feedback.ladder_config import ladder_config_version
        from alphalens_pipeline.market.market_state import MARKET_STATE_CONFIG_VERSION
        from alphalens_pipeline.thematic.options_telemetry.features import (
            OPTIONS_CONFIG_VERSION,
        )
        from alphalens_pipeline.thematic.screening.insider_signal import (
            INSIDER_SIGNAL_VERSION,
        )
        from alphalens_pipeline.thematic.screening.selection_score import (
            SCORER_CONFIG_VERSION,
        )
        from alphalens_pipeline.thematic.trade_setup.config_version import (
            setup_builder_config_version,
        )

        for key in (
            SCORER_CONFIG_VERSION,
            MARKET_STATE_CONFIG_VERSION,
            OPTIONS_CONFIG_VERSION,
            INSIDER_SIGNAL_VERSION,
            ladder_config_version(order_ttl_days=7),
            setup_builder_config_version(),
        ):
            self.assertIsInstance(key, str)
            self.assertTrue(key)


class TestLensRegistryContract(unittest.TestCase):
    """R4: display-only lenses — valid statuses, bounded registry."""

    def test_statuses_valid_and_registry_bounded(self):
        from alphalens_pipeline.feedback.breakeven_lenses import (
            BREAKEVEN_LENSES,
            MAX_REGISTERED_LENSES,
        )

        self.assertLessEqual(len(BREAKEVEN_LENSES), MAX_REGISTERED_LENSES)
        for lens in BREAKEVEN_LENSES:
            self.assertIn(lens.status, {"in_sample", "validated"})
            self.assertTrue(lens.lens_id)


if __name__ == "__main__":
    unittest.main()
