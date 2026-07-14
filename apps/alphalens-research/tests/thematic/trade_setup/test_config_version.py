"""ADR 0013 action item 1: the setup-builder geometry poolability key.

The token must cover ALL ``trade_setup/`` geometry constants (builder + ladder
+ levels + sizing) — a builder-only token would let a TP-R-multiple or spacing
change pool silently, the exact failure class the key exists to prevent.
"""

from __future__ import annotations

import json
import unittest
from unittest import mock

import numpy as np
import pandas as pd
from alphalens_pipeline.thematic.trade_setup import builder as builder_mod
from alphalens_pipeline.thematic.trade_setup.builder import build_trade_setup_from_frame
from alphalens_pipeline.thematic.trade_setup.config_version import (
    setup_builder_config_version,
)
from alphalens_pipeline.thematic.trade_setup.model import SCHEMA_VERSION, TradeSetup


def _ohlcv_frame(n: int = 120) -> pd.DataFrame:
    rng = np.random.default_rng(0)
    close = 100.0 + np.cumsum(rng.normal(0, 1.0, n))
    return pd.DataFrame(
        {
            "open": close - 0.2,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": np.full(n, 1_000_000.0),
        }
    )


class TestSetupBuilderConfigVersion(unittest.TestCase):
    def test_token_is_canonical_json_and_deterministic(self):
        t1, t2 = setup_builder_config_version(), setup_builder_config_version()
        self.assertEqual(t1, t2)
        parsed = json.loads(t1)
        self.assertIn("schema", parsed)

    def test_token_covers_all_four_geometry_modules(self):
        parsed = json.loads(setup_builder_config_version())
        # One representative constant per module; values pinned to current code
        # so a silent constant edit fails HERE as well as via token drift.
        self.assertEqual(parsed["swing_threshold_mult"], 2.5)  # builder.py
        self.assertEqual(parsed["r_multiple_fallback"], [2.0, 3.0, 4.0])  # ladder.py
        self.assertEqual(parsed["cluster_radius_mult"], 0.5)  # levels.py
        self.assertEqual(parsed["max_exposure_pct"], 25.0)  # sizing.py
        self.assertEqual(parsed["min_bars"], 30)
        self.assertEqual(parsed["disaster_floor_frac"], 0.75)

    def test_token_changes_when_a_constant_changes(self):
        base = setup_builder_config_version()
        with mock.patch.object(builder_mod, "_SWING_THRESHOLD_MULT", 3.0):
            self.assertNotEqual(setup_builder_config_version(), base)

    def test_schema_version_bumped_for_the_new_field(self):
        self.assertEqual(SCHEMA_VERSION, "1.1.0")

    def test_no_structure_setup_carries_the_token(self):
        setup = TradeSetup.no_structure(asof_close=10.0, atr=1.0, order_ttl_days=7)
        d = setup.to_dict()
        self.assertEqual(d["builder_config_version"], setup_builder_config_version())
        self.assertEqual(d["schema_version"], "1.1.0")

    def test_built_setup_carries_the_token(self):
        setup = build_trade_setup_from_frame(_ohlcv_frame())
        d = setup.to_dict()
        self.assertEqual(d["builder_config_version"], setup_builder_config_version())


if __name__ == "__main__":
    unittest.main()
