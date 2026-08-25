"""The #1112 cost gates and the quantity they price at (PR #1116 round 2, point 1).

The arm gate and the exit gate share the same cost FUNCTION but call it with
DIFFERENT quantities, so they do not draw the same threshold. Three docstrings
claimed the opposite; the claim is refuted by
:func:`alphalens_pipeline.brokers.automanager.costs.min_profitable_exit_price` itself.
"""

from __future__ import annotations

import unittest

from alphalens_pipeline.brokers.automanager import costs, entry_trail_geometry, live_exit_engine
from alphalens_pipeline.brokers.automanager.costs import min_profitable_exit_price

_REFUTED_DOCSTRING_CLAIMS: tuple[str, ...] = (
    "draw the same line",
    "can never be armed on a target",
    "the ONE threshold both",
)
"""Phrases asserting the two gates share ONE threshold. Measured false: at an
entry of 60.00 the threshold is 60.8000 for 10 shares and 62.6000 for 1 share.
Kept here so re-introducing the claim in any of the three modules turns a test
red rather than the money rail."""

_ENTRY_PRICE = 60.0
_ARM_GATE_QTY = 10.0
"""A whole-position quantity, the shape the arm gate prices at."""
_EXIT_TRANCHE_QTY = 3.0
"""A fractional tranche of that position, the shape a restored multi-tranche
exit plan would price at."""


class TestTheTwoGatesPriceAtDifferentQuantities(unittest.TestCase):
    def test_the_threshold_rises_as_the_priced_quantity_falls(self) -> None:
        big = min_profitable_exit_price(entry_price=_ENTRY_PRICE, qty=_ARM_GATE_QTY)
        small = min_profitable_exit_price(entry_price=_ENTRY_PRICE, qty=_EXIT_TRANCHE_QTY)
        assert big is not None and small is not None
        self.assertGreater(
            small,
            big,
            "a smaller exit quantity must need a HIGHER exit price — the per-fill "
            "USD minimum weighs more on a small notional",
        )

    def test_the_two_gates_do_not_share_one_threshold(self) -> None:
        # The exact numbers, so a change to the cost model shows up here rather
        # than quietly making the two gates agree again.
        self.assertAlmostEqual(
            min_profitable_exit_price(entry_price=_ENTRY_PRICE, qty=_ARM_GATE_QTY), 60.80, places=4
        )
        self.assertAlmostEqual(
            min_profitable_exit_price(entry_price=_ENTRY_PRICE, qty=_EXIT_TRANCHE_QTY),
            61.2667,
            places=4,
        )


class TestNoDocstringClaimsTheGatesShareAThreshold(unittest.TestCase):
    """The three docstrings that asserted one shared threshold were wrong, and
    the wrongness is load-bearing — a reader who believes it will not add the
    guard that makes the arm gate's whole-position pricing safe."""

    def _docstrings(self) -> dict[str, str]:
        # Whitespace-collapsed: a docstring wraps its prose, so a claim can sit
        # across two source lines and a raw substring search would miss it.
        return {
            name: " ".join((doc or "").split())
            for name, doc in (
                ("costs.min_profitable_exit_price", costs.min_profitable_exit_price.__doc__),
                (
                    "entry_trail_geometry.arms_inside_exit_region",
                    entry_trail_geometry.arms_inside_exit_region.__doc__,
                ),
                ("live_exit_engine._exit_clears_cost", live_exit_engine._exit_clears_cost.__doc__),
            )
        }

    def test_none_of_the_three_docstrings_repeats_a_refuted_claim(self) -> None:
        offenders = [
            (name, claim)
            for name, doc in self._docstrings().items()
            for claim in _REFUTED_DOCSTRING_CLAIMS
            if claim in doc
        ]
        self.assertEqual(
            offenders,
            [],
            "these docstrings assert the two #1112 gates share one threshold; "
            "they evaluate the same function at different quantities",
        )

    def test_each_docstring_states_the_quantity_dependence_instead(self) -> None:
        for name, doc in self._docstrings().items():
            with self.subTest(name=name):
                self.assertIn(
                    "quantit",
                    doc.lower(),
                    "the docstring must say the threshold depends on the quantity "
                    "it is evaluated at",
                )


if __name__ == "__main__":
    unittest.main()
