"""Hermetic tests for the vendor-agnostic reconcile core (``brokers/reconcile.py``).

The core is a PURE join over (journal records x Broker Protocol x optional
vendor capabilities): open orders stay WORKING (with a trading-day expiry
sweep), disappeared orders resolve through the ``SupportsOrderResolution``
extension Protocol, FILLED verdicts cross-check through
``SupportsFillCrossCheck`` — and a broker lacking a capability degrades to
UNRESOLVED(capability_absent), never a guessed terminal state (no FakeBroker /
conformance-mixin changes needed, by design).

All dates are pinned via the ``today=`` seam; the calendar math delegates to
``paper.calendar.trading_days_elapsed`` (XNYS sessions).
"""

from __future__ import annotations

import datetime as dt
import unittest
from typing import Any

from alphalens_pipeline.brokers.contract import (
    BrokerError,
    InstrumentRef,
    OrderState,
    OrderStatus,
    Position,
)
from alphalens_pipeline.brokers.reconcile import (
    REASON_AUDIT_ERROR,
    REASON_CAPABILITY_ABSENT,
    ReconcileVerdict,
    SupportsFillCrossCheck,
    SupportsOrderResolution,
    _effective_settlement_rate,
    compute_realized_r,
    filled_sum_matches_owned,
    has_failures,
    reconcile_brackets,
    summarize,
)

from tests.brokers.test_broker_contract import FakeBroker

# Mon 2026-07-06 submission; 2026-07-08 = 2 XNYS sessions later,
# 2026-07-17 = 9 sessions later (past a 5-trading-day TTL).
_TS = "2026-07-06T18:00:00+00:00"
_TODAY_FRESH = dt.date(2026, 7, 8)
_TODAY_STALE = dt.date(2026, 7, 17)


def _bracket(**overrides: Any) -> dict[str, Any]:
    bracket: dict[str, Any] = {
        "client_request_id": "rid-1",
        "entry_order_id": "E-1",
        "exit_order_ids": ["T-1", "S-1"],
        "qty": 10,
        "entry": 50.0,
        "stop": 45.0,
        "tp": 60.0,
        "ttl": 5,
    }
    bracket.update(overrides)
    return bracket


def _record(**overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "execution_config_version": "execution-v1-test",
        "ts": _TS,
        "brief_date": "2026-07-06",
        "ticker": "KO",
        "mic": "XNYS",
        "uic": "307",
        "brackets": [_bracket()],
        "precheck": [],
    }
    record.update(overrides)
    return record


def _order_state(
    order_id: str,
    status: OrderStatus,
    *,
    filled: float = 0.0,
    raw_status: str = "",
) -> OrderState:
    return OrderState(
        order_id=order_id,
        status=status,
        instrument=None,
        filled_quantity=filled,
        raw_status=raw_status,
    )


class _ResolvingBroker:
    """Open-orders view + the resolution capability (NO fill cross-check)."""

    name = "stub-resolving"

    def __init__(
        self,
        *,
        open_orders: list[OrderState] | None = None,
        outcomes: dict[str, OrderState] | None = None,
        resolve_error: BrokerError | None = None,
    ):
        self._open_orders = open_orders or []
        self._outcomes = outcomes or {}
        self._resolve_error = resolve_error
        self.resolve_calls: list[str] = []

    def list_open_orders(self) -> list[OrderState]:
        return list(self._open_orders)

    def resolve_order_outcome(self, order_id: str) -> OrderState:
        self.resolve_calls.append(order_id)
        if self._resolve_error is not None:
            raise self._resolve_error
        return self._outcomes.get(
            order_id,
            _order_state(order_id, OrderStatus.UNKNOWN, raw_status="not_in_retention"),
        )


