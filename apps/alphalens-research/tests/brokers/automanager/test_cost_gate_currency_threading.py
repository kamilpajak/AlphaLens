"""The #1112 exit-side cost gates learn the instrument currency (#1238 PR 3).

Write-time the daemon knows both currencies (the resolved instrument and the
account); the gates run later from journal state only. So the currency pair is
STAMPED on the ``watch_open`` and ``tranche_plan`` lines at write time, folded
back per uic, and turned into :class:`CostGateFacts` at the gate. A legacy line
without the stamps keeps the conservative pre-#1238 constants (over-refuse,
never under-charge).
"""

from __future__ import annotations

import unittest
from typing import Any
from unittest import mock

from alphalens_pipeline.brokers.automanager import control_loop as cl
from alphalens_pipeline.brokers.automanager import entry_trails, live_exit_engine
from alphalens_pipeline.brokers.automanager.costs import CostGateFacts, cost_gate_facts
from broker_contract.sizing import TpTranchePlan


class _Instrument:
    exchange_mic = "XNYS"
    broker_instrument_id = "307"
    currency = "USD"

    def __init__(self, currency: str = "USD", mic: str = "XNYS") -> None:
        self.currency = currency
        self.exchange_mic = mic


class _Fx:
    def __init__(self, account_currency: str, rate: float = 4.34) -> None:
        self.account_currency = account_currency
        self.rate = rate


class _Intent:
    class meta:
        brief_date = "2026-09-01"


class _Tier:
    def __init__(self, qty: float = 10.0, limit: float = 100.0, index: int = 0) -> None:
        self.qty = qty
        self.limit_price = limit
        self.tier_index = index


class _Plan:
    disaster_stop = 90.0

    def __init__(self) -> None:
        self.entry_tiers = (_Tier(),)
        self.tp_tranches = (
            TpTranchePlan(
                tranche_index=0,
                target_price=110.0,
                tranche_frac=1.0,
                r_multiple=1.0,
                tag="geometry",
            ),
        )


def _captured_watch_lines(*, instrument: _Instrument, fx: _Fx | None) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    with mock.patch.object(entry_trails, "append_entry_trail_line", lines.append):
        cl._open_entry_watches(_Intent(), "KO", instrument, _Plan(), fx, d_bps=50)
    return lines


class TestWatchOpenStampsCurrencies(unittest.TestCase):
    def test_cross_currency_watch_stamps_both(self) -> None:
        lines = _captured_watch_lines(instrument=_Instrument("USD"), fx=_Fx("PLN"))
        self.assertEqual(lines[0]["instrument_currency"], "USD")
        self.assertEqual(lines[0]["sizing_currency"], "PLN")

    def test_same_currency_watch_stamps_the_instrument_currency_twice(self) -> None:
        # fx is None on the same-currency path — the sizing currency IS the
        # instrument currency by construction.
        lines = _captured_watch_lines(instrument=_Instrument("PLN", "XWAR"), fx=None)
        self.assertEqual(lines[0]["instrument_currency"], "PLN")
        self.assertEqual(lines[0]["sizing_currency"], "PLN")


class TestTranchePlanLineStampsCurrencies(unittest.TestCase):
    def test_bracket_path_stamps_both(self) -> None:
        lines: list[dict[str, Any]] = []

        class _Placement:
            disaster_stop_price = 90.0

        with mock.patch.object(cl, "_append_standalone_stop_journal", lines.append):
            cl._journal_tranche_plan(
                plan=_Plan(),
                exit_spec=None,
                placement=_Placement(),
                instrument=_Instrument("PLN", "XWAR"),
                use_geometry=False,
                fx=_Fx("EUR"),
            )
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["instrument_currency"], "PLN")
        self.assertEqual(lines[0]["sizing_currency"], "EUR")


