"""Property-based tests for equal-risk position sizing.

``thematic/trade_setup/sizing.py`` is the money-relevant allocation math
(criticality 25 on the mutation-target map): ``equal_risk_allocations`` splits an
entry ladder so every tier risks the same fraction to the stop, and
``suggested_size_pct`` derives the total book exposure under a hard 25% cap.

The strongest tests here are ALGEBRAIC identities and metamorphic relations
(round-trip risk share, permutation- and scale-invariance) rather than soft
bounds — inequalities alone leave too many wrong implementations alive.
"""

from __future__ import annotations

from typing import Any

from alphalens_pipeline.thematic.trade_setup.sizing import (
    _MAX_EXPOSURE_PCT,
    blended_entry,
    equal_risk_allocations,
    suggested_size_pct,
)
from hypothesis import given
from hypothesis import strategies as st

from .base import PropertyTestCase
from .strategies import finite_prices


@st.composite
def valid_sizing(
    draw: Any, *, with_weights: bool = False
) -> tuple[list[float], float, list[float] | None]:
    """Entries each strictly above the stop (the ``equal_risk_allocations`` precondition)."""
    stop = draw(finite_prices(1.0, 1e4))
    n = draw(st.integers(1, 4))
    entries = [stop + draw(finite_prices(0.5, 1e3)) for _ in range(n)]
    weights = (
        draw(st.lists(finite_prices(1e-2, 100.0), min_size=n, max_size=n)) if with_weights else None
    )
    return entries, stop, weights


class TestEqualRiskAllocations(PropertyTestCase):
    @given(
        entries=st.lists(finite_prices(1.0, 1e4), min_size=1, max_size=4),
        stop=finite_prices(1.0, 1e4),
    )
    def test_raises_iff_any_entry_le_stop_else_valid_allocs(
        self, entries: list[float], stop: float
    ) -> None:
        if any(e <= stop for e in entries):
            with self.assertRaises(ValueError):
                equal_risk_allocations(entries, stop)
        else:
            allocs = equal_risk_allocations(entries, stop)
            self.assertEqual(len(allocs), len(entries))
            for a in allocs:
                self.assertGreater(a, 0.0)
            self.assert_close(sum(allocs), 100.0, rel_tol=1e-9, abs_tol=1e-6)

    @given(valid_sizing(with_weights=True))
    def test_risk_share_proportional_to_weights(
        self, inp: tuple[list[float], float, list[float]]
    ) -> None:
        """The identity the module exists for: per-tier risk share ∝ the input weights.

        risk_i = shares_i·(E_i−S) ∝ alloc_i·(E_i−S)/E_i; normalised it equals the
        normalised weight vector — for ANY weights, not just the equal default.
        """
        entries, stop, weights = inp
        allocs = equal_risk_allocations(entries, stop, weights=weights)
        risk_share = [a * (e - stop) / e for a, e in zip(allocs, entries, strict=True)]
        rs_total = sum(risk_share)
        w_total = sum(weights)
        for r, w in zip(risk_share, weights, strict=True):
            self.assert_close(r / rs_total, w / w_total, rel_tol=1e-6, abs_tol=1e-9)

    @given(inp=valid_sizing(with_weights=True), data=st.data())
    def test_permutation_invariance(
        self, inp: tuple[list[float], float, list[float]], data: Any
    ) -> None:
        entries, stop, weights = inp
        perm = data.draw(st.permutations(range(len(entries))))
        allocs = equal_risk_allocations(entries, stop, weights=weights)
        e2 = [entries[i] for i in perm]
        w2 = [weights[i] for i in perm]
        allocs2 = equal_risk_allocations(e2, stop, weights=w2)
        for j, i in enumerate(perm):
            self.assert_close(allocs2[j], allocs[i])

    @given(inp=valid_sizing(with_weights=True), k=finite_prices(1e-2, 1e3))
    def test_scale_invariance(self, inp: tuple[list[float], float, list[float]], k: float) -> None:
        """Scaling every price by k leaves allocations unchanged; blended scales by k."""
        entries, stop, weights = inp
        a1 = equal_risk_allocations(entries, stop, weights=weights)
        scaled = [e * k for e in entries]
        a2 = equal_risk_allocations(scaled, stop * k, weights=weights)
        for x, y in zip(a1, a2, strict=True):
            self.assert_close(x, y, rel_tol=1e-6, abs_tol=1e-6)
        self.assert_close(
            blended_entry(scaled, a2), blended_entry(entries, a1) * k, rel_tol=1e-6, abs_tol=1e-4
        )


class TestSuggestedSize(PropertyTestCase):
    @given(
        inp=valid_sizing(),
        rb1=finite_prices(1e-3, 50.0),
        rb2=finite_prices(1e-3, 50.0),
    )
    def test_cap_and_monotone_in_risk_budget(
        self, inp: tuple[list[float], float, None], rb1: float, rb2: float
    ) -> None:
        entries, stop, _ = inp
        s1 = suggested_size_pct(entries, stop, rb1)
        self.assertGreaterEqual(s1, 0.0)
        self.assertLessEqual(s1, _MAX_EXPOSURE_PCT + 1e-9)  # hard cap
        lo, hi = sorted((rb1, rb2))
        self.assertLessEqual(
            suggested_size_pct(entries, stop, lo),
            suggested_size_pct(entries, stop, hi) + 1e-9,
        )


class TestBlendedEntry(PropertyTestCase):
    @given(valid_sizing(with_weights=True))
    def test_within_entry_bounds(self, inp: tuple[list[float], float, list[float]]) -> None:
        entries, stop, weights = inp
        allocs = equal_risk_allocations(entries, stop, weights=weights)
        b = blended_entry(entries, allocs)
        self.assertGreaterEqual(b, min(entries) - 1e-6)
        self.assertLessEqual(b, max(entries) + 1e-6)

    @given(st.lists(finite_prices(1.0, 1e4), min_size=1, max_size=5))
    def test_degenerate_allocations_fall_back_to_simple_mean(self, entries: list[float]) -> None:
        b = blended_entry(entries, [0.0] * len(entries))
        self.assert_close(b, sum(entries) / len(entries))