class _FullBroker(_ResolvingBroker):
    """Resolution + fill cross-check capabilities."""

    name = "stub-full"

    def __init__(
        self,
        *,
        open_refs: list[str] | None = None,
        closed_rows: list[dict[str, Any]] | None = None,
        positions: list[Position] | None = None,
        **kw: Any,
    ):
        super().__init__(**kw)
        self._open_refs = open_refs or []
        self._closed_rows = closed_rows or []
        self._positions = positions or []

    def get_open_position_references(self) -> list[str]:
        return list(self._open_refs)

    def get_closed_position_rows(self) -> list[dict[str, Any]]:
        return list(self._closed_rows)

    def get_positions(self) -> list[Position]:
        return list(self._positions)


def _single(verdicts: list[ReconcileVerdict]) -> ReconcileVerdict:
    assert len(verdicts) == 1, verdicts
    return verdicts[0]


def _position(*, uic: int, quantity: float, avg_price: float = 50.0) -> Position:
    """A netted broker position keyed to a uic (broker_instrument_id == str(Uic))."""
    return Position(
        instrument=InstrumentRef(
            ticker="KO",
            exchange_mic="XNYS",
            asset_type="Stock",
            broker_instrument_id=str(uic),
            broker_symbol="KO:xnys",
        ),
        quantity=quantity,
        avg_price=avg_price,
        market_value=None,
        unrealized_pnl=None,
        position_id=f"P-{uic}",
    )


class TestJournalJoin(unittest.TestCase):
    def test_open_entry_is_working(self):
        broker = _FullBroker(open_orders=[_order_state("E-1", OrderStatus.WORKING)])

        verdict = _single(reconcile_brackets([_record()], broker, today=_TODAY_FRESH))

        self.assertEqual(verdict.status, "WORKING")
        self.assertEqual(verdict.verdict, "WORKING")
        self.assertFalse(verdict.divergence)
        self.assertEqual(verdict.ticker, "KO")
        self.assertEqual(verdict.brief_date, "2026-07-06")
        self.assertEqual(verdict.entry_order_id, "E-1")
        self.assertEqual(verdict.qty, 10)

    def test_open_partially_filled_entry_keeps_partial_status(self):
        broker = _FullBroker(
            open_orders=[_order_state("E-1", OrderStatus.PARTIALLY_FILLED, filled=4.0)]
        )

        verdict = _single(reconcile_brackets([_record()], broker, today=_TODAY_FRESH))

        self.assertEqual(verdict.status, "PARTIALLY_FILLED")
        self.assertFalse(verdict.divergence)

    def test_one_verdict_per_bracket_across_records(self):
        records = [
            _record(),
            _record(
                ticker="NVDA",
                brackets=[
                    _bracket(entry_order_id="E-2", client_request_id="rid-2"),
                    _bracket(entry_order_id="E-3", client_request_id="rid-3"),
                ],
            ),
        ]
        broker = _FullBroker(
            open_orders=[
                _order_state("E-1", OrderStatus.WORKING),
                _order_state("E-2", OrderStatus.WORKING),
                _order_state("E-3", OrderStatus.WORKING),
            ]
        )

        verdicts = reconcile_brackets(records, broker, today=_TODAY_FRESH)

        self.assertEqual([v.entry_order_id for v in verdicts], ["E-1", "E-2", "E-3"])
        self.assertEqual([v.ticker for v in verdicts], ["KO", "NVDA", "NVDA"])

    def test_summarize_and_has_failures_on_clean_run(self):
        broker = _FullBroker(open_orders=[_order_state("E-1", OrderStatus.WORKING)])

        verdicts = reconcile_brackets([_record()], broker, today=_TODAY_FRESH)

        summary = summarize(verdicts)
        self.assertEqual(summary["total"], 1)
        self.assertEqual(summary["working"], 1)
        self.assertEqual(summary["terminal"], 0)
        self.assertEqual(summary["unresolved"], 0)
        self.assertEqual(summary["divergent"], 0)
        self.assertFalse(has_failures(verdicts))


