"""Mutation-hardening pins for ``brokers/reconcile.py``.

Each test targets a specific KILLABLE mutation survivor: it PASSES against the
real source and FAILS under exactly one mutant. Tests are grouped by the logic
they pin (immutability contracts, realized-R math, the qty tolerance, the CLI
summary counters, the small parsing helpers, the open-order TTL sweep, the
resolved cascade note, and the FILLED cross-check join). Hand-written anchors
carry the identity / exact-value kills; ``hypothesis`` property sweeps
supplement the comparison / counter mutants where a broad sweep pins them more
robustly than a single example.

Survivor ids (from the mutation run) appear next to each test.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import math
import unittest

from alphalens_pipeline.brokers.contract import (
    _QTY_EPS,
    OrderState,
    OrderStatus,
)
from alphalens_pipeline.brokers.reconcile import (
    _UNRESOLVED,
    ReconcileVerdict,
    _base_verdict_fields,
    _CrossCheckData,
    _effective_settlement_rate,
    _reconcile_filled,
    _reconcile_open,
    _reconcile_resolved,
    _submission_date,
    _uic_key,
    compute_realized_r,
    filled_sum_matches_owned,
    summarize,
)
from hypothesis import given, settings
from hypothesis import strategies as st

_FIFO_NOTE = "round trip closed (FIFO pair)"
_NETTED_NOTE = "position open (netted tier), matched by uic"


def _verdict(**overrides) -> ReconcileVerdict:
    fields = {
        "brief_date": "d",
        "ticker": "T",
        "qty": 1.0,
        "entry_order_id": "e",
        "status": "WORKING",
        "verdict": "WORKING",
    }
    fields.update(overrides)
    return ReconcileVerdict(**fields)


def _filled_state(*, filled_quantity) -> OrderState:
    return OrderState(
        order_id="E",
        status=OrderStatus.FILLED,
        instrument=None,
        filled_quantity=filled_quantity,
        raw_status="",
    )


def _cross_check(*, open_refs=None, closed_rows=None, owned=None) -> _CrossCheckData:
    return _CrossCheckData(
        open_references=set(open_refs or []),
        closed_rows=list(closed_rows or []),
        owned_by_uic=dict(owned or {}),
    )


def _run_filled(
    bracket,
    *,
    filled_quantity,
    details=None,
    cross_check,
    activity_time=None,
) -> ReconcileVerdict:
    return _reconcile_filled(
        bracket,
        _filled_state(filled_quantity=filled_quantity),
        brief=("d", "T", 10.0, "E"),
        details=dict(details or {}),
        cross_check=cross_check,
        activity_time=activity_time,
    )


# --------------------------------------------------------------------------
# Immutability / hashability contracts (frozen=True).
# --------------------------------------------------------------------------


class TestFrozenContracts(unittest.TestCase):
    def test_reconcile_verdict_is_immutable(self):
        # id 0 (L105): frozen=True -> assignment raises FrozenInstanceError.
        verdict = _verdict()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            verdict.qty = 2.0  # type: ignore[misc]

    def test_cross_check_snapshot_is_immutable(self):
        # id 69 (L254): the _CrossCheckData snapshot is a frozen fact.
        snapshot = _cross_check()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            snapshot.open_references = set()  # type: ignore[misc]


# --------------------------------------------------------------------------
# The ``unresolved`` property compares by value, not identity.
# --------------------------------------------------------------------------


class TestUnresolvedProperty(unittest.TestCase):
    def test_unresolved_matches_equal_but_noninterned_status(self):
        # id 1 (L125): status == _UNRESOLVED (value), not `is` (identity).
        runtime_status = "".join(["UN", "RESOLVED"])
        # Guard the pin: the runtime-built string must be a DISTINCT object,
        # otherwise the `is` mutant would coincidentally pass.
        self.assertIsNot(runtime_status, _UNRESOLVED)
        self.assertEqual(runtime_status, _UNRESOLVED)

        verdict = _verdict(status=runtime_status, verdict="x")

        self.assertTrue(verdict.unresolved)


# --------------------------------------------------------------------------
# compute_realized_r — the None guards and the risk denominator.
# --------------------------------------------------------------------------


class TestComputeRealizedR(unittest.TestCase):
    def test_none_close_price_short_circuits_to_none(self):
        # id 46 (L154): each of the three inputs guards independently; a None
        # close_price with real entry/stop must return None, never float(None).
        self.assertIsNone(compute_realized_r(None, 5.0, 4.0))

    def test_risk_denominator_is_entry_minus_stop(self):
        # id 47 (L156): risk = entry - stop (not entry % stop).
        self.assertEqual(compute_realized_r(15.0, 10.0, 5.0), 1.0)

    def test_inverted_risk_distance_is_rejected(self):
        # id 48 (L157): risk <= 0 rejects a NEGATIVE risk (entry < stop),
        # not only exactly-zero risk.
        self.assertIsNone(compute_realized_r(6.0, 5.0, 10.0))

    @settings(deadline=None, max_examples=200)
    @given(
        close=st.floats(min_value=-1e6, max_value=1e6),
        entry=st.floats(min_value=-1e6, max_value=1e6),
        gap=st.floats(min_value=0.01, max_value=1e6),
    )
    def test_realized_r_equals_subtractive_formula(self, close, entry, gap):
        # Supplements ids 47/48: with a strictly positive risk distance the
        # result is exactly (close - entry) / (entry - stop); the modulo mutant
        # (id 47) and the `== 0` guard mutant (id 48) both diverge from this.
        stop = entry - gap  # entry > stop, so risk = gap > 0
        expected = (close - entry) / gap
        result = compute_realized_r(close, entry, stop)
        assert result is not None
        # Relative tolerance: values span many orders of magnitude, but the
        # modulo (id 47) and `== 0` (id 48) mutants diverge grossly, not by ULPs.
        self.assertTrue(math.isclose(result, expected, rel_tol=1e-6, abs_tol=1e-9))


# --------------------------------------------------------------------------
# filled_sum_matches_owned — the qty tolerance boundary.
# --------------------------------------------------------------------------


class TestFilledSumMatchesOwned(unittest.TestCase):
    def test_delta_exactly_at_tolerance_is_accepted(self):
        # id 50 (L176): abs(delta) <= eps accepts the exact-tolerance case.
        self.assertTrue(filled_sum_matches_owned([2.0], 7.0, eps=5.0))


# --------------------------------------------------------------------------
# summarize — the four counters must each increment by exactly one.
# --------------------------------------------------------------------------


class TestSummarizeCounters(unittest.TestCase):
    def test_terminal_counted_exactly_once(self):
        # ids 51 & 52 (L189): += 1, never += 0 or += 2.
        verdict = _verdict(status=OrderStatus.FILLED.value, verdict="FILLED")
        self.assertEqual(summarize([verdict])["terminal"], 1)

    def test_unresolved_counted_exactly_once(self):
        # ids 53 & 54 (L191): += 1, never += 2 or += 0.
        verdict = _verdict(status=_UNRESOLVED, verdict="UNRESOLVED(x)")
        self.assertEqual(summarize([verdict])["unresolved"], 1)

    def test_divergent_counted_exactly_once(self):
        # ids 55 & 56 (L193): += 1, never += 2 or += 0.
        verdict = _verdict(status=OrderStatus.FILLED.value, verdict="FILLED", divergence=True)
        self.assertEqual(summarize([verdict])["divergent"], 1)

    @settings(deadline=None, max_examples=200)
    @given(
        n_terminal=st.integers(min_value=0, max_value=8),
        n_unresolved=st.integers(min_value=0, max_value=8),
        n_divergent=st.integers(min_value=0, max_value=8),
    )
    def test_counters_are_exact_multiplicity(self, n_terminal, n_unresolved, n_divergent):
        # Supplements ids 51-56: over an arbitrary mix, each counter equals the
        # true count; the += 0 and += 2 mutants diverge for any non-zero count.
        verdicts = []
        verdicts += [
            _verdict(status=OrderStatus.FILLED.value, verdict="FILLED") for _ in range(n_terminal)
        ]
        verdicts += [
            _verdict(status=_UNRESOLVED, verdict="UNRESOLVED(x)") for _ in range(n_unresolved)
        ]
        # Divergent rows carry a terminal status too (separate `if`, not elif),
        # so count them into the terminal expectation as well.
        verdicts += [
            _verdict(status=OrderStatus.FILLED.value, verdict="FILLED", divergence=True)
            for _ in range(n_divergent)
        ]
        summary = summarize(verdicts)
        self.assertEqual(summary["terminal"], n_terminal + n_divergent)
        self.assertEqual(summary["unresolved"], n_unresolved)
        self.assertEqual(summary["divergent"], n_divergent)


# --------------------------------------------------------------------------
# Small parsing helpers — settlement rate, submission date, qty coercion.
# --------------------------------------------------------------------------


class TestEffectiveSettlementRate(unittest.TestCase):
    def test_negative_base_pnl_reconstructs_rate(self):
        # id 81 (L300): only a ZERO base-currency PnL is rejected; a negative
        # (losing-trade) PnL still yields the reconstructed settlement rate.
        rate = _effective_settlement_rate(
            {"ProfitLossOnTrade": -100.0, "ProfitLossOnTradeInBaseCurrency": -90.0}
        )
        assert rate is not None
        self.assertAlmostEqual(rate, 100.0 / 90.0, places=6)


class TestSubmissionDate(unittest.TestCase):
    def test_malformed_timestamp_degrades_to_none(self):
        # id 93 (L311): a ValueError from fromisoformat is caught -> None,
        # never propagated.
        self.assertIsNone(_submission_date({"ts": "not-a-date"}))


class TestBaseVerdictFields(unittest.TestCase):
    def test_none_qty_falls_back_to_zero(self):
        # ids 105 & 108 (L327/L328): float(None) TypeError is caught and the
        # fallback is 0.0 (not 1.0).
        _, _, qty, _ = _base_verdict_fields({}, {"qty": None})
        self.assertEqual(qty, 0.0)

    def test_nonnumeric_qty_falls_back_to_zero(self):
        # ids 106 & 107 (L327/L328): float('abc') ValueError is caught and the
        # fallback is 0.0 (not -1.0).
        _, _, qty, _ = _base_verdict_fields({}, {"qty": "abc"})
        self.assertEqual(qty, 0.0)


# --------------------------------------------------------------------------
# _reconcile_open — filled-qty detail guard + the trading-day TTL sweep.
# --------------------------------------------------------------------------


class TestReconcileOpen(unittest.TestCase):
    def _open_verdict(self, *, record, bracket, filled_quantity, today):
        state = OrderState(
            order_id="E-1",
            status=OrderStatus.WORKING,
            instrument=None,
            filled_quantity=filled_quantity,
            raw_status="",
        )
        return _reconcile_open(
            record,
            bracket,
            state,
            brief=("2026-07-06", "KO", 10.0, "E-1"),
            details={},
            today=today,
        )

    def test_positive_filled_quantity_is_recorded(self):
        # id 133 (L426): a truthy filled_quantity is recorded in details
        # (the negated guard would omit it).
        verdict = self._open_verdict(
            record={},  # no ts -> TTL branch skipped
            bracket={},  # no ttl
            filled_quantity=5.0,
            today=dt.date(2026, 7, 8),
        )
        self.assertEqual(verdict.details.get("filled_quantity"), 5.0)

    def test_past_ttl_reason_names_the_record_exchange(self):
        # id 134 (L435): exchange = record.mic or default; the PAST-TTL reason
        # string must name the record's own MIC (XWAR), not the default XNYS.

        verdict = self._open_verdict(
            record={"mic": "XWAR", "ts": "2026-07-06T18:00:00+00:00"},
            bracket={"ttl": 1},
            filled_quantity=0.0,
            today=dt.date(2026, 7, 17),
        )
        self.assertTrue(verdict.divergence)
        assert verdict.reason is not None
        self.assertIn("on XWAR", verdict.reason)

    def test_elapsed_equal_to_ttl_is_not_past_ttl(self):
        # id 135 (L439): elapsed > ttl (strict); at elapsed == ttl the order is
        # still within TTL, so not divergent.

        verdict = self._open_verdict(
            record={"mic": "XNYS", "ts": "2026-07-06T18:00:00+00:00"},
            bracket={"ttl": 1},  # 2026-07-07 == exactly 1 XNYS session later
            filled_quantity=0.0,
            today=dt.date(2026, 7, 7),
        )
        self.assertFalse(verdict.divergence)
        self.assertEqual(verdict.verdict, "WORKING")


# --------------------------------------------------------------------------
# _reconcile_resolved — the cancel-cascade note gate.
# --------------------------------------------------------------------------


class TestReconcileResolvedCascadeNote(unittest.TestCase):
    def test_rejected_terminal_with_children_carries_no_cascade_note(self):
        # id 151 (L487): the cascade note is gated on CANCELLED *and* children;
        # a REJECTED terminal with exit_order_ids must NOT get the note.
        state = OrderState(
            order_id="E",
            status=OrderStatus.REJECTED,
            instrument=None,
            filled_quantity=0.0,
            raw_status="",
        )
        verdict = _reconcile_resolved(
            {"exit_order_ids": ["x"]},
            state,
            brief=("d", "T", 10.0, "E"),
            details={},
            cross_check=None,
        )
        self.assertIsNone(verdict.note)


# --------------------------------------------------------------------------
# _reconcile_filled — closed-pair FIFO join by OpeningExternalReferenceId.
# --------------------------------------------------------------------------


class TestReconcileFilledClosedPairJoin(unittest.TestCase):
    def test_greater_reference_is_not_a_fifo_match(self):
        # id 188 (L602): the join is `==`, not `>=`; a lexicographically larger
        # OpeningExternalReferenceId must not fabricate a FIFO pair.
        verdict = _run_filled(
            {"client_request_id": "AAA", "entry": 5.0, "stop": 4.0},
            filled_quantity=5.0,
            cross_check=_cross_check(
                closed_rows=[{"OpeningExternalReferenceId": "ZZZ", "ClosingPrice": 10.0}]
            ),
        )
        self.assertNotEqual(verdict.note, _FIFO_NOTE)
        self.assertTrue(verdict.divergence)

    def test_equal_but_noninterned_reference_is_a_fifo_match(self):
        # id 189 (L602): the join is `==` (value), not `is` (identity); two
        # equal-valued but distinct string objects must still match.
        ref_bracket = "".join(["ref-", "9001"])
        ref_row = "".join(["ref-", "9001"])
        # Guard the pin: the two references must be DISTINCT objects.
        self.assertIsNot(ref_bracket, ref_row)

        verdict = _run_filled(
            {"client_request_id": ref_bracket, "entry": 5.0, "stop": 4.0},
            filled_quantity=5.0,
            cross_check=_cross_check(
                closed_rows=[{"OpeningExternalReferenceId": ref_row, "ClosingPrice": 10.0}]
            ),
        )
        self.assertEqual(verdict.note, _FIFO_NOTE)

    def test_nonmatching_reference_does_not_match_first_row(self):
        # id 190 (L602): `request_id and ...==request_id`, not `or`; a truthy
        # request_id must not match a row whose reference differs.
        verdict = _run_filled(
            {"client_request_id": "REQ1", "entry": 5.0, "stop": 4.0},
            filled_quantity=5.0,
            cross_check=_cross_check(
                closed_rows=[{"OpeningExternalReferenceId": "OTHER", "ClosingPrice": 10.0}]
            ),
        )
        self.assertTrue(verdict.divergence)
        self.assertNotEqual(verdict.note, _FIFO_NOTE)

    def test_smaller_reference_is_not_a_fifo_match(self):
        # id 191 (L602): the join is `==`, not `<=`; a lexicographically smaller
        # OpeningExternalReferenceId must not fabricate a FIFO pair.
        verdict = _run_filled(
            {"client_request_id": "REQ2", "entry": 5.0, "stop": 4.0},
            filled_quantity=5.0,
            cross_check=_cross_check(
                closed_rows=[{"OpeningExternalReferenceId": "REQ1", "ClosingPrice": 10.0}]
            ),
        )
        self.assertTrue(verdict.divergence)
        self.assertNotEqual(verdict.note, _FIFO_NOTE)


# --------------------------------------------------------------------------
# _reconcile_filled — per-uic netted-tier match guards.
# --------------------------------------------------------------------------


class TestReconcileFilledUicMatch(unittest.TestCase):
    def test_missing_uic_owned_fallback_is_zero(self):
        # id 192 (L635): when uic_key is falsy the owned fallback is 0.0 (not
        # 1.0), so a filled tier with no uic is a divergence.
        verdict = _run_filled(
            {"client_request_id": "rid-x", "entry": 5.0, "stop": 4.0},
            filled_quantity=5.0,
            details={},  # no 'uic' -> uic_key == ""
            cross_check=_cross_check(),
        )
        self.assertTrue(verdict.divergence)
        self.assertNotEqual(verdict.note, _NETTED_NOTE)

    def test_zero_filled_amount_with_live_owned_is_divergence(self):
        # ids 195, 197, 199 (L636/L637): filled_amount falls back to 0.0 (not
        # 1.0), and the guard is `> _QTY_EPS` (not `!=` / `is not`), so a FILLED
        # tier reporting no filled qty is a divergence even with a live netted
        # position.
        verdict = _run_filled(
            {"client_request_id": "rid-x", "entry": 5.0, "stop": 4.0},
            filled_quantity=None,  # -> filled_amount 0.0
            details={"uic": "123"},
            cross_check=_cross_check(owned={"123": 100.0}),
        )
        self.assertTrue(verdict.divergence)
        self.assertNotEqual(verdict.note, _NETTED_NOTE)

    def test_owned_exactly_at_eps_boundary_is_divergence(self):
        # id 198 (L637): owned > _QTY_EPS (strict); owned == _QTY_EPS is not a
        # live position, so the tier is a divergence.
        verdict = _run_filled(
            {"client_request_id": "rid-x", "entry": 5.0, "stop": 4.0},
            filled_quantity=5.0,
            details={"uic": "123"},
            cross_check=_cross_check(owned={"123": _QTY_EPS}),
        )
        self.assertTrue(verdict.divergence)
        self.assertNotEqual(verdict.note, _NETTED_NOTE)

    def test_filled_amount_exactly_at_eps_boundary_is_divergence(self):
        # id 200 (L637): filled_amount > _QTY_EPS (strict); filled_amount ==
        # _QTY_EPS does not clear the guard, so the tier is a divergence.
        verdict = _run_filled(
            {"client_request_id": "rid-x", "entry": 5.0, "stop": 4.0},
            filled_quantity=_QTY_EPS,
            details={"uic": "123"},
            cross_check=_cross_check(owned={"123": 100.0}),
        )
        self.assertTrue(verdict.divergence)
        self.assertNotEqual(verdict.note, _NETTED_NOTE)

    def test_live_owned_and_filled_tier_matches_by_uic(self):
        # Positive control for the uic block: a genuine live position (owned and
        # filled both clear _QTY_EPS) DOES yield the netted-tier note, so the
        # divergence assertions above pin the guards rather than a dead branch.
        verdict = _run_filled(
            {"client_request_id": "rid-x", "entry": 5.0, "stop": 4.0},
            filled_quantity=5.0,
            details={"uic": "123"},
            cross_check=_cross_check(owned={"123": 100.0}),
        )
        self.assertFalse(verdict.divergence)
        self.assertEqual(verdict.note, _NETTED_NOTE)


class TestHelperSanity(unittest.TestCase):
    def test_uic_key_normalises_none_to_empty(self):
        # Anchors the falsy-uic_key precondition used by id 192.
        self.assertEqual(_uic_key(None), "")
        self.assertEqual(_uic_key(123), "123")


if __name__ == "__main__":
    unittest.main()
