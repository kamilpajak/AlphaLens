# Live-Market Execution — INC-1 (Market-Order Adapter) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Saxo market-order path (market BUY entry / market SELL exit tranche) as a capability-protocol method, mirroring the existing `place_standalone_stop`, with hermetic tests — the foundation the live-trigger engine (INC-3/4) will fire.

**Architecture:** A new `SupportsMarketOrders` capability Protocol on the broker contract; `SaxoBroker.place_market_order` builds a `{"OrderType":"Market"}` body (no `OrderPrice`, `DayOrder` duration) and reuses the existing ALLOW_ORDERS gate → precheck → single POST → `_handle_placement_response` chain; the acceptance `FakeBroker` gains a matching in-memory `place_market_order`. No caller is wired yet (INC-1 is inert until INC-3).

**Tech Stack:** Python 3.12, `unittest`, the `broker_contract` leaf + `alphalens_pipeline.brokers.saxo` adapter.

## Global Constraints

- SIM-only; placement stays gated on `ALPHALENS_BROKER_ALLOW_ORDERS=1` (raise `BrokerCapabilityError` when unset).
- Order side MUST be canonical `"BUY"`/`"SELL"` (uppercase) — `_require_order_side` raises `ValueError` naming both forms BEFORE any client call (incident 2026-07: a Saxo-form `"Sell"` silently flipped to a BUY stop).
- Research tests MUST subclass `unittest.TestCase` (pytest-style is silently skipped in CI). English-only in code/comments.
- Commits: Conventional Commits, `git commit -s` (DCO), sign-off `Kamil Pająk <kamilpajak@users.noreply.github.com>` (diacritic required); NO mention of AI/Claude.
- `alphalens_pipeline` must not import `alphalens_research`. `broker_contract` stays a dependency-free leaf (no vendor imports).
- Run tests from `apps/alphalens-research` with the workspace venv: `../../.venv/bin/python -m unittest <dotted.path> -v`.
- Market body carries NO `OrderPrice` (a market order has no price) and uses `OrderDuration.DurationType = "DayOrder"` (immediate; GTC is nonsensical for market). `ManualOrder` = `execution_policy._MANUAL_ORDER` (False). `ExternalReference` = the x-request-id.
- LIVE-ONLY question deferred to the 15:30 SIM probe: whether Saxo `precheck` accepts a price-less market body. Hermetic tests assume it does (stub returns Ok); if the probe shows otherwise, a follow-up drops precheck for market only.

---

### Task 1: `SupportsMarketOrders` capability Protocol

**Files:**
- Modify: `apps/alphalens-broker-contract/broker_contract/contract.py` (add Protocol after `SupportsAmendStop` ~line 353; add name to `__all__` ~line 356)
- Test: `apps/alphalens-research/tests/brokers/test_contract_mutation_hardening.py` (extend — it already imports the contract surface) OR a focused new assertion in `test_broker_contract.py`. Use `test_saxo_broker_market.py` (Task 2) as the behavioral test; here just assert the Protocol exists and is `runtime_checkable`.

**Interfaces:**
- Produces: `SupportsMarketOrders` Protocol with `place_market_order(self, uic: int, side: str, qty: float, request_id: str | None = None) -> PlacedOrder`.

- [ ] **Step 1: Write the failing test** — append to `apps/alphalens-research/tests/brokers/test_saxo_broker_market.py` (created in Task 2, but this import-level test can live in `test_broker_contract.py`; if unsure, put it at the top of the Task-2 file):

```python
def test_supports_market_orders_is_runtime_checkable_protocol():
    from broker_contract.contract import SupportsMarketOrders
    from typing import runtime_checkable
    # A trivial object with the method structurally satisfies the Protocol.
    class _M:
        def place_market_order(self, uic, side, qty, request_id=None):
            return None
    assert isinstance(_M(), SupportsMarketOrders)
    class _N:  # missing the method
        pass
    assert not isinstance(_N(), SupportsMarketOrders)
```