class TestFoldTranchePlanCurrencies(unittest.TestCase):
    _PLAN_LINE = {
        "kind": "tranche_plan",
        "uic": 307,
        "reference_qty": 10.0,
        "stop_price": 90.0,
        "tp_tranches": [
            {
                "tranche_index": 0,
                "target_price": 110.0,
                "tranche_frac": 1.0,
                "r_multiple": 1.0,
                "tag": "geometry",
            }
        ],
    }

    def test_latest_stamp_per_uic_wins_and_retraction_pops(self) -> None:
        stamped = dict(self._PLAN_LINE, instrument_currency="PLN", sizing_currency="PLN")
        out = cl.fold_tranche_plan_currencies([self._PLAN_LINE, stamped])
        self.assertEqual(out[307], ("PLN", "PLN"))

        retracted = cl.fold_tranche_plan_currencies(
            [stamped, {"kind": "tranche_plan_retracted", "uic": 307}]
        )
        self.assertNotIn(307, retracted)

    def test_legacy_line_folds_to_unknown(self) -> None:
        out = cl.fold_tranche_plan_currencies([self._PLAN_LINE])
        self.assertEqual(out[307], (None, None))


class TestBuildManagedExitsDerivesFacts(unittest.TestCase):
    def _managed(self, plan_currencies: dict[int, tuple[str | None, str | None]]) -> Any:
        class _Pos:
            def __init__(self) -> None:
                self.uic = 307
                self.quantity = 10.0

        plan = (
            (
                TpTranchePlan(
                    tranche_index=0,
                    target_price=110.0,
                    tranche_frac=1.0,
                    r_multiple=1.0,
                    tag="geometry",
                ),
            ),
            10.0,
            90.0,
        )
        with mock.patch.object(cl, "_position_uic", lambda _p: 307):
            managed = cl._build_managed_exits(
                long_positions=[_Pos()],
                tranche_plans={307: plan},
                fired={},
                trailed={},
                plan_currencies=plan_currencies,
            )
        return managed[0]

    def test_stamped_currencies_become_real_facts(self) -> None:
        exit_ = self._managed({307: ("PLN", "PLN")})
        self.assertEqual(
            exit_.cost_facts,
            cost_gate_facts(instrument_currency="PLN", sizing_currency="PLN"),
        )

    def test_unstamped_uic_keeps_legacy_facts(self) -> None:
        exit_ = self._managed({})
        self.assertEqual(exit_.cost_facts, CostGateFacts.legacy())


class TestExitClearsCostUsesFacts(unittest.TestCase):
    """Realistic GPW size (231 x 40 ~ 9240 PLN): the PLN-on-PLN bar is
    ~232.71 (24 bps fee + 50 bps edge), the legacy bar ~233.68 (US minimum
    semantics + FX). A bid between the two must fire under the real facts and
    stay refused under the legacy default."""

    _KW = {
        "price": 233.0,
        "target_price": 232.9,
        "qty": 40.0,
        "realised_entry": 231.0,
        "tag": "tp1",
    }

    def test_pln_on_pln_facts_fire_the_tranche(self) -> None:
        facts = cost_gate_facts(instrument_currency="PLN", sizing_currency="PLN")
        self.assertTrue(live_exit_engine._exit_clears_cost(**self._KW, facts=facts))

    def test_legacy_default_still_refuses_it(self) -> None:
        self.assertFalse(live_exit_engine._exit_clears_cost(**self._KW))


class TestInsideExitRegionNoteUsesStamps(unittest.TestCase):
    """Arm-time mirror of the same numbers: with a 231.0 fill estimate and a
    233.0 stamped exit target, the legacy constants refuse the arm (bar
    ~233.68) while the stamped PLN-on-PLN facts clear it (bar ~232.71)."""

    _RECORD_BASE = {"geometry": {"applied": True, "geometry_tp": 233.0}}

    def _note(self, record: dict[str, Any]) -> str | None:
        with mock.patch.object(cl.entry_trail_geometry, "entry_fill_estimate", lambda **_kw: 231.0):
            return cl._inside_exit_region_note(record, 50, 231.0, 230.0, 40.0)

    def test_legacy_record_refuses(self) -> None:
        self.assertIsNotNone(self._note(dict(self._RECORD_BASE)))

    def test_pln_on_pln_stamps_clear_the_arm(self) -> None:
        record = dict(self._RECORD_BASE, instrument_currency="PLN", sizing_currency="PLN")
        self.assertIsNone(self._note(record))


if __name__ == "__main__":
    unittest.main()
