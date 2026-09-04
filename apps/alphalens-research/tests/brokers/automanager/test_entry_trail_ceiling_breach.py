"""Hermetic tests for the G1 ceiling observation (#1317).

``observe_ceiling`` compares the price an entry-trail fire ACTUALLY executed at
against the ceiling that same order was armed with. It exists because nothing on
disk could answer that question: the ceiling lived only in a journald log line,
so one breach was found by accident and seven were never seen at all.

Three kinds of test here, and the last two are the point:

1. **The incident, frozen.** Every fire on record (``tests.incident_1317_fixture``)
   run through the real function, asserting the 8-of-23 breach count and the two
   extreme rows by value.
2. **A positive control.** A fabricated fill one tick above the ceiling MUST
   flag. Without it a detector that silently stopped detecting would keep every
   other test in this file green, which is exactly how the original §4b probe
   passed while proving nothing.
3. **Unknown is not OK.** A missing or degenerate input yields ``None`` — "no
   verdict", never "clean". The caller-side half of that rule lives in
   ``test_entry_watch_reconcile.py``.
"""

from __future__ import annotations

import math
import unittest

from alphalens_pipeline.brokers.automanager.entry_trail_geometry import (
    CEILING_EPS_FRAC,
    observe_ceiling,
)

from tests.incident_1317_fixture import (
    AMBA,
    AMBA_BREACH_ABS,
    AMBA_BREACH_BPS,
    BAH,
    BAH_BREACH_ABS,
    BAH_BREACH_BPS,
    BAH_DERIVED_CEILING,
    EXPECTED_BREACHES,
    FIRES,
)

_TICK = 0.01  # every instrument in the fixture is a US equity on a 1-cent grid


class TestEveryRecordedFire(unittest.TestCase):
    """The 23 measured fires, replayed through the shipped function."""

    def test_breach_count_matches_the_measurement(self) -> None:
        observed = [observe_ceiling(ceiling=fire.ceiling, fill=fire.fill) for fire in FIRES]
        self.assertNotIn(None, observed, "every recorded fire has both prices")
        breached = [obs for obs in observed if obs is not None and obs.breached]
        self.assertEqual(len(breached), EXPECTED_BREACHES)

    def test_each_row_agrees_with_its_own_arithmetic(self) -> None:
        for fire in FIRES:
            with self.subTest(crid=fire.crid, order_id=fire.order_id):
                obs = observe_ceiling(ceiling=fire.ceiling, fill=fire.fill)
                assert obs is not None
                self.assertEqual(obs.breached, fire.breached)
                self.assertAlmostEqual(obs.breach_abs, fire.fill - fire.ceiling, places=10)

    def test_the_live_breach_that_opened_the_issue(self) -> None:
        obs = observe_ceiling(ceiling=AMBA.ceiling, fill=AMBA.fill)
        assert obs is not None
        self.assertTrue(obs.breached)
        self.assertAlmostEqual(obs.breach_abs, AMBA_BREACH_ABS, places=10)
        self.assertAlmostEqual(obs.breach_bps, AMBA_BREACH_BPS, places=6)

    def test_the_largest_breach_on_record(self) -> None:
        obs = observe_ceiling(ceiling=BAH.ceiling, fill=BAH.fill)
        assert obs is not None
        self.assertTrue(obs.breached)
        self.assertAlmostEqual(obs.breach_abs, BAH_BREACH_ABS, places=10)
        self.assertAlmostEqual(obs.breach_bps, BAH_BREACH_BPS, places=6)

    def test_only_live_rows_speak_about_the_real_matching_engine(self) -> None:
        # Documented split, pinned so the SIM/LIVE reading rule cannot be lost:
        # 1 of the 5 LIVE fires breached, 7 of the 18 SIM ones.
        live = [f for f in FIRES if f.env == "LIVE"]
        sim = [f for f in FIRES if f.env == "SIM"]
        self.assertEqual((len(live), sum(f.breached for f in live)), (5, 1))
        self.assertEqual((len(sim), sum(f.breached for f in sim)), (18, 7))


class TestPositiveControl(unittest.TestCase):
    """A detector that cannot produce a breach has tested nothing."""

    def test_one_tick_above_the_ceiling_is_a_breach(self) -> None:
        obs = observe_ceiling(ceiling=100.0, fill=100.0 + _TICK)
        assert obs is not None
        self.assertTrue(obs.breached)
        self.assertAlmostEqual(obs.breach_abs, _TICK, places=10)
        self.assertAlmostEqual(obs.breach_bps, _TICK / 100.0 * 1e4, places=10)

    def test_one_tick_below_the_ceiling_is_not(self) -> None:
        obs = observe_ceiling(ceiling=100.0, fill=100.0 - _TICK)
        assert obs is not None
        self.assertFalse(obs.breached)
        self.assertAlmostEqual(obs.breach_abs, -_TICK, places=10)

    def test_exactly_at_the_ceiling_is_not(self) -> None:
        # A limit fills AT its price — the cap is inclusive, so equality is the
        # best possible outcome of an enforced clamp, never a breach.
        obs = observe_ceiling(ceiling=100.0, fill=100.0)
        assert obs is not None
        self.assertFalse(obs.breached)
        self.assertEqual(obs.breach_abs, 0.0)
        self.assertEqual(obs.breach_bps, 0.0)


class TestUnknownIsNotClean(unittest.TestCase):
    """Missing inputs yield NO VERDICT, never a passing one."""

    def test_missing_fill_yields_no_verdict(self) -> None:
        # An unresolved audit read leaves avg_fill_price None: we do not know
        # what it filled at, which is not the same as knowing it was fine.
        self.assertIsNone(observe_ceiling(ceiling=100.0, fill=None))

    def test_missing_ceiling_yields_no_verdict(self) -> None:
        # A tier armed before the ceiling was journaled (#1317) — every fire
        # before 2026-09-04 is in this state.
        self.assertIsNone(observe_ceiling(ceiling=None, fill=100.0))

    def test_degenerate_inputs_yield_no_verdict(self) -> None:
        # ``True`` is in the list for the same reason entry_trails rejects it on
        # every journaled price: it is finite and positive, so a bare isfinite
        # gate lets it through and produces a 990000 bps "breach" out of a JSON
        # true. Matches ``entry_trails._finite_positive_float``.
        bad = (math.nan, math.inf, -math.inf, 0.0, -1.0, True, False)
        for value in bad:
            with self.subTest(value=value):
                self.assertIsNone(observe_ceiling(ceiling=value, fill=100.0))
                self.assertIsNone(observe_ceiling(ceiling=100.0, fill=value))


class TestCeilingMustBeJournaledNotDerived(unittest.TestCase):
    """Why the ceiling is stored rather than recomputed at read time."""

    def test_the_obvious_reconstruction_is_wrong_on_a_real_row(self) -> None:
        # would_be_trigger * (1 + eps) matches the arm line on 22 of 23 rows and
        # misses BAH, because the fired blob's would_be_trigger comes from the
        # MINIMUM trough ever journaled, not the trough at arm time.
        self.assertAlmostEqual(BAH_DERIVED_CEILING, 73.80 * 1.005 * (1.0 + CEILING_EPS_FRAC))
        self.assertLess(BAH_DERIVED_CEILING, BAH.ceiling)
        derived = observe_ceiling(ceiling=BAH_DERIVED_CEILING, fill=BAH.fill)
        armed = observe_ceiling(ceiling=BAH.ceiling, fill=BAH.fill)
        assert derived is not None and armed is not None
        # Both call it a breach, but the derived one overstates it by 45%.
        self.assertGreater(derived.breach_bps, armed.breach_bps * 1.4)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