- [ ] **Step 2: Run it, expect FAIL** (`ImportError: cannot import name 'SupportsMarketOrders'`)

Run: `cd apps/alphalens-research && ../../.venv/bin/python -m unittest tests.brokers.test_saxo_broker_market -v`

- [ ] **Step 3: Implement** — in `contract.py`, after the `SupportsAmendStop` class:

```python
@runtime_checkable
class SupportsMarketOrders(Protocol):
    """Extension capability: fire a MARKET order (BUY entry / SELL exit tranche).

    The live-market execution model (docs/research/live_market_execution_model_
    design_2026_08_05.md): entries and take-profit tranches are realized as market
    orders off the price stream, not as resting Limit/OCO orders. Off the frozen
    base :class:`Broker` Protocol (capability-protocol pattern, like
    :class:`SupportsStandaloneStop`): a caller ``isinstance``-narrows a ``Broker``
    to this Protocol; a broker without it runs the resting-order path unchanged.

    ``side`` is canonical ``"BUY"``/``"SELL"``. ``request_id`` is the POST
    x-request-id (Saxo 15 s dedup) and the per-order ``ExternalReference`` — pass a
    DETERMINISTIC value so a crash-window re-POST does not double-fire; ``None``
    mints a fresh uuid4. Returns ``PlacedOrder(entry_order_id=<order id>,
    exit_order_ids=())``.
    """

    def place_market_order(
        self, uic: int, side: str, qty: float, request_id: str | None = None
    ) -> PlacedOrder: ...
```

Add `"SupportsMarketOrders",` to `__all__` (keep alphabetical-ish with the other `Supports*`).

- [ ] **Step 4: Run it, expect PASS**

Run: `cd apps/alphalens-research && ../../.venv/bin/python -m unittest tests.brokers.test_saxo_broker_market -v`

- [ ] **Step 5: Commit**

```bash
git add apps/alphalens-broker-contract/broker_contract/contract.py apps/alphalens-research/tests/brokers/test_saxo_broker_market.py
git commit -s -m "feat(broker-contract): add SupportsMarketOrders capability protocol"
```

---

### Task 2: `SaxoBroker.place_market_order` + `_build_market_order_body`

**Files:**
- Modify: `apps/alphalens-pipeline/alphalens_pipeline/brokers/execution.py` (add `_MARKET_ORDER_DURATION = "DayOrder"` next to `_EXIT_DURATION` ~line 100)
- Modify: `apps/alphalens-pipeline/alphalens_pipeline/brokers/saxo/broker.py` (add the two methods after `place_standalone_stop`/`_build_standalone_stop_body` ~line 546)
- Test: `apps/alphalens-research/tests/brokers/test_saxo_broker_market.py` (new — mirror `test_saxo_broker_standalone_stop.py`)

**Interfaces:**
- Consumes: `SupportsMarketOrders` (Task 1); existing `_require_order_side`, `_SIDE_TO_SAXO_BUY_SELL`, `ALLOW_ORDERS_ENV`, `_translate_saxo_errors`, `self._resolve_account_key`, `self._quantize_price` (unused here), `self._precheck_or_raise`, `self._client.place_order`, `self._handle_placement_response`, `execution_policy._MANUAL_ORDER`.
- Produces: `SaxoBroker.place_market_order(uic, side, qty, request_id=None) -> PlacedOrder`.

- [ ] **Step 1: Write the failing tests** — create `test_saxo_broker_market.py`, mirroring the standalone-stop test file. Reuse the `_StubStopClient` shape (rename to `_StubMarketClient`) and `_DETAILS_KO` (its `SupportedOrderTypes` already includes `"Market"`):

