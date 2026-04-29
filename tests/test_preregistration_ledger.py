"""Tests for the strategy pre-registration ledger.

The ledger tracks every strategy hypothesis tested in AlphaLens, grouped by
signal class. Per Harvey-Liu-Zhu (2016), the count of hypotheses in a class
drives the Bonferroni denominator for the required t-stat threshold.

Pre-registration semantics enforced here:
  - You must `add()` a hypothesis BEFORE running the multi-phase audit.
  - You `complete()` it ONCE (re-completing must raise; re-running with
    different params requires a new id).
  - `count_in_class()` includes every status — registered, running,
    completed, abandoned — because every test consumed a degree of freedom.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path


def _make_reg_kwargs(**overrides):
    base = dict(
        id="tri_factor_2026_04_29",
        signal_class="fundamental_quality_x_momentum",
        hypothesis="Tri-factor (ROE TTM + 6m momentum + earnings revisions) generates Carhart-4F α t≥1.5 phase-robust.",
        scorer_path="scripts/experiment_tri_factor_edgar.py",
        params_frozen={
            "top_n": 5,
            "holding": 20,
            "rebalance_stride": 5,
            "weights": {"roe": 0.4, "mom": 0.3, "rev": 0.3},
            "adv_thresholds": [5_000_000],
            "cost_half_spreads": [5],
        },
        periods={
            "is_start": "2015-01-01",
            "is_end": "2022-12-31",
            "oos_start": "2023-01-01",
            "oos_end": "2026-04-22",
        },
        success_criteria={
            "mode": "multi_phase",
            "min_alpha_t_pass": 1.5,
            "min_alpha_t_mid": 1.0,
        },
        registered_at=date(2026, 4, 29),
    )
    base.update(overrides)
    return base


class TestRegistrationDataclass(unittest.TestCase):
    def test_construct_minimal_registration_defaults_to_registered(self):
        from alphalens.preregistration.ledger import Registration

        reg = Registration(**_make_reg_kwargs())

        self.assertEqual(reg.status, "registered")
        self.assertIsNone(reg.outcome)

    def test_to_dict_roundtrip(self):
        from alphalens.preregistration.ledger import Registration

        reg = Registration(**_make_reg_kwargs())

        restored = Registration.from_dict(reg.to_dict())

        self.assertEqual(restored, reg)

    def test_invalid_status_rejected(self):
        from alphalens.preregistration.ledger import Registration

        with self.assertRaises(ValueError):
            Registration(**_make_reg_kwargs(status="bogus"))

    def test_empty_signal_class_rejected(self):
        from alphalens.preregistration.ledger import Registration

        with self.assertRaises(ValueError):
            Registration(**_make_reg_kwargs(signal_class=""))


class TestLedgerPersistence(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_add_persists_to_json(self):
        from alphalens.preregistration.ledger import Ledger, Registration

        ledger = Ledger(self.root)
        reg = Registration(**_make_reg_kwargs())

        ledger.add(reg)

        path = self.root / "ledger.json"
        self.assertTrue(path.exists())
        payload = json.loads(path.read_text())
        self.assertEqual(len(payload["entries"]), 1)
        self.assertEqual(payload["entries"][0]["id"], reg.id)

    def test_load_existing_ledger(self):
        from alphalens.preregistration.ledger import Ledger, Registration

        Ledger(self.root).add(Registration(**_make_reg_kwargs()))
        reloaded = Ledger(self.root)

        retrieved = reloaded.get("tri_factor_2026_04_29")

        self.assertEqual(retrieved.signal_class, "fundamental_quality_x_momentum")

    def test_duplicate_id_raises(self):
        from alphalens.preregistration.ledger import Ledger, Registration

        ledger = Ledger(self.root)
        ledger.add(Registration(**_make_reg_kwargs()))

        with self.assertRaises(ValueError):
            ledger.add(Registration(**_make_reg_kwargs()))

    def test_get_unknown_id_raises(self):
        from alphalens.preregistration.ledger import Ledger

        with self.assertRaises(KeyError):
            Ledger(self.root).get("missing")


class TestLedgerListing(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _ledger_with_three(self):
        from alphalens.preregistration.ledger import Ledger, Registration

        ledger = Ledger(self.root)
        ledger.add(Registration(**_make_reg_kwargs(id="a", signal_class="momentum")))
        ledger.add(Registration(**_make_reg_kwargs(id="b", signal_class="momentum")))
        ledger.add(Registration(**_make_reg_kwargs(id="c", signal_class="quality")))
        return ledger

    def test_list_all(self):
        ledger = self._ledger_with_three()

        ids = sorted(r.id for r in ledger.list())

        self.assertEqual(ids, ["a", "b", "c"])

    def test_list_filtered_by_class(self):
        ledger = self._ledger_with_three()

        ids = sorted(r.id for r in ledger.list(signal_class="momentum"))

        self.assertEqual(ids, ["a", "b"])

    def test_count_in_class(self):
        ledger = self._ledger_with_three()

        self.assertEqual(ledger.count_in_class("momentum"), 2)
        self.assertEqual(ledger.count_in_class("quality"), 1)
        self.assertEqual(ledger.count_in_class("never_tested"), 0)


class TestLedgerCompletion(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_complete_sets_outcome_and_status(self):
        from alphalens.preregistration.ledger import Ledger, Registration

        ledger = Ledger(self.root)
        ledger.add(Registration(**_make_reg_kwargs()))

        ledger.complete(
            "tri_factor_2026_04_29",
            verdict="FAIL",
            mean_alpha_t=0.34,
            mean_excess_net=-0.085,
            audit_path="docs/research/tri_factor_multi_phase_audit.json",
            completed_at=date(2026, 4, 29),
            notes="Phase-robust FAIL across 5 phases.",
        )

        reg = ledger.get("tri_factor_2026_04_29")
        self.assertEqual(reg.status, "completed")
        self.assertEqual(reg.outcome["verdict"], "FAIL")
        self.assertAlmostEqual(reg.outcome["mean_alpha_t"], 0.34)

    def test_complete_persists(self):
        from alphalens.preregistration.ledger import Ledger, Registration

        Ledger(self.root).add(Registration(**_make_reg_kwargs()))
        Ledger(self.root).complete(
            "tri_factor_2026_04_29",
            verdict="FAIL",
            mean_alpha_t=0.34,
            mean_excess_net=-0.085,
            audit_path="docs/research/tri_factor_multi_phase_audit.json",
            completed_at=date(2026, 4, 29),
        )

        reloaded = Ledger(self.root).get("tri_factor_2026_04_29")
        self.assertEqual(reloaded.status, "completed")

    def test_invalid_verdict_rejected(self):
        from alphalens.preregistration.ledger import Ledger, Registration

        ledger = Ledger(self.root)
        ledger.add(Registration(**_make_reg_kwargs()))

        with self.assertRaises(ValueError):
            ledger.complete(
                "tri_factor_2026_04_29",
                verdict="MAYBE",
                mean_alpha_t=0.0,
                mean_excess_net=0.0,
                audit_path="x.json",
                completed_at=date(2026, 4, 29),
            )

    def test_recompletion_rejected(self):
        """Pre-registration discipline: a hypothesis is completed exactly once.

        Re-running with different params must use a new id.
        """
        from alphalens.preregistration.ledger import Ledger, Registration

        ledger = Ledger(self.root)
        ledger.add(Registration(**_make_reg_kwargs()))
        ledger.complete(
            "tri_factor_2026_04_29",
            verdict="FAIL",
            mean_alpha_t=0.34,
            mean_excess_net=-0.085,
            audit_path="x.json",
            completed_at=date(2026, 4, 29),
        )

        with self.assertRaises(ValueError):
            ledger.complete(
                "tri_factor_2026_04_29",
                verdict="PASS",
                mean_alpha_t=2.0,
                mean_excess_net=0.1,
                audit_path="y.json",
                completed_at=date(2026, 4, 30),
            )

    def test_abandon_marks_status_without_outcome(self):
        from alphalens.preregistration.ledger import Ledger, Registration

        ledger = Ledger(self.root)
        ledger.add(Registration(**_make_reg_kwargs()))
        ledger.abandon("tri_factor_2026_04_29", reason="Superseded by quality_momentum_combo.")

        reg = ledger.get("tri_factor_2026_04_29")
        self.assertEqual(reg.status, "abandoned")
        self.assertIsNone(reg.outcome)


class TestBonferroniThreshold(unittest.TestCase):
    """The threshold call answers: given the class as it stands NOW, what
    is the corrected critical |t|? The natural workflow is `add` first,
    then `threshold` — so the function counts every entry currently in
    the class (no implicit +1)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_threshold_uses_current_class_size(self):
        from alphalens.backtest.multiple_testing import bonferroni_critical_tstat
        from alphalens.preregistration.ledger import Ledger, Registration

        ledger = Ledger(self.root)
        ledger.add(Registration(**_make_reg_kwargs(id="a", signal_class="momentum")))
        ledger.add(Registration(**_make_reg_kwargs(id="b", signal_class="momentum")))
        ledger.add(Registration(**_make_reg_kwargs(id="c", signal_class="momentum")))

        threshold = ledger.bonferroni_threshold(signal_class="momentum", alpha=0.05)

        # 3 entries currently in class → n=3
        self.assertAlmostEqual(threshold, bonferroni_critical_tstat(n_tests=3, alpha=0.05))

    def test_threshold_for_empty_class_floors_at_one(self):
        """Asking the bar before any entries exist — sensible to return the
        n=1 critical |t| (≈1.96 at α=0.05) rather than divide by zero."""
        from alphalens.backtest.multiple_testing import bonferroni_critical_tstat
        from alphalens.preregistration.ledger import Ledger

        ledger = Ledger(self.root)

        threshold = ledger.bonferroni_threshold(signal_class="brand_new", alpha=0.05)

        self.assertAlmostEqual(threshold, bonferroni_critical_tstat(n_tests=1, alpha=0.05))

    def test_threshold_grows_with_class_size(self):
        from alphalens.preregistration.ledger import Ledger, Registration

        ledger = Ledger(self.root)
        ledger.add(Registration(**_make_reg_kwargs(id="a", signal_class="momentum")))
        t_with_one = ledger.bonferroni_threshold(signal_class="momentum")
        ledger.add(Registration(**_make_reg_kwargs(id="b", signal_class="momentum")))
        t_with_two = ledger.bonferroni_threshold(signal_class="momentum")

        self.assertGreater(t_with_two, t_with_one)


if __name__ == "__main__":
    unittest.main()
