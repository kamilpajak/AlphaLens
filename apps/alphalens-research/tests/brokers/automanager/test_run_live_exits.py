from __future__ import annotations

import datetime as dt
import unittest
from unittest import mock

from alphalens_pipeline.brokers.automanager import control_loop as cl
from alphalens_pipeline.brokers.automanager.live_exit_engine import (
    LiveExitBroker,
    ManagedExit,
    plan_tranche_exits,
    run_live_exits,
)
from alphalens_pipeline.brokers.execution import RAIL_LATTICE
from broker_contract.price_feed import PricePoint
from broker_contract.sizing import TpTranchePlan

from tests.brokers.automanager.acceptance.fake_broker import FakeBroker
from tests.incident_1112_fixture import (
    SMG_ACTUAL_FILL,
    SMG_EXIT_DECISION_BID,
    SMG_GEOMETRY_TP,
    SMG_TP_TRANCHES,
)

_DECISION_EVENT_TIME = dt.datetime(2026, 8, 5, tzinfo=dt.UTC)


class _FakeFeed:
    def __init__(self, prices, *, bid=None, ask=None, source="test"):
        self._p = prices  # {uic: price|None}
        self._bid = bid
        self._ask = ask
        self._source = source

    def latest(self, uic):
        px = self._p.get(uic)
        if px is None:
            return None
        return PricePoint(
            uic=uic,
            bid=self._bid if self._bid is not None else px,
            ask=self._ask if self._ask is not None else px,
            event_time=_DECISION_EVENT_TIME,
            received_at=_DECISION_EVENT_TIME,
            source=self._source,
        )


def _tr(index, target, pct):
    return TpTranchePlan(
        tranche_index=index,
        target_price=target,
        tranche_frac=pct,
        r_multiple=1.0,
        tag=f"tp{index + 1}",
    )


class TestRunLiveExits(unittest.TestCase):
    def _mk(self, price):
        b = FakeBroker()
        uic = b.uic_of("KO")
        b.set_position("KO", 100, avg_price=15.0)
        b.add_resting_sell("KO", 100, 13.0, order_type="StopIfTraded")
        feed = _FakeFeed({uic: price})
        managed = [
            ManagedExit(
                uic=uic,
                tp_tranches=(_tr(0, 16.0, 0.5), _tr(1, 18.0, 0.3)),
                reference_qty=100,
                stop_price=13.0,
                already_fired=frozenset(),
            )
        ]
        return b, uic, feed, managed

    def test_touch_fires_tranche_and_shrinks_sl(self):
        b, uic, feed, managed = self._mk(price=16.5)
        n = run_live_exits(b, feed, managed, lattice=RAIL_LATTICE)
        self.assertEqual(len(n), 1)
        self.assertEqual(b.get_positions_by_uic(uic).quantity, 50.0)

    def test_stale_price_vetoes_all_fires(self):
        b, uic, feed, managed = self._mk(price=None)  # feed.latest -> None
        n = run_live_exits(b, feed, managed, lattice=RAIL_LATTICE)
        self.assertEqual(n, [])
        self.assertEqual(b.get_positions_by_uic(uic).quantity, 100.0)

    def test_gap_through_fires_both_and_sl_tracks_remaining_owned(self):
        # price crosses tp1(16, 50%) AND tp2(18, 30%) of ref 100 in ONE pass.
        # Guards the batch bug: the 2nd amend must use LIVE owned, not a stale
        # captured sl_leg.amount (which would set the SL to 100-30=70, over-hedged).
        b, uic, feed, managed = self._mk(price=18.5)
        records: list[dict] = []
        with mock.patch.object(cl, "_append_standalone_stop_journal", side_effect=records.append):
            n = run_live_exits(b, feed, managed, lattice=RAIL_LATTICE)
        self.assertEqual(len(n), 2)
        self.assertEqual(b.get_positions_by_uic(uic).quantity, 20.0)  # sold 50 + 30
        sl_now = next(o for o in b.list_working_sell_orders() if o.order_type == "StopIfTraded")
        self.assertEqual(sl_now.amount, 20.0)  # SL tracks remaining owned, not stale 70
        # Each tranche carries its OWN market-SELL join key (non-None, distinct) —
        # guards against a shared/stale sell_order_id leaking across the batch.
        sell_ids = [
            r["telemetry"]["sell_order_id"] for r in records if r.get("kind") == "tranche_fired"
        ]
        self.assertEqual(len(sell_ids), 2)
        self.assertTrue(all(sell_ids))
        self.assertEqual(len(set(sell_ids)), 2)

    def test_fire_stamps_decision_telemetry_from_the_pricepoint(self):
        # (test c) A fire journals decision-side telemetry sourced from the
        # in-scope PricePoint (the BID drives the sell decision) and the
        # TrancheExit. Capture the journal line via the shared append seam.
        b = FakeBroker()
        uic = b.uic_of("KO")
        b.set_position("KO", 100, avg_price=15.0)
        b.add_resting_sell("KO", 100, 13.0, order_type="StopIfTraded")
        feed = _FakeFeed({uic: 16.5}, bid=16.5, ask=16.7, source="saxo-live-l1")
        managed = [
            ManagedExit(
                uic=uic,
                tp_tranches=(_tr(0, 16.0, 0.5),),
                reference_qty=100,
                stop_price=13.0,
                already_fired=frozenset(),
            )
        ]
        records: list[dict] = []
        with mock.patch.object(cl, "_append_standalone_stop_journal", side_effect=records.append):
            n = run_live_exits(b, feed, managed, lattice=RAIL_LATTICE)
        self.assertEqual(len(n), 1)
        fired = [r for r in records if r.get("kind") == "tranche_fired"]
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0]["uic"], uic)
        self.assertEqual(fired[0]["tag"], "tp1")
        # (test d) the market-SELL order id FakeBroker.place_market_order returns
        # (add_resting_sell bumps seq to 1 -> "resting-1"; the sell is seq 2).
        self.assertEqual(
            fired[0]["telemetry"],
            {
                "decision_bid": 16.5,
                "decision_ask": 16.7,
                "decision_mid": 16.6,
                "spread_abs": 16.7 - 16.5,
                "target_price": 16.0,
                "qty": 50,
                "event_time": _DECISION_EVENT_TIME.isoformat(),
                "source": "saxo-live-l1",
                "sell_order_id": "mkt-2",
            },
        )

    def test_the_engine_requirement_set_is_stated_and_fake_broker_satisfies_it(self):
        """#1141: run_live_exits no longer getattr-probes per uic — its broker
        parameter IS the contract (LiveExitBroker) and the CALLER
        (control_loop._run_live_exits_pass) isinstance-narrows before invoking,
        so a broker missing a capability skips the whole pass with an alert
        (tests/brokers/automanager/test_live_exits_pass.py::
        TestLiveExitsPassCapabilityGuard) instead of warning per uic here.
        This pin proves the conformance fake satisfies the stated set and that
        the runtime check genuinely discriminates (negative control)."""
        self.assertIsInstance(FakeBroker(), LiveExitBroker)

        # Negative control per MEMBER: the combined runtime check must reject a
        # broker missing ANY of the six, not merely the one a single hand-built
        # stub happens to omit — this pins the union semantics of the composed
        # protocol as executable fact.
        members = (
            "amend_stop_amount",
            "place_market_order",
            "get_long_positions",
            "get_positions_by_uic",
            "cancel_order",
            "list_working_sell_orders",
        )
        for missing in members:
            with self.subTest(missing=missing):
                stub = type(
                    "_AlmostCapable",
                    (),
                    {
                        "name": "almost",
                        **{m: (lambda self, *a, **k: None) for m in members if m != missing},
                    },
                )()
                self.assertNotIsInstance(stub, LiveExitBroker)