class TestExpirySweep(unittest.TestCase):
    def test_within_ttl_is_plain_working(self):
        broker = _FullBroker(open_orders=[_order_state("E-1", OrderStatus.WORKING)])

        verdict = _single(reconcile_brackets([_record()], broker, today=_TODAY_FRESH))

        self.assertEqual(verdict.verdict, "WORKING")
        self.assertFalse(verdict.divergence)
        self.assertEqual(verdict.details.get("trading_days_elapsed"), 2)

    def test_past_ttl_open_entry_is_a_divergence(self):
        broker = _FullBroker(open_orders=[_order_state("E-1", OrderStatus.WORKING)])

        verdict = _single(reconcile_brackets([_record()], broker, today=_TODAY_STALE))

        self.assertEqual(verdict.status, "WORKING")
        self.assertEqual(verdict.verdict, "WORKING(PAST-TTL!)")
        self.assertTrue(verdict.divergence)
        self.assertIn("ttl", (verdict.reason or "").lower())
        self.assertEqual(verdict.details.get("trading_days_elapsed"), 9)
        self.assertTrue(has_failures(reconcile_brackets([_record()], broker, today=_TODAY_STALE)))

    def test_missing_ttl_skips_the_sweep(self):
        broker = _FullBroker(open_orders=[_order_state("E-1", OrderStatus.WORKING)])
        record = _record(brackets=[_bracket(ttl=None)])

        verdict = _single(reconcile_brackets([record], broker, today=_TODAY_STALE))

        self.assertEqual(verdict.verdict, "WORKING")
        self.assertFalse(verdict.divergence)


class TestTerminalResolutionMapping(unittest.TestCase):
    def _resolve_to(self, state: OrderState, **broker_kw: Any) -> ReconcileVerdict:
        broker = _FullBroker(outcomes={"E-1": state}, **broker_kw)
        return _single(reconcile_brackets([_record()], broker, today=_TODAY_FRESH))

    def test_cancelled_maps_with_cascade_note(self):
        verdict = self._resolve_to(
            _order_state("E-1", OrderStatus.CANCELLED, raw_status="Cancelled/Confirmed")
        )
        self.assertEqual(verdict.status, "CANCELLED")
        self.assertEqual(verdict.verdict, "CANCELLED")
        self.assertIn("cascade", verdict.note or "")
        self.assertFalse(verdict.divergence)
        self.assertEqual(verdict.details.get("raw_status"), "Cancelled/Confirmed")

    def test_rejected_and_expired_map_plainly(self):
        for status, expected in (
            (OrderStatus.REJECTED, "REJECTED"),
            (OrderStatus.EXPIRED, "EXPIRED"),
        ):
            with self.subTest(status=status):
                verdict = self._resolve_to(_order_state("E-1", status, raw_status="x/y"))
                self.assertEqual(verdict.status, expected)
                self.assertEqual(verdict.verdict, expected)
                self.assertFalse(verdict.divergence)

    def test_unknown_resolution_surfaces_reason_as_unresolved(self):
        verdict = self._resolve_to(
            _order_state(
                "E-1",
                OrderStatus.UNKNOWN,
                raw_status="inconsistent_state (Placed/Confirmed LogId=1)",
            )
        )
        self.assertEqual(verdict.status, "UNRESOLVED")
        self.assertEqual(verdict.verdict, "UNRESOLVED(inconsistent_state)")
        self.assertIn("Placed/Confirmed", verdict.reason or "")

    def test_resolver_error_is_unresolved_audit_error_not_an_exception(self):
        broker = _FullBroker(resolve_error=BrokerError("audit endpoint 502"))

        verdict = _single(reconcile_brackets([_record()], broker, today=_TODAY_FRESH))

        self.assertEqual(verdict.status, "UNRESOLVED")
        self.assertEqual(verdict.verdict, f"UNRESOLVED({REASON_AUDIT_ERROR})")
        self.assertIn("audit endpoint 502", verdict.reason or "")

    def test_activity_time_extracted_from_diagnostics_for_display(self):
        verdict = self._resolve_to(
            _order_state(
                "E-1",
                OrderStatus.CANCELLED,
                raw_status=(
                    "Cancelled/Confirmed LogId=249474866 ActivityTime=2026-07-17T11:42:10.360000Z"
                ),
            )
        )
        self.assertEqual(verdict.activity_time, "2026-07-17T11:42:10.360000Z")


