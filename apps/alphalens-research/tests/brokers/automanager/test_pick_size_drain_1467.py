"""The drain side of #1467: a pick states its amount, and the daemon judges it.

Two facts of the document are checked before the day-1 gate, because a pick
armed before the open would otherwise sit deferred for hours and be refused
only after it: the currency must be the account's, and the amount must not
exceed ``ALPHALENS_BROKER_MAX_PICK_NOTIONAL``. Both refusals are terminal.

The journal still holds pre-#1467 lines that size by percent. They must not
decode, must not log a warning every tick, and an unplaced one must be refused
exactly once, with a message that does not invite a double buy of a now tranche
that already went out.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import logging
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from alphalens_pipeline.brokers.automanager import control_loop as cl
from alphalens_pipeline.brokers.automanager import picks
from alphalens_pipeline.brokers.automanager.live_rails import MAX_PICK_NOTIONAL_ENV
from broker_contract.trade_intent.codec import intent_to_jsonable
from broker_contract.trade_intent.schema import PickSize

from tests.brokers.automanager import test_control_loop as _cl_tests
from tests.brokers.automanager.test_control_loop import (
    _acct,
    _frozen_now,
    _pick,
    _RaisingProbe,
    _RecordingBroker,
)

# Imported through the module, never by name: a TestCase class bound at module
# level here would be collected and run a second time from this file.
_DAY1_CASE = _cl_tests.TestPlacePickDay1GapGateIntegration
_BRIEF = _DAY1_CASE._BRIEF
_PREOPEN = _DAY1_CASE._DAY1_OPEN - dt.timedelta(minutes=1)


def _sized(amount: float, currency: str = "USD", ticker: str = "KO") -> Any:
    pick = _pick(ticker, _BRIEF)
    spec = dataclasses.replace(pick.spec, size=PickSize(notional_acct=amount, currency=currency))
    return dataclasses.replace(pick, spec=spec)


class _CountingBroker(_RecordingBroker):
    def __init__(self, currency: str = "USD", **kw: Any) -> None:
        super().__init__(on_account=lambda: _acct(currency), **kw)
        self.account_reads = 0

    def get_account(self) -> Any:
        self.account_reads += 1
        return super().get_account()


def _placer(case: unittest.TestCase, broker: Any, **kw: Any) -> tuple[Any, list, list]:
    """Borrows the day-1 integration placer (every broker seam stubbed)."""
    harness = _DAY1_CASE("test_flag_off_places_even_when_probe_would_defer")
    placer, alerts, refusals = harness._placer(broker, **kw)
    case.addCleanup(harness.doCleanups)
    return placer, alerts, refusals


class TestCurrencyIsJudgedBeforeTheDay1Gate(unittest.TestCase):
    def test_a_wrong_currency_pick_is_refused_pre_open_with_one_alert(self) -> None:
        broker = _CountingBroker(currency="PLN")
        with (
            _frozen_now(_PREOPEN),
            mock.patch.dict("os.environ", {cl._DAY1_GAP_GATE_ENV: "1"}, clear=True),
        ):
            placer, alerts, refusals = _placer(self, broker, day1_gap_price_probe=_RaisingProbe())
            self.assertFalse(placer(_sized(1500.0, "USD")))

        self.assertEqual(broker.placed, [])
        self.assertEqual(len(refusals), 1)
        self.assertEqual([key for _msg, key in alerts], ["pick-currency:KO"])
        self.assertIn("PLN", alerts[0][0])

    def test_the_next_pick_in_the_same_tick_still_reaches_the_gate(self) -> None:
        broker = _CountingBroker(currency="PLN")
        with (
            _frozen_now(_PREOPEN),
            mock.patch.dict("os.environ", {cl._DAY1_GAP_GATE_ENV: "1"}, clear=True),
        ):
            placer, _alerts, refusals = _placer(self, broker, day1_gap_price_probe=_RaisingProbe())
            placer(_sized(1500.0, "USD", ticker="KO"))
            # Pre-open, a correct pick defers at the gate: not refused, not placed.
            self.assertFalse(placer(_sized(1500.0, "PLN", ticker="MU")))

        self.assertEqual(len(refusals), 1, "only the wrong-currency pick is refused")
        self.assertEqual(broker.placed, [])

    def test_a_warm_daemon_defers_without_reading_the_account_again(self) -> None:
        broker = _CountingBroker(currency="USD")
        with (
            _frozen_now(_PREOPEN),
            mock.patch.dict("os.environ", {cl._DAY1_GAP_GATE_ENV: "1"}, clear=True),
        ):
            placer, _alerts, _refusals = _placer(self, broker, day1_gap_price_probe=_RaisingProbe())
            for _tick in range(3):
                self.assertFalse(placer(_sized(1500.0)))

        self.assertEqual(broker.account_reads, 1, "the currency is read once per daemon")

    def test_a_failed_account_read_defers_instead_of_refusing(self) -> None:
        from broker_contract.contract import BrokerError

        def _down() -> Any:
            raise BrokerError("account read down")

        broker = _RecordingBroker(on_account=_down)
        with mock.patch.dict("os.environ", {}, clear=True):
            placer, alerts, refusals = _placer(self, broker)
            self.assertFalse(placer(_sized(1500.0)))

        self.assertEqual((refusals, alerts, broker.placed), ([], [], []))


class TestThePerPickCap(unittest.TestCase):
    def _run(self, env: dict[str, str], amount: float) -> tuple[Any, list, list]:
        broker = _RecordingBroker()
        with mock.patch.dict("os.environ", env, clear=True):
            placer, alerts, refusals = _placer(self, broker)
            placer(_sized(amount))
        return broker, alerts, refusals

    def test_an_amount_above_the_cap_is_refused_and_never_shrunk(self) -> None:
        broker, alerts, refusals = self._run({MAX_PICK_NOTIONAL_ENV: "15000"}, 16000.0)

        self.assertEqual(broker.placed, [])
        self.assertEqual(len(refusals), 1)
        self.assertEqual([key for _msg, key in alerts], ["pick-cap:KO"])
        self.assertIn("never shrunk", alerts[0][0])

    def test_an_amount_at_the_cap_places(self) -> None:
        broker, alerts, refusals = self._run({MAX_PICK_NOTIONAL_ENV: "15000"}, 15000.0)

        self.assertTrue(broker.placed)
        self.assertEqual((alerts, refusals), ([], []))

    def test_no_cap_configured_means_no_cap(self) -> None:
        broker, _alerts, refusals = self._run({}, 1_000_000.0)

        self.assertTrue(broker.placed)
        self.assertEqual(refusals, [])

    def test_a_malformed_cap_refuses_fail_closed(self) -> None:
        broker, alerts, refusals = self._run({MAX_PICK_NOTIONAL_ENV: "15k"}, 100.0)

        self.assertEqual(broker.placed, [])
        self.assertEqual(len(refusals), 1)
        self.assertIn(MAX_PICK_NOTIONAL_ENV, alerts[0][0])


class _JournalCase(unittest.TestCase):
    """A real picks journal in a temp dir; the pre-#1467 lines are raw JSON."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "picks.jsonl"

    def _append(self, record: dict[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")

    def _arm_v2(self, ticker: str) -> None:
        document = intent_to_jsonable(_pick(ticker, _BRIEF))
        del document["spec"]["size"]
        document["spec"]["suggested_size_pct"] = 2.0
        document["spec"]["schema_version"] = "2"
        document["meta"]["schema_version"] = "2"
        self._append({"ticker": ticker, "date": _BRIEF, "status": "armed", "intent": document})


class TestTheJournalReaderSkipsPercentLinesSilently(_JournalCase):
    def test_v2_lines_are_not_yielded_and_log_no_warning(self) -> None:
        self._arm_v2("OLD")
        picks.arm_pick(_sized(1500.0, ticker="NEW"), path=self.path)

        with self.assertNoLogs("alphalens_pipeline.brokers.automanager.picks", logging.WARNING):
            yielded = [p.instrument.ticker for p in picks.iter_picks(path=self.path)]

        self.assertEqual(yielded, ["NEW"])
        legacy = [p.ticker for p in picks.iter_legacy_size_pct_picks(path=self.path)]
        self.assertEqual(legacy, ["OLD"])


class TestTheDrainRefusesUnplacedPercentPicksOnce(_JournalCase):
    def _deps(self, records: list[dict[str, Any]]) -> tuple[Any, list[tuple[str, str]]]:
        alerts: list[tuple[str, str]] = []

        def _alert(message: str, key: str) -> bool:
            alerts.append((message, key))
            return True

        deps = mock.Mock()
        deps.now_entry_scope = None
        deps.read_records = lambda: records
        deps.iter_picks = lambda: picks.iter_picks(path=self.path)
        deps.iter_legacy_size_pct_picks = lambda: picks.iter_legacy_size_pct_picks(path=self.path)
        deps.alert_throttled = _alert
        deps.place_pick = mock.Mock(return_value=False)
        return deps, alerts

    def _tick(self, deps: Any) -> None:
        refused_path = self.path
        real_mark_refused = picks.mark_refused

        def _mark_refused(ticker: str, date: dt.date, reason: str, **kw: Any) -> None:
            real_mark_refused(ticker, date, reason, path=refused_path, **kw)

        with mock.patch.object(picks, "mark_refused", _mark_refused):
            cl._run_placement_drain(deps, cl.TickReport(), enabled=True)

    def test_an_unplaced_v2_pick_is_refused_exactly_once_across_two_ticks(self) -> None:
        self._arm_v2("OLD")
        deps, alerts = self._deps(records=[])

        self._tick(deps)
        self._tick(deps)

        self.assertEqual(len(alerts), 1)
        self.assertIn("Re-arm it with an amount", alerts[0][0])
        # #1552: the only command that took --notional is gone; point at the templates.
        self.assertIn("examples/manual-pick", alerts[0][0])
        self.assertNotIn("--notional", alerts[0][0])
        statuses = [r.status for r in picks.read_pick_fold(path=self.path).records]
        self.assertEqual(statuses, [picks.STATUS_REFUSED])
        deps.place_pick.assert_not_called()

    def test_a_placed_v2_pick_is_left_alone(self) -> None:
        self._arm_v2("OLD")
        placed = {"ticker": "OLD", "trade_date": _BRIEF, "brackets": [{"entry_order_id": "E-1"}]}
        deps, alerts = self._deps(records=[placed])

        self._tick(deps)

        self.assertEqual(alerts, [])
        statuses = [r.status for r in picks.read_pick_fold(path=self.path).records]
        self.assertEqual(statuses, [picks.STATUS_ARMED])

    def test_a_v2_pick_whose_now_tranche_went_out_warns_against_a_full_re_arm(self) -> None:
        self._arm_v2("OLD")
        now_half = {"ticker": "OLD", "trade_date": _BRIEF, "tranche": "now", "brackets": []}
        deps, alerts = self._deps(records=[now_half])

        self._tick(deps)

        self.assertEqual(len(alerts), 1)
        self.assertIn("now tranche is ALREADY placed", alerts[0][0])
        self.assertIn("pullback tiers only", alerts[0][0])
        self.assertIn("examples/manual-pick", alerts[0][0])
        self.assertNotIn("--notional", alerts[0][0])


if __name__ == "__main__":
    unittest.main()