class TestRunLiveExitsCostGate(unittest.TestCase):
    """Issue #1112 step 2, wired end to end: ``run_live_exits`` reads the
    realised entry off the Position it already fetches and threads it into the
    decision, so a refused exit places NO market order and touches NO stop."""

    def _mk_smg(self):
        b = FakeBroker()
        uic = b.uic_of("KO")
        b.set_position("KO", 1, avg_price=SMG_ACTUAL_FILL)
        b.add_resting_sell("KO", 1, 49.412, order_type="StopIfTraded")
        feed = _FakeFeed({uic: SMG_EXIT_DECISION_BID})
        managed = [
            ManagedExit(
                uic=uic,
                tp_tranches=(
                    TpTranchePlan(
                        tranche_index=0,
                        target_price=SMG_GEOMETRY_TP,
                        tranche_frac=1.0,
                        r_multiple=0.0,
                        tag="geometry",
                    ),
                ),
                reference_qty=1,
                stop_price=49.412,
                already_fired=frozenset(),
            )
        ]
        return b, uic, feed, managed

    def test_refused_exit_sells_nothing_and_leaves_the_stop_alone(self):
        b, uic, feed, managed = self._mk_smg()
        records: list[dict] = []
        with mock.patch.object(cl, "_append_standalone_stop_journal", side_effect=records.append):
            n = run_live_exits(b, feed, managed, lattice=RAIL_LATTICE)
        self.assertEqual(n, [])
        self.assertEqual(b.get_positions_by_uic(uic).quantity, 1.0, "position untouched")
        sl_now = next(o for o in b.list_working_sell_orders() if o.order_type == "StopIfTraded")
        self.assertEqual(sl_now.amount, 1.0, "the disaster stop is neither shrunk nor cancelled")
        self.assertEqual([r for r in records if r.get("kind") == "tranche_fired"], [])

    def test_a_target_beyond_cost_plus_buffer_still_fires(self):
        b, uic, feed, managed = self._mk_smg()
        far = managed[0]
        managed = [
            ManagedExit(
                uic=far.uic,
                tp_tranches=(
                    TpTranchePlan(
                        tranche_index=0,
                        target_price=SMG_TP_TRANCHES[0],
                        tranche_frac=1.0,
                        r_multiple=0.0,
                        tag="geometry",
                    ),
                ),
                reference_qty=1,
                stop_price=49.412,
                already_fired=frozenset(),
            )
        ]
        feed = _FakeFeed({uic: SMG_TP_TRANCHES[0] + 0.05})
        with mock.patch.object(cl, "_append_standalone_stop_journal"):
            n = run_live_exits(b, feed, managed, lattice=RAIL_LATTICE)
        self.assertEqual(len(n), 1)
        self.assertEqual(b.get_positions_by_uic(uic).quantity, 0.0)


