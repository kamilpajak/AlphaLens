"""Tests for legacy αpct migration script (Tier 2.C).

Pre-fix bug: `alpha_annualized = alpha_per_period * 252` regardless of stride.
Migration: divide alpha fields by stride to produce post-fix value.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.migrate_fix_alpha_annualized import migrate_cell_json


class TestMigrateCellJson(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, name: str, payload: dict) -> Path:
        path = self.dir / name
        path.write_text(json.dumps(payload))
        return path

    def test_migrates_alpha_fields_for_stride_5(self):
        path = self._write(
            "cell.json",
            {
                "config": {"stride_days": 5},
                "stats": {
                    "alpha_gross_4f": 32.95,  # pre-fix inflated by 5×
                    "alpha_net_4f": 32.94,
                    "alpha_t_4f": 2.03,  # not in ALPHA_FIELDS, must be untouched
                },
            },
        )
        result = migrate_cell_json(path)
        self.assertEqual(result["status"], "migrated")
        self.assertEqual(result["stride"], 5)
        self.assertEqual(result["n_modified"], 2)

        payload = json.loads(path.read_text())
        self.assertAlmostEqual(payload["stats"]["alpha_gross_4f"], 32.95 / 5, places=8)
        self.assertAlmostEqual(payload["stats"]["alpha_net_4f"], 32.94 / 5, places=8)
        self.assertAlmostEqual(payload["stats"]["alpha_t_4f"], 2.03)  # untouched
        self.assertEqual(payload["migrated_alpha_at"], "2026-05-05")
        self.assertEqual(payload["migrated_stride_days"], 5)

    def test_idempotent_skip_already_migrated(self):
        path = self._write(
            "cell.json",
            {
                "config": {"stride_days": 5},
                "stats": {"alpha_gross_4f": 32.95},
                "migrated_alpha_at": "2026-05-05",
            },
        )
        result = migrate_cell_json(path)
        self.assertEqual(result["status"], "skip_already_migrated")
        # Value unchanged on second pass
        payload = json.loads(path.read_text())
        self.assertEqual(payload["stats"]["alpha_gross_4f"], 32.95)

    def test_skip_no_stride_info(self):
        path = self._write(
            "cell.json",
            {
                "stats": {"alpha_gross_4f": 32.95},
            },
        )
        result = migrate_cell_json(path)
        self.assertEqual(result["status"], "skip_no_stride_or_daily")

    def test_skip_no_alpha_fields(self):
        path = self._write(
            "cell.json",
            {
                "config": {"stride_days": 5},
                "metadata": {"author": "test"},
            },
        )
        result = migrate_cell_json(path)
        self.assertEqual(result["status"], "skip_no_alpha_fields")

    def test_skip_daily_stride(self):
        path = self._write(
            "cell.json",
            {
                "config": {"stride_days": 1},
                "stats": {"alpha_gross_4f": 32.95},
            },
        )
        result = migrate_cell_json(path)
        self.assertEqual(result["status"], "skip_no_stride_or_daily")

    def test_dry_run_does_not_write(self):
        path = self._write(
            "cell.json",
            {
                "config": {"stride_days": 5},
                "stats": {"alpha_gross_4f": 32.95},
            },
        )
        result = migrate_cell_json(path, dry_run=True)
        self.assertEqual(result["status"], "migrated")
        # File untouched in dry-run
        payload = json.loads(path.read_text())
        self.assertEqual(payload["stats"]["alpha_gross_4f"], 32.95)
        self.assertNotIn("migrated_alpha_at", payload)

    def test_recursive_walk_nested_alpha(self):
        path = self._write(
            "cell.json",
            {
                "config": {"rebalance_stride": 5},
                "base_stats": {"alpha_gross_4f": 30.0, "alpha_net_4f": 29.5},
                "overlay_stats": {"alpha_gross_4f": 25.0, "alpha_net_4f": 24.5},
            },
        )
        result = migrate_cell_json(path)
        self.assertEqual(result["n_modified"], 4)
        payload = json.loads(path.read_text())
        self.assertAlmostEqual(payload["base_stats"]["alpha_gross_4f"], 6.0)
        self.assertAlmostEqual(payload["overlay_stats"]["alpha_gross_4f"], 5.0)

    def test_skips_active_driver_subdir(self):
        # If path is under v9d_retrospective_pre_2018, skip (driver re-runs).
        sub = self.dir / "v9d_retrospective_pre_2018"
        sub.mkdir()
        path = sub / "U2_GFC_recovery_p0.json"
        path.write_text(
            json.dumps(
                {
                    "config": {"stride_days": 5},
                    "stats": {"alpha_gross_4f": 32.95},
                }
            )
        )
        result = migrate_cell_json(path)
        self.assertEqual(result["status"], "skip_active_driver_dir")


if __name__ == "__main__":
    unittest.main()
