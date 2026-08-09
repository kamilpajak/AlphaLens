"""Hermetic tests for the daemon-side ``PeakTracker`` state (trailing-ATR
Task 3): the ``LoopDeps.peak_tracker`` mutable dict and the ``_update_peaks``
helper that fetches the live feed and ratchets it monotonically upward.

NOT covered here (out of scope for Task 3): wiring ``_update_peaks`` into any
tick pass (Task 4) and the pure ``trailing_atr`` policy / ``_maybe_trail``
reconcile arm (Tasks 1-2, already shipped) that will eventually consume the
``peak``/``last_price`` maps this helper returns."""

from __future__ import annotations

import datetime as dt
import math
import unittest
from pathlib import Path

from alphalens_pipeline.brokers.automanager import control_loop as cl
from broker_contract.contract import InstrumentRef, Position
from broker_contract.price_feed import PricePoint


def _mk_pos(*, uic: int, qty: float = 100.0, ticker: str = "KO") -> Position:
    return Position(
        instrument=InstrumentRef(
            ticker=ticker,
            exchange_mic="XNYS",
            asset_type="Stock",
            broker_instrument_id=str(uic),
            broker_symbol=f"{ticker}:xnys",
        ),
        quantity=qty,
        avg_price=15.0,
        market_value=None,
        unrealized_pnl=None,
        position_id=f"pos-{uic}",
    )


def _point(uic: int, price: float) -> PricePoint:
    return PricePoint(
        uic=uic,
        bid=price,
        ask=price,
        event_time=dt.datetime(2026, 8, 9, tzinfo=dt.UTC),
        received_at=dt.datetime(2026, 8, 9, tzinfo=dt.UTC),
        source="test",
    )


class _FakeFeed:
    """One tick's worth of scripted prices. ``None`` means "no trustworthy
    quote" (the stream-health veto), mirroring _NullPriceFeed / a stale
    SaxoLivePriceFeed quote."""

    def __init__(self, prices: dict[int, float | None]) -> None:
        self._prices = prices

    def latest(self, uic: int) -> PricePoint | None:
        px = self._prices.get(uic)
        return None if px is None else _point(uic, px)


class _ScriptedFeedFactory:
    """Returns one ``_FakeFeed`` per call, popped off a per-tick script in
    order -- lets a test drive ``_update_peaks`` across several simulated
    ticks with different prices each time."""

    def __init__(self, ticks: list[dict[int, float | None]]) -> None:
        self._ticks = list(ticks)

    def __call__(self, _uic_to_instrument: object) -> _FakeFeed:
        return _FakeFeed(self._ticks.pop(0))


def _deps(*, live_exits_feed_factory: object) -> cl.LoopDeps:
    return cl.LoopDeps(
        broker=object(),  # type: ignore[arg-type]
        kill_file=Path("/nonexistent/KILL"),
        ensure_alive=lambda: type("C", (), {"alive": True, "reason": None})(),  # noqa: PLW0108
        iter_picks=lambda: iter(()),
        place_pick=lambda pick: False,
        read_records=list,
        verdicts_fn=lambda records, broker: [],
        build_position_view=lambda broker, records: cl.BrokerView(working_children={}),
        build_protection_view=cl.build_protection_view,
        execute_protection=lambda action, kill, report: None,
        sweep_orphans_fn=lambda broker: [],
        alert=lambda msg: None,
        alert_throttled=lambda msg, reason: True,
        live_exits_feed_factory=live_exits_feed_factory,  # type: ignore[arg-type]
    )


class TestUpdatePeaksMonotone(unittest.TestCase):
    def test_peak_stays_at_the_high_after_a_rise_then_a_fall(self) -> None:
        pos = _mk_pos(uic=100)
        deps = _deps(
            live_exits_feed_factory=_ScriptedFeedFactory([{100: 10.0}, {100: 12.0}, {100: 11.0}])
        )

        peak1, last1 = cl._update_peaks(deps, [pos])
        peak2, last2 = cl._update_peaks(deps, [pos])
        peak3, last3 = cl._update_peaks(deps, [pos])

        self.assertEqual(peak1[100], 10.0)
        self.assertEqual(last1[100], 10.0)
        self.assertEqual(peak2[100], 12.0)
        self.assertEqual(last2[100], 12.0)
        # Price fell to 11.0 but the high-water peak must not fall with it.
        self.assertEqual(peak3[100], 12.0)
        self.assertEqual(last3[100], 11.0)
        self.assertEqual(deps.peak_tracker[100], 12.0)


class TestUpdatePeaksNonePoint(unittest.TestCase):
    def test_none_point_leaves_peak_unchanged_and_omits_the_uic(self) -> None:
        pos = _mk_pos(uic=200)
        deps = _deps(live_exits_feed_factory=_ScriptedFeedFactory([{200: 15.0}, {200: None}]))

        cl._update_peaks(deps, [pos])
        self.assertEqual(deps.peak_tracker[200], 15.0)

        peak, last = cl._update_peaks(deps, [pos])

        self.assertNotIn(200, peak)
        self.assertNotIn(200, last)
        self.assertEqual(deps.peak_tracker[200], 15.0)  # untouched