class TestDivergenceClassification(unittest.TestCase):
    _FILLED = _order_state("E-1", OrderStatus.FILLED, filled=10.0, raw_status="FinalFill/Confirmed")

    def test_filled_with_closed_pair_computes_realized_r(self):
        broker = _FullBroker(
            outcomes={"E-1": self._FILLED},
            closed_rows=[
                {
                    "OpeningExternalReferenceId": "rid-1",
                    "ClosingPrice": 55.0,
                    "ProfitLossOnTrade": 50.0,
                }
            ],
        )

        verdict = _single(reconcile_brackets([_record()], broker, today=_TODAY_FRESH))

        # entry 50, stop 45 -> risk 5; close 55 -> r = +1.00
        self.assertEqual(verdict.status, "FILLED")
        self.assertEqual(verdict.verdict, "FILLED(closed r=+1.00)")
        self.assertFalse(verdict.divergence)
        self.assertEqual(verdict.details.get("realized_r"), 1.0)
        self.assertEqual(verdict.details.get("profit_loss_on_trade"), 50.0)

    def test_filled_with_closed_pair_in_envelope_shape(self):
        broker = _FullBroker(
            outcomes={"E-1": self._FILLED},
            closed_rows=[
                {"ClosedPosition": {"OpeningExternalReferenceId": "rid-1", "ClosingPrice": 47.5}},
            ],
        )

        verdict = _single(reconcile_brackets([_record()], broker, today=_TODAY_FRESH))

        # close 47.5 vs entry 50, risk 5 -> r = -0.50 (partial adverse exit)
        self.assertEqual(verdict.verdict, "FILLED(closed r=-0.50)")
        self.assertEqual(verdict.details.get("realized_r"), -0.5)

    def test_filled_with_real_captured_closed_row_matches_and_computes_r(self):
        # Byte-shaped from the T1 first-fill closed pair
        # (~/.alphalens/broker_orders/experiments/first_fill_2026-07-20/
        #  32_closedpositions.json). The real closedposition row carries the
        # opening leg's reference as ``OpeningExternalReferenceId`` and the
        # close price as ``ClosingPrice`` — NOT the doc-guessed
        # ``ExternalReference`` / ``ClosePrice`` (which do not exist), the
        # second Saxo reconcile bug surfaced by the live T1 run.
        record = _record(
            brackets=[
                _bracket(
                    client_request_id="87e0ab88-c1f2-4e88-b5b8-8fbbbb6e1a6d",
                    entry=82.09,
                    stop=81.09,
                )
            ]
        )
        broker = _FullBroker(
            outcomes={"E-1": self._FILLED},
            closed_rows=[
                {
                    "Amount": 2.0,
                    "BuyOrSell": "Buy",
                    "ClosingExternalReferenceId": "8e0fbe45-6952-4647-a58e-67a5884768dc",
                    "ClosingPrice": 82.15,
                    "OpenPrice": 82.09,
                    "OpeningExternalReferenceId": "87e0ab88-c1f2-4e88-b5b8-8fbbbb6e1a6d",
                    "ProfitLossOnTrade": 0.12,
                    "Uic": 307,
                }
            ],
        )

        verdict = _single(reconcile_brackets([record], broker, today=_TODAY_FRESH))

        # entry 82.09, stop 81.09 -> risk 1.00; close 82.15 -> r = +0.06
        self.assertEqual(verdict.status, "FILLED")
        self.assertFalse(verdict.divergence)
        self.assertEqual(verdict.verdict, "FILLED(closed r=+0.06)")
        self.assertAlmostEqual(verdict.details["realized_r"], 0.06, places=9)
        self.assertEqual(verdict.details.get("profit_loss_on_trade"), 0.12)
        self.assertIn("round trip closed", verdict.note or "")

    def test_filled_with_open_position_is_clean(self):
        broker = _FullBroker(outcomes={"E-1": self._FILLED}, open_refs=["rid-1"])

        verdict = _single(reconcile_brackets([_record()], broker, today=_TODAY_FRESH))

        self.assertEqual(verdict.verdict, "FILLED")
        self.assertFalse(verdict.divergence)
        self.assertIn("position open", verdict.note or "")

    def test_filled_without_position_or_closed_pair_is_a_divergence(self):
        broker = _FullBroker(outcomes={"E-1": self._FILLED})

        verdict = _single(reconcile_brackets([_record()], broker, today=_TODAY_FRESH))

        self.assertEqual(verdict.status, "FILLED")
        self.assertTrue(verdict.divergence)
        self.assertIn("no open position or closed pair", verdict.reason or "")
        self.assertTrue(has_failures([verdict]))

    def test_compute_realized_r_guards_degenerate_risk(self):
        self.assertEqual(compute_realized_r(55.0, 50.0, 45.0), 1.0)
        self.assertIsNone(compute_realized_r(55.0, 50.0, 50.0), "zero risk -> None")
        self.assertIsNone(compute_realized_r(55.0, 50.0, None), "no stop -> None")


