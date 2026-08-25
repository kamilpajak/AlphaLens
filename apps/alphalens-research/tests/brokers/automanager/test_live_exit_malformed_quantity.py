"""A malformed owned quantity must not crash the live-exit pass (#1125 review).

`_run_live_exits_pass` wraps `run_live_exits` in `except BrokerError` ONLY, and
the very next statement in the tick is `_run_protection_pass` — the never-naked
backstop that re-asserts SL == owned. So any other exception escaping the exits
pass does not merely lose one exit: it starves protection for that tick. The
control loop says so itself, right above the two calls.

`round()` is the sharp edge. It raises on exactly the values a broker read can
degrade to:

    round(nan) -> ValueError        round(inf) -> OverflowError
    max(None, 0.0) -> TypeError

The rail already defends this way elsewhere (`_point_sample_bids`, `_update_peaks`
both reject None / non-finite / non-positive before arithmetic). These pin the
same stance for the quantity side: fail to "no exit", never raise.
"""

from __future__ import annotations

import unittest

from alphalens_pipeline.brokers.automanager.live_exit_engine import (
    plan_tranche_exits,
    tranche_tag,
)
from broker_contract.sizing import TpTranchePlan


def _tr(index: int, target: float, frac: float) -> TpTranchePlan:
    return TpTranchePlan(
        tranche_index=index,
        target_price=target,
        tranche_frac=frac,
        r_multiple=1.0,
        tag=tranche_tag(index),
    )


_LADDER = (_tr(0, 16.0, 1.0),)


class TestPlanTrancheExitsMalformedOwned(unittest.TestCase):
    """`owned` comes from a live broker read; the price side is already guarded
    by `_is_decidable_price`, the quantity side was not."""

    def _plan(self, owned: object) -> list:
        return plan_tranche_exits(
            price=20.0,  # well past the only target, so a healthy owned WOULD fire
            tp_tranches=_LADDER,
            reference_qty=100,
            owned=owned,  # type: ignore[arg-type]
            already_fired=frozenset(),
        )

    def test_healthy_owned_still_fires(self) -> None:
        # Guard against a fix that refuses everything: the same inputs with a
        # real quantity must still produce the exit.
        out = self._plan(100.0)
        self.assertEqual([e.tag for e in out], ["tp1"])

    def test_nan_owned_plans_nothing_instead_of_raising(self) -> None:
        self.assertEqual(self._plan(float("nan")), [])

    def test_inf_owned_plans_nothing_instead_of_raising(self) -> None:
        self.assertEqual(self._plan(float("inf")), [])

    def test_none_owned_plans_nothing_instead_of_raising(self) -> None:
        self.assertEqual(self._plan(None), [])

    def test_negative_owned_plans_nothing(self) -> None:
        # A short/degenerate row is not something this long-only rail sells.
        self.assertEqual(self._plan(-5.0), [])


if __name__ == "__main__":
    unittest.main()