```python
from __future__ import annotations
import unittest, uuid
from typing import Any
from unittest import mock
from alphalens_pipeline.brokers.saxo.broker import ALLOW_ORDERS_ENV, SaxoBroker
from broker_contract.contract import BrokerCapabilityError, OrderRejectedError, PlacedOrder

_ALLOW = {ALLOW_ORDERS_ENV: "1"}
_ACCOUNTS = {"Data": [{"AccountKey": "AK-1", "AccountId": "16371XYZ", "Currency": "USD"}]}
_DETAILS_KO = {
    "Uic": 307, "AssetType": "Stock",
    "Format": {"Decimals": 2, "OrderDecimals": 2},
    "TickSizeScheme": {"DefaultTickSize": 0.01, "Elements": [{"HighPrice": 0.9999, "TickSize": 0.0001}]},
    "SupportedOrderTypes": ["Limit", "Market", "Stop", "StopIfTraded", "StopLimit"],
}

class _StubMarketClient:
    def __init__(self, *, details=None,
                 precheck_response=(200, {"PreCheckResult": "Ok"}),
                 place_response=(200, {"OrderId": "M-900"})):
        self.details = details or _DETAILS_KO
        self.precheck_response = precheck_response
        self.place_response = place_response
        self.precheck_calls: list[dict[str, Any]] = []
        self.place_calls: list[tuple[dict[str, Any], str]] = []
    def get_accounts(self): return _ACCOUNTS
    def get_instrument_details(self, uic, asset_type="Stock"): return dict(self.details)
    def precheck_order(self, body): self.precheck_calls.append(body); return self.precheck_response
    def place_order(self, body, *, request_id): self.place_calls.append((body, request_id)); return self.place_response

def _make(stub): return SaxoBroker(stub), stub  # type: ignore[arg-type]

class TestMarketOrderBody(unittest.TestCase):
    def test_body_is_market_no_price_dayorder_buy(self):
        broker, stub = _make(_StubMarketClient())
        with mock.patch.dict("os.environ", _ALLOW):
            placed = broker.place_market_order(uic=307, side="BUY", qty=2)
        body, request_id = stub.place_calls[0]
        self.assertNotIn("Orders", body)
        self.assertNotIn("OrderPrice", body, "a market order carries NO price")
        self.assertEqual(body["Uic"], 307)
        self.assertEqual(body["AssetType"], "Stock")
        self.assertEqual(body["AccountKey"], "AK-1")
        self.assertEqual(body["OrderType"], "Market")
        self.assertEqual(body["BuySell"], "Buy")
        self.assertEqual(body["Amount"], 2)
        self.assertEqual(body["OrderDuration"], {"DurationType": "DayOrder"})
        self.assertIs(body["ManualOrder"], False)
        self.assertEqual(body["ExternalReference"], request_id)
        self.assertEqual(placed.entry_order_id, "M-900")
        self.assertEqual(placed.exit_order_ids, ())
        self.assertIsInstance(placed, PlacedOrder)

    def test_sell_side_mirrors(self):
        broker, stub = _make(_StubMarketClient())
        with mock.patch.dict("os.environ", _ALLOW):
            broker.place_market_order(uic=307, side="SELL", qty=3)
        body, _ = stub.place_calls[0]
        self.assertEqual(body["BuySell"], "Sell")
        self.assertEqual(body["Amount"], 3)

    def test_explicit_request_id_reused_as_external_reference(self):
        broker, stub = _make(_StubMarketClient())
        with mock.patch.dict("os.environ", _ALLOW):
            broker.place_market_order(uic=307, side="SELL", qty=2, request_id="rid-tp1")
        body, request_id = stub.place_calls[0]
        self.assertEqual(request_id, "rid-tp1")
        self.assertEqual(body["ExternalReference"], "rid-tp1")

    def test_request_id_defaults_to_uuid(self):
        broker, stub = _make(_StubMarketClient())
        with mock.patch.dict("os.environ", _ALLOW):
            broker.place_market_order(uic=307, side="BUY", qty=2)
        _, request_id = stub.place_calls[0]
        uuid.UUID(request_id)

class TestMarketOrderSafety(unittest.TestCase):
    def test_allow_orders_gate_blocks_before_any_client_call(self):
        broker, stub = _make(_StubMarketClient())
        for env in ({}, {ALLOW_ORDERS_ENV: "0"}, {ALLOW_ORDERS_ENV: "true"}):
            with self.subTest(env=env), mock.patch.dict("os.environ", env, clear=True):
                with self.assertRaises(BrokerCapabilityError) as ctx:
                    broker.place_market_order(uic=307, side="BUY", qty=2)
                self.assertIn(ALLOW_ORDERS_ENV, str(ctx.exception))
        self.assertEqual(stub.place_calls, [], "gate must fire before any POST")

    def test_saxo_form_side_rejected_before_any_http(self):
        broker, stub = _make(_StubMarketClient())
        for bad in ("Sell", "Buy", "sell", "buy", ""):
            with self.subTest(side=bad), mock.patch.dict("os.environ", _ALLOW):
                with self.assertRaises(ValueError) as ctx:
                    broker.place_market_order(uic=307, side=bad, qty=2)
                self.assertIn("'BUY'", str(ctx.exception))
                self.assertIn("'SELL'", str(ctx.exception))
        self.assertEqual(stub.place_calls, [], "invalid side must never POST")

    def test_unsupported_market_type_rejected_pre_post(self):
        no_mkt = dict(_DETAILS_KO); no_mkt["SupportedOrderTypes"] = ["Limit", "StopIfTraded"]
        broker, stub = _make(_StubMarketClient(details=no_mkt))
        with mock.patch.dict("os.environ", _ALLOW):
            with self.assertRaises(OrderRejectedError) as ctx:
                broker.place_market_order(uic=307, side="BUY", qty=2)
        self.assertIn("Market", str(ctx.exception))
        self.assertEqual(stub.place_calls, [], "unsupported type must never POST")

    def test_failed_precheck_blocks_post(self):
        stub = _StubMarketClient(precheck_response=(200, {"PreCheckResult": "Error",
            "ErrorInfo": {"ErrorCode": "OrderValueToSmall", "Message": "too small"}}))
        broker, _ = _make(stub)
        with mock.patch.dict("os.environ", _ALLOW):
            with self.assertRaises(OrderRejectedError) as ctx:
                broker.place_market_order(uic=307, side="BUY", qty=2)
        self.assertEqual(stub.place_calls, [], "failed precheck must block the POST")
        self.assertIn("OrderValueToSmall", str(ctx.exception))

if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run, expect FAIL** (`AttributeError: 'SaxoBroker' object has no attribute 'place_market_order'`)

Run: `cd apps/alphalens-research && ../../.venv/bin/python -m unittest tests.brokers.test_saxo_broker_market -v`

- [ ] **Step 3: Implement**

In `execution.py`, next to `_EXIT_DURATION = "GoodTillCancel"`:

```python
_MARKET_ORDER_DURATION = "DayOrder"  # a market order fills immediately; GTC is nonsensical
```

In `saxo/broker.py`, after `_build_standalone_stop_body`:

```python
    def place_market_order(
        self, uic: int, side: str, qty: float, request_id: str | None = None
    ) -> PlacedOrder:
        """Fire ONE market order (BUY entry / SELL exit tranche) — live-market model.

        Same safety order as :meth:`place_standalone_stop`: canonical-side check,
        ALLOW_ORDERS gate, precheck, then ONE POST with x-request-id =
        ``request_id``. No ``OrderPrice`` (market), ``DayOrder`` duration. Pass a
        DETERMINISTIC ``request_id`` so a crash-window re-POST hits Saxo's 15 s
        dedup instead of double-firing; ``None`` mints a fresh uuid4.
        ``exit_order_ids`` is empty.
        """
        _require_order_side(side)
        if os.environ.get(ALLOW_ORDERS_ENV) != "1":
            raise BrokerCapabilityError(
                f"order placement is disabled: set {ALLOW_ORDERS_ENV}=1 to allow "
                "SIM order submission (design memo §P2 safety rail). No order was sent."
            )
        client_request_id = request_id or str(uuid.uuid4())
        with _translate_saxo_errors():
            account_key = self._resolve_account_key()
            body = self._build_market_order_body(uic, side, qty, client_request_id, account_key)
            self._precheck_or_raise(body, label=f"market Uic {uic} {client_request_id}")
            status, payload = self._client.place_order(body, request_id=client_request_id)
            return self._handle_placement_response(status, payload, client_request_id, account_key)

    def _build_market_order_body(
        self, uic: int, side: str, qty: float, client_request_id: str, account_key: str
    ) -> dict[str, Any]:
        """Market order body — no Orders array, no OrderPrice, DayOrder duration."""
        asset_type = "Stock"  # MVP scope: single-name equities only
        details = self._client.get_instrument_details(uic, asset_type)
        supported = details.get("SupportedOrderTypes") or []
        if supported and "Market" not in supported:
            raise OrderRejectedError(
                f"instrument Uic {uic} does not support Market orders "
                f"(SupportedOrderTypes={supported})"
            )
        return {
            "Uic": int(uic),
            "AssetType": asset_type,
            "AccountKey": account_key,
            "Amount": qty,
            "BuySell": _SIDE_TO_SAXO_BUY_SELL[side],
            "OrderType": "Market",
            "OrderDuration": {"DurationType": execution_policy._MARKET_ORDER_DURATION},
            "ManualOrder": execution_policy._MANUAL_ORDER,
            "ExternalReference": client_request_id,
        }