class TestFxDiagnostics(unittest.TestCase):
    """Schema-2 FX provenance surfacing + the effective-settlement-rate
    reconstruction (the ONLY empirical FX-slippage signal — ClosedPosition
    does not expose the settlement rate)."""

    _FILLED = _order_state("E-1", OrderStatus.FILLED, filled=10.0, raw_status="FinalFill/Confirmed")

    def _fx_record(self) -> dict[str, Any]:
        return _record(
            mic="XWAR",
            instrument_currency="PLN",
            sizing_currency="EUR",
            fx_rate=4.34,
        )

    def test_journal_fx_provenance_lands_in_verdict_details(self):
        broker = _FullBroker(open_orders=[_order_state("E-1", OrderStatus.WORKING)])

        verdict = _single(reconcile_brackets([self._fx_record()], broker, today=_TODAY_FRESH))

        self.assertEqual(verdict.details.get("instrument_currency"), "PLN")
        self.assertEqual(verdict.details.get("sizing_fx_rate"), 4.34)

    def test_v1_record_without_fx_keys_adds_no_fx_details(self):
        broker = _FullBroker(open_orders=[_order_state("E-1", OrderStatus.WORKING)])

        verdict = _single(reconcile_brackets([_record()], broker, today=_TODAY_FRESH))

        self.assertNotIn("instrument_currency", verdict.details)
        self.assertNotIn("sizing_fx_rate", verdict.details)

    def test_effective_settlement_rate_reconstructed_from_pnl_ratio(self):
        # PLN 100 on-trade vs EUR 23 in base -> effective PLN/EUR ~4.3478,
        # recorded next to the journaled sizing rate for the cross-check.
        broker = _FullBroker(
            outcomes={"E-1": self._FILLED},
            closed_rows=[
                {
                    "OpeningExternalReferenceId": "rid-1",
                    "ClosingPrice": 55.0,
                    "ProfitLossOnTrade": 100.0,
                    "ProfitLossOnTradeInBaseCurrency": 23.0,
                }
            ],
        )

        verdict = _single(reconcile_brackets([self._fx_record()], broker, today=_TODAY_FRESH))

        self.assertAlmostEqual(verdict.details["effective_settlement_rate"], 100.0 / 23.0, places=9)
        self.assertEqual(verdict.details.get("sizing_fx_rate"), 4.34)

    def test_effective_rate_absent_when_base_pnl_missing_or_zero(self):
        for closed_row in (
            {
                "OpeningExternalReferenceId": "rid-1",
                "ClosingPrice": 55.0,
                "ProfitLossOnTrade": 100.0,
            },
            {
                "OpeningExternalReferenceId": "rid-1",
                "ClosingPrice": 55.0,
                "ProfitLossOnTrade": 100.0,
                "ProfitLossOnTradeInBaseCurrency": 0.0,
            },
        ):
            with self.subTest(row=closed_row):
                broker = _FullBroker(outcomes={"E-1": self._FILLED}, closed_rows=[closed_row])
                verdict = _single(
                    reconcile_brackets([self._fx_record()], broker, today=_TODAY_FRESH)
                )
                self.assertNotIn("effective_settlement_rate", verdict.details)

    def test_boolean_settled_fields_are_never_read_as_rates(self):
        # The ConversionRateInstrumentToBaseSettled* gotcha class: a BOOLEAN
        # in either PnL field must never produce a fabricated rate.
        broker = _FullBroker(
            outcomes={"E-1": self._FILLED},
            closed_rows=[
                {
                    "OpeningExternalReferenceId": "rid-1",
                    "ClosingPrice": 55.0,
                    "ProfitLossOnTrade": True,
                    "ProfitLossOnTradeInBaseCurrency": True,
                }
            ],
        )

        verdict = _single(reconcile_brackets([self._fx_record()], broker, today=_TODAY_FRESH))

        self.assertNotIn("effective_settlement_rate", verdict.details)


