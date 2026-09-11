"""#1236: the document's reaction plan resolves to the policy that manages its stop.

Until now "may the daemon move this stop" was a side effect — true exactly when
the ``planned`` journal line carried a geometry blob with a finite ATR — and
*which* move happened came from one process-wide environment variable. Neither
fact was in the document, which is what blocks the #1406 door: it has nothing to
accept and nothing to refuse on the subject.

Each producer now declares what it wants, in the vocabulary the contract already
had. The primitives describe themselves, so an external client is not coupled to
our registry or its version:

  * nothing declared  -> the stop is never moved;
  * ``TrailingStop(arm_trigger_r, trail_frac)`` -> break-even + fractional
    giveback trail with those parameters;
  * ``ReanchorOnFill(k_atr, atr)`` -> a one-shot re-anchor to
    ``avg_price - k_atr*atr`` on fill-complete.

Parameters are FREE within the door's rules, and that is the point rather than an
oversight: ``0.5R`` is not a comparable quantity across hand-set stops (1R is
6.8% of entry on AMBA and 29% on RHI), so an operator or a client must be able to
choose. The policy ``name`` stays the registry family name so an audit line still
says what ran.
"""

from __future__ import annotations

import math
import unittest

from broker_contract.exit_geometry.policy import BreakevenTrailPolicy, SetupStaticPolicy
from broker_contract.exit_geometry.registry import (
    exit_policy_registry,
    resolve_declared_policy,
)
from broker_contract.trade_intent.schema import ModelPush, ReanchorOnFill, TrailingStop


class NoDeclarationMeansTheStopIsNeverMovedTest(unittest.TestCase):
    """THE MIGRATION RULE. Every pick armed before this change declares nothing,
    and a manual pick declares nothing by design (#1325). If absent fell back to
    the daemon-wide policy, every one of them would start trailing the moment
    this deploys — a decision nobody made."""

    def test_absent_resolves_to_the_inert_policy(self):
        policy = resolve_declared_policy(None)
        self.assertIsInstance(policy, SetupStaticPolicy)
        self.assertFalse(policy.trails)
        self.assertIsNone(policy.decide_reanchor(100.0, 2.0, peak=200.0, plan_stop=90.0))


class ATrailingStopDeclarationCarriesItsOwnParametersTest(unittest.TestCase):
    def test_it_resolves_to_the_breakeven_trail_family(self):
        policy = resolve_declared_policy(TrailingStop(arm_trigger_r=0.5, trail_frac=0.6))
        self.assertIsInstance(policy, BreakevenTrailPolicy)
        self.assertTrue(policy.trails)
        # The FAMILY name, so a log line and the journal stamp still name a
        # policy an operator can look up; the parameters travel beside it.
        self.assertEqual(policy.name, "breakeven_trail")

    def test_the_declared_parameters_are_the_ones_used(self):
        """The discriminator. If resolution ignored the declaration and handed
        back the registry's own 0.5/0.6 entry, this would still pass for those
        numbers — so it is asserted with DIFFERENT ones."""
        policy = resolve_declared_policy(TrailingStop(arm_trigger_r=1.0, trail_frac=0.25))
        registry_default = exit_policy_registry()["breakeven_trail"]
        # 1R is 4.00 here (59.00 - 55.00), so a peak of 70.00 arms BOTH the
        # declared 1.0R and the registry's 0.5R — the two then differ only in
        # `trail_frac`, which is the thing being asserted.
        kwargs = {"peak": 70.00, "last_price": 69.50, "plan_stop": 55.00}
        declared = policy.decide_reanchor(59.00, None, **kwargs)
        default = registry_default.decide_reanchor(59.00, None, **kwargs)
        self.assertIsNotNone(declared)
        self.assertNotEqual(declared, default)

    def test_a_wider_arm_trigger_stays_dark_where_the_default_would_fire(self):
        """1R here is 4.00 (59.00 - 55.00), so a peak of 61.00 is 0.5R: the
        registry default arms, a declared 1.0R does not."""
        kwargs = {"peak": 61.00, "last_price": 60.90, "plan_stop": 55.00}
        self.assertIsNotNone(
            resolve_declared_policy(TrailingStop(0.5, 0.6)).decide_reanchor(59.00, None, **kwargs)
        )
        self.assertIsNone(
            resolve_declared_policy(TrailingStop(1.0, 0.6)).decide_reanchor(59.00, None, **kwargs)
        )


class AReanchorDeclarationCarriesItsOwnMultipleTest(unittest.TestCase):
    def test_it_reanchors_to_the_declared_multiple(self):
        policy = resolve_declared_policy(ReanchorOnFill(k_atr=1.5, atr=2.0))
        self.assertFalse(policy.trails)
        target = policy.decide_reanchor(101.0, 2.0)
        assert target is not None
        self.assertTrue(math.isclose(target, 101.0 - 3.0))

    def test_a_different_declared_multiple_gives_a_different_stop(self):
        """Without this, resolution could silently use the registry geometry's
        own 1.5 and nothing would notice."""
        target = resolve_declared_policy(ReanchorOnFill(k_atr=3.0, atr=2.0)).decide_reanchor(
            101.0, 2.0
        )
        assert target is not None
        self.assertTrue(math.isclose(target, 101.0 - 6.0))

    def test_it_places_no_geometry_of_its_own(self):
        """A declared re-anchor MANAGES a stop; it does not decide what is
        placed. Since #1414 nothing in the policy layer does — placement is the
        document's own ``initial_levels``."""
        policy = resolve_declared_policy(ReanchorOnFill(k_atr=1.5, atr=2.0))
        self.assertIsNone(policy.decide_placement_geometry(100.0, 2.0, ceiling_price=None))

    def test_a_degenerate_atr_is_refused_not_raised(self):
        policy = resolve_declared_policy(ReanchorOnFill(k_atr=1.5, atr=2.0))
        for atr in (None, 0.0, float("nan"), float("inf")):
            with self.subTest(atr=atr):
                self.assertIsNone(policy.decide_reanchor(101.0, atr))


class AnUnhonourablePrimitiveIsInertNotFatalTest(unittest.TestCase):
    """``ModelPush`` reserves a kind whose levels arrive through an ``amend_exit``
    call that does not exist. The DOOR refuses it loudly (that is a validation
    rule); resolution is reached from a journal stamp inside the protection pass,
    where raising would starve the never-naked backstop. So it degrades to the
    inert policy: the stop is simply never moved."""

    def test_model_push_resolves_to_the_inert_policy(self):
        self.assertIsInstance(resolve_declared_policy(ModelPush()), SetupStaticPolicy)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