```

(`OrderRejectedError`, `BrokerCapabilityError`, `_require_order_side`, `_SIDE_TO_SAXO_BUY_SELL`, `os`, `uuid`, `execution_policy`, `Any` are already imported in broker.py — confirm; do not re-import.)

- [ ] **Step 4: Run, expect PASS**

Run: `cd apps/alphalens-research && ../../.venv/bin/python -m unittest tests.brokers.test_saxo_broker_market -v`

- [ ] **Step 5: Commit**

```bash
git add apps/alphalens-pipeline/alphalens_pipeline/brokers/execution.py \
        apps/alphalens-pipeline/alphalens_pipeline/brokers/saxo/broker.py \
        apps/alphalens-research/tests/brokers/test_saxo_broker_market.py
git commit -s -m "feat(brokers): SaxoBroker.place_market_order (live-market model INC-1)"
```

---

### Task 3: `FakeBroker.place_market_order` (acceptance in-memory)

**Files:**
- Modify: `apps/alphalens-research/tests/brokers/automanager/acceptance/fake_broker.py` (add method after `amend_stop_amount` ~line 254)
- Test: `apps/alphalens-research/tests/brokers/automanager/acceptance/test_fake_broker_market.py` (new)

**Interfaces:**
- Consumes: existing `FakeBroker` internals — `self.uic_of`, `self._positions`, `self._instrument`, `self._guard_write`, `Position`, `_QTY_EPS`, `PlacedOrder`, `dataclasses.replace`.
- Produces: `FakeBroker.place_market_order(uic, side, qty, request_id=None) -> PlacedOrder` that mutates the in-memory netted position (BUY adds, SELL reduces, never below 0).

- [ ] **Step 1: Write the failing test** — `test_fake_broker_market.py`:

```python
from __future__ import annotations
import unittest
from tests.brokers.automanager.acceptance.fake_broker import FakeBroker
from broker_contract.contract import SupportsMarketOrders

