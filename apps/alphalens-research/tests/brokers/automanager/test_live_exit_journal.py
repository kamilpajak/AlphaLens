from __future__ import annotations

import unittest

from alphalens_pipeline.brokers.automanager.live_exit_engine import fold_fired_tranches


class TestFoldFiredTranches(unittest.TestCase):
    def test_folds_lines_into_per_uic_tag_sets(self):
        lines = [
            {"kind": "tranche_fired", "uic": 486, "tag": "tp1"},
            {"kind": "tranche_fired", "uic": 486, "tag": "tp2"},
            {"kind": "tranche_fired", "uic": 999, "tag": "tp1"},
            {"kind": "oco_placed", "uic": 486},  # ignored
            {"kind": "tranche_fired", "uic": 486},  # malformed (no tag) ignored
        ]
        out = fold_fired_tranches(lines)
        self.assertEqual(out[486], frozenset({"tp1", "tp2"}))
        self.assertEqual(out[999], frozenset({"tp1"}))


if __name__ == "__main__":
    unittest.main()
