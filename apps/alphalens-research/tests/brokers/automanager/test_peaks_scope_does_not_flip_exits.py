"""#1587: the trailing peak update must not flip the exits slice of the shared
price-stream subscription.

One tick builds two price feeds off the same stream: the live-exits pass
subscribes EVERY long position, then the protection pass subscribes the
positions whose plan declares a trail (#1236). While both wrote the one
``exits`` scope, the slice alternated between "all longs" and "trailing longs"
every tick. ``ensure_subscribed`` forgets the quotes of uics that leave the
union, so the non-trailing positions lost their quote at the end of every tick
and the next exits pass read them priceless (SIM, 2026-09-25: ``no_price=7`` on
every tick for exactly the seven non-trailing positions).

These tests drive the REAL ``SaxoPriceStream`` through the production
``_default_live_exits_feed_factory``. A recording fake would encode the union
rules this test exists to check.

The exits side is driven through ``_build_live_exits_feed`` with every long
position, which is the call ``_run_live_exits_pass`` makes on every tick
(pinned by ``test_live_exits_pass.TestLiveExitsScopeMaintenance``).
"""

from __future__ import annotations

import datetime as dt
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from alphalens_pipeline.brokers.automanager import control_loop as cl
from alphalens_pipeline.data.alt_data.saxo_price_stream import SaxoPriceStream
from broker_contract.contract import InstrumentRef, Position
from broker_contract.trade_intent.schema import TrailingStop

from tests.brokers.test_trail_wiring import _Broker, _deps, _stop_leg

_TRAILING_UIC = 101
_PLAIN_UIC = 202
_LIVE_UIC = {_TRAILING_UIC: 9101, _PLAIN_UIC: 9202}
_TICKER = {_TRAILING_UIC: "TTT", _PLAIN_UIC: "NNN"}
_T0 = dt.datetime(2026, 9, 25, 15, 0, tzinfo=dt.UTC)
_DECLARED_TRAIL = TrailingStop(arm_trigger_r=0.5, trail_frac=0.6)


class _Resolver:
    """Stand-in for ``SaxoMarketDataClient``: only ``resolve_uic`` is reached."""

    def resolve_uic(self, ticker: str, *, exchange_mic: str) -> int | None:
        by_ticker = {_TICKER[uic]: live for uic, live in _LIVE_UIC.items()}
        return by_ticker.get(ticker.upper())


class _TokenProvider:
    """Stand-in for ``LiveTokenProvider``; the reader thread is never started."""


def _position(uic: int) -> Position:
    return Position(
        instrument=InstrumentRef(
            ticker=_TICKER[uic],
            exchange_mic="XNYS",
            asset_type="Stock",
            broker_instrument_id=str(uic),
            broker_symbol=f"{_TICKER[uic]}:xnys",
        ),
        quantity=10.0,
        avg_price=100.0,
        market_value=None,
        unrealized_pnl=None,
        position_id=f"pos-{uic}",
    )


def _seed_plan(journal: Path, uic: int, *, reaction: object) -> None:
    with mock.patch.object(cl, "_standalone_stop_journal_path", lambda: journal):
        cl._append_standalone_stop_journal(
            cl._build_planned_line(
                entry_crid=f"crid-{uic}",
                uic=uic,
                side="SELL",
                stop_price=90.0,
                take_profit=None,
                tier_index=0,
                reaction=reaction,  # type: ignore[arg-type]
            )
        )


def _seed_quote(stream: SaxoPriceStream, live_uic: int) -> None:
    stream.cache.apply(
        {
            "Uic": live_uic,
            "LastUpdated": "2026-09-25T15:00:00Z",
            "Quote": {"Bid": 100.0, "Ask": 100.05, "DelayedByMinutes": 0},
        },
        received_at=_T0,
    )


class _TwoPositionTick(unittest.TestCase):
    """One trailing long and one non-trailing long, both covered by a stop."""

    def setUp(self) -> None:
        self.stream = SaxoPriceStream(_Resolver(), _TokenProvider())  # type: ignore[arg-type]
        positions = [_position(_TRAILING_UIC), _position(_PLAIN_UIC)]
        self.broker = _Broker(
            positions=positions,
            sells=[
                _stop_leg(10.0, uic=_TRAILING_UIC),
                _stop_leg(10.0, uic=_PLAIN_UIC),
            ],
            by_uic=dict(zip((_TRAILING_UIC, _PLAIN_UIC), positions, strict=True)),
        )
        self.deps = _deps(self.broker, feed_factory=None, sink=[])
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        for patcher in (
            mock.patch.object(cl, "_quote_source", lambda: self.stream),
            mock.patch.dict(os.environ, {cl._SAXO_LIVE_PRICES_ENV: "1"}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _journal(self, name: str, *, trailing: bool) -> Path:
        journal = self.tmp / name
        _seed_plan(journal, _TRAILING_UIC, reaction=_DECLARED_TRAIL if trailing else None)
        _seed_plan(journal, _PLAIN_UIC, reaction=None)
        return journal

    def _tick(self, journal: Path) -> None:
        all_longs = {uic: (_TICKER[uic], "XNYS") for uic in (_TRAILING_UIC, _PLAIN_UIC)}
        cl._build_live_exits_feed(self.deps, all_longs, cl.TickReport())
        with mock.patch.object(cl, "_standalone_stop_journal_path", lambda: journal):
            cl._run_protection_pass(self.deps, [], False, cl.TickReport())


class TestPeakUpdateDoesNotUnsubscribeNonTrailingPositions(_TwoPositionTick):
    def test_the_non_trailing_quote_survives_the_protection_pass(self) -> None:
        journal = self._journal("trailing.jsonl", trailing=True)
        self._tick(journal)
        for live_uic in _LIVE_UIC.values():
            _seed_quote(self.stream, live_uic)

        self._tick(journal)

        self.assertIsNotNone(self.stream.cache.get(_LIVE_UIC[_PLAIN_UIC]))

    def test_the_wire_union_holds_both_positions_after_the_tick(self) -> None:
        journal = self._journal("trailing.jsonl", trailing=True)

        self._tick(journal)

        self.assertEqual(self.stream._desired_uics(), set(_LIVE_UIC.values()))

    def test_a_repeated_tick_does_not_recreate_the_subscription(self) -> None:
        journal = self._journal("trailing.jsonl", trailing=True)
        self._tick(journal)
        self.stream._sub_dirty.clear()

        self._tick(journal)

        self.assertFalse(self.stream._sub_dirty.is_set())


class TestNonTrailingTickKeepsHeldTrailingPosition(_TwoPositionTick):
    """A tick on which no plan declares a trail releases the peaks scope. The
    position that trailed until then is still long, so the exits scope keeps it
    in the union and its quote is not forgotten."""

    def test_the_formerly_trailing_quote_survives_the_release(self) -> None:
        self._tick(self._journal("trailing.jsonl", trailing=True))
        for live_uic in _LIVE_UIC.values():
            _seed_quote(self.stream, live_uic)

        self._tick(self._journal("plain.jsonl", trailing=False))

        self.assertIsNotNone(self.stream.cache.get(_LIVE_UIC[_TRAILING_UIC]))
        self.assertEqual(self.stream._desired_uics(), set(_LIVE_UIC.values()))


if __name__ == "__main__":
    unittest.main()
