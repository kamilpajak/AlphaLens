import unittest

from alphalens_pipeline.thematic.trade_setup.model import (
    EntryTier,
    TpTranche,
    TradeSetup,
    entry_tier_label,
    tp_tranche_label,
)


def _setup_with_three_tiers() -> TradeSetup:
    entry_tiers = tuple(
        EntryTier(
            limit=100.0 - i, alloc_pct=40.0 - 5.0 * i, atr_distance=0.5 * (i + 1), tag=f"e{i}"
        )
        for i in range(3)
    )
    tp_tranches = tuple(
        TpTranche(target=110.0 + i, tranche_pct=40.0 - 5.0 * i, r_multiple=2.0 + i, tag=f"t{i}")
        for i in range(3)
    )
    return TradeSetup(
        schema_version="1.1.0",
        status="OK",
        asof_close=100.0,
        atr=5.0,
        disaster_stop=80.0,
        suggested_size_pct=10.0,
        order_ttl_days=7,
        entry_tiers=entry_tiers,
        tp_tranches=tp_tranches,
        builder_config_version="tok",
    )


class TestLabelHelpers(unittest.TestCase):
    def test_entry_tier_label_is_one_based_e_prefixed(self):
        self.assertEqual([entry_tier_label(i) for i in range(3)], ["E1", "E2", "E3"])

    def test_tp_tranche_label_is_one_based_tp_prefixed(self):
        self.assertEqual([tp_tranche_label(i) for i in range(3)], ["TP1", "TP2", "TP3"])


class TestToDictOrdinalLabels(unittest.TestCase):
    def test_entry_tiers_carry_ordinal_labels_in_order(self):
        d = _setup_with_three_tiers().to_dict()
        self.assertEqual([t["label"] for t in d["entry_tiers"]], ["E1", "E2", "E3"])

    def test_tp_tranches_carry_ordinal_labels_in_order(self):
        d = _setup_with_three_tiers().to_dict()
        self.assertEqual([t["label"] for t in d["tp_tranches"]], ["TP1", "TP2", "TP3"])

    def test_label_is_purely_additive_existing_entry_keys_unchanged(self):
        d = _setup_with_three_tiers().to_dict()
        tier0 = d["entry_tiers"][0]
        self.assertEqual(
            {k: v for k, v in tier0.items() if k != "label"},
            {"limit": 100.0, "alloc_pct": 40.0, "atr_distance": 0.5, "tag": "e0"},
        )

    def test_label_is_purely_additive_existing_tp_keys_unchanged(self):
        d = _setup_with_three_tiers().to_dict()
        tp0 = d["tp_tranches"][0]
        self.assertEqual(
            {k: v for k, v in tp0.items() if k != "label"},
            {"target": 110.0, "tranche_pct": 40.0, "r_multiple": 2.0, "tag": "t0"},
        )


if __name__ == "__main__":
    unittest.main()
