"""API contract test for the external ``phase-robust-backtesting`` dep.

Per ADR 0006 the methodology bundle (preregistration ledger + multi-phase
audit + Bonferroni thresholds + audit driver) lives in an external
package. AlphaLens depends on a specific subset of its public API. If a
future OSS release renames a kwarg, drops a function, or changes an
import path, the AlphaLens callers break at runtime — sometimes mid-way
through a multi-day backfill.

These tests do **not** re-test math (that's the OSS package's job — it
has its own test suite). They assert that the **API surface AlphaLens
relies on still exists** in the installed package, with the kwargs and
shapes we expect. Failure here is a red flag at AlphaLens CI time
rather than at runtime in production.

Updating this file: when AlphaLens starts depending on a new
function/kwarg from the OSS package, add the corresponding contract
assertion here. When OSS drops something, this test fails — choose
between pinning a stale OSS version or updating AlphaLens callers.
"""

from __future__ import annotations

import inspect
import tempfile
import unittest
from datetime import date
from pathlib import Path


class TestLedgerContract(unittest.TestCase):
    def test_ledger_and_registration_constructible(self):
        from phase_robust_backtesting.ledger import Ledger, Registration

        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(root=Path(tmp))
            reg = Registration(
                id="contract_test_2026_05_06",
                signal_class="contract_test",
                hypothesis="API contract test fixture",
                scorer_path="scripts/contract_test.py",
                params_frozen={"k": "v"},
                periods={"oos_start": "2024-01-01", "oos_end": "2024-12-31"},
                success_criteria={"alpha_t_min": 2.0},
                registered_at=date(2026, 5, 6),
            )
            ledger.add(reg)
            # In-memory roundtrip — same object reachable via get().
            self.assertEqual(ledger.get(reg.id).id, reg.id)
            # Disk roundtrip — instantiating a fresh Ledger forces JSON
            # deserialisation. Catches future OSS releases that change
            # the on-disk format in a way AlphaLens-side existing
            # ledger.json (~30 entries) couldn't load.
            ledger_reloaded = Ledger(root=Path(tmp))
            self.assertEqual(ledger_reloaded.get(reg.id).id, reg.id)


class TestMultiPhaseContract(unittest.TestCase):
    def test_robust_verdict_accepts_dispersion_threshold_kwarg(self):
        # AlphaLens depends on dispersion_threshold_pp added in OSS v0.2.0
        # (default 50pp). If the kwarg is renamed or removed, our pre-reg
        # gates break silently.
        from phase_robust_backtesting.multi_phase import robust_verdict

        rows = [
            {"alpha_t": 1.6, "excess_net_ann": 0.06},
            {"alpha_t": 1.7, "excess_net_ann": 0.08},
        ]
        verdict = robust_verdict(rows, dispersion_threshold_pp=70.0)
        self.assertIn(verdict, {"PASS", "MID", "FAIL"})

    def test_summarise_phase_results_callable(self):
        from phase_robust_backtesting.multi_phase import summarise_phase_results

        out = summarise_phase_results(
            [
                {"alpha_t": 1.5, "excess_net_ann": 0.10, "sharpe_net": 0.8},
                {"alpha_t": 1.7, "excess_net_ann": 0.12, "sharpe_net": 0.9},
            ]
        )
        self.assertIn("alpha_t", out)
        self.assertIn("excess_net_ann", out)


class TestMultipleTestingContract(unittest.TestCase):
    def test_bonferroni_critical_tstat_callable(self):
        from phase_robust_backtesting.multiple_testing import bonferroni_critical_tstat

        critical = bonferroni_critical_tstat(n_tests=27)
        # Bonferroni at n=27, alpha=0.05 two-sided ≈ 2.86. Just sanity-check
        # the value is in the right ballpark — we're not re-testing math.
        self.assertGreater(critical, 1.5)
        self.assertLess(critical, 5.0)

    def test_apply_bonferroni_importable(self):
        # Used by scripts/layer2c_revalidation.py.
        from phase_robust_backtesting.multiple_testing import apply_bonferroni

        self.assertTrue(callable(apply_bonferroni))


class TestAuditMultiPhaseContract(unittest.TestCase):
    def test_run_audit_callable(self):
        from phase_robust_backtesting.audit_multi_phase import run_audit

        self.assertTrue(callable(run_audit))

    def test_run_audit_signature_keyword_only_kwargs(self):
        # AlphaLens scripts/audit_multi_phase.py wrapper passes
        # rebalance_stride= and out= as kwargs. If a future OSS release
        # makes them positional, the wrapper still works at the source
        # level (kwargs-as-positional is compatible) but LOSING the
        # keyword-only marker would weaken our forward compat — surface
        # any change here at AlphaLens CI time.
        from phase_robust_backtesting.audit_multi_phase import run_audit

        sig = inspect.signature(run_audit)
        self.assertIn("rebalance_stride", sig.parameters)
        self.assertIn("out", sig.parameters)
        self.assertEqual(
            sig.parameters["rebalance_stride"].kind,
            inspect.Parameter.KEYWORD_ONLY,
            "run_audit.rebalance_stride should remain keyword-only",
        )
        self.assertEqual(
            sig.parameters["out"].kind,
            inspect.Parameter.KEYWORD_ONLY,
            "run_audit.out should remain keyword-only",
        )


if __name__ == "__main__":
    unittest.main()
