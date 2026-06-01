"""Hermetic test for the L4 live-probe classifier (``tests.live.run_probes``).

This is the ONE module under ``tests/live/`` that is NOT env-gated — it runs in
the default ``unittest discover`` so the directory carries CI coverage and the
>50% majority-success math is pinned. The four vendor probes that import
``run_probes`` are skipped without their flags; this proves the shared logic
they all rely on.

Test-strategy memo Phase 5 / L4 (docs/research/integration_e2e_test_strategy_2026_06_01.md).
"""

from __future__ import annotations

import unittest

from tests.live import (
    PermanentProbeError,
    ProbeOutcome,
    TransientProbeError,
    run_probes,
)


def _ok() -> None:
    return None


def _permanent() -> None:
    raise PermanentProbeError("shape break")


def _transient() -> None:
    raise TransientProbeError("HTTP 429")


def _unexpected() -> None:
    raise ValueError("surprise KeyError-style break")


class TestRunProbes(unittest.TestCase):
    """The run_probes classifier — given probe callables, when run, then assert."""

    def setUp(self) -> None:
        # A throwaway TestCase to receive the helper's own assertions, so a
        # helper-level failure raises AssertionError HERE (catchable) instead
        # of failing this test.
        self.dummy = unittest.TestCase()

    def test_all_ok_passes_and_tallies(self) -> None:
        out = run_probes(self.dummy, {"a": _ok, "b": _ok, "c": _ok}, label="t")
        self.assertEqual(out.ok, ["a", "b", "c"])
        self.assertEqual(out.transient, [])
        self.assertEqual(out.permanent, [])

    def test_single_permanent_fails(self) -> None:
        with self.assertRaises(AssertionError):
            run_probes(self.dummy, {"a": _ok, "b": _permanent}, label="t")

    def test_unexpected_exception_reclassified_permanent(self) -> None:
        # A non-Probe exception must surface as a permanent failure (-> FAIL),
        # never be silently swallowed or treated as transient.
        with self.assertRaises(AssertionError):
            run_probes(self.dummy, {"a": _unexpected}, label="t")
        # ...and it lands in `permanent` (not `transient`) with a typed message.
        out = run_probes_collect_only({"a": _unexpected})
        self.assertEqual(len(out.permanent), 1)
        self.assertIn("unexpected ValueError", out.permanent[0][1])
        self.assertEqual(out.transient, [])

    def test_minority_transient_passes(self) -> None:
        # 3 items, 1 transient -> ok=2 >= 3//2=1 -> passes.
        out = run_probes(self.dummy, {"a": _ok, "b": _ok, "c": _transient}, label="t")
        self.assertEqual(out.ok, ["a", "b"])
        self.assertEqual(len(out.transient), 1)

    def test_majority_transient_fails(self) -> None:
        # 2 items, both transient -> ok=0 < 2//2=1 -> the >50% gate FAILS.
        with self.assertRaises(AssertionError):
            run_probes(self.dummy, {"a": _transient, "b": _transient}, label="t")

    def test_permanent_dominates_over_transient_tolerance(self) -> None:
        # Even a lone permanent in an otherwise-ok run FAILS (permanent gate is
        # independent of the >50% transient gate).
        with self.assertRaises(AssertionError):
            run_probes(
                self.dummy,
                {"a": _ok, "b": _ok, "c": _ok, "d": _permanent},
                label="t",
            )


def run_probes_collect_only(items: dict) -> ProbeOutcome:
    """Run probe callables and return the tally WITHOUT the final asserts.

    Mirrors ``run_probes`` classification so a test can inspect the tri-state
    without tripping the assertions. Kept local to this test (the production
    helper always asserts — that is its contract).
    """
    out = ProbeOutcome()
    for name, fn in items.items():
        try:
            fn()
            out.ok.append(name)
        except PermanentProbeError as exc:
            out.permanent.append((name, str(exc)))
        except TransientProbeError as exc:
            out.transient.append((name, str(exc)))
        except Exception as exc:  # mirror run_probes: unexpected -> permanent
            out.permanent.append((name, f"unexpected {type(exc).__name__}: {exc}"))
    return out


if __name__ == "__main__":
    unittest.main()