class TestCapabilityAbsentDegradesUnresolved(unittest.TestCase):
    def test_fake_broker_without_resolution_degrades_not_raises(self):
        # FakeBroker implements the frozen Protocol ONLY — by design it needs
        # ZERO changes for P3; a disappeared order degrades honestly.
        broker = FakeBroker()
        self.assertNotIsInstance(broker, SupportsOrderResolution)
        self.assertNotIsInstance(broker, SupportsFillCrossCheck)

        verdict = _single(reconcile_brackets([_record()], broker, today=_TODAY_FRESH))

        self.assertEqual(verdict.status, "UNRESOLVED")
        self.assertEqual(verdict.verdict, f"UNRESOLVED({REASON_CAPABILITY_ABSENT})")
        self.assertFalse(verdict.divergence)
        terminal_tokens = {"FILLED", "CANCELLED", "REJECTED", "EXPIRED"}
        self.assertNotIn(verdict.status, terminal_tokens, "must never guess a terminal state")

    def test_resolver_without_cross_check_keeps_filled_clean(self):
        broker = _ResolvingBroker(
            outcomes={
                "E-1": _order_state("E-1", OrderStatus.FILLED, filled=10.0, raw_status="FinalFill")
            }
        )
        self.assertIsInstance(broker, SupportsOrderResolution)
        self.assertNotIsInstance(broker, SupportsFillCrossCheck)

        verdict = _single(reconcile_brackets([_record()], broker, today=_TODAY_FRESH))

        self.assertEqual(verdict.verdict, "FILLED")
        self.assertFalse(verdict.divergence, "no cross-check capability -> no divergence claim")
        self.assertIn("cross-check unavailable", verdict.note or "")


class TestEffectiveSettlementRateCoercion(unittest.TestCase):
    def test_float_coercible_scalars_are_accepted(self):
        # numpy/pandas scalars are not int/float subclasses; a __float__-bearing
        # scalar must still produce the diagnostic (review finding, PR #849).
        class _Scalar:
            def __init__(self, v):
                self._v = v

            def __float__(self):
                return self._v

        row = {
            "ProfitLossOnTrade": _Scalar(43.4),
            "ProfitLossOnTradeInBaseCurrency": _Scalar(10.0),
        }
        self.assertAlmostEqual(_effective_settlement_rate(row), 4.34)

    def test_booleans_still_rejected(self):
        row = {"ProfitLossOnTrade": True, "ProfitLossOnTradeInBaseCurrency": 10.0}
        self.assertIsNone(_effective_settlement_rate(row))