class TestUpdatePeaksNoneBid(unittest.TestCase):
    def test_none_bid_is_vetoed_not_raised(self) -> None:
        """``PricePoint.bid`` is annotated ``float``, but nothing at runtime
        stops a feed from constructing one with ``bid=None`` (the same "must
        not trust the caller" doubt ``is_fresh`` already guards against, and
        the exact pattern ``test_saxo_live_price_feed.py`` exercises for the
        wired feed's own upstream quote). Before the fix, ``math.isfinite(None)``
        raised ``TypeError`` here instead of vetoing -- a crash on a feed
        producing a naked position, rather than the intended "doubt becomes a
        veto" contract this helper documents for non-finite/non-positive
        prices."""
        pos = _mk_pos(uic=500)
        bad_point = PricePoint(
            uic=500,
            bid=None,  # type: ignore[arg-type]
            ask=25.0,
            event_time=dt.datetime(2026, 8, 9, tzinfo=dt.UTC),
            received_at=dt.datetime(2026, 8, 9, tzinfo=dt.UTC),
            source="test",
        )

        class _NoneBidFeed:
            def latest(self, uic: int) -> PricePoint | None:
                return bad_point if uic == 500 else None

        deps = _deps(live_exits_feed_factory=lambda _uic_to_instrument: _NoneBidFeed())

        peak, last = cl._update_peaks(deps, [pos])  # must not raise

        self.assertNotIn(500, peak)
        self.assertNotIn(500, last)
        self.assertEqual(deps.peak_tracker, {})


class TestUpdatePeaksNonPositivePrice(unittest.TestCase):
    def test_non_finite_or_non_positive_price_is_also_a_veto(self) -> None:
        pos = _mk_pos(uic=300)
        deps = _deps(
            live_exits_feed_factory=_ScriptedFeedFactory(
                [{300: 20.0}, {300: 0.0}, {300: math.nan}, {300: -5.0}]
            )
        )

        cl._update_peaks(deps, [pos])
        self.assertEqual(deps.peak_tracker[300], 20.0)

        for _ in range(3):
            peak, last = cl._update_peaks(deps, [pos])
            self.assertNotIn(300, peak)
            self.assertNotIn(300, last)
            self.assertEqual(deps.peak_tracker[300], 20.0)


class TestUpdatePeaksRestartReset(unittest.TestCase):
    def test_fresh_tracker_seeds_peak_to_the_first_observed_price(self) -> None:
        pos = _mk_pos(uic=400)
        deps = _deps(live_exits_feed_factory=_ScriptedFeedFactory([{400: 7.5}]))

        # A fresh LoopDeps carries an empty peak_tracker (default_factory) --
        # no explicit "reset" call is needed; this is the restart behaviour.
        self.assertEqual(deps.peak_tracker, {})

        peak, last = cl._update_peaks(deps, [pos])

        self.assertEqual(peak[400], 7.5)
        self.assertEqual(last[400], 7.5)
        self.assertEqual(deps.peak_tracker[400], 7.5)


class TestUpdatePeaksPerUicFaultIsolation(unittest.TestCase):
    def test_one_raising_uic_does_not_abort_the_others_or_leak_a_partial_state(
        self,
    ) -> None:
        """A ``feed.latest(uic)`` that raises mid-loop for uic 2 must not
        propagate (the caller, ``_fetch_protection_peaks``, has its own
        broader boundary, but ``_update_peaks`` itself must be per-uic
        fault-isolated so ONE bad uic degrades only that uic, not the whole
        tick) and must not leave ``deps.peak_tracker`` mutated for uic 2 --
        the fix builds into a local ``new_peaks`` copy and only commits after
        the loop completes."""
        pos1 = _mk_pos(uic=1, ticker="AAA")
        pos2 = _mk_pos(uic=2, ticker="BBB")
        pos3 = _mk_pos(uic=3, ticker="CCC")

        class _RaisingOnUic2Feed:
            def latest(self, uic: int) -> PricePoint | None:
                if uic == 2:
                    raise RuntimeError("feed blew up for uic 2")
                return _point(uic, {1: 10.0, 3: 30.0}[uic])

        deps = _deps(live_exits_feed_factory=lambda _uic_to_instrument: _RaisingOnUic2Feed())

        peak, last = cl._update_peaks(deps, [pos1, pos2, pos3])  # must not raise

        self.assertEqual(peak[1], 10.0)
        self.assertEqual(last[1], 10.0)
        self.assertEqual(peak[3], 30.0)
        self.assertEqual(last[3], 30.0)
        self.assertNotIn(2, peak)
        self.assertNotIn(2, last)
        self.assertEqual(dict(deps.peak_tracker), {1: 10.0, 3: 30.0})


class TestUpdatePeaksPruning(unittest.TestCase):
    def test_a_uic_no_longer_in_long_positions_is_pruned(self) -> None:
        pos1 = _mk_pos(uic=1, ticker="AAA")
        pos2 = _mk_pos(uic=2, ticker="BBB")
        deps = _deps(live_exits_feed_factory=_ScriptedFeedFactory([{1: 5.0, 2: 6.0}, {1: 5.5}]))

        cl._update_peaks(deps, [pos1, pos2])
        self.assertEqual(set(deps.peak_tracker), {1, 2})

        # uic 2's position is gone this tick (closed) -- its stale peak must
        # not survive so a later re-pick of uic 2 cannot inherit it.
        peak, last = cl._update_peaks(deps, [pos1])

        self.assertEqual(set(deps.peak_tracker), {1})
        self.assertNotIn(2, peak)
        self.assertNotIn(2, last)


if __name__ == "__main__":
    unittest.main()