class TestFakeBrokerMarket(unittest.TestCase):
    def test_market_buy_opens_and_grows_the_netted_position(self):
        b = FakeBroker()
        uic = b.uic_of("KO")
        b.place_market_order(uic, "BUY", 100)
        self.assertEqual(b.get_positions_by_uic(uic).quantity, 100.0)
        b.place_market_order(uic, "BUY", 50)
        self.assertEqual(b.get_positions_by_uic(uic).quantity, 150.0)

    def test_market_sell_reduces_and_never_below_zero(self):
        b = FakeBroker()
        uic = b.uic_of("KO")
        b.place_market_order(uic, "BUY", 100)
        b.place_market_order(uic, "SELL", 30)
        self.assertEqual(b.get_positions_by_uic(uic).quantity, 70.0)
        b.place_market_order(uic, "SELL", 999)  # oversell clamps to flat
        self.assertEqual(b.get_positions_by_uic(uic).quantity, 0.0)

    def test_satisfies_capability_protocol(self):
        self.assertIsInstance(FakeBroker(), SupportsMarketOrders)

if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run, expect FAIL** (`AttributeError: ... 'place_market_order'`)

Run: `cd apps/alphalens-research && ../../.venv/bin/python -m unittest tests.brokers.automanager.acceptance.test_fake_broker_market -v`

