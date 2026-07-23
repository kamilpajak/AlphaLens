"""Mutation-hardening for ``brokers/contract.py``.

The baseline cosmic-ray run over the broker package left 6 KILLABLE survivors in
``contract.py`` (the other 33 are EQUIVALENT ``|``-in-a-type-annotation swaps that
never evaluate under ``from __future__ import annotations``). The killable gaps are
in the two structured error classifiers and the two capability-Protocol
``@runtime_checkable`` decorators — both money-critical: a mis-classified Saxo
rejection makes the auto-manager defer or crash on the wrong condition, and a
Protocol that no longer answers ``isinstance`` silently disables a capability
narrow (standalone-stop / OCO exit) so protection degrades unnoticed.

Killable survivors pinned here:
  - L85 ``_is_sell_orders_already_exist``: ``==`` -> ``is`` and ``==`` -> ``>=``
  - L96 ``_is_too_far_from_entry``:        ``==`` -> ``is`` and ``==`` -> ``>=``
  - L236 ``@runtime_checkable`` on ``SupportsStandaloneStop``
  - L260 ``@runtime_checkable`` on ``SupportsOcoExit``

Hand anchors pin the exact discriminating cases; the hypothesis property is the
supplement — it sweeps the whole string space so the ``>=`` (ordering) mutant
cannot hide behind a hand-picked negative.
"""

from __future__ import annotations

import unittest

from alphalens_pipeline.brokers.contract import (
    BrokerError,
    OrderRejectedError,
    SupportsOcoExit,
    SupportsStandaloneStop,
    _is_sell_orders_already_exist,
    _is_too_far_from_entry,
)
from hypothesis import given, settings
from hypothesis import strategies as st

_SELL_CODE = "SellOrdersAlreadyExistForOwnedContracts"
_TOOFAR_CODE = "TooFarFromEntryOrder"


def _distinct_copy(s: str) -> str:
    """A value-equal but NON-interned copy of ``s`` (a fresh str object).

    ``"".join(list(s))`` defeats CPython literal interning, so ``copy is s_literal``
    is False while ``copy == s_literal`` is True — the exact wedge that separates
    ``==`` (value) from the ``is`` (identity) mutant.
    """
    return "".join(list(s))


class TestSellOrdersClassifierValueEquality(unittest.TestCase):
    """L85: the classifier must compare error_code by VALUE, not identity/order."""

    def test_matches_a_distinct_equal_string_object_kills_is(self) -> None:
        # A different str object with the same value: `==` -> True, `is` -> False.
        code = _distinct_copy(_SELL_CODE)
        self.assertIsNot(code, _SELL_CODE)  # guard: really a distinct object
        err = OrderRejectedError("rejected", error_code=code)
        self.assertTrue(_is_sell_orders_already_exist(err))

    def test_lexicographically_greater_nonmatch_is_false_kills_ge(self) -> None:
        # "ZZZ..." >= "Sell..." is True, so the `>=` mutant would mis-fire; the
        # correct `==` classifier returns False.
        err = OrderRejectedError("rejected", error_code="ZZZ_not_this_error_code")
        self.assertFalse(_is_sell_orders_already_exist(err))

    def test_none_error_code_is_false_not_raises(self) -> None:
        # None error_code: `== str` is False; `>= str` would raise TypeError.
        err = OrderRejectedError("rejected", error_code=None)
        self.assertFalse(_is_sell_orders_already_exist(err))

    def test_non_order_rejected_error_is_false(self) -> None:
        self.assertFalse(_is_sell_orders_already_exist(BrokerError("boom")))


class TestTooFarClassifierValueEquality(unittest.TestCase):
    """L96: same value-equality contract for the TooFarFromEntry classifier."""

    def test_matches_a_distinct_equal_string_object_kills_is(self) -> None:
        code = _distinct_copy(_TOOFAR_CODE)
        self.assertIsNot(code, _TOOFAR_CODE)
        err = OrderRejectedError("rejected", error_code=code)
        self.assertTrue(_is_too_far_from_entry(err))

    def test_lexicographically_greater_nonmatch_is_false_kills_ge(self) -> None:
        err = OrderRejectedError("rejected", error_code="ZZZ_not_this_error_code")
        self.assertFalse(_is_too_far_from_entry(err))

    def test_none_error_code_is_false_not_raises(self) -> None:
        err = OrderRejectedError("rejected", error_code=None)
        self.assertFalse(_is_too_far_from_entry(err))

    def test_non_order_rejected_error_is_false(self) -> None:
        self.assertFalse(_is_too_far_from_entry(BrokerError("boom")))


class TestClassifierExactCodeProperty(unittest.TestCase):
    """PBT supplement: each classifier fires for EXACTLY its own code and nothing
    else. Sweeping the string space pins the `>=` ordering mutant (which fires on
    every code lexicographically >= the sentinel) without a hand-picked negative."""

    @settings(deadline=None, max_examples=300)
    @given(code=st.text())
    def test_sell_classifier_true_iff_exact_code(self, code: str) -> None:
        err = OrderRejectedError("rejected", error_code=code)
        self.assertEqual(_is_sell_orders_already_exist(err), code == _SELL_CODE)

    @settings(deadline=None, max_examples=300)
    @given(code=st.text())
    def test_too_far_classifier_true_iff_exact_code(self, code: str) -> None:
        err = OrderRejectedError("rejected", error_code=code)
        self.assertEqual(_is_too_far_from_entry(err), code == _TOOFAR_CODE)


class _StopConformant:
    """Structurally satisfies SupportsStandaloneStop (method presence only)."""

    def place_standalone_stop(
        self, uic: int, side: str, qty: float, stop_price: float, request_id: str | None = None
    ):  # pragma: no cover - never called, presence is what matters
        raise NotImplementedError


class _OcoConformant:
    """Structurally satisfies SupportsOcoExit."""

    def place_oco_exit(
        self,
        uic: int,
        side: str,
        qty: float,
        stop_price: float,
        take_profit: float,
        request_id: str,
        position_id: str | None = None,
    ):  # pragma: no cover - never called, presence is what matters
        raise NotImplementedError


class TestCapabilityProtocolsAreRuntimeCheckable(unittest.TestCase):
    """L236 / L260: the two capability Protocols MUST keep ``@runtime_checkable`` —
    the auto-manager narrows a Broker to them via ``isinstance`` to decide whether
    to place a standalone stop / an OCO exit. Without the decorator ``isinstance``
    against a Protocol raises TypeError, so these calls assert a real bool result."""

    def test_standalone_stop_protocol_isinstance_true_for_conformant(self) -> None:
        self.assertIsInstance(_StopConformant(), SupportsStandaloneStop)

    def test_standalone_stop_protocol_isinstance_false_for_nonconformant(self) -> None:
        self.assertNotIsInstance(object(), SupportsStandaloneStop)

    def test_oco_exit_protocol_isinstance_true_for_conformant(self) -> None:
        self.assertIsInstance(_OcoConformant(), SupportsOcoExit)

    def test_oco_exit_protocol_isinstance_false_for_nonconformant(self) -> None:
        self.assertNotIsInstance(object(), SupportsOcoExit)


if __name__ == "__main__":
    unittest.main()