class TestDegeneratePriceFiresNothing(unittest.TestCase):
    """#1116 round 2, point 3: a non-finite or non-positive price must decide
    NOTHING.

    The motivating case is not a crash, it is a WRONG ACTION. Measured on this
    branch before the guard:

        price=inf, realised entry known   -> fires tp1, tp2, tp3
        price=inf, realised entry unknown -> fires tp1, tp2, tp3
        price=NaN, realised entry unknown -> fires tp1, tp2, tp3
        price=NaN, realised entry known   -> fires nothing

    ``price >= target`` is true for infinity, so every tranche is touched at
    once and the cost gate lets them all through. The NaN case only came out
    safe because ``_exit_clears_cost`` happened to refuse it, and that gate
    fails OPEN whenever the realised entry is unknown.

    This is defence in depth, NOT a reachable live defect today. Both
    production feeds already withhold such a quote, by different mechanisms:
    ``yfinance_price_feed`` checks ``isfinite`` / ``> 0`` before building the
    point, ``saxo_live_price_feed`` returns its point only when
    ``price_feed.is_fresh`` passes, which vetoes a non-finite, non-positive or
    crossed side. Neither rule belongs to the exit engine, so it states its own.
    """

    _DEGENERATE_BIDS = (float("inf"), float("-inf"), float("nan"), 0.0, -1.0)

    def _managed(self, uic: int) -> list[ManagedExit]:
        return [
            ManagedExit(
                uic=uic,
                tp_tranches=(_tr(0, 16.0, 0.5), _tr(1, 18.0, 0.3)),
                reference_qty=100,
                stop_price=13.0,
                already_fired=frozenset(),
            )
        ]

    def _run(self, bid: float, *, avg_price: float) -> tuple[int, float]:
        b = FakeBroker()
        uic = b.uic_of("KO")
        b.set_position("KO", 100, avg_price=avg_price)
        b.add_resting_sell("KO", 100, 13.0, order_type="StopIfTraded")
        feed = _FakeFeed({uic: 16.5}, bid=bid, ask=16.5)
        with mock.patch.object(cl, "_append_standalone_stop_journal"):
            fired = run_live_exits(b, feed, self._managed(uic), lattice=RAIL_LATTICE)
        return fired, b.get_positions_by_uic(uic).quantity

    def test_no_degenerate_bid_fires_a_tranche(self) -> None:
        for bid in self._DEGENERATE_BIDS:
            with self.subTest(bid=bid):
                fired, owned = self._run(bid, avg_price=15.0)
                self.assertEqual(fired, [])
                self.assertEqual(owned, 100.0, "nothing may be sold on a degenerate price")

    def test_no_degenerate_bid_fires_when_the_realised_entry_is_unknown(self) -> None:
        # The cost gate fails open on an unknown realised entry, so it cannot be
        # what protects this path.
        for bid in self._DEGENERATE_BIDS:
            with self.subTest(bid=bid):
                fired, owned = self._run(bid, avg_price=0.0)
                self.assertEqual(fired, [])
                self.assertEqual(owned, 100.0)

    def test_a_finite_positive_bid_still_fires(self) -> None:
        fired, owned = self._run(16.5, avg_price=15.0)
        self.assertEqual(len(fired), 1)
        self.assertEqual(owned, 50.0)


class TestPlanTrancheExitsRejectsDegeneratePrices(unittest.TestCase):
    """The same guard at the pure-decision seam, so a future caller that does not
    go through :func:`run_live_exits` cannot reintroduce it."""

    _TRANCHES = (_tr(0, 16.0, 0.5), _tr(1, 18.0, 0.3))

    def test_degenerate_prices_plan_no_exit(self) -> None:
        for price in (float("inf"), float("-inf"), float("nan"), 0.0, -1.0):
            with self.subTest(price=price):
                self.assertEqual(
                    plan_tranche_exits(
                        price=price,
                        tp_tranches=self._TRANCHES,
                        reference_qty=100,
                        owned=100,
                        already_fired=frozenset(),
                        lattice=RAIL_LATTICE,
                    ),
                    [],
                )

    def test_a_finite_positive_price_still_plans(self) -> None:
        planned = plan_tranche_exits(
            price=18.5,
            tp_tranches=self._TRANCHES,
            reference_qty=100,
            owned=100,
            already_fired=frozenset(),
            lattice=RAIL_LATTICE,
        )
        self.assertEqual([e.tag for e in planned], ["tp1", "tp2"])


if __name__ == "__main__":
    unittest.main()
