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
from alphalens_pipeline.brokers.automanager.costs import (
    XAMS_FEE_CARD,
    CostGateFacts,
    cost_gate_facts,
)
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
        self.assertEqual(out[307], ("PLN", "PLN", None))

        retracted = cl.fold_tranche_plan_currencies(
            [stamped, {"kind": "tranche_plan_retracted", "uic": 307}]
        )
        self.assertNotIn(307, retracted)

    def test_legacy_line_folds_to_unknown(self) -> None:
        out = cl.fold_tranche_plan_currencies([self._PLAN_LINE])
        self.assertEqual(out[307], (None, None, None))

    def test_stamped_mic_folds_through(self) -> None:
        # #1271: the venue MIC joins the currency stamps in the same fold.
        stamped = dict(
            self._PLAN_LINE,
            instrument_currency="EUR",
            sizing_currency="EUR",
            exchange_mic="XAMS",
        )
        out = cl.fold_tranche_plan_currencies([stamped])
        self.assertEqual(out[307], ("EUR", "EUR", "XAMS"))


class TestBuildManagedExitsDerivesFacts(unittest.TestCase):
    def _managed(
        self, plan_currencies: dict[int, tuple[str | None, str | None, str | None]]
    ) -> Any:
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
        exit_ = self._managed({307: ("PLN", "PLN", None)})
        self.assertEqual(
            exit_.cost_facts,
            cost_gate_facts(instrument_currency="PLN", sizing_currency="PLN"),
        )

    def test_unstamped_uic_keeps_legacy_facts(self) -> None:
        exit_ = self._managed({})
        self.assertEqual(exit_.cost_facts, CostGateFacts.legacy())

    def test_stamped_mic_discriminates_the_eur_venues(self) -> None:
        # #1271: with the MIC folded through, the exit gate prices Euronext's
        # EUR 2 minimum, not the conservative Xetra EUR 3 the bare-currency
        # fallback would give.
        exit_ = self._managed({307: ("EUR", "EUR", "XAMS")})
        self.assertIs(exit_.cost_facts.card, XAMS_FEE_CARD)


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


class TestMicThreading(unittest.TestCase):
    """#1271: the venue MIC joins the currency stamps end to end, so the cost
    gates can discriminate the two EUR venues (Xetra min EUR 3 vs Euronext
    Amsterdam min EUR 2) instead of pricing every EUR record on the
    conservative currency fallback."""

    def test_bracket_path_stamps_the_mic_on_the_tranche_plan_line(self) -> None:
        lines: list[dict[str, Any]] = []

        class _Placement:
            disaster_stop_price = 90.0

        with mock.patch.object(cl, "_append_standalone_stop_journal", lines.append):
            cl._journal_tranche_plan(
                plan=_Plan(),
                exit_spec=None,
                placement=_Placement(),
                instrument=_Instrument("EUR", "XETR"),
                use_geometry=False,
                fx=None,
            )
        self.assertEqual(lines[0]["exchange_mic"], "XETR")

    def test_note_gate_reads_the_record_mic(self) -> None:
        captured: dict[str, Any] = {}

        def _spy(**kw: Any) -> CostGateFacts:
            captured.update(kw)
            return CostGateFacts.legacy()

        record = {
            "geometry": {"applied": True, "geometry_tp": 233.0},
            "instrument_currency": "EUR",
            "sizing_currency": "EUR",
            "exchange_mic": "XETR",
        }
        with (
            mock.patch.object(cl, "cost_gate_facts", _spy),
            mock.patch.object(cl.entry_trail_geometry, "entry_fill_estimate", lambda **_kw: 231.0),
        ):
            cl._inside_exit_region_note(record, 50, 231.0, 230.0, 40.0)
        self.assertEqual(captured["exchange_mic"], "XETR")

    def test_now_gate_reads_the_instrument_mic(self) -> None:
        captured: dict[str, Any] = {}

        def _spy(**kw: Any) -> CostGateFacts:
            captured.update(kw)
            return CostGateFacts.legacy()

        with mock.patch.object(cl, "cost_gate_facts", _spy):
            cl._now_cost_gate_violation(
                cap=100.0,
                plan=_Plan(),
                exit_spec=None,
                exit_policy=None,
                instrument=_Instrument("EUR", "XAMS"),
                fx=None,
            )
        self.assertEqual(captured.get("exchange_mic"), "XAMS")

    def test_fee_estimator_discriminates_the_eur_venues(self) -> None:
        # _Plan: one 10 x 100 tier, one 100% tranche at 110 -> per-fill
        # ad-valorem 0.80/0.88 EUR, both under either minimum, so the round
        # trip is 2 x the venue minimum on a 1000 EUR gross: Euronext (2+2)
        # -> 40 bps, Xetra (3+3) -> 60 bps. Same inputs, different venue,
        # different number — the discrimination the re-key exists for.
        xams = cl._estimate_round_trip_fee_bps(
            _Plan(), None, instrument_currency="EUR", exchange_mic="XAMS"
        )
        xetr = cl._estimate_round_trip_fee_bps(
            _Plan(), None, instrument_currency="EUR", exchange_mic="XETR"
        )
        self.assertIsNotNone(xams)
        self.assertIsNotNone(xetr)
        self.assertAlmostEqual(xams, 40.0)
        self.assertAlmostEqual(xetr, 60.0)

    def test_fee_floor_prices_the_venue_card(self) -> None:
        # Cap 50 bps sits BETWEEN the two venue estimates above: the Euronext
        # plan clears, the Xetra plan is refused — the floor demonstrably
        # prices the MIC, not just the currency.
        from alphalens_pipeline.brokers.automanager.live_rails import MAX_FEE_BPS_ENV

        with mock.patch.dict("os.environ", {MAX_FEE_BPS_ENV: "50"}):
            self.assertIsNone(
                cl._check_fee_floor(
                    _Plan(), None, ticker="ASML", instrument_currency="EUR", exchange_mic="XAMS"
                )
            )
            self.assertIsNotNone(
                cl._check_fee_floor(
                    _Plan(), None, ticker="RHM", instrument_currency="EUR", exchange_mic="XETR"
                )
            )