- [ ] **Step 3: Implement** — in `fake_broker.py`, after `amend_stop_amount`:

```python
    def place_market_order(
        self, uic: int, side: str, qty: float, request_id: str | None = None
    ) -> PlacedOrder:
        """In-memory market fill: BUY grows the netted position, SELL reduces it
        (clamped at flat — the netting account never flips short on an oversell).
        Deterministic order id; no resting order is created (a market order fills)."""
        self._guard_write(uic)
        current = self._positions.get(uic)
        held = current.quantity if current is not None else 0.0
        delta = float(qty) if side == "BUY" else -float(qty)
        new_qty = max(held + delta, 0.0)
        self._seq += 1
        order_id = f"mkt-{self._seq}"
        if new_qty <= _QTY_EPS:
            self._positions.pop(uic, None)
        elif current is not None:
            self._positions[uic] = dataclasses.replace(current, quantity=new_qty)
        else:
            ticker = self._ticker_by_uic.get(uic, f"UIC{uic}")
            self._positions[uic] = Position(
                instrument=self._instrument(ticker),
                quantity=new_qty,
                avg_price=0.0,
                market_value=None,
                unrealized_pnl=None,
                position_id=f"pos-{uic}",
            )
        return PlacedOrder(entry_order_id=order_id, exit_order_ids=())
```

- [ ] **Step 4: Run, expect PASS** (also run the full acceptance suite to confirm no regression)

Run: `cd apps/alphalens-research && ../../.venv/bin/python -m unittest tests.brokers.automanager.acceptance.test_fake_broker_market -v`
Run: `cd apps/alphalens-research && ../../.venv/bin/python -m unittest discover -s tests/brokers/automanager/acceptance -t . -v`

- [ ] **Step 5: Commit**

```bash
git add apps/alphalens-research/tests/brokers/automanager/acceptance/fake_broker.py \
        apps/alphalens-research/tests/brokers/automanager/acceptance/test_fake_broker_market.py
git commit -s -m "test(brokers): FakeBroker.place_market_order for the acceptance suite"
```

---

## Final gate (whole increment)

- [ ] `cd apps/alphalens-research && ../../.venv/bin/python -m unittest discover -s tests/brokers -t . -q` — all green.
- [ ] `../../.venv/bin/python -m unittest tests.test_module_dependencies -v` — DAG intact (`alphalens_pipeline` ↛ `alphalens_research`; `broker_contract` leaf clean).
- [ ] `../../.venv/bin/ruff check ../alphalens-broker-contract ../alphalens-pipeline/alphalens_pipeline/brokers` — clean.
- [ ] Open PR; zen `deepseek/deepseek-v4-pro` (thinking=high) pre-merge; apply findings as additional commits; merge on green CI.

## Self-review

- **Spec coverage:** memo §3.2 (market adapter NEW) → Tasks 1-2; "fake-broker support" → Task 3. INC-1 scope fully covered. INC-2..5 out of scope by design (planned post-probe).
- **Placeholder scan:** none — every step has complete code.
- **Type consistency:** `place_market_order(uic, side, qty, request_id=None) -> PlacedOrder` identical across Protocol (Task 1), SaxoBroker (Task 2), FakeBroker (Task 3); `_MARKET_ORDER_DURATION`/`"DayOrder"` consistent.
- **Live-only caveat:** precheck-accepts-market-body is validated at the 15:30 SIM probe, not here (hermetic stub returns Ok).