class TestSecondFilledTierNotDivergent(unittest.TestCase):
    """Multi-tier ladder → one netted position (saxo-oco §8, fixes C-S6).

    ``get_open_position_references()`` returns ONE ``ExternalReference`` per
    netted row (the source/oldest tier crid), so every OTHER filled tier on the
    same uic is absent from that set and would fall through ``_reconcile_filled``
    to ``divergence=True`` → a per-tick ``AlertOnly`` storm (and a later FIFO
    mapping flip could un-protect). The fix matches a filled tier to the netted
    position BY UIC: a tier is "position open" iff its uic has ``owned > 0`` and
    its own audit ``FilledAmount > 0``.
    """

    _FINAL = "FinalFill/Confirmed"

    def _two_tier_record(self) -> dict[str, Any]:
        # Two tiers on ONE uic (307): tier-0 filled 20, tier-1 filled 26.
        return _record(
            uic="307",
            brackets=[
                _bracket(client_request_id="rid-0", entry_order_id="E-0", qty=20),
                _bracket(client_request_id="rid-1", entry_order_id="E-1", qty=26),
            ],
        )

    def _broker(self) -> _FullBroker:
        return _FullBroker(
            outcomes={
                "E-0": _order_state("E-0", OrderStatus.FILLED, filled=20.0, raw_status=self._FINAL),
                "E-1": _order_state("E-1", OrderStatus.FILLED, filled=26.0, raw_status=self._FINAL),
            },
            # Only the source/oldest tier crid surfaces as the netted row's ref.
            open_refs=["rid-0"],
            # One netted position: owned == 20 + 26 == 46.
            positions=[_position(uic=307, quantity=46.0)],
        )

    def test_non_source_filled_tier_matches_by_uic_not_divergent(self):
        verdicts = reconcile_brackets([self._two_tier_record()], self._broker(), today=_TODAY_FRESH)
        by_id = {v.entry_order_id: v for v in verdicts}

        # Source tier — matched via open_references (existing clean path).
        self.assertEqual(by_id["E-0"].status, "FILLED")
        self.assertFalse(by_id["E-0"].divergence)

        # Second tier — absent from open_references; matched by uic to the
        # netted position. Must be FILLED, NOT a false divergence.
        self.assertEqual(by_id["E-1"].status, "FILLED")
        self.assertFalse(
            by_id["E-1"].divergence, "non-source filled tier must not be flagged divergence"
        )
        self.assertEqual(by_id["E-1"].details.get("netted_owned"), 46.0)
        self.assertFalse(has_failures(verdicts))

    def test_sum_filled_equals_owned_crosscheck_passes(self):
        # The Σ FilledAmount == owned correlation validator for the netted
        # position: 20 + 26 == 46 within the qty tolerance.
        self.assertTrue(filled_sum_matches_owned([20.0, 26.0], 46.0))
        # Sub-share float noise still reconciles (tolerance, not bare ==).
        self.assertTrue(filled_sum_matches_owned([20.0, 25.9999999], 46.0))
        # A genuinely unaccounted fill does NOT reconcile.
        self.assertFalse(filled_sum_matches_owned([20.0], 46.0))

    def test_filled_tier_on_flat_uic_still_diverges(self):
        # No netted position on the uic (owned == 0) and no open ref / closed
        # pair → the per-uic match must NOT rescue it; genuine divergence stands.
        broker = _FullBroker(
            outcomes={
                "E-1": _order_state("E-1", OrderStatus.FILLED, filled=26.0, raw_status=self._FINAL)
            },
            open_refs=[],
            positions=[],
        )
        record = _record(
            uic="307",
            brackets=[_bracket(client_request_id="rid-1", entry_order_id="E-1", qty=26)],
        )

        verdict = _single(reconcile_brackets([record], broker, today=_TODAY_FRESH))

        self.assertEqual(verdict.status, "FILLED")
        self.assertTrue(verdict.divergence)
        self.assertIn("no open position or closed pair", verdict.reason or "")


if __name__ == "__main__":
    unittest.main()