class TestBriefPlanArmRefusalUsesStamps(unittest.TestCase):
    """zen review (PR #1241 HIGH): the brief-ladder arm gate — the PRODUCTION
    path under the no-geometry policy — must price the stamped facts too.
    Same numbers as the geometry-note test: fill estimate 231.0, tp1 233.0;
    legacy bar ~233.68 refuses, the PLN-on-PLN bar ~232.71 clears."""

    def _refusal(self, record: dict[str, Any]) -> Any:
        plan = (
            (
                TpTranchePlan(
                    tranche_index=0,
                    target_price=233.0,
                    tranche_frac=1.0,
                    r_multiple=1.0,
                    tag="tp1",
                ),
            ),
            40.0,
            220.0,
        )
        with (
            mock.patch.object(cl, "_governing_plan_lookup", lambda _r: (plan, None)),
            mock.patch.object(cl.entry_trail_geometry, "entry_fill_estimate", lambda **_kw: 231.0),
        ):
            return cl._brief_plan_arm_refusal(record, 50, 231.0, 230.0)

    def test_legacy_record_refuses(self) -> None:
        self.assertIsNotNone(self._refusal({}))

    def test_pln_on_pln_stamps_clear_the_arm(self) -> None:
        self.assertIsNone(self._refusal({"instrument_currency": "PLN", "sizing_currency": "PLN"}))


class TestPlacementEstimatorUsesVenueCard(unittest.TestCase):
    """zen review (PR #1241 MEDIUM): the placement-side per-tier estimate and
    fee floor price the venue card too — a PLN plan pays 0.12% with the
    PLN 10 minimum, not the US shape."""

    def test_pln_plan_prices_the_wse_card(self) -> None:
        class _EstTier:
            qty = 40.0
            limit_price = 231.0

        class _EstPlan:
            entry_tiers = (_EstTier(),)
            tp_tranches = ()

        # gross 9240 PLN; per fill max(10, 0.0012*9240) = 11.088; entry+exit
        # (no tranches -> exit mirrors entry) = 22.176 -> 24.0 bps of gross.
        bps = cl._estimate_round_trip_fee_bps(_EstPlan(), None, instrument_currency="PLN")
        self.assertAlmostEqual(bps, 24.0, places=4)

    def test_usd_plan_is_byte_identical_to_the_old_shape(self) -> None:
        class _EstTier:
            qty = 10.0
            limit_price = 60.0

        class _EstPlan:
            entry_tiers = (_EstTier(),)
            tp_tranches = ()

        # gross 600 USD; per fill max(1, 0.48) = 1; entry+exit = 2 -> 33.33 bps.
        bps = cl._estimate_round_trip_fee_bps(_EstPlan(), None, instrument_currency="USD")
        self.assertAlmostEqual(bps, 2.0 / 600.0 * 10_000.0, places=6)


class TestCurrencyFoldParity(unittest.TestCase):
    """zen review (PR #1241 LOW): the two tranche_plan folds and the journal
    compactor must agree over one mixed input — a malformed line updates
    neither fold, and the currency stamp survives compaction."""

    _GOOD = {
        "kind": "tranche_plan",
        "uic": 307,
        "reference_qty": 10.0,
        "stop_price": 90.0,
        "instrument_currency": "PLN",
        "sizing_currency": "PLN",
        "tp_tranches": [
            {
                "tranche_index": 0,
                "target_price": 110.0,
                "tranche_frac": 1.0,
                "r_multiple": 1.0,
                "tag": "tp1",
            }
        ],
    }
    _MALFORMED = {"kind": "tranche_plan", "uic": 308, "tp_tranches": "not-a-list"}
    _RETRACTED_PLAN = {**_GOOD, "uic": 309, "instrument_currency": "USD"}

    def _lines(self) -> list[dict[str, Any]]:
        return [
            dict(self._GOOD),
            dict(self._MALFORMED),
            dict(self._RETRACTED_PLAN),
            {"kind": "tranche_plan_retracted", "uic": 309},
        ]

    def test_both_folds_govern_the_same_uic_set(self) -> None:
        lines = self._lines()
        self.assertEqual(
            set(cl.fold_tranche_plans(lines)), set(cl.fold_tranche_plan_currencies(lines))
        )
        self.assertEqual(cl.fold_tranche_plan_currencies(lines), {307: ("PLN", "PLN", None)})

    def test_stamp_survives_journal_compaction(self) -> None:
        compacted = cl._compact_standalone_stop_journal_lines(self._lines())
        currencies = cl.fold_tranche_plan_currencies(compacted)
        self.assertEqual(currencies, {307: ("PLN", "PLN", None)})


if __name__ == "__main__":
    unittest.main()
