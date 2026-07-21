# Saxo Auto-Manager MVP Implementation Plan

> For agentic workers: execute this plan with **superpowers:subagent-driven-development** — each task is an independent, TDD-shaped unit (red→green→commit) that a subagent can own end-to-end. Tasks are ordered by dependency; a task may assume every lower-numbered task has merged. All tests are `unittest.TestCase` run via `unittest discover`.

**Goal.** Give the deferred trade-side exit-manager (ADR-0013 T6 IN-FLIGHT / T7 EXIT) a live SIM consumer: a human-picked candidate is sized, placed (in-band bracket subset + one always-standalone disaster stop), then watched to a terminal state by an always-on polling daemon that reconciles, self-heals on crash, keeps the OAuth chain alive, and can be killed instantly.

**Architecture.** Semi-auto: the human cherry-picks off the read-only web card and types `alphalens broker arm` (appends to `picks.jsonl`); an always-on VPS `systemd --user` polling daemon (`alphalens broker manage`) drains picks, places orders, and manages each base position to terminal, recomputing all status each tick from the shipped read-only `reconcile_brackets` engine (no durable in-memory state). Fill detection is a **pluggable fill-source** (polling MVP; streaming is a phase-B drop-in behind the same `poll_tick()` interface). The disaster stop is **always** a standalone `StopIfTraded` placed after the entry fills and sized to realized qty (never an OCO child), accepting a ~30–60 s unprotected window on SIM.

**Tech Stack.** Python (pipeline package `alphalens_pipeline.brokers.automanager`), `unittest` (discover, no pytest), Saxo OpenAPI **SIM** via the shipped `SaxoBroker`/`SaxoClient`, `systemd --user` units + Prometheus textfile metrics on the VPS.

---

## Global Constraints

- **SIM-only structural rail** — `SaxoClient` refuses any non-SIM base URL; `LIVE_TRADING_ENABLED=False`. LIVE is unreachable in code. The `$1000`/`~$100` live escape is a future separate ADR + separate env (`ALPHALENS_BROKER_LIVE=1`), never in scope here.
- **ALLOW_ORDERS master arm** — every real POST is gated on `ALPHALENS_BROKER_ALLOW_ORDERS=1` (enforced inside the broker + re-checked in the safety-gate). `cancel_order`/`precheck` stay ungated (safe ops).
- **Append-only journals** — `picks.jsonl` + `submissions.jsonl` are never rewritten; `status` is a recorded fact per line, never mutated in place (T8 config-version-cohort discipline: live intent lines never pool with broker-free replays). Malformed/undated lines are skipped, not fatal; a missing file is a valid empty state.
- **Disaster stop ALWAYS standalone-after-fill** — never a bracket child (`bracket.stop_loss` is always `None`); represented exactly once at the plan level; placed once as an Option-B standalone `StopIfTraded` after the entry fills, sized to **realized** filled qty (memo Risk 2).
- **Fill-source pluggable** — the MVP ships `PollingFillSource` only; `StreamingFillSource` is a documented phase-B seam, NOT built.
- **Tests** — `unittest.TestCase` subclasses run via `unittest discover` (pytest-style is silently skipped in CI). TDD always: red→green→commit even for small changes.
- **English-only** in code/comments/identifiers. **Conventional Commits.** **ADR-0011 placement** — all new live-infra lands under `apps/alphalens-pipeline/alphalens_pipeline/brokers/automanager/`; CLI under `apps/alphalens-pipeline/alphalens_cli/commands/broker.py`; tests under `apps/alphalens-research/tests/brokers/automanager/`.
- **Worktree preflight (once)** — this plan edits pipeline package code, so the worktree needs its OWN `uv sync` or `alphalens_*` imports resolve to the wrong tree:
  ```bash
  git worktree add -b feature/saxo-automanager .claude/worktrees/automanager origin/main
  cd .claude/worktrees/automanager && uv sync
  .venv/bin/python -c "import alphalens_pipeline.brokers.saxo.broker as m; print(m.__file__)"  # must be INSIDE the worktree
  ```
  All commands below run from the worktree root.
- **Canonical package `__init__.py`** — first created in **Task 2** (with `__status__ = "ACTIVE"` to match the sibling `brokers/__init__.py` house style; `brokers/` is not a research `LAYER_ROOT` so `__status__` is optional but harmless and needs no `__closed_*` fields). Every later task treats it as create-if-missing and keeps it unchanged if already present.

---

## File Structure

**Created**
| File | Responsibility |
|---|---|
| `.../brokers/automanager/__init__.py` | Package marker + `__status__ = "ACTIVE"` (Task 2) |
| `.../brokers/automanager/placement_planner.py` | Option-C in-band-subset classifier: which TP legs are bracket children, which are operator-managed; disaster stop = one plan-level scalar |
| `.../brokers/automanager/picks.py` | Append-only pick queue (`Pick`, `arm_pick`, `iter_picks`) |
| `.../brokers/automanager/safety.py` | Pure-predicate portfolio safety gate (`check`) |
| `.../brokers/automanager/session_keeper.py` | OAuth token-chain liveness (`ChainStatus`, `SessionKeeper`) |
| `.../brokers/automanager/fill_source.py` | Pluggable fill detection (`FillSource` Protocol + `PollingFillSource`) |
| `.../brokers/automanager/reconcile_bridge.py` | Thin adapter over `reconcile_brackets` (`verdicts`) |
| `.../brokers/automanager/orphan_sweeper.py` | Read-only detector for placed-but-unjournaled orders/positions (`sweep`) |
| `.../brokers/automanager/position_manager.py` | Pure `advance(verdict, broker_view) -> Action` (the "act" half) |
| `.../brokers/automanager/control_loop.py` | Daemon shell: `LoopDeps`, `TickReport`, `run_once`, `run_daemon`, `build_default_deps` |
| `deploy/systemd/alphalens-broker-manager.service` | `Type=simple` daemon running `broker manage --poll-seconds 45` |
| `deploy/systemd/alphalens-saxo-refresh.service` | `Type=oneshot` idle OAuth keep-alive (`broker auth --refresh`) |
| `deploy/systemd/alphalens-saxo-refresh.timer` | ~20 min timer for the keep-alive |
| `apps/alphalens-research/tests/brokers/automanager/__init__.py` | Test package marker |
| `apps/alphalens-research/tests/brokers/automanager/test_*.py` | Hermetic tests per module |

**Modified**
| File | Change |
|---|---|
| `.../brokers/saxo/broker.py` | `place_standalone_stop` + two reuse refactors (`_precheck_or_raise(label=)`, `_handle_placement_response(request_id=)`) |
| `alphalens_cli/commands/broker.py` | `arm` + `manage` subcommands |
| `deploy/monitoring/prometheus/rules/alphalens.yaml` | broker-manager heartbeat + saxo-refresh job rules |
| `apps/alphalens-research/tests/test_deploy_systemd_units.py` | new unit + rule suites; `saxo-refresh` added to `ACTIVE_SERVICES` |

---

## Task 1 — `SaxoBroker.place_standalone_stop` (Option-B standalone `StopIfTraded`)

Net-new broker method: a protective stop POSTed as its own `/trade/v2/orders` with **no `Orders` array and no entry parent** cannot trip `TooFarFromEntryOrder`; Saxo resolves `relation=StandAlone` (SIM-validated 2026-07-20: KO qty 2 Sell `@61.36`, precheck 200 `Ok`, place 200 `OrderId=5039296412`). The disaster stop is NEVER a bracket child; the position-manager (Task 10) calls this after entry fill, sized to **realized** qty (memo Risk 2). Two mechanical refactors let it reuse the bracket precheck/response machinery.

**Files:** modify `apps/alphalens-pipeline/alphalens_pipeline/brokers/saxo/broker.py`; create `apps/alphalens-research/tests/brokers/test_saxo_broker_standalone_stop.py`; regression `apps/alphalens-research/tests/brokers/test_saxo_broker_orders.py` (stays green).

**Interfaces:**
- Consumes (verified): `SaxoBroker._resolve_account_key() -> str`; `._quantize_price(price, details, *, label) -> float`; `SaxoClient.get_instrument_details(uic, asset_type="Stock") -> dict`; `.precheck_order(body) -> tuple[int,dict]`; `.place_order(body, *, request_id) -> tuple[int,dict]`; `execution._STOP_ORDER_TYPE == "StopIfTraded"`, `execution._EXIT_DURATION == "GoodTillCancel"`, `execution._MANUAL_ORDER == False`; `contract.PlacedOrder(entry_order_id, exit_order_ids)`; `contract.{OrderRejectedError, BrokerCapabilityError, BrokerError}`; `broker.ALLOW_ORDERS_ENV == "ALPHALENS_BROKER_ALLOW_ORDERS"`.
- Produces: `SaxoBroker.place_standalone_stop(uic: int, side: str, qty: float, stop_price: float) -> PlacedOrder`.

**Steps:**

1. Write the FAILING test `test_saxo_broker_standalone_stop.py`:
```python
"""Hermetic tests for SaxoBroker.place_standalone_stop (Option-B standalone stop).

The disaster stop is NEVER a bracket child — placed as its own POST with NO
Orders array and no parent (relation=StandAlone; SIM-validated 2026-07-20: KO
qty 2 Sell @61.36, OrderId 5039296412). Pins body shape, ALLOW_ORDERS gate,
precheck-before-POST, realized (float) qty, empty-exits PlacedOrder.
"""

from __future__ import annotations

import unittest
import uuid
from typing import Any
from unittest import mock

from alphalens_pipeline.brokers.contract import (
    BrokerCapabilityError,
    OrderRejectedError,
    PlacedOrder,
)
from alphalens_pipeline.brokers.saxo.broker import ALLOW_ORDERS_ENV, SaxoBroker

_ALLOW = {ALLOW_ORDERS_ENV: "1"}
_ACCOUNTS = {"Data": [{"AccountKey": "AK-1", "AccountId": "16371XYZ", "Currency": "USD"}]}
_DETAILS_KO = {
    "Uic": 307,
    "AssetType": "Stock",
    "Format": {"Decimals": 2, "OrderDecimals": 2},
    "TickSizeScheme": {
        "DefaultTickSize": 0.01,
        "Elements": [{"HighPrice": 0.9999, "TickSize": 0.0001}],
    },
    "SupportedOrderTypes": ["Limit", "Market", "Stop", "StopIfTraded", "StopLimit"],
}


class _StubStopClient:
    """Minimal stub SaxoClient for the standalone-stop surface (records calls)."""

    def __init__(
        self,
        *,
        details: dict[str, Any] | None = None,
        precheck_response: tuple[int, dict[str, Any]] = (200, {"PreCheckResult": "Ok"}),
        place_response: tuple[int, dict[str, Any]] = (200, {"OrderId": "S-900"}),
    ):
        self.details = details or _DETAILS_KO
        self.precheck_response = precheck_response
        self.place_response = place_response
        self.precheck_calls: list[dict[str, Any]] = []
        self.place_calls: list[tuple[dict[str, Any], str]] = []

    def get_accounts(self) -> dict[str, Any]:
        return _ACCOUNTS

    def get_instrument_details(self, uic: int | str, asset_type: str = "Stock") -> dict[str, Any]:
        return dict(self.details)

    def precheck_order(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        self.precheck_calls.append(body)
        return self.precheck_response

    def place_order(self, body: dict[str, Any], *, request_id: str) -> tuple[int, dict[str, Any]]:
        self.place_calls.append((body, request_id))
        return self.place_response


def _make(stub: _StubStopClient) -> tuple[SaxoBroker, _StubStopClient]:
    return SaxoBroker(stub), stub  # type: ignore[arg-type]


class TestStandaloneStopBody(unittest.TestCase):
    def test_body_has_no_orders_array_and_is_stopiftraded(self):
        broker, stub = _make(_StubStopClient())
        with mock.patch.dict("os.environ", _ALLOW):
            placed = broker.place_standalone_stop(uic=307, side="SELL", qty=2, stop_price=61.36)
        body, request_id = stub.place_calls[0]
        self.assertNotIn("Orders", body, "a standalone stop carries NO Orders array (no parent)")
        self.assertEqual(body["Uic"], 307)
        self.assertEqual(body["AssetType"], "Stock")
        self.assertEqual(body["AccountKey"], "AK-1")
        self.assertEqual(body["OrderType"], "StopIfTraded")
        self.assertEqual(body["BuySell"], "Sell")
        self.assertEqual(body["Amount"], 2)
        self.assertEqual(body["OrderPrice"], 61.36)
        self.assertEqual(body["OrderDuration"], {"DurationType": "GoodTillCancel"})
        self.assertIs(body["ManualOrder"], False)
        self.assertEqual(body["ExternalReference"], request_id)
        self.assertEqual(placed.entry_order_id, "S-900")
        self.assertEqual(placed.exit_order_ids, ())
        self.assertIsInstance(placed, PlacedOrder)

    def test_buy_side_stop_mirrors(self):
        broker, stub = _make(_StubStopClient())
        with mock.patch.dict("os.environ", _ALLOW):
            broker.place_standalone_stop(uic=307, side="BUY", qty=2, stop_price=61.36)
        body, _ = stub.place_calls[0]
        self.assertEqual(body["BuySell"], "Buy")

    def test_amount_is_realized_qty_float_not_planned(self):
        broker, stub = _make(_StubStopClient())
        with mock.patch.dict("os.environ", _ALLOW):
            broker.place_standalone_stop(uic=307, side="SELL", qty=2.0, stop_price=61.36)
        body, _ = stub.place_calls[0]
        self.assertEqual(body["Amount"], 2.0)

    def test_request_id_is_uuid_and_reused_as_external_reference(self):
        broker, stub = _make(_StubStopClient())
        with mock.patch.dict("os.environ", _ALLOW):
            broker.place_standalone_stop(uic=307, side="SELL", qty=2, stop_price=61.36)
        body, request_id = stub.place_calls[0]
        uuid.UUID(request_id)
        self.assertEqual(body["ExternalReference"], request_id)


class TestStandaloneStopSafety(unittest.TestCase):
    def test_allow_orders_gate_blocks_before_any_client_call(self):
        broker, stub = _make(_StubStopClient())
        for env in ({}, {ALLOW_ORDERS_ENV: "0"}, {ALLOW_ORDERS_ENV: "true"}):
            with self.subTest(env=env):
                with mock.patch.dict("os.environ", env, clear=True):
                    with self.assertRaises(BrokerCapabilityError) as ctx:
                        broker.place_standalone_stop(uic=307, side="SELL", qty=2, stop_price=61.36)
                self.assertIn(ALLOW_ORDERS_ENV, str(ctx.exception))
        self.assertEqual(stub.precheck_calls, [], "gate must fire before precheck")
        self.assertEqual(stub.place_calls, [], "gate must fire before any POST")

    def test_precheck_runs_with_costs_and_blocks_on_not_ok(self):
        stub = _StubStopClient(
            precheck_response=(
                200,
                {"PreCheckResult": "Error",
                 "ErrorInfo": {"ErrorCode": "OrderValueToSmall", "Message": "too small"}},
            )
        )
        broker, _ = _make(stub)
        with mock.patch.dict("os.environ", _ALLOW):
            with self.assertRaises(OrderRejectedError) as ctx:
                broker.place_standalone_stop(uic=307, side="SELL", qty=2, stop_price=61.36)
        self.assertEqual(len(stub.precheck_calls), 1)
        self.assertEqual(stub.precheck_calls[0].get("FieldGroups"), ["Costs"])
        self.assertEqual(stub.place_calls, [], "a failed precheck must block the real POST")
        self.assertIn("OrderValueToSmall", str(ctx.exception))

    def test_unsupported_stop_type_rejected_pre_post(self):
        no_stop = dict(_DETAILS_KO)
        no_stop["SupportedOrderTypes"] = ["Limit", "Market"]
        broker, stub = _make(_StubStopClient(details=no_stop))
        with mock.patch.dict("os.environ", _ALLOW):
            with self.assertRaises(OrderRejectedError) as ctx:
                broker.place_standalone_stop(uic=307, side="SELL", qty=2, stop_price=61.36)
        self.assertIn("StopIfTraded", str(ctx.exception))
        self.assertEqual(stub.place_calls, [], "unsupported type must never POST")


if __name__ == "__main__":
    unittest.main()
```

2. Run it, expect FAIL (`AttributeError`):
```bash
.venv/bin/python -m unittest discover -s apps/alphalens-research/tests/brokers \
    -t apps/alphalens-research -p test_saxo_broker_standalone_stop.py -v
```

3. Implement in `broker.py`:
   - **(3a)** Add `import uuid` after `import os` in the stdlib block.
   - **(3b)** Refactor `_precheck_or_raise` to take `*, label: str` instead of the full request:
```python
    def _precheck_or_raise(self, body: dict[str, Any], *, label: str) -> dict[str, Any]:
        """POST /trade/v2/orders/precheck; non-Ok blocks the real POST."""
        precheck_body = dict(body)
        precheck_body["FieldGroups"] = ["Costs"]
        status, payload = self._client.precheck_order(precheck_body)
        error_info = payload.get("ErrorInfo")
        result = payload.get("PreCheckResult")
        if status >= 400 or error_info or (result is not None and result != "Ok"):
            raise OrderRejectedError(
                f"precheck rejected {label}: status={status} "
                f"PreCheckResult={result!r} ErrorInfo={error_info!r} — nothing was placed"
            )
        return payload
```
     Update its two call sites (`place_bracket_order`, `precheck_bracket_order`) to `self._precheck_or_raise(body, label=f"bracket {request.client_request_id} ({request.instrument.broker_symbol})")` (the latter returns it).
   - **(3c)** Refactor `_handle_placement_response` to take `request_id: str` instead of `request`, replacing every `request.client_request_id` inside with `request_id`:
```python
    def _handle_placement_response(
        self, status: int, payload: dict[str, Any], request_id: str, account_key: str,
    ) -> PlacedOrder:
        if status in (200, 201):
            order_id = payload.get("OrderId")
            if not order_id:
                raise BrokerError(
                    f"Saxo placement returned HTTP {status} without an OrderId "
                    f"for {request_id}: {payload!r}"
                )
            exit_ids = tuple(
                str(child["OrderId"])
                for child in payload.get("Orders") or []
                if isinstance(child, dict) and child.get("OrderId")
            )
            return PlacedOrder(entry_order_id=str(order_id), exit_order_ids=exit_ids)
        if status == 202:
            order_id = self._find_order_id(payload)
            raise BrokerError(
                f"Saxo 202 TradeNotCompleted for {request_id}: "
                f"entry order {order_id} is likely LIVE while its related exits were "
                "CANCELLED by Saxo (naked-entry hazard). No automatic action taken — "
                "order state is genuinely unknown for ~seconds. Reconcile: poll "
                "'alphalens broker orders', then 'alphalens broker cancel "
                f"{order_id}' if the entry is unwanted."
            )
        detail = self._rejection_detail(payload)
        live_order_id = self._find_order_id(payload)
        if live_order_id:
            cleanup = self._repair_partial_acceptance(live_order_id, account_key)
            raise OrderRejectedError(
                f"Saxo rejected {request_id} with HTTP {status} "
                f"AFTER accepting order {live_order_id} ({detail}); cleanup: {cleanup}"
            )
        raise OrderRejectedError(f"Saxo rejected {request_id} with HTTP {status}: {detail}")
```
     Update its call site in `place_bracket_order` to `return self._handle_placement_response(status, payload, request.client_request_id, account_key)`.
   - **(3d)** Add the method + body builder in the `# ----- orders (P2) -----` section (after `precheck_bracket_order`). `execution_policy` is the module already imported as the `execution` alias — use whatever alias the file already binds:
```python
    def place_standalone_stop(
        self, uic: int, side: str, qty: float, stop_price: float
    ) -> PlacedOrder:
        """Place ONE Option-B standalone StopIfTraded (no bracket parent).

        The disaster stop is NEVER a bracket child (MVP memo §Place): placed
        here AFTER the entry fills, sized to REALIZED filled qty (Risk 2). Same
        safety order as place_bracket_order: ALLOW_ORDERS gate first, then
        precheck, then ONE POST with x-request-id = client_request_id. No Orders
        array + no parent => relation=StandAlone, no TooFarFromEntryOrder
        (SIM-validated 2026-07-20, OrderId 5039296412). exit_order_ids is empty.
        """
        if os.environ.get(ALLOW_ORDERS_ENV) != "1":
            raise BrokerCapabilityError(
                f"order placement is disabled: set {ALLOW_ORDERS_ENV}=1 to allow "
                "SIM order submission (design memo §P2 safety rail). No order was sent."
            )
        client_request_id = str(uuid.uuid4())
        with _translate_saxo_errors():
            account_key = self._resolve_account_key()
            body = self._build_standalone_stop_body(
                uic, side, qty, stop_price, client_request_id, account_key
            )
            self._precheck_or_raise(body, label=f"standalone-stop Uic {uic} {client_request_id}")
            status, payload = self._client.place_order(body, request_id=client_request_id)
            return self._handle_placement_response(status, payload, client_request_id, account_key)

    def _build_standalone_stop_body(
        self, uic: int, side: str, qty: float, stop_price: float,
        client_request_id: str, account_key: str,
    ) -> dict[str, Any]:
        """Option-B standalone StopIfTraded body — no Orders array, no entry."""
        asset_type = "Stock"  # MVP scope: single-name equities only
        details = self._client.get_instrument_details(uic, asset_type)
        supported = details.get("SupportedOrderTypes") or []
        stop_type = execution_policy._STOP_ORDER_TYPE
        if supported and stop_type not in supported:
            raise OrderRejectedError(
                f"instrument Uic {uic} does not support {stop_type} orders "
                f"(SupportedOrderTypes={supported})"
            )
        stop_q = self._quantize_price(stop_price, details, label="stop_price")
        return {
            "Uic": int(uic),
            "AssetType": asset_type,
            "AccountKey": account_key,
            "Amount": qty,
            "BuySell": "Sell" if side == "SELL" else "Buy",
            "OrderType": stop_type,
            "OrderPrice": stop_q,
            "OrderDuration": {"DurationType": execution_policy._EXIT_DURATION},
            "ManualOrder": execution_policy._MANUAL_ORDER,
            "ExternalReference": client_request_id,
        }
```
   > If the file binds the execution-policy module under a different name (e.g. `from alphalens_pipeline.brokers import execution`), use that existing name in `_build_standalone_stop_body` rather than introducing `execution_policy`.

4. Run the new test, expect PASS (same command as step 2).
5. Run the bracket regression, expect PASS:
```bash
.venv/bin/python -m unittest discover -s apps/alphalens-research/tests/brokers \
    -t apps/alphalens-research -p test_saxo_broker_orders.py -v
```
6. Commit:
```bash
git add apps/alphalens-pipeline/alphalens_pipeline/brokers/saxo/broker.py \
        apps/alphalens-research/tests/brokers/test_saxo_broker_standalone_stop.py
git commit -m "feat(brokers): place_standalone_stop standalone StopIfTraded (Option-B)"
```

---

## Task 2 — `placement_planner.classify` (Option-C in-band-subset classifier) + package creation

Net-new stateless module in the net-new `automanager` package (**this task creates the canonical `__init__.py`**). Given a sized `SetupPlan`, decide per non-zero tier which legs place as native bracket children and which are operator-managed. Two rules: (1) a TP is a bracket child only when `abs(tp-entry)/entry <= execution._MAX_CHILD_DISTANCE_FRAC` (0.15, inclusive); a farther TP is reported operator-managed, never POSTed. (2) The disaster stop is never a child (`bracket.stop_loss` always `None`); represented once at plan level; placed later as one standalone `StopIfTraded` after fill.

**Files:** create `.../automanager/__init__.py`, `.../automanager/placement_planner.py`, `apps/alphalens-research/tests/brokers/automanager/__init__.py`, `.../automanager/test_placement_planner.py`.

**Interfaces:**
- Consumes (verified): `execution.decompose_setup_plan(setup_plan, instrument, *, side="BUY") -> list[BracketOrderRequest]`; `execution._MAX_CHILD_DISTANCE_FRAC == 0.15`; `contract.{BracketOrderRequest, InstrumentRef}` (frozen → `dataclasses.replace`); `sizing.{SetupPlan, TierPlan, TpTranchePlan}` from `alphalens_pipeline.paper.sizing`.
- Produces: `classify(setup_plan, instrument, *, side="BUY") -> PlacementPlan`; `PlacementPlan(tiers: tuple[TierPlacement,...], disaster_stop_price: float, operator_report: str)`; `TierPlacement(bracket: BracketOrderRequest, tp_placed_as_child: bool, tp_operator_managed: float | None)`.

**Steps:**

1. Create `.../automanager/__init__.py` (canonical, ACTIVE-stamped):
```python
"""Saxo auto-manager (SIM-first exit-management engine) — control-loop seams.

Net-new live-infra layer (ADR 0011) wiring the shipped placement + reconcile
primitives into an always-on polling daemon. Design:
docs/research/saxo_automanager_mvp_design_2026_07_21.md. Each module is a thin
single-responsibility seam; the loop holds no durable in-memory state (status
is recomputed each tick by the read-only reconcile engine). __status__ is not
REQUIRED (brokers/ is not a research LAYER_ROOT) but we stamp ACTIVE to match
the sibling brokers/__init__.py house style (needs no __closed_* fields).

ADR 0013 inheritance (via brokers): R2 (no execution output feeds T2
SELECTION), R3 (placement carries execution_config_version), T8 (live fills
never pool with broker-free replays).
"""

from typing import Literal

__status__: Literal["ACTIVE", "CLOSED", "RESEARCH_ONLY", "ARCHIVED"] = "ACTIVE"
```
   Create `apps/alphalens-research/tests/brokers/automanager/__init__.py`:
```python
"""Tests for the Saxo auto-manager (MVP) package."""
```

2. Write the FAILING test `test_placement_planner.py`:
```python
"""Hermetic tests for placement_planner.classify (Option-C in-band-subset).

Rules: (1) a tier's TP is a bracket CHILD only when it clears the 0.15
child-distance guard (inclusive <=); a farther TP is reported operator-managed,
never dropped, never POSTed; (2) the disaster stop is NEVER a child —
represented exactly once at plan level, placed later as a standalone
StopIfTraded after fill. Fixtures: LAZ 2026-07-14, S 2026-07-13, +15.00 vs
+15.01 knife-edge.
"""

from __future__ import annotations

import unittest

from alphalens_pipeline.brokers import execution as execution_policy
from alphalens_pipeline.brokers.automanager.placement_planner import (
    PlacementPlan,
    TierPlacement,
    classify,
)
from alphalens_pipeline.brokers.contract import BracketOrderRequest, InstrumentRef
from alphalens_pipeline.paper.sizing import SetupPlan, TierPlan, TpTranchePlan


def _instrument(ticker: str = "LAZ") -> InstrumentRef:
    return InstrumentRef(
        ticker=ticker, exchange_mic="XNYS", asset_type="Stock",
        broker_instrument_id="999", broker_symbol=f"{ticker.lower()}:xnys",
    )


def _setup_plan(*, disaster_stop: float, entries: list[float], tps: list[float],
                order_ttl_days: int = 7) -> SetupPlan:
    alloc = 100.0 / len(entries)
    tiers = tuple(
        TierPlan(tier_index=i, limit_price=lim, qty=10, alloc_pct=alloc, tag=f"t{i}")
        for i, lim in enumerate(entries)
    )
    tranches = tuple(
        TpTranchePlan(tranche_index=i, target_price=t, tranche_pct=100.0 / len(tps),
                      r_multiple=1.5, tag=f"tp{i}")
        for i, t in enumerate(tps)
    )
    return SetupPlan(
        suggested_size_pct=10.0, scale_factor=1.0, final_size_pct=10.0,
        total_notional=10_000.0, paper_equity=100_000.0, disaster_stop=disaster_stop,
        order_ttl_days=order_ttl_days, entry_tiers=tiers, tp_tranches=tranches, fx=None,
    )


def _laz() -> tuple[SetupPlan, InstrumentRef]:
    return (_setup_plan(disaster_stop=35.76, entries=[41.10, 38.85], tps=[46.54, 50.95]),
            _instrument("LAZ"))


def _s() -> tuple[SetupPlan, InstrumentRef]:
    return (_setup_plan(disaster_stop=12.57, entries=[18.08, 16.68, 15.81],
                        tps=[18.81, 19.30, 21.40]), _instrument("S"))


def _knife() -> tuple[SetupPlan, InstrumentRef]:
    return (_setup_plan(disaster_stop=90.0, entries=[100.0, 100.0], tps=[115.00, 115.01]),
            _instrument("KNF"))


class TestClassifyLaz(unittest.TestCase):
    def test_tier0_places_tp_child_tier1_operator_managed(self):
        setup, instrument = _laz()
        plan = classify(setup, instrument)
        self.assertIsInstance(plan, PlacementPlan)
        self.assertEqual(len(plan.tiers), 2)
        t0, t1 = plan.tiers
        self.assertIsInstance(t0, TierPlacement)
        self.assertTrue(t0.tp_placed_as_child)
        self.assertEqual(t0.bracket.take_profit, 46.54)
        self.assertIsNone(t0.tp_operator_managed)
        self.assertIsNone(t0.bracket.stop_loss, "disaster stop is never a child")
        self.assertEqual(t0.bracket.entry_limit, 41.10)
        self.assertFalse(t1.tp_placed_as_child)
        self.assertIsNone(t1.bracket.take_profit, "no Limit child for a far TP")
        self.assertEqual(t1.tp_operator_managed, 50.95)
        self.assertIsNone(t1.bracket.stop_loss)
        self.assertEqual(t1.bracket.entry_limit, 38.85, "the sized entry is preserved")

    def test_report_enumerates_every_tier_and_tp_no_silent_drop(self):
        setup, instrument = _laz()
        report = classify(setup, instrument).operator_report
        for token in ("tier 0", "tier 1", "46.54", "50.95", "operator-managed", "13.2", "31.1"):
            self.assertIn(token, report)


class TestClassifyKnifeEdge(unittest.TestCase):
    def test_15_00_places_15_01_operator_managed(self):
        setup, instrument = _knife()
        plan = classify(setup, instrument)
        self.assertTrue(plan.tiers[0].tp_placed_as_child, "+15.00% clears the inclusive (<=) guard")
        self.assertFalse(plan.tiers[1].tp_placed_as_child, "+15.01% is beyond the guard")
        self.assertEqual(plan.tiers[1].tp_operator_managed, 115.01)

    def test_boundary_uses_the_shared_execution_constant(self):
        self.assertEqual(execution_policy._MAX_CHILD_DISTANCE_FRAC, 0.15)


class TestFarTpTierShape(unittest.TestCase):
    def test_far_tp_tier_emits_entry_only_bracket_not_a_reject(self):
        setup, instrument = _laz()
        tier1 = classify(setup, instrument).tiers[1]
        self.assertFalse(tier1.tp_placed_as_child)
        self.assertIsNone(tier1.bracket.take_profit)
        self.assertIsNone(tier1.bracket.stop_loss)
        self.assertIsInstance(tier1.bracket, BracketOrderRequest)


class TestDisasterStopExactlyOnce(unittest.TestCase):
    def test_disaster_stop_represented_exactly_once_across_fixtures(self):
        for name, factory in (("LAZ", _laz), ("S", _s), ("knife", _knife)):
            with self.subTest(fixture=name):
                setup, instrument = factory()
                plan = classify(setup, instrument)
                self.assertEqual(plan.disaster_stop_price, setup.disaster_stop)
                self.assertGreater(plan.disaster_stop_price, 0.0)
                for tier in plan.tiers:
                    self.assertIsNone(tier.bracket.stop_loss)
                self.assertEqual(plan.operator_report.lower().count("disaster stop"), 1)

    def test_s_incident_all_stops_far_still_one_standalone(self):
        setup, instrument = _s()
        plan = classify(setup, instrument)
        self.assertEqual(len(plan.tiers), 3)
        for tier in plan.tiers:
            self.assertIsNone(tier.bracket.stop_loss)
        self.assertEqual(plan.disaster_stop_price, 12.57)


if __name__ == "__main__":
    unittest.main()
```

3. Run it, expect FAIL (`ModuleNotFoundError`):
```bash
.venv/bin/python -m unittest discover -s apps/alphalens-research/tests/brokers/automanager \
    -t apps/alphalens-research -p test_placement_planner.py -v
```

4. Implement `placement_planner.py`:
```python
"""Option-C in-band-subset placement classifier (MVP auto-manager).

Pure, stateless. Given a sized SetupPlan, decide per non-zero tier which legs
place as native Saxo bracket children and which are reported operator-managed
(design memo §Place). A TP is a child only when it clears
execution._MAX_CHILD_DISTANCE_FRAC (15%, inclusive <=); a farther TP is reported
operator-managed and never POSTed (far standalone SELL LIMIT unproven — MVP risk
R1, phase B). The disaster stop is NEVER a child (bracket.stop_loss always None);
represented once at plan level and placed later as ONE standalone StopIfTraded
after fill, sized to realized qty (avoids the FifoRealTime partial-fill
over-hedge — Risk 2). Consumes execution.decompose_setup_plan.
"""

from __future__ import annotations

import dataclasses
from typing import Literal

from alphalens_pipeline.brokers import execution as execution_policy
from alphalens_pipeline.brokers.contract import BracketOrderRequest, InstrumentRef
from alphalens_pipeline.brokers.execution import decompose_setup_plan
from alphalens_pipeline.paper.sizing import SetupPlan


@dataclasses.dataclass(frozen=True)
class TierPlacement:
    bracket: BracketOrderRequest
    tp_placed_as_child: bool
    tp_operator_managed: float | None


@dataclasses.dataclass(frozen=True)
class PlacementPlan:
    tiers: tuple[TierPlacement, ...]
    disaster_stop_price: float
    operator_report: str


def classify(
    setup_plan: SetupPlan,
    instrument: InstrumentRef,
    *,
    side: Literal["BUY", "SELL"] = "BUY",
) -> PlacementPlan:
    """Classify a sized plan into the in-band placeable subset + operator report."""
    limit_frac = execution_policy._MAX_CHILD_DISTANCE_FRAC
    brackets = decompose_setup_plan(setup_plan, instrument, side=side)
    tiers: list[TierPlacement] = []
    for bracket in brackets:
        tp = bracket.take_profit
        tp_placed_as_child = False
        tp_operator_managed: float | None = None
        if tp is not None:
            dist_frac = abs(tp - bracket.entry_limit) / bracket.entry_limit
            tp_placed_as_child = dist_frac <= limit_frac
            if not tp_placed_as_child:
                tp_operator_managed = tp
        placed = dataclasses.replace(
            bracket,
            take_profit=tp if tp_placed_as_child else None,
            stop_loss=None,  # disaster stop is NEVER a child — plan-level standalone
        )
        tiers.append(TierPlacement(
            bracket=placed, tp_placed_as_child=tp_placed_as_child,
            tp_operator_managed=tp_operator_managed,
        ))
    report = _operator_report(instrument, tuple(tiers), setup_plan.disaster_stop, limit_frac)
    return PlacementPlan(
        tiers=tuple(tiers), disaster_stop_price=setup_plan.disaster_stop, operator_report=report,
    )


def _operator_report(
    instrument: InstrumentRef, tiers: tuple[TierPlacement, ...],
    disaster_stop: float, limit_frac: float,
) -> str:
    """Whole-plan report; the phrase 'disaster stop' appears EXACTLY ONCE."""
    lines = [f"{instrument.ticker} placement plan ({len(tiers)} non-zero tiers):"]
    for idx, tier in enumerate(tiers):
        entry = tier.bracket.entry_limit
        if tier.tp_placed_as_child:
            tp = tier.bracket.take_profit
            pct = abs(tp - entry) / entry * 100.0
            lines.append(f"  tier {idx}: entry {entry:.2f} + TP {tp:.2f} child (+{pct:.1f}%)")
        elif tier.tp_operator_managed is not None:
            tp = tier.tp_operator_managed
            pct = abs(tp - entry) / entry * 100.0
            lines.append(
                f"  tier {idx}: entry {entry:.2f} (entry-only); "
                f"TP {tp:.2f} operator-managed (+{pct:.1f}%, beyond {limit_frac * 100:.0f}%)"
            )
        else:
            lines.append(f"  tier {idx}: entry {entry:.2f} (entry-only, no TP)")
    lines.append(
        f"  disaster stop {disaster_stop:.2f}: standalone StopIfTraded after fill (placed once)"
    )
    return "\n".join(lines)


__all__ = ["PlacementPlan", "TierPlacement", "classify"]
```

5. Run it, expect PASS (same command as step 3).
6. Run the whole brokers tree once (confirms discovery, no regressions):
```bash
.venv/bin/python -m unittest discover -s apps/alphalens-research/tests/brokers \
    -t apps/alphalens-research -v
```
7. Commit:
```bash
git add apps/alphalens-pipeline/alphalens_pipeline/brokers/automanager/ \
        apps/alphalens-research/tests/brokers/automanager/
git commit -m "feat(brokers): add placement_planner.classify in-band-subset Option-C classifier"
```

---

## Task 3 — `picks.py` (append-only pick queue)

Append-only human-intent inbox mirroring `submission_log.py`. **Package `__init__.py` files already exist from Task 2** — do not recreate.

**Files:** create `.../automanager/picks.py`, `.../automanager/test_picks.py`.

**Interfaces:**
- Consumes: stdlib only (leaf module).
- Produces: `Pick(ticker: str, date: dt.date, armed_ts: str, status: str)` (frozen); `arm_pick(ticker, date, *, path=None) -> None`; `iter_picks(*, path=None) -> Iterator[Pick]`; `DEFAULT_PICKS_PATH = ~/.alphalens/broker_orders/picks.jsonl`; `STATUS_ARMED = "armed"`.

**Steps:**

1. Write the FAILING test `test_picks.py`:
```python
"""Hermetic tests for the append-only pick queue.

Mirrors submission_log.py: one JSON line per arm, file never rewritten,
malformed/undated lines skipped not fatal, missing file yields nothing.
"""

from __future__ import annotations

import datetime as dt
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from alphalens_pipeline.brokers.automanager.picks import (
    STATUS_ARMED,
    Pick,
    arm_pick,
    iter_picks,
)


class ArmPickTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.path = Path(self._tmp.name) / "picks.jsonl"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_arm_pick_appends_one_armed_line(self) -> None:
        arm_pick("ko", dt.date(2026, 7, 20), path=self.path)
        lines = self.path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        record = json.loads(lines[0])
        self.assertEqual(record["ticker"], "KO")
        self.assertEqual(record["date"], "2026-07-20")
        self.assertEqual(record["status"], STATUS_ARMED)
        self.assertTrue(record["armed_ts"])

    def test_arm_pick_never_rewrites_appends_second_line(self) -> None:
        arm_pick("KO", dt.date(2026, 7, 20), path=self.path)
        arm_pick("MU", dt.date(2026, 7, 21), path=self.path)
        self.assertEqual(len(self.path.read_text().splitlines()), 2)

    def test_arm_pick_creates_parent_dir(self) -> None:
        nested = Path(self._tmp.name) / "broker_orders" / "picks.jsonl"
        arm_pick("KO", dt.date(2026, 7, 20), path=nested)
        self.assertTrue(nested.exists())


class IterPicksTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.path = Path(self._tmp.name) / "picks.jsonl"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_iter_missing_file_yields_nothing(self) -> None:
        self.assertEqual(list(iter_picks(path=self.path)), [])

    def test_iter_round_trips_in_append_order(self) -> None:
        arm_pick("KO", dt.date(2026, 7, 20), path=self.path)
        arm_pick("MU", dt.date(2026, 7, 21), path=self.path)
        picks = list(iter_picks(path=self.path))
        self.assertEqual([p.ticker for p in picks], ["KO", "MU"])
        self.assertEqual(picks[0].date, dt.date(2026, 7, 20))
        self.assertIsInstance(picks[0], Pick)
        self.assertEqual(picks[0].status, STATUS_ARMED)

    def test_iter_skips_malformed_and_undated_lines(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            "not json\n"
            + json.dumps(["a", "list"]) + "\n"
            + json.dumps({"ticker": "NODATE", "status": "armed"}) + "\n"
            + json.dumps({"ticker": "GOOD", "date": "2026-07-20",
                          "armed_ts": "2026-07-20T00:00:00+00:00", "status": "armed"}) + "\n",
            encoding="utf-8",
        )
        picks = list(iter_picks(path=self.path))
        self.assertEqual([p.ticker for p in picks], ["GOOD"])


if __name__ == "__main__":
    unittest.main()
```

2. Run it, expect FAIL:
```bash
.venv/bin/python -m unittest discover -s apps/alphalens-research/tests/brokers/automanager \
    -t apps/alphalens-research -p test_picks.py -v
```

3. Implement `picks.py`:
```python
"""Append-only pick queue for the Saxo auto-manager.

One JSON line per `alphalens broker arm` under
~/.alphalens/broker_orders/picks.jsonl — the durable human-intent inbox the
control loop drains. Mirrors submission_log.py: the file is NEVER rewritten;
status is a recorded fact per line (T8 cohort discipline). Malformed/undated
lines are skipped; a missing file yields nothing.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

DEFAULT_PICKS_PATH = Path.home() / ".alphalens" / "broker_orders" / "picks.jsonl"

STATUS_ARMED = "armed"


@dataclass(frozen=True)
class Pick:
    ticker: str
    date: dt.date
    armed_ts: str
    status: str


def arm_pick(ticker: str, date: dt.date, *, path: Path | None = None) -> None:
    """Append one 'armed' intent line (append-only; never rewrites)."""
    target = path or DEFAULT_PICKS_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ticker": ticker.upper(),
        "date": date.isoformat(),
        "armed_ts": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "status": STATUS_ARMED,
    }
    line = json.dumps(record, sort_keys=True)
    with target.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def iter_picks(*, path: Path | None = None) -> Iterator[Pick]:
    """Yield parsed picks in append order. Malformed/undated lines skipped."""
    target = path or DEFAULT_PICKS_PATH
    if not target.exists():
        return
    with target.open("r", encoding="utf-8") as fh:
        for raw_line in fh:
            line = raw_line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            try:
                parsed_date = dt.date.fromisoformat(str(record["date"]))
            except (KeyError, ValueError):
                continue
            yield Pick(
                ticker=str(record.get("ticker", "")),
                date=parsed_date,
                armed_ts=str(record.get("armed_ts", "")),
                status=str(record.get("status", "")),
            )


__all__ = ["DEFAULT_PICKS_PATH", "STATUS_ARMED", "Pick", "arm_pick", "iter_picks"]
```

4. Run it, expect PASS. 5. Commit:
```bash
git add apps/alphalens-pipeline/alphalens_pipeline/brokers/automanager/picks.py \
        apps/alphalens-research/tests/brokers/automanager/test_picks.py
git commit -m "feat(brokers): append-only picks queue for the Saxo auto-manager"
```

---

## Task 4 — `alphalens broker arm` CLI subcommand

The attended hand-off seam: validates `(ticker, date)` against the brief at arm time, then appends one `armed` line. CLI lazy-imports inside the command body (matches `test_broker_cli.py`), so test patches target the SOURCE modules.

**Files:** modify `apps/alphalens-pipeline/alphalens_cli/commands/broker.py`; create `.../automanager/test_arm_cli.py`.

**Interfaces:**
- Consumes: `arm_pick` + `DEFAULT_PICKS_PATH` (Task 3); `load_brief(brief_date, briefs_dir) -> list[CandidateBrief]` from `alphalens_pipeline.paper.brief_loader` (`CandidateBrief.ticker: str`); the file's existing `_fail` helper + `_DEFAULT_BRIEFS_DIR`.
- Produces: `alphalens broker arm TICKER --date YYYY-MM-DD [--briefs-dir DIR]` on `broker_app`. Exit 1 on malformed date / missing brief / absent ticker; exit 0 + one appended line otherwise.

**Steps:**

1. Write the FAILING test `test_arm_cli.py`:
```python
"""CLI tests for `alphalens broker arm`.

Validates (ticker, date) against the brief at arm time, then appends one
'armed' line. Loading is lazy-imported inside the command body, so patches
target the SOURCE modules.
"""

from __future__ import annotations

import datetime as dt
import unittest
from unittest import mock

from alphalens_pipeline.paper.brief_loader import CandidateBrief
from typer.testing import CliRunner

_BRIEF_DATE = dt.date(2026, 7, 20)


def _candidate(ticker: str = "KO") -> CandidateBrief:
    return CandidateBrief(
        brief_date=_BRIEF_DATE, ticker=ticker, theme="test-theme", verified=True,
        suggested_size_pct=3.0, trade_setup=None, n_gates_passed=3, n_gates_failed=0,
        layer4_weighted_score=1.0, scorer_config_version="scorer-v1-test",
    )


class ArmCommandTest(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = CliRunner()

    def test_arm_valid_pick_appends_and_exits_zero(self) -> None:
        from alphalens_cli.commands.broker import broker_app
        with (
            mock.patch("alphalens_pipeline.paper.brief_loader.load_brief",
                       return_value=[_candidate("KO"), _candidate("MU")]),
            mock.patch("alphalens_pipeline.brokers.automanager.picks.arm_pick") as arm,
        ):
            result = self.runner.invoke(broker_app, ["arm", "ko", "--date", "2026-07-20"])
        self.assertEqual(result.exit_code, 0, result.output)
        arm.assert_called_once_with("KO", _BRIEF_DATE)
        self.assertIn("armed KO", result.output)

    def test_arm_ticker_absent_from_brief_refuses(self) -> None:
        from alphalens_cli.commands.broker import broker_app
        with (
            mock.patch("alphalens_pipeline.paper.brief_loader.load_brief",
                       return_value=[_candidate("KO")]),
            mock.patch("alphalens_pipeline.brokers.automanager.picks.arm_pick") as arm,
        ):
            result = self.runner.invoke(broker_app, ["arm", "ZZZZ", "--date", "2026-07-20"])
        self.assertEqual(result.exit_code, 1)
        self.assertIn("not in the 2026-07-20 brief", result.output)
        arm.assert_not_called()

    def test_arm_bad_date_refuses(self) -> None:
        from alphalens_cli.commands.broker import broker_app
        result = self.runner.invoke(broker_app, ["arm", "KO", "--date", "not-a-date"])
        self.assertEqual(result.exit_code, 1)
        self.assertIn("invalid --date", result.output)

    def test_arm_missing_brief_parquet_refuses(self) -> None:
        from alphalens_cli.commands.broker import broker_app
        with mock.patch("alphalens_pipeline.paper.brief_loader.load_brief",
                        side_effect=FileNotFoundError("thematic brief parquet not found: /x.parquet")):
            result = self.runner.invoke(broker_app, ["arm", "KO", "--date", "2026-07-20"])
        self.assertEqual(result.exit_code, 1)
        self.assertIn("not found", result.output)


if __name__ == "__main__":
    unittest.main()
```

2. Run it, expect FAIL (no `arm` command). Command:
```bash
.venv/bin/python -m unittest discover -s apps/alphalens-research/tests/brokers/automanager \
    -t apps/alphalens-research -p test_arm_cli.py -v
```

3. Add the `arm` command to `broker.py`, after `submit_command` (before `orders_command`), following the file's lazy-import + `_fail` idiom:
```python
@broker_app.command(name="arm")
def arm_command(
    ticker: str = typer.Argument(..., help="Plain ticker from the brief, e.g. KO."),
    date: str = typer.Option(..., "--date", help="Brief date (YYYY-MM-DD)."),
    briefs_dir: Path = typer.Option(
        _DEFAULT_BRIEFS_DIR, "--briefs-dir", help="Thematic briefs parquet directory."
    ),
) -> None:
    """Arm a picked candidate — append human intent to the picks queue.

    Validates the row against the brief at arm time (fail fast if the parquet
    is missing or the ticker is absent), then appends ONE 'armed' line to
    picks.jsonl. The VPS control loop drains the queue; this command places
    nothing.
    """
    from alphalens_pipeline.brokers.automanager.picks import DEFAULT_PICKS_PATH, arm_pick
    from alphalens_pipeline.paper.brief_loader import load_brief

    try:
        brief_date = dt.date.fromisoformat(date)
    except ValueError as exc:
        raise _fail(f"invalid --date {date!r}: {exc}") from exc

    try:
        candidates = load_brief(brief_date, briefs_dir)
    except (FileNotFoundError, ValueError) as exc:
        raise _fail(str(exc)) from exc

    wanted = ticker.upper()
    candidate = next((c for c in candidates if c.ticker.upper() == wanted), None)
    if candidate is None:
        raise _fail(f"{wanted} not in the {brief_date} brief ({len(candidates)} candidates)")

    arm_pick(wanted, brief_date)
    typer.echo(f"armed {wanted} @ {brief_date.isoformat()} -> {DEFAULT_PICKS_PATH}")
```
   Also add to the module docstring's subcommand list:
```
    alphalens broker arm KO --date 2026-07-20   — validate against the brief,
        append an "armed" pick to picks.jsonl (the auto-manager hand-off seam)
```
   > If `dt` / `Path` / `typer` / `_fail` / `_DEFAULT_BRIEFS_DIR` are not already imported at module top, reuse the file's existing imports; do not add new top-level pipeline→research imports.

4. Run it, expect PASS. 5. Commit:
```bash
git add apps/alphalens-pipeline/alphalens_cli/commands/broker.py \
        apps/alphalens-research/tests/brokers/automanager/test_arm_cli.py
git commit -m "feat(brokers): alphalens broker arm subcommand for the pick hand-off"
```

---

## Task 5 — `safety.py` (pure-predicate portfolio safety gate)

`check` runs for every armed pick before placement. Pure function of inputs + two process rails read at call time (KILL file, `ALLOW_ORDERS`). Writes nothing — the daily-loss branch RETURNS `Refuse` (tripping the KILL file is the control loop's job).

**Files:** create `.../automanager/safety.py`, `.../automanager/test_safety.py`.

**Interfaces:**
- Consumes: `Pick` (Task 3, carried through, not branched on); a duck-typed session state with `alive: bool` (satisfied by Task 6 `ChainStatus`).
- Produces: `Allow` (no fields); `Refuse(reason: str)`; `Decision = Allow | Refuse`; `SessionState` Protocol (`alive: bool`); `JournalView(open_bracket_count:int, gross_committed:float, realized_r_today:float)`; `BrokerView(open_position_count:int, equity:float)`; `check(pick, journal_view, broker_view, session_state, *, kill_path=None) -> Decision`; `DEFAULT_KILL_PATH`; env names `ALLOW_ORDERS_ENV`, `MAX_OPEN_ENV`, `PORTFOLIO_GROSS_FRAC_ENV`, `DAILY_LOSS_LIMIT_R_ENV`; defaults `DEFAULT_MAX_OPEN=3`, `DEFAULT_PORTFOLIO_GROSS_FRAC=1.0`, `DEFAULT_DAILY_LOSS_LIMIT_R=3.0`.

> Note: `safety.BrokerView`/`JournalView` are distinct from `position_manager.BrokerView` (Task 10) — different modules, different fields. Keep both; do not merge.

**Steps:**

1. Write the FAILING test `test_safety.py`:
```python
"""Hermetic tests for the pure-predicate safety gate.

check is a pure function of inputs + two rails read at call time (KILL file,
ALLOW_ORDERS). Writes nothing — even the daily-loss branch RETURNS Refuse. One
refusal branch per test; first failing rail wins.
"""

from __future__ import annotations

import datetime as dt
import os
import unittest
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from alphalens_pipeline.brokers.automanager.picks import Pick
from alphalens_pipeline.brokers.automanager.safety import (
    ALLOW_ORDERS_ENV,
    DAILY_LOSS_LIMIT_R_ENV,
    MAX_OPEN_ENV,
    PORTFOLIO_GROSS_FRAC_ENV,
    Allow,
    BrokerView,
    JournalView,
    Refuse,
    check,
)


@dataclass
class _StubSession:
    alive: bool


_PICK = Pick(ticker="KO", date=dt.date(2026, 7, 20), armed_ts="ts", status="armed")
_CLEAR_JOURNAL = JournalView(open_bracket_count=0, gross_committed=0.0, realized_r_today=0.0)
_CLEAR_BROKER = BrokerView(open_position_count=0, equity=1_000.0)


class SafetyGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.kill = Path(self._tmp.name) / "KILL"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    @mock.patch.dict(os.environ, {ALLOW_ORDERS_ENV: "1"}, clear=False)
    def test_all_rails_clear_allows(self) -> None:
        d = check(_PICK, _CLEAR_JOURNAL, _CLEAR_BROKER, _StubSession(alive=True), kill_path=self.kill)
        self.assertIsInstance(d, Allow)

    @mock.patch.dict(os.environ, {ALLOW_ORDERS_ENV: "1"}, clear=False)
    def test_kill_file_present_refuses_first(self) -> None:
        self.kill.write_text("stop", encoding="utf-8")
        d = check(_PICK, _CLEAR_JOURNAL, _CLEAR_BROKER, _StubSession(alive=True), kill_path=self.kill)
        self.assertIsInstance(d, Refuse)
        self.assertIn("KILL", d.reason)

    @mock.patch.dict(os.environ, {ALLOW_ORDERS_ENV: "1"}, clear=False)
    def test_dead_chain_refuses(self) -> None:
        d = check(_PICK, _CLEAR_JOURNAL, _CLEAR_BROKER, _StubSession(alive=False), kill_path=self.kill)
        self.assertIsInstance(d, Refuse)
        self.assertIn("chain", d.reason.lower())

    @mock.patch.dict(os.environ, {ALLOW_ORDERS_ENV: "0"}, clear=False)
    def test_allow_orders_not_set_refuses(self) -> None:
        d = check(_PICK, _CLEAR_JOURNAL, _CLEAR_BROKER, _StubSession(alive=True), kill_path=self.kill)
        self.assertIsInstance(d, Refuse)
        self.assertIn(ALLOW_ORDERS_ENV, d.reason)

    @mock.patch.dict(os.environ, {ALLOW_ORDERS_ENV: "1", MAX_OPEN_ENV: "2"}, clear=False)
    def test_max_open_cap_refuses(self) -> None:
        journal = JournalView(open_bracket_count=1, gross_committed=0.0, realized_r_today=0.0)
        broker = BrokerView(open_position_count=1, equity=1_000.0)
        d = check(_PICK, journal, broker, _StubSession(alive=True), kill_path=self.kill)
        self.assertIsInstance(d, Refuse)
        self.assertIn("MAX_OPEN", d.reason)

    @mock.patch.dict(os.environ, {ALLOW_ORDERS_ENV: "1", PORTFOLIO_GROSS_FRAC_ENV: "1.0"}, clear=False)
    def test_portfolio_gross_cap_refuses(self) -> None:
        journal = JournalView(open_bracket_count=0, gross_committed=1_200.0, realized_r_today=0.0)
        d = check(_PICK, journal, _CLEAR_BROKER, _StubSession(alive=True), kill_path=self.kill)
        self.assertIsInstance(d, Refuse)
        self.assertIn("gross", d.reason.lower())

    @mock.patch.dict(os.environ, {ALLOW_ORDERS_ENV: "1", DAILY_LOSS_LIMIT_R_ENV: "3.0"}, clear=False)
    def test_daily_loss_limit_refuses_without_side_effects(self) -> None:
        journal = JournalView(open_bracket_count=0, gross_committed=0.0, realized_r_today=-3.5)
        d = check(_PICK, journal, _CLEAR_BROKER, _StubSession(alive=True), kill_path=self.kill)
        self.assertIsInstance(d, Refuse)
        self.assertIn("loss", d.reason.lower())
        self.assertFalse(self.kill.exists())


if __name__ == "__main__":
    unittest.main()
```

2. Run it, expect FAIL. Command:
```bash
.venv/bin/python -m unittest discover -s apps/alphalens-research/tests/brokers/automanager \
    -t apps/alphalens-research -p test_safety.py -v
```

3. Implement `safety.py`:
```python
"""Pure-predicate portfolio safety gate for the Saxo auto-manager.

check(...) runs for every armed pick BEFORE any placement. Pure function of
inputs + two process rails read at call time (KILL file, ALLOW_ORDERS). Places,
cancels, and writes nothing: the daily-loss branch RETURNS Refuse; tripping the
KILL file is the control loop's job. Refusal order (first failing rail wins):
KILL file -> chain dead -> ALLOW_ORDERS != '1' -> MAX_OPEN cap -> portfolio
gross cap -> daily-loss limit. The cap numbers are operator policy with no
validated basis (memo risk 7) — set conservatively.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

DEFAULT_KILL_PATH = Path.home() / ".alphalens" / "broker_orders" / "KILL"

ALLOW_ORDERS_ENV = "ALPHALENS_BROKER_ALLOW_ORDERS"
MAX_OPEN_ENV = "ALPHALENS_BROKER_MAX_OPEN"
PORTFOLIO_GROSS_FRAC_ENV = "ALPHALENS_BROKER_PORTFOLIO_GROSS_FRAC"
DAILY_LOSS_LIMIT_R_ENV = "ALPHALENS_BROKER_DAILY_LOSS_LIMIT_R"

DEFAULT_MAX_OPEN = 3
DEFAULT_PORTFOLIO_GROSS_FRAC = 1.0
DEFAULT_DAILY_LOSS_LIMIT_R = 3.0


@dataclass(frozen=True)
class Allow:
    """The pick clears every rail and may be placed."""


@dataclass(frozen=True)
class Refuse:
    reason: str


Decision = Allow | Refuse


class SessionState(Protocol):
    alive: bool


@dataclass(frozen=True)
class JournalView:
    open_bracket_count: int
    gross_committed: float
    realized_r_today: float


@dataclass(frozen=True)
class BrokerView:
    open_position_count: int
    equity: float


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def check(
    pick,
    journal_view: JournalView,
    broker_view: BrokerView,
    session_state: SessionState,
    *,
    kill_path: Path | None = None,
) -> Decision:
    """Return Allow iff every rail clears; else the first Refuse. Pure predicate."""
    kill = kill_path or DEFAULT_KILL_PATH
    if kill.exists():
        return Refuse(f"KILL file present at {kill} — emergency stop, placement halted")
    if not session_state.alive:
        return Refuse("OAuth chain is dead — cannot place; re-run `alphalens broker auth`")
    if os.environ.get(ALLOW_ORDERS_ENV) != "1":
        return Refuse(f"{ALLOW_ORDERS_ENV} != '1' — master arm not set, placement inert")

    max_open = _int_env(MAX_OPEN_ENV, DEFAULT_MAX_OPEN)
    open_total = journal_view.open_bracket_count + broker_view.open_position_count
    if open_total >= max_open:
        return Refuse(
            f"open brackets+positions {open_total} >= MAX_OPEN {max_open} — refusing new pick"
        )

    gross_frac = _float_env(PORTFOLIO_GROSS_FRAC_ENV, DEFAULT_PORTFOLIO_GROSS_FRAC)
    gross_limit = gross_frac * broker_view.equity
    if journal_view.gross_committed > gross_limit:
        return Refuse(
            f"committed gross {journal_view.gross_committed:,.2f} exceeds portfolio cap "
            f"{gross_limit:,.2f} ({gross_frac:g} x equity {broker_view.equity:,.2f})"
        )

    loss_limit_r = abs(_float_env(DAILY_LOSS_LIMIT_R_ENV, DEFAULT_DAILY_LOSS_LIMIT_R))
    if journal_view.realized_r_today <= -loss_limit_r:
        return Refuse(
            f"daily realized r {journal_view.realized_r_today:+.2f} <= "
            f"-{loss_limit_r:.2f} daily-loss limit — the day is closed to new picks"
        )

    return Allow()


__all__ = [
    "ALLOW_ORDERS_ENV", "DAILY_LOSS_LIMIT_R_ENV", "DEFAULT_DAILY_LOSS_LIMIT_R",
    "DEFAULT_KILL_PATH", "DEFAULT_MAX_OPEN", "DEFAULT_PORTFOLIO_GROSS_FRAC",
    "MAX_OPEN_ENV", "PORTFOLIO_GROSS_FRAC_ENV", "Allow", "BrokerView", "Decision",
    "JournalView", "Refuse", "SessionState", "check",
]
```

4. Run it, expect PASS. 5. Commit:
```bash
git add apps/alphalens-pipeline/alphalens_pipeline/brokers/automanager/safety.py \
        apps/alphalens-research/tests/brokers/automanager/test_safety.py
git commit -m "feat(brokers): pure-predicate safety gate for the auto-manager"
```

---

## Task 6 — `session_keeper.py` (OAuth token-chain liveness)

Thin liveness wrapper over the shipped OAuth provider: `ensure_alive` delegates to `get_access_token` (self-refreshes at `expires_in − 120 s` internally), `keep_alive` delegates to `refresh_now` (idle-timer path). A lost chain surfaces as `ChainStatus(alive=False, reason=...)`, never an exception escaping into the loop.

**Files:** create `.../automanager/session_keeper.py`, `.../automanager/test_session_keeper.py`.

**Interfaces:**
- Consumes (verified): a provider structurally exposing `get_access_token() -> str` and `refresh_now() -> str` (prod: `OAuthTokenProvider` from `alphalens_pipeline.brokers.saxo.tokens`); `SaxoAuthError` from `alphalens_pipeline.brokers.saxo.errors`.
- Produces: `ChainStatus(alive: bool, reason: str | None = None)` (frozen); `SessionKeeper(provider)`; `SessionKeeper.ensure_alive() -> ChainStatus`; `SessionKeeper.keep_alive() -> ChainStatus`.

**Steps:**

1. Write the FAILING test `test_session_keeper.py`:
```python
"""Hermetic tests for the auto-manager session-keeper (token-chain liveness).

ensure_alive delegates to get_access_token (provider self-refreshes at
expires_in - 120s internally); keep_alive delegates to refresh_now. A lost
chain surfaces as ChainStatus(alive=False, reason=...), never an exception.
"""

from __future__ import annotations

import unittest

from alphalens_pipeline.brokers.automanager.session_keeper import ChainStatus, SessionKeeper
from alphalens_pipeline.brokers.saxo.errors import SaxoAuthError


class _StubProvider:
    def __init__(self, *, error: Exception | None = None):
        self._error = error
        self.get_calls = 0
        self.refresh_calls = 0

    def get_access_token(self) -> str:
        self.get_calls += 1
        if self._error is not None:
            raise self._error
        return "tok-access"

    def refresh_now(self) -> str:
        self.refresh_calls += 1
        if self._error is not None:
            raise self._error
        return "tok-refreshed"


class SessionKeeperEnsureAliveTests(unittest.TestCase):
    def test_ensure_alive_delegates_and_reports_alive(self) -> None:
        provider = _StubProvider()
        status = SessionKeeper(provider).ensure_alive()
        self.assertEqual(status, ChainStatus(alive=True, reason=None))
        self.assertEqual(provider.get_calls, 1)
        self.assertEqual(provider.refresh_calls, 0)

    def test_ensure_alive_dead_chain_returns_not_alive_with_reason(self) -> None:
        status = SessionKeeper(_StubProvider(error=SaxoAuthError("Saxo OAuth refresh chain lost"))).ensure_alive()
        self.assertFalse(status.alive)
        self.assertIsNotNone(status.reason)
        self.assertIn("chain lost", status.reason)

    def test_ensure_alive_does_not_leak_saxo_auth_error(self) -> None:
        try:
            SessionKeeper(_StubProvider(error=SaxoAuthError("dead"))).ensure_alive()
        except SaxoAuthError:
            self.fail("ensure_alive must translate SaxoAuthError into ChainStatus")


class SessionKeeperKeepAliveTests(unittest.TestCase):
    def test_keep_alive_delegates_to_refresh_now(self) -> None:
        provider = _StubProvider()
        status = SessionKeeper(provider).keep_alive()
        self.assertEqual(status, ChainStatus(alive=True, reason=None))
        self.assertEqual(provider.refresh_calls, 1)
        self.assertEqual(provider.get_calls, 0)

    def test_keep_alive_dead_chain_returns_not_alive(self) -> None:
        status = SessionKeeper(_StubProvider(error=SaxoAuthError("refresh token expired (>40 min gap)"))).keep_alive()
        self.assertFalse(status.alive)
        self.assertIn("40 min", status.reason)


if __name__ == "__main__":
    unittest.main()
```

2. Run it, expect FAIL. Command:
```bash
.venv/bin/python -m unittest discover -s apps/alphalens-research/tests/brokers/automanager \
    -t apps/alphalens-research -p test_session_keeper.py -v
```

3. Implement `session_keeper.py`:
```python
"""Session-keeper — OAuth token-chain liveness at the top of every tick.

Thin wrapper over the shipped OAuthTokenProvider. ensure_alive touches
get_access_token, which self-refreshes the access token at expires_in - 120s on
the provider's own clock (the keeper never re-implements that schedule).
keep_alive is the idle-timer primitive (the alphalens-saxo-refresh unit),
forcing an unconditional refresh_now during no-bracket stretches so the ~40min
refresh window never lapses. A lost chain raises SaxoAuthError inside the
provider (which also fires the Telegram _chain_lost alert); the keeper
TRANSLATES that into ChainStatus(alive=False, reason=...) so the loop reads a
verdict and stops placing, never crashes mid-tick.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from alphalens_pipeline.brokers.saxo.errors import SaxoAuthError


@dataclass(frozen=True)
class ChainStatus:
    alive: bool
    reason: str | None = None


@runtime_checkable
class _SessionProvider(Protocol):
    def get_access_token(self) -> str: ...
    def refresh_now(self) -> str: ...


class SessionKeeper:
    """Per-tick + idle-timer liveness gate over the OAuth token chain."""

    def __init__(self, provider: _SessionProvider):
        self._provider = provider

    def ensure_alive(self) -> ChainStatus:
        try:
            self._provider.get_access_token()
        except SaxoAuthError as exc:
            return ChainStatus(alive=False, reason=str(exc))
        return ChainStatus(alive=True, reason=None)

    def keep_alive(self) -> ChainStatus:
        try:
            self._provider.refresh_now()
        except SaxoAuthError as exc:
            return ChainStatus(alive=False, reason=str(exc))
        return ChainStatus(alive=True, reason=None)


__all__ = ["ChainStatus", "SessionKeeper"]
```

4. Run it, expect PASS. 5. Commit:
```bash
git add apps/alphalens-pipeline/alphalens_pipeline/brokers/automanager/session_keeper.py \
        apps/alphalens-research/tests/brokers/automanager/test_session_keeper.py
git commit -m "feat(brokers): add auto-manager session-keeper chain-liveness gate"
```

---

## Task 7 — `fill_source.py` (pluggable fill detection + polling impl)

`FillSource` is the seam the loop asks "what changed since last tick?". MVP ships `PollingFillSource` over the shipped read-only `reconcile_brackets`. `StreamingFillSource` is a phase-B drop-in behind the SAME `poll_tick` interface — NOT built.

**Files:** create `.../automanager/fill_source.py`, `.../automanager/test_fill_source.py`.

**Interfaces:**
- Consumes (verified): `reconcile_brackets(records, broker, *, today=None) -> list[ReconcileVerdict]` and `ReconcileVerdict` (fields `ticker, entry_order_id, status, verdict, details, ...`) from `alphalens_pipeline.brokers.reconcile`; `Broker`, `OrderStatus`, `OrderState` from `alphalens_pipeline.brokers.contract`. Status tokens: `WORKING/PARTIALLY_FILLED/FILLED/CANCELLED/REJECTED/EXPIRED/UNRESOLVED`; a past-TTL entry keeps `status="WORKING"` with `verdict="WORKING(PAST-TTL!)"`.
- Produces: `Transition(order_id:str, kind:Literal['FILLED','TERMINAL','PARTIAL'], ticker:str='', filled_quantity:float=0.0, verdict:ReconcileVerdict|None=None)` (frozen); `FillSource` runtime_checkable Protocol (`poll_tick() -> list[Transition]`); `PollingFillSource(broker, load_records, *, today=None)`; `TransitionKind`.

**Steps:**

1. Write the FAILING test `test_fill_source.py`:
```python
"""Hermetic tests for the pluggable fill-source (polling implementation).

PollingFillSource diffs successive reconcile snapshots and emits one Transition
per entry the FIRST time its status enters a reportable state (FILLED /
PARTIALLY_FILLED / a terminal cancel/reject/expire). A plain WORKING entry —
including WORKING(PAST-TTL!) — emits NOTHING: TTL divergence is an alert the
reconcile-bridge/position-manager owns, never a fill signal.
"""

from __future__ import annotations

import datetime as dt
import unittest
from typing import Any

from alphalens_pipeline.brokers.automanager.fill_source import (
    FillSource,
    PollingFillSource,
    Transition,
)
from alphalens_pipeline.brokers.contract import OrderState, OrderStatus

_TS = "2026-07-06T18:00:00+00:00"


def _order_state(order_id: str, status: OrderStatus, *, filled: float = 0.0) -> OrderState:
    return OrderState(order_id=order_id, status=status, instrument=None,
                      filled_quantity=filled, raw_status="")


class _StubBroker:
    name = "stub"

    def __init__(self) -> None:
        self.open_orders: list[OrderState] = []
        self.outcomes: dict[str, OrderState] = {}
        self.open_refs: list[str] = []
        self.closed_rows: list[dict[str, Any]] = []

    def list_open_orders(self) -> list[OrderState]:
        return list(self.open_orders)

    def resolve_order_outcome(self, order_id: str) -> OrderState:
        return self.outcomes.get(order_id, _order_state(order_id, OrderStatus.UNKNOWN))

    def get_open_position_references(self) -> list[str]:
        return list(self.open_refs)

    def get_closed_position_rows(self) -> list[dict[str, Any]]:
        return list(self.closed_rows)


def _record(**overrides: Any) -> dict[str, Any]:
    bracket: dict[str, Any] = {
        "client_request_id": "rid-1", "entry_order_id": "E-1",
        "exit_order_ids": ["T-1", "S-1"], "qty": 2, "entry": 82.0,
        "stop": 78.0, "tp": 90.0, "ttl": 5,
    }
    record: dict[str, Any] = {
        "execution_config_version": "execution-v1-test", "ts": _TS,
        "brief_date": "2026-07-06", "ticker": "KO", "mic": "XNYS", "uic": "307",
        "brackets": [bracket], "precheck": [],
    }
    record.update(overrides)
    return record


class PollingFillSourceProtocolTests(unittest.TestCase):
    def test_is_a_fill_source(self) -> None:
        self.assertIsInstance(PollingFillSource(_StubBroker(), lambda: []), FillSource)


class PollingFillSourceTransitionTests(unittest.TestCase):
    def test_working_then_filled_emits_one_filled_transition(self) -> None:
        broker = _StubBroker()
        source = PollingFillSource(broker, lambda: [_record()])
        broker.open_orders = [_order_state("E-1", OrderStatus.WORKING)]
        self.assertEqual(source.poll_tick(), [])
        broker.open_orders = []
        broker.outcomes = {"E-1": _order_state("E-1", OrderStatus.FILLED, filled=2.0)}
        broker.open_refs = ["rid-1"]
        transitions = source.poll_tick()
        self.assertEqual(len(transitions), 1)
        t = transitions[0]
        self.assertEqual(t.order_id, "E-1")
        self.assertEqual(t.kind, "FILLED")
        self.assertEqual(t.ticker, "KO")
        self.assertEqual(t.filled_quantity, 2.0)
        self.assertIsNotNone(t.verdict)

    def test_filled_state_does_not_re_emit_on_the_next_tick(self) -> None:
        broker = _StubBroker()
        source = PollingFillSource(broker, lambda: [_record()])
        broker.open_orders = []
        broker.outcomes = {"E-1": _order_state("E-1", OrderStatus.FILLED, filled=2.0)}
        broker.open_refs = ["rid-1"]
        self.assertEqual(len(source.poll_tick()), 1)
        self.assertEqual(source.poll_tick(), [])

    def test_working_then_expired_emits_terminal(self) -> None:
        broker = _StubBroker()
        source = PollingFillSource(broker, lambda: [_record()])
        broker.open_orders = [_order_state("E-1", OrderStatus.WORKING)]
        source.poll_tick()
        broker.open_orders = []
        broker.outcomes = {"E-1": _order_state("E-1", OrderStatus.EXPIRED)}
        transitions = source.poll_tick()
        self.assertEqual([t.kind for t in transitions], ["TERMINAL"])
        self.assertEqual(transitions[0].order_id, "E-1")

    def test_past_ttl_working_is_not_a_transition(self) -> None:
        broker = _StubBroker()
        source = PollingFillSource(broker, lambda: [_record()], today=dt.date(2026, 7, 17))
        broker.open_orders = [_order_state("E-1", OrderStatus.WORKING)]
        self.assertEqual(source.poll_tick(), [])


if __name__ == "__main__":
    unittest.main()
```

2. Run it, expect FAIL. Command:
```bash
.venv/bin/python -m unittest discover -s apps/alphalens-research/tests/brokers/automanager \
    -t apps/alphalens-research -p test_fill_source.py -v
```

3. Implement `fill_source.py`:
```python
"""Pluggable fill-detection interface + the MVP polling implementation.

FillSource is the seam the control loop asks "what changed since last tick?".
The MVP ships PollingFillSource over the read-only reconcile_brackets engine
(the reconciliation floor); a StreamingFillSource (WebSocket push) is a phase-B
drop-in behind the SAME poll_tick interface with NO control-loop change —
deliberately NOT built here. poll_tick re-reads the journal each call (via
load_records) so a freshly-placed bracket is visible, recomputes every verdict,
and emits one Transition per entry the FIRST time its status enters a reportable
state (FILLED / PARTIALLY_FILLED / terminal). A plain WORKING entry — including
WORKING(PAST-TTL!) — emits nothing (TTL divergence is an alert, not a fill).
Downstream idempotency (a re-observed FILLED after restart must not double-place
a stop) is the position-manager's job; the prev-status map only suppresses
re-emitting the SAME state on a later tick.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

from alphalens_pipeline.brokers.contract import Broker, OrderStatus
from alphalens_pipeline.brokers.reconcile import ReconcileVerdict, reconcile_brackets

TransitionKind = Literal["FILLED", "TERMINAL", "PARTIAL"]

_FILLED = OrderStatus.FILLED.value
_PARTIAL = OrderStatus.PARTIALLY_FILLED.value
_TERMINAL_TOKENS = frozenset(
    {OrderStatus.CANCELLED.value, OrderStatus.REJECTED.value, OrderStatus.EXPIRED.value}
)


@dataclass(frozen=True)
class Transition:
    order_id: str
    kind: TransitionKind
    ticker: str = ""
    filled_quantity: float = 0.0
    verdict: ReconcileVerdict | None = None


@runtime_checkable
class FillSource(Protocol):
    def poll_tick(self) -> list[Transition]: ...


# StreamingFillSource (phase-B, WebSocket push) is intentionally NOT defined
# here. It will implement this SAME FillSource Protocol and layer ABOVE the
# polling floor with no control-loop change; adding it also adds the
# PUT /streaming/ws/authorize reauth to the session-keeper.


def _classify(status: str) -> TransitionKind | None:
    if status == _FILLED:
        return "FILLED"
    if status == _PARTIAL:
        return "PARTIAL"
    if status in _TERMINAL_TOKENS:
        return "TERMINAL"
    return None


class PollingFillSource:
    """MVP fill-source: diff successive reconcile_brackets snapshots."""

    def __init__(
        self,
        broker: Broker,
        load_records: Callable[[], Iterable[Mapping[str, Any]]],
        *,
        today: dt.date | None = None,
    ):
        self._broker = broker
        self._load_records = load_records
        self._today = today
        self._prev_status: dict[str, str] = {}

    def poll_tick(self) -> list[Transition]:
        records = list(self._load_records())
        verdicts = reconcile_brackets(records, self._broker, today=self._today)
        transitions: list[Transition] = []
        for verdict in verdicts:
            order_id = verdict.entry_order_id
            if not order_id:
                continue
            previous = self._prev_status.get(order_id)
            current = verdict.status
            self._prev_status[order_id] = current
            if current == previous:
                continue
            kind = _classify(current)
            if kind is None:
                continue
            transitions.append(Transition(
                order_id=order_id, kind=kind, ticker=verdict.ticker,
                filled_quantity=float(verdict.details.get("filled_quantity") or 0.0),
                verdict=verdict,
            ))
        return transitions


__all__ = ["FillSource", "PollingFillSource", "Transition", "TransitionKind"]
```

4. Run it, expect PASS. 5. Commit:
```bash
git add apps/alphalens-pipeline/alphalens_pipeline/brokers/automanager/fill_source.py \
        apps/alphalens-research/tests/brokers/automanager/test_fill_source.py
git commit -m "feat(brokers): add pluggable fill-source with polling implementation"
```

---

## Task 8 — `reconcile_bridge.py` (thin adapter over `reconcile_brackets`)

One faithful delegate to the shipped read-only engine; the loop + crash-recovery both call `verdicts` instead of reaching into `reconcile` directly (single `automanager` seam). Adds no reclassification; forwards `today`.

**Files:** create `.../automanager/reconcile_bridge.py`, `.../automanager/test_reconcile_bridge.py`.

**Interfaces:**
- Consumes (verified): `reconcile_brackets(records, broker, *, today=None) -> list[ReconcileVerdict]`, `ReconcileVerdict` (`.divergence`, `.verdict`), `Broker`, `OrderState`, `OrderStatus`.
- Produces: `verdicts(records: Iterable[Mapping[str,Any]], broker: Broker, *, today: dt.date | None = None) -> list[ReconcileVerdict]`.

**Steps:**

1. Write the FAILING test `test_reconcile_bridge.py`:
```python
"""Hermetic tests for the reconcile-bridge adapter.

verdicts is a thin faithful delegate to reconcile_brackets — must return exactly
what the engine returns (no reclassification) and forward the today seam so the
trading-day PAST-TTL sweep is drivable from the control loop.
"""

from __future__ import annotations

import datetime as dt
import unittest
from typing import Any

from alphalens_pipeline.brokers.automanager.reconcile_bridge import verdicts
from alphalens_pipeline.brokers.contract import OrderState, OrderStatus
from alphalens_pipeline.brokers.reconcile import reconcile_brackets

_TS = "2026-07-06T18:00:00+00:00"
_TODAY_FRESH = dt.date(2026, 7, 8)
_TODAY_STALE = dt.date(2026, 7, 17)


def _order_state(order_id: str, status: OrderStatus) -> OrderState:
    return OrderState(order_id=order_id, status=status, instrument=None,
                      filled_quantity=0.0, raw_status="")


class _StubBroker:
    name = "stub"

    def __init__(self, open_orders: list[OrderState]) -> None:
        self._open_orders = open_orders

    def list_open_orders(self) -> list[OrderState]:
        return list(self._open_orders)


def _record() -> dict[str, Any]:
    return {
        "execution_config_version": "execution-v1-test", "ts": _TS,
        "brief_date": "2026-07-06", "ticker": "KO", "mic": "XNYS", "uic": "307",
        "brackets": [{"client_request_id": "rid-1", "entry_order_id": "E-1",
                      "exit_order_ids": ["T-1", "S-1"], "qty": 2, "entry": 82.0,
                      "stop": 78.0, "tp": 90.0, "ttl": 5}],
        "precheck": [],
    }


class ReconcileBridgeTests(unittest.TestCase):
    def test_verdicts_matches_reconcile_brackets_exactly(self) -> None:
        via_bridge = verdicts([_record()], _StubBroker([_order_state("E-1", OrderStatus.WORKING)]),
                              today=_TODAY_FRESH)
        direct = reconcile_brackets([_record()], _StubBroker([_order_state("E-1", OrderStatus.WORKING)]),
                                    today=_TODAY_FRESH)
        self.assertEqual(via_bridge, direct)

    def test_today_is_forwarded_so_past_ttl_divergence_surfaces(self) -> None:
        stale = verdicts([_record()], _StubBroker([_order_state("E-1", OrderStatus.WORKING)]),
                        today=_TODAY_STALE)
        self.assertEqual(len(stale), 1)
        self.assertTrue(stale[0].divergence)
        self.assertIn("PAST-TTL", stale[0].verdict)


if __name__ == "__main__":
    unittest.main()
```

2. Run it, expect FAIL. Command:
```bash
.venv/bin/python -m unittest discover -s apps/alphalens-research/tests/brokers/automanager \
    -t apps/alphalens-research -p test_reconcile_bridge.py -v
```

3. Implement `reconcile_bridge.py`:
```python
"""Reconcile-bridge — the auto-manager's adapter over the read-only engine.

One thin delegate to the shipped reconcile_brackets (design memo, Components
§10): the control loop and the crash-recovery start-up path both call verdicts
instead of reaching into brokers.reconcile directly, so the loop depends on a
single automanager seam. It adds NO reclassification — reconcile stays the sole
source of truth. today is forwarded verbatim so the trading-day PAST-TTL sweep
is pinnable from the caller.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping
from typing import Any

from alphalens_pipeline.brokers.contract import Broker
from alphalens_pipeline.brokers.reconcile import ReconcileVerdict, reconcile_brackets


def verdicts(
    records: Iterable[Mapping[str, Any]],
    broker: Broker,
    *,
    today: dt.date | None = None,
) -> list[ReconcileVerdict]:
    """Recompute every journal bracket's verdict (loop tick + crash recovery)."""
    return reconcile_brackets(records, broker, today=today)


__all__ = ["verdicts"]
```

4. Run it, expect PASS. 5. Commit:
```bash
git add apps/alphalens-pipeline/alphalens_pipeline/brokers/automanager/reconcile_bridge.py \
        apps/alphalens-research/tests/brokers/automanager/test_reconcile_bridge.py
git commit -m "feat(brokers): add reconcile-bridge adapter over read-only engine"
```

---

## Task 9 — `orphan_sweeper.py` (read-only place-before-journal crash detector)

`_place_and_record` journals AFTER placement, so a crash between POST and journal write leaves an order/position at Saxo the journal never recorded. On start the sweeper flags them: an open ORDER whose id is absent from journal entry+exit ids, and an open POSITION whose `ExternalReference` (== bracket `client_request_id`) is absent from journal client_request_ids. Strictly read-only + alert-only. Position arm gated on `SupportsFillCrossCheck` (degrades to order-only sweep).

**Files:** create `.../automanager/orphan_sweeper.py`, `.../automanager/test_orphan_sweeper.py`.

**Interfaces:**
- Consumes (verified): `Broker.list_open_orders() -> list[OrderState]` (`.order_id`); `SupportsFillCrossCheck.get_open_position_references() -> list[str]` from `alphalens_pipeline.brokers.reconcile`. Journal records are `submissions.jsonl` dicts: `brackets[i]` carries `entry_order_id`, `exit_order_ids: [...]`, `client_request_id`.
- Produces: `Orphan(order_id:str, external_reference:str, kind:Literal['order','position'])` (frozen); `sweep(broker, journal) -> list[Orphan]`; `OrphanKind`.

**Steps:**

1. Write the FAILING test `test_orphan_sweeper.py`:
```python
"""Hermetic tests for the orphan-sweeper (place-before-journal crash detector).

On start it flags any open ORDER whose id the journal never recorded (entry +
exit ids) and any open POSITION whose ExternalReference is absent from the
journal's client_request_ids. Strictly read-only + alert-only; degrades to an
order-only sweep when the broker lacks the position-reference capability.
"""

from __future__ import annotations

import unittest
from typing import Any

from alphalens_pipeline.brokers.automanager.orphan_sweeper import Orphan, sweep
from alphalens_pipeline.brokers.contract import OrderState, OrderStatus


def _order_state(order_id: str) -> OrderState:
    return OrderState(order_id=order_id, status=OrderStatus.WORKING, instrument=None,
                      filled_quantity=0.0, raw_status="")


class _FullStubBroker:
    name = "stub-full"

    def __init__(self, *, open_orders: list[OrderState], open_refs: list[str]) -> None:
        self._open_orders = open_orders
        self._open_refs = open_refs

    def list_open_orders(self) -> list[OrderState]:
        return list(self._open_orders)

    def get_open_position_references(self) -> list[str]:
        return list(self._open_refs)


class _OrdersOnlyStubBroker:
    name = "stub-orders-only"

    def __init__(self, *, open_orders: list[OrderState]) -> None:
        self._open_orders = open_orders

    def list_open_orders(self) -> list[OrderState]:
        return list(self._open_orders)


def _record() -> dict[str, Any]:
    return {"brackets": [{"client_request_id": "rid-1", "entry_order_id": "E-1",
                          "exit_order_ids": ["T-1", "S-1"], "qty": 2}]}


class OrphanSweeperTests(unittest.TestCase):
    def test_flags_unjournaled_order_and_position(self) -> None:
        broker = _FullStubBroker(open_orders=[_order_state("E-1"), _order_state("X-9")],
                                 open_refs=["rid-1", "rid-orphan"])
        orphans = sweep(broker, [_record()])
        self.assertIn(Orphan(order_id="X-9", external_reference="", kind="order"), orphans)
        self.assertIn(Orphan(order_id="", external_reference="rid-orphan", kind="position"), orphans)
        self.assertEqual(len(orphans), 2)

    def test_all_known_ids_yield_no_orphans(self) -> None:
        broker = _FullStubBroker(
            open_orders=[_order_state("E-1"), _order_state("T-1"), _order_state("S-1")],
            open_refs=["rid-1"])
        self.assertEqual(sweep(broker, [_record()]), [])

    def test_degrades_to_orders_only_without_position_capability(self) -> None:
        broker = _OrdersOnlyStubBroker(open_orders=[_order_state("X-9")])
        self.assertEqual(sweep(broker, [_record()]),
                         [Orphan(order_id="X-9", external_reference="", kind="order")])


if __name__ == "__main__":
    unittest.main()
```

2. Run it, expect FAIL. Command:
```bash
.venv/bin/python -m unittest discover -s apps/alphalens-research/tests/brokers/automanager \
    -t apps/alphalens-research -p test_orphan_sweeper.py -v
```

3. Implement `orphan_sweeper.py`:
```python
"""Orphan-sweeper — read-only detector for the place-before-journal crash window.

_place_and_record journals AFTER placement, so a crash between the POST and the
journal write leaves an order/position at Saxo the append-only journal never
recorded. On start-of-process the sweeper flags them (design memo, Components
§12): an open ORDER whose id is absent from the journal's known entry + exit
order ids, and an open POSITION whose ExternalReference (the bracket
client_request_id) is absent from the journal's client_request_ids. STRICTLY
read-only + alert-only — never cancels an unrecorded order. The position arm is
gated on SupportsFillCrossCheck, so a broker without it degrades to an
order-only sweep (mirrors the reconcile engine's capability degradation).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from alphalens_pipeline.brokers.contract import Broker
from alphalens_pipeline.brokers.reconcile import SupportsFillCrossCheck

OrphanKind = Literal["order", "position"]


@dataclass(frozen=True)
class Orphan:
    order_id: str
    external_reference: str
    kind: OrphanKind


def _journal_index(journal: Iterable[Mapping[str, Any]]) -> tuple[set[str], set[str]]:
    known_order_ids: set[str] = set()
    known_refs: set[str] = set()
    for record in journal:
        for bracket in record.get("brackets") or []:
            entry_id = bracket.get("entry_order_id")
            if entry_id:
                known_order_ids.add(str(entry_id))
            for exit_id in bracket.get("exit_order_ids") or []:
                if exit_id:
                    known_order_ids.add(str(exit_id))
            request_id = bracket.get("client_request_id")
            if request_id:
                known_refs.add(str(request_id))
    return known_order_ids, known_refs


def sweep(broker: Broker, journal: Iterable[Mapping[str, Any]]) -> list[Orphan]:
    """Flag open orders/positions at the broker that the journal never recorded."""
    known_order_ids, known_refs = _journal_index(journal)
    orphans: list[Orphan] = []
    for state in broker.list_open_orders():
        if str(state.order_id) not in known_order_ids:
            orphans.append(Orphan(order_id=str(state.order_id), external_reference="", kind="order"))
    if isinstance(broker, SupportsFillCrossCheck):
        for reference in broker.get_open_position_references():
            if str(reference) not in known_refs:
                orphans.append(Orphan(order_id="", external_reference=str(reference), kind="position"))
    return orphans


__all__ = ["Orphan", "OrphanKind", "sweep"]
```

4. Run it, expect PASS. 5. Commit:
```bash
git add apps/alphalens-pipeline/alphalens_pipeline/brokers/automanager/orphan_sweeper.py \
        apps/alphalens-research/tests/brokers/automanager/test_orphan_sweeper.py
git commit -m "feat(brokers): add read-only orphan-sweeper for unjournaled orders"
```

---

## Task 10 — `position_manager.advance` (verdict → Action, realized-qty standalone stop)

Pure decision function: one reconcile verdict + a per-tick `BrokerView` → the single `Action`. No I/O. The flagship rule: on entry-FILLED with an open position and no protective stop yet, size the standalone stop to the REALIZED entry fill (`verdict.details['filled_quantity']`), NEVER planned `verdict.qty` (memo Risk 2).

**Files:** create `.../automanager/position_manager.py`, `.../automanager/test_position_manager.py`.

**Interfaces:**
- Consumes (shipped, unchanged): `reconcile.ReconcileVerdict` (fields incl. `note`, `divergence`, `.unresolved`), `reconcile.reconcile_brackets`, `contract.OrderStatus`/`OrderState`. Reconcile stamps `details["filled_quantity"]` and sets `note="position open, exit orders working"` (open) / `note="round trip closed (FIFO pair)"` (closed).
- Produces: `DisasterStop(uic:int, side:str, stop_price:float)`; `BrokerView(protected_request_ids:frozenset[str], disaster_stops:Mapping[str,DisasterStop], working_children:Mapping[str,tuple[str,...]])`; `PlaceStandaloneStop(qty:float, stop_price:float)`; `CancelRemaining()`; `AlertOnly(reason:str)`; `NoOp()`; `Action = PlaceStandaloneStop | CancelRemaining | AlertOnly | NoOp`; `advance(verdict, broker_view) -> Action`.

**Steps:**

1. Write the FAILING test `test_position_manager.py`:
```python
"""Hermetic tests for position_manager.advance.

The flagship case drives the verdict through the shipped reconcile core with a
stub broker returning the REAL SIM FinalFill quantity (FillAmount==2.0, entry
order 5039287596 captured 2026-07-20) so the standalone stop sizes to the
REALIZED fill (2.0), never the planned qty (3). Realized-qty = design memo Risk 2.
"""

from __future__ import annotations

import unittest
from typing import Any

from alphalens_pipeline.brokers.automanager.position_manager import (
    AlertOnly,
    BrokerView,
    CancelRemaining,
    DisasterStop,
    NoOp,
    PlaceStandaloneStop,
    advance,
)
from alphalens_pipeline.brokers.contract import OrderState, OrderStatus
from alphalens_pipeline.brokers.reconcile import ReconcileVerdict, reconcile_brackets

_RID = "87e0ab88-c1f2-4e88-b5b8-8fbbbb6e1a6d"
_ENTRY = "5039287596"


def _record() -> dict[str, Any]:
    return {
        "execution_config_version": "execution-v2-test", "ts": "2026-07-20T14:00:00+00:00",
        "brief_date": "2026-07-20", "ticker": "KO", "mic": "XNYS", "uic": "307",
        "brackets": [{"client_request_id": _RID, "entry_order_id": _ENTRY, "exit_order_ids": [],
                      "qty": 3, "entry": 82.86, "stop": 79.0, "tp": 90.0, "ttl": 7}],
        "precheck": [],
    }


class _FilledOpenBroker:
    name = "stub-filled-open"

    def list_open_orders(self) -> list[OrderState]:
        return []

    def resolve_order_outcome(self, order_id: str) -> OrderState:
        return OrderState(order_id=order_id, status=OrderStatus.FILLED, instrument=None,
                          filled_quantity=2.0, raw_status="FinalFill/Confirmed LogId=249519481")

    def get_open_position_references(self) -> list[str]:
        return [_RID]

    def get_closed_position_rows(self) -> list[dict[str, Any]]:
        return []


def _view(**over: Any) -> BrokerView:
    base: dict[str, Any] = {
        "protected_request_ids": frozenset(),
        "disaster_stops": {_RID: DisasterStop(uic=307, side="SELL", stop_price=79.0)},
        "working_children": {},
    }
    base.update(over)
    return BrokerView(**base)


class TestAdvanceFilledSizesRealizedQty(unittest.TestCase):
    def test_filled_open_places_stop_at_realized_fill_not_planned(self) -> None:
        verdicts = reconcile_brackets([_record()], _FilledOpenBroker())
        self.assertEqual(len(verdicts), 1)
        verdict = verdicts[0]
        self.assertEqual(verdict.status, "FILLED")
        self.assertEqual(verdict.details["filled_quantity"], 2.0)
        action = advance(verdict, _view())
        self.assertIsInstance(action, PlaceStandaloneStop)
        assert isinstance(action, PlaceStandaloneStop)
        self.assertEqual(action.qty, 2.0)
        self.assertNotEqual(action.qty, verdict.qty)
        self.assertEqual(action.stop_price, 79.0)


class TestAdvanceDecisionTable(unittest.TestCase):
    def _verdict(self, **over: Any) -> ReconcileVerdict:
        base: dict[str, Any] = {
            "brief_date": "2026-07-20", "ticker": "KO", "qty": 3, "entry_order_id": _ENTRY,
            "status": "WORKING", "verdict": "WORKING", "details": {"client_request_id": _RID},
        }
        base.update(over)
        return ReconcileVerdict(**base)

    def test_working_is_noop(self) -> None:
        self.assertIsInstance(advance(self._verdict(), _view()), NoOp)

    def test_divergence_alerts_never_cancels(self) -> None:
        v = self._verdict(status="WORKING", verdict="WORKING(PAST-TTL!)", divergence=True,
                          reason="entry still working past ttl")
        action = advance(v, _view())
        self.assertIsInstance(action, AlertOnly)
        assert isinstance(action, AlertOnly)
        self.assertIn("past ttl", action.reason)

    def test_unresolved_alerts(self) -> None:
        v = self._verdict(status="UNRESOLVED", verdict="UNRESOLVED(audit_error)",
                          reason="audit_error: boom")
        self.assertIsInstance(advance(v, _view()), AlertOnly)

    def test_terminal_cancelled_cancels_remaining(self) -> None:
        self.assertIsInstance(advance(self._verdict(status="CANCELLED", verdict="CANCELLED"), _view()),
                              CancelRemaining)

    def test_filled_round_trip_closed_cancels_remaining(self) -> None:
        v = self._verdict(status="FILLED", verdict="FILLED(closed r=+1.00)",
                          note="round trip closed (FIFO pair)",
                          details={"client_request_id": _RID, "filled_quantity": 2.0})
        self.assertIsInstance(advance(v, _view()), CancelRemaining)

    def test_filled_open_already_protected_is_noop(self) -> None:
        v = self._verdict(status="FILLED", verdict="FILLED",
                          note="position open, exit orders working",
                          details={"client_request_id": _RID, "filled_quantity": 2.0})
        self.assertIsInstance(advance(v, _view(protected_request_ids=frozenset({_RID}))), NoOp)

    def test_filled_open_missing_disaster_stop_alerts(self) -> None:
        v = self._verdict(status="FILLED", verdict="FILLED",
                          note="position open, exit orders working",
                          details={"client_request_id": _RID, "filled_quantity": 2.0})
        self.assertIsInstance(advance(v, _view(disaster_stops={})), AlertOnly)

    def test_filled_open_unknown_fill_qty_alerts_never_sizes(self) -> None:
        v = self._verdict(status="FILLED", verdict="FILLED",
                          note="position open, exit orders working",
                          details={"client_request_id": _RID})
        self.assertIsInstance(advance(v, _view()), AlertOnly)


if __name__ == "__main__":
    unittest.main()
```

2. Run it, expect FAIL. Command:
```bash
.venv/bin/python -m unittest discover -s apps/alphalens-research/tests/brokers/automanager \
    -t apps/alphalens-research -p test_position_manager.py -v
```

3. Implement `position_manager.py`:
```python
"""Position-manager — the "act" half of the auto-manager loop.

Pure decision function: one reconcile verdict + a per-tick BrokerView (the
control loop assembles it from the broker + the standalone-stop journal) -> the
single Action to take. No I/O — the control loop executes the returned Action.

MVP action set (design memo Components §9):
  entry FILLED, position open, no protective stop yet
      -> PlaceStandaloneStop(realized filled qty, journaled disaster stop)
  round-trip closed / CANCELLED / REJECTED / EXPIRED -> CancelRemaining
  PAST-TTL / divergence / UNRESOLVED -> AlertOnly(reason) (never auto-cancel)
  else (still WORKING, or already protected) -> NoOp

Realized-qty rule (Risk 2): the stop MUST size to the REALIZED entry fill
(verdict.details['filled_quantity']), NEVER planned verdict.qty — a planned-qty
stop over-hedges and can flip short after a partial fill.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from alphalens_pipeline.brokers.contract import OrderStatus
from alphalens_pipeline.brokers.reconcile import ReconcileVerdict

# Exact reconcile note string this module keys on (brokers/reconcile.py
# _reconcile_filled). Pinned as a constant so a reconcile-side wording change
# fails these tests loudly rather than silently mis-classifying a live position.
_NOTE_ROUND_TRIP_CLOSED = "round trip closed (FIFO pair)"

_TERMINAL_NON_FILLED = frozenset(
    {OrderStatus.CANCELLED.value, OrderStatus.REJECTED.value, OrderStatus.EXPIRED.value}
)


@dataclass(frozen=True)
class DisasterStop:
    uic: int
    side: str  # "SELL" for a long position's protective stop
    stop_price: float


@dataclass(frozen=True)
class BrokerView:
    protected_request_ids: frozenset[str]  # entries already carrying a live standalone stop
    disaster_stops: Mapping[str, DisasterStop]  # journaled disaster stop per entry client_request_id
    working_children: Mapping[str, tuple[str, ...]]  # request_id -> still-working exit order ids


@dataclass(frozen=True)
class PlaceStandaloneStop:
    qty: float
    stop_price: float


@dataclass(frozen=True)
class CancelRemaining:
    pass


@dataclass(frozen=True)
class AlertOnly:
    reason: str


@dataclass(frozen=True)
class NoOp:
    pass


Action = PlaceStandaloneStop | CancelRemaining | AlertOnly | NoOp


def advance(verdict: ReconcileVerdict, broker_view: BrokerView) -> Action:
    """One verdict -> the single MVP Action (pure; no side effects)."""
    request_id = str(verdict.details.get("client_request_id") or "")
    if verdict.divergence:
        return AlertOnly(verdict.reason or f"{verdict.ticker}: divergence — {verdict.verdict}")
    if verdict.unresolved:
        return AlertOnly(verdict.reason or f"{verdict.ticker}: {verdict.verdict}")
    if verdict.status == OrderStatus.FILLED.value:
        return _advance_filled(verdict, broker_view, request_id)
    if verdict.status in _TERMINAL_NON_FILLED:
        return CancelRemaining()
    return NoOp()  # WORKING / PARTIALLY_FILLED, not past TTL


def _advance_filled(verdict: ReconcileVerdict, broker_view: BrokerView, request_id: str) -> Action:
    if verdict.note == _NOTE_ROUND_TRIP_CLOSED:
        return CancelRemaining()
    if request_id in broker_view.protected_request_ids:
        return NoOp()  # standalone stop already placed on a prior tick
    filled = verdict.details.get("filled_quantity")
    try:
        realized_qty = float(filled)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        realized_qty = 0.0
    if realized_qty <= 0:
        return AlertOnly(
            f"{verdict.ticker}: entry FILLED but realized fill qty is unknown "
            f"({filled!r}) — refusing to size a standalone stop"
        )
    disaster = broker_view.disaster_stops.get(request_id)
    if disaster is None:
        return AlertOnly(
            f"{verdict.ticker}: entry FILLED but no journaled disaster stop for "
            f"request {request_id!r} — cannot protect the position"
        )
    return PlaceStandaloneStop(qty=realized_qty, stop_price=disaster.stop_price)


__all__ = [
    "Action", "AlertOnly", "BrokerView", "CancelRemaining", "DisasterStop",
    "NoOp", "PlaceStandaloneStop", "advance",
]
```

4. Run it, expect PASS. 5. Commit:
```bash
git add apps/alphalens-pipeline/alphalens_pipeline/brokers/automanager/position_manager.py \
        apps/alphalens-research/tests/brokers/automanager/test_position_manager.py
git commit -m "feat(brokers): position-manager advance() sizes standalone stop to realized fill"
```

---

## Task 11 — `control_loop` tick orchestration + `alphalens broker manage`

The daemon shell. Each tick: kill-gate → session-keeper → orphan-sweep (start only) → drain+place armed picks → reconcile-bridge → `position_manager.advance` → execute Action. All Tasks 1–10 seams arrive via `LoopDeps` so the tick logic is stub-testable; `build_default_deps` is the only site that wires the real modules (SIM-probe-covered, not unit-tested).

**Files:** create `.../automanager/control_loop.py`; modify `alphalens_cli/commands/broker.py`; create `.../automanager/test_control_loop.py`.

**Interfaces:**
- Consumes (Task 10): `position_manager.{advance, BrokerView, DisasterStop, PlaceStandaloneStop, CancelRemaining, AlertOnly, NoOp}`.
- Consumes (Tasks 1–9, wired ONLY inside `build_default_deps`, stubbed in tests): `picks.iter_picks`, `safety.check`, `session_keeper.SessionKeeper(...).ensure_alive`, `placement_planner.classify`, `fill_source.PollingFillSource`, `reconcile_bridge.verdicts`, `orphan_sweeper.sweep`, `SaxoBroker.place_standalone_stop`.
- Consumes (shipped): `brokers.registry.get_default_broker`, `brokers.submission_log.{iter_submission_records, DEFAULT_SUBMISSIONS_PATH}`, `brokers.contract.{Broker, BrokerError}`, `data.alt_data.telegram_client.get_default_telegram_alert`.
- Produces: `LoopDeps`, `TickReport(picks_placed, stops_placed, cancels, alerts, orphans, verdict_count, actions)`, `run_once(deps, *, sweep_orphans=False) -> TickReport`, `run_daemon(deps, *, once, poll_seconds, sleep_fn=time.sleep, is_running=_always, heartbeat_fn=_default_emit_heartbeat) -> None`, `build_default_deps(*, poll_seconds) -> LoopDeps`, `KILL_FILE_PATH`, `_default_emit_heartbeat` (placeholder in this task; the real emitter lands in **Task 13**); CLI `broker manage`.

**Steps:**

1. Write the FAILING test `test_control_loop.py`:
```python
"""Hermetic tests for control_loop.run_once / run_daemon.

Every Task 1-10 dependency is injected as a stub (build_default_deps is covered
by the SIM probe). Under test: kill-gate placement, always reconcile, execute
the position-manager Action, re-derive identical classification on restart.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from alphalens_pipeline.brokers.automanager import control_loop as cl
from alphalens_pipeline.brokers.automanager.position_manager import BrokerView, DisasterStop
from alphalens_pipeline.brokers.reconcile import ReconcileVerdict

_RID = "rid-KO"


class _StubBroker:
    name = "stub"

    def __init__(self) -> None:
        self.cancelled: list[str] = []

    def cancel_order(self, order_id: str) -> None:
        self.cancelled.append(order_id)


def _verdict(**over: Any) -> ReconcileVerdict:
    base: dict[str, Any] = {
        "brief_date": "2026-07-20", "ticker": "KO", "qty": 3, "entry_order_id": "E-1",
        "status": "WORKING", "verdict": "WORKING", "details": {"client_request_id": _RID},
    }
    base.update(over)
    return ReconcileVerdict(**base)


def _view() -> BrokerView:
    return BrokerView(protected_request_ids=frozenset(),
                      disaster_stops={_RID: DisasterStop(uic=307, side="SELL", stop_price=79.0)},
                      working_children={_RID: ("T-1",)})


def _deps(broker: _StubBroker, *, kill_file: Path, verdicts: list[ReconcileVerdict],
          place_calls: list, stop_calls: list, alerts: list, picks: list | None = None,
          chain_alive: bool = True) -> cl.LoopDeps:
    return cl.LoopDeps(
        broker=broker, kill_file=kill_file,
        ensure_alive=lambda: type("C", (), {"alive": chain_alive, "reason": None})(),
        iter_picks=lambda: iter(picks or []),
        place_pick=lambda pick: (place_calls.append(pick) or True),
        read_records=lambda: [{"brackets": [{"client_request_id": _RID}]}],
        verdicts_fn=lambda records, broker: list(verdicts),
        build_position_view=lambda broker, records: _view(),
        place_standalone_stop=lambda uic, side, qty, price: stop_calls.append((uic, side, qty, price)),
        sweep_orphans_fn=lambda broker: [],
        alert=lambda msg: alerts.append(msg),
    )


class TestRunOncePlacement(unittest.TestCase):
    def test_filled_open_places_standalone_stop_at_realized_qty(self) -> None:
        with TemporaryDirectory() as d:
            broker = _StubBroker()
            stop_calls: list = []
            v = _verdict(status="FILLED", verdict="FILLED",
                        note="position open, exit orders working",
                        details={"client_request_id": _RID, "filled_quantity": 2.0})
            deps = _deps(broker, kill_file=Path(d) / "KILL", verdicts=[v],
                        place_calls=[], stop_calls=stop_calls, alerts=[])
            report = cl.run_once(deps)
            self.assertEqual(stop_calls, [(307, "SELL", 2.0, 79.0)])
            self.assertEqual(report.stops_placed, 1)

    def test_drains_armed_pick_when_chain_alive_and_no_kill(self) -> None:
        with TemporaryDirectory() as d:
            place_calls: list = []
            deps = _deps(_StubBroker(), kill_file=Path(d) / "KILL", verdicts=[],
                        place_calls=place_calls, stop_calls=[], alerts=[], picks=["pick-KO"])
            cl.run_once(deps)
            self.assertEqual(place_calls, ["pick-KO"])


class TestKillFileGate(unittest.TestCase):
    def test_kill_present_suppresses_placement_but_still_cancels(self) -> None:
        with TemporaryDirectory() as d:
            kill = Path(d) / "KILL"
            kill.write_text("halt")
            broker = _StubBroker()
            place_calls: list = []
            stop_calls: list = []
            alerts: list = []
            terminal = _verdict(status="CANCELLED", verdict="CANCELLED")
            filled = _verdict(status="FILLED", verdict="FILLED",
                            note="position open, exit orders working",
                            details={"client_request_id": _RID, "filled_quantity": 2.0})
            deps = _deps(broker, kill_file=kill, verdicts=[terminal, filled],
                        place_calls=place_calls, stop_calls=stop_calls, alerts=alerts,
                        picks=["pick-KO"])
            cl.run_once(deps)
            self.assertEqual(place_calls, [])
            self.assertEqual(stop_calls, [])
            self.assertEqual(broker.cancelled, ["T-1"])
            self.assertTrue(any("KILL" in a for a in alerts))


class TestCrashRecovery(unittest.TestCase):
    def test_restart_re_derives_identical_classification(self) -> None:
        with TemporaryDirectory() as d:
            broker = _StubBroker()
            v = _verdict(status="CANCELLED", verdict="CANCELLED")
            deps = _deps(broker, kill_file=Path(d) / "KILL", verdicts=[v],
                        place_calls=[], stop_calls=[], alerts=[])
            r1 = cl.run_once(deps)
            r2 = cl.run_once(deps)
            self.assertEqual(r1.actions, r2.actions)
            self.assertEqual(r1.verdict_count, r2.verdict_count)


class TestRunDaemonOnce(unittest.TestCase):
    def test_once_runs_single_tick_sweeps_orphans_and_never_sleeps(self) -> None:
        with TemporaryDirectory() as d:
            broker = _StubBroker()
            sweeps: list = []
            deps = _deps(broker, kill_file=Path(d) / "KILL", verdicts=[],
                        place_calls=[], stop_calls=[], alerts=[])
            deps = cl.LoopDeps(**{**deps.__dict__, "sweep_orphans_fn": lambda b: sweeps.append(1) or []})
            slept: list = []
            beats: list = []
            cl.run_daemon(deps, once=True, poll_seconds=45,
                          sleep_fn=lambda s: slept.append(s), heartbeat_fn=lambda: beats.append(1))
            self.assertEqual(len(sweeps), 1)
            self.assertEqual(slept, [])
            self.assertEqual(len(beats), 1)


class TestManageCommandRegistered(unittest.TestCase):
    def test_broker_app_has_manage_command(self) -> None:
        from alphalens_cli.commands.broker import broker_app
        names = {c.name for c in broker_app.registered_commands}
        self.assertIn("manage", names)


if __name__ == "__main__":
    unittest.main()
```

2. Run it, expect FAIL. Command:
```bash
.venv/bin/python -m unittest discover -s apps/alphalens-research/tests/brokers/automanager \
    -t apps/alphalens-research -p test_control_loop.py -v
```

3. Implement `control_loop.py`:
```python
"""Control-loop — the always-on daemon shell (design Approach 1).

Each tick: kill-gate -> session-keeper -> orphan-sweep (start only) ->
drain+place armed picks -> reconcile-bridge -> position_manager.advance ->
execute Action. State lives entirely in the append-only journals; status is
recomputed every tick by reconcile (crash-recovery = re-run the read-only
verdict engine). All Task 1-10 seams arrive via LoopDeps so the tick logic is
testable against stubs; build_default_deps() is the only site that wires the
real modules.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from alphalens_pipeline.brokers.automanager.position_manager import (
    AlertOnly,
    BrokerView,
    CancelRemaining,
    NoOp,
    PlaceStandaloneStop,
    advance,
)

if TYPE_CHECKING:
    from alphalens_pipeline.brokers.contract import Broker
    from alphalens_pipeline.brokers.reconcile import ReconcileVerdict

logger = logging.getLogger(__name__)

KILL_FILE_PATH = Path.home() / ".alphalens" / "broker_orders" / "KILL"

# Prometheus heartbeat gauge (Task 13 wires _default_emit_heartbeat as the
# run_daemon default; the metric name has one home here).
HEARTBEAT_METRIC = 'alphalens_broker_manager_last_tick_timestamp_seconds{job="broker-manager"}'


@dataclass(frozen=True)
class LoopDeps:
    broker: "Broker"
    kill_file: Path
    ensure_alive: Callable[[], Any]  # () -> ChainStatus(alive, reason)
    iter_picks: Callable[[], Iterator[Any]]
    place_pick: Callable[[Any], bool]  # safety.check + classify + place + journal; True if placed
    read_records: Callable[[], list[Mapping[str, Any]]]
    verdicts_fn: Callable[[list[Mapping[str, Any]], "Broker"], list["ReconcileVerdict"]]
    build_position_view: Callable[["Broker", list[Mapping[str, Any]]], BrokerView]
    place_standalone_stop: Callable[[int, str, float, float], None]
    sweep_orphans_fn: Callable[["Broker"], list[Any]]
    alert: Callable[[str], None]


@dataclass
class TickReport:
    picks_placed: int = 0
    stops_placed: int = 0
    cancels: int = 0
    alerts: int = 0
    orphans: int = 0
    verdict_count: int = 0
    actions: list[tuple[str, str]] = field(default_factory=list)  # (ticker, Action class)


def _always() -> bool:
    return True


def _default_emit_heartbeat() -> None:
    """Placeholder — Task 13 replaces this with the real textfile emitter."""


def run_once(deps: LoopDeps, *, sweep_orphans: bool = False) -> TickReport:
    """One control-loop tick. Placement is gated on (no KILL) AND (chain alive);
    reconcile + Action execution ALWAYS run so a KILL still cancels and a dead
    chain still surfaces terminal state."""
    report = TickReport()
    kill = deps.kill_file.exists()
    chain = deps.ensure_alive()
    if not getattr(chain, "alive", False):
        deps.alert(f"session-keeper: chain dead — {getattr(chain, 'reason', None)}; placement halted")

    if sweep_orphans:
        for orphan in deps.sweep_orphans_fn(deps.broker):
            deps.alert(f"orphan (placed but never journaled): {orphan}")
            report.orphans += 1

    if not kill and getattr(chain, "alive", False):
        for pick in deps.iter_picks():
            if deps.place_pick(pick):
                report.picks_placed += 1

    records = deps.read_records()
    verdicts = deps.verdicts_fn(records, deps.broker)
    report.verdict_count = len(verdicts)
    position_view = deps.build_position_view(deps.broker, records)
    for verdict in verdicts:
        action = advance(verdict, position_view)
        report.actions.append((verdict.ticker, type(action).__name__))
        _execute_action(deps, verdict, action, position_view, kill=kill, report=report)
    return report


def _execute_action(deps: LoopDeps, verdict: "ReconcileVerdict", action: Any,
                    position_view: BrokerView, *, kill: bool, report: TickReport) -> None:
    request_id = str(verdict.details.get("client_request_id") or "")
    if isinstance(action, NoOp):
        return
    if isinstance(action, AlertOnly):
        deps.alert(action.reason)
        report.alerts += 1
        return
    if isinstance(action, CancelRemaining):
        for order_id in position_view.working_children.get(request_id, ()):  # ungated safe op
            deps.broker.cancel_order(order_id)
            report.cancels += 1
        return
    if isinstance(action, PlaceStandaloneStop):
        if kill:
            deps.alert(f"KILL active — NOT placing standalone stop for {verdict.ticker}")
            return
        disaster = position_view.disaster_stops.get(request_id)
        if disaster is None:  # defence: advance already alerted, but never place blind
            deps.alert(f"{verdict.ticker}: standalone-stop placement skipped — no journaled disaster stop")
            return
        deps.place_standalone_stop(disaster.uic, disaster.side, action.qty, action.stop_price)
        report.stops_placed += 1


def run_daemon(deps: LoopDeps, *, once: bool, poll_seconds: float,
               sleep_fn: Callable[[float], None] = time.sleep,
               is_running: Callable[[], bool] = _always,
               heartbeat_fn: Callable[[], None] = _default_emit_heartbeat) -> None:
    """Drive run_once forever (orphan sweep on the FIRST tick only), or once."""
    first = True
    while is_running():
        run_once(deps, sweep_orphans=first)
        heartbeat_fn()  # Task 13: writes the Prometheus heartbeat gauge
        first = False
        if once:
            return
        sleep_fn(poll_seconds)


def build_default_deps(*, poll_seconds: float) -> LoopDeps:
    """Wire the real Task 1-10 seams. Imported lazily so the alphalens binary's
    startup budget stays off this path (lazy-CLI doctrine); covered by the
    SAXO_LIVE_TEST=1 SIM probe, not the hermetic unit tests. The four factory
    helpers (_default_oauth_provider, _make_place_pick, _make_position_view_builder,
    _make_standalone_stop_placer) compose the Task 1-10 seams; they are validated
    only by the SIM probe. The pluggable fill-source (fill_source.PollingFillSource)
    stays a tested seam for the phase-B streaming drop-in; the MVP loop detects
    fills through reconcile_bridge.verdicts (reconcile classifies FILLED), so no
    PollingFillSource instance is wired into LoopDeps here."""
    from alphalens_pipeline.brokers.automanager import (  # noqa: F401 (planner/safety used by _make_place_pick)
        orphan_sweeper,
        picks,
        placement_planner,
        reconcile_bridge,
        safety,
        session_keeper,
    )
    from alphalens_pipeline.brokers.registry import get_default_broker
    from alphalens_pipeline.brokers.submission_log import (
        DEFAULT_SUBMISSIONS_PATH,
        iter_submission_records,
    )
    from alphalens_pipeline.data.alt_data.telegram_client import get_default_telegram_alert

    broker = get_default_broker()
    keeper = session_keeper.SessionKeeper(_default_oauth_provider())

    def _read_records() -> list[Mapping[str, Any]]:
        return list(iter_submission_records(DEFAULT_SUBMISSIONS_PATH))

    return LoopDeps(
        broker=broker,
        kill_file=KILL_FILE_PATH,
        ensure_alive=keeper.ensure_alive,
        iter_picks=picks.iter_picks,
        place_pick=_make_place_pick(broker),
        read_records=_read_records,
        verdicts_fn=reconcile_bridge.verdicts,
        build_position_view=_make_position_view_builder(broker),
        place_standalone_stop=_make_standalone_stop_placer(broker),
        sweep_orphans_fn=lambda b: orphan_sweeper.sweep(b, _read_records()),
        alert=get_default_telegram_alert(),
    )
```
   > The four SIM-probe-only factory helpers — `_default_oauth_provider()` (returns the shipped `OAuthTokenProvider`), `_make_place_pick(broker)` (composes `safety.check` → `placement_planner.classify` → placer loop over `place_bracket_order` + journal, reusing `client_request_id` on retry), `_make_position_view_builder(broker)` (broker snapshot + out-of-band standalone-stop journal → `position_manager.BrokerView`), and `_make_standalone_stop_placer(broker)` (adapts `SaxoBroker.place_standalone_stop` + out-of-band journal write) — are implemented in this file but exercised end-to-end only by the SIM live probe (see Deferred follow-ups). Write them as thin composers; they carry no hermetic unit-test cycle. This is the spec's Component 6 "placer" home.

4. Add the `manage` command to `broker.py` (after `cancel_command`, lazy imports inside the body):
```python
@broker_app.command(name="manage")
def manage_command(
    once: bool = typer.Option(False, "--once", help="Run a single control-loop tick and exit."),
    poll_seconds: float = typer.Option(45.0, "--poll-seconds",
                                       help="Seconds to sleep between ticks in daemon mode (30-60s)."),
) -> None:
    """Run the SIM auto-manager loop: drain armed picks, place the in-band
    subset + standalone disaster stop, reconcile, and manage each base position
    to terminal. Kill instantly with `touch ~/.alphalens/broker_orders/KILL`.
    SIM-only; placement still needs ALPHALENS_BROKER_ALLOW_ORDERS=1 (enforced
    inside the broker)."""
    from alphalens_pipeline.brokers.automanager.control_loop import build_default_deps, run_daemon
    from alphalens_pipeline.brokers.contract import BrokerError

    try:
        deps = build_default_deps(poll_seconds=poll_seconds)
        run_daemon(deps, once=once, poll_seconds=poll_seconds)
    except BrokerError as exc:
        raise _fail(f"broker manage failed: {exc}") from exc
    if once:
        typer.echo("manage: single tick complete")
```

5. Run it, expect PASS.
   > If `broker_app.registered_commands` is unavailable on the installed Typer version, fall back to `typer.main.get_command(broker_app)` and assert `"manage" in that_group.commands` — do NOT skip the assertion.
6. Commit:
```bash
git add apps/alphalens-pipeline/alphalens_pipeline/brokers/automanager/control_loop.py \
        apps/alphalens-pipeline/alphalens_cli/commands/broker.py \
        apps/alphalens-research/tests/brokers/automanager/test_control_loop.py
git commit -m "feat(brokers): auto-manager control loop + 'alphalens broker manage' entrypoint"
```

---

## Task 12 — systemd units (broker-manager daemon + saxo-refresh idle keep-alive)

**Files:** create `deploy/systemd/alphalens-broker-manager.service`, `deploy/systemd/alphalens-saxo-refresh.service`, `deploy/systemd/alphalens-saxo-refresh.timer`; modify `apps/alphalens-research/tests/test_deploy_systemd_units.py`.

**Interfaces:**
- Consumes: `alphalens broker manage --poll-seconds 45` (Task 11), `alphalens broker auth --refresh` (shipped), the shipped `deploy/systemd/bin/alphalens-emit-job-metrics` hook. Job names: `broker-manager` (daemon, excluded from the emit-hook glob like `form4-backfill`), `saxo-refresh` (timer oneshot, in the glob).
- Produces: three unit files + guarding suites.

**Steps:**

1. Write the FAILING test additions in `test_deploy_systemd_units.py`. Add path constants near the other unit constants (after `EDGE_MIRROR_TIMER`):
```python
BROKER_MANAGER_SERVICE = SYSTEMD_DIR / "alphalens-broker-manager.service"
SAXO_REFRESH_SERVICE = SYSTEMD_DIR / "alphalens-saxo-refresh.service"
SAXO_REFRESH_TIMER = SYSTEMD_DIR / "alphalens-saxo-refresh.timer"
```
   Extend `ACTIVE_SERVICES` to include the refresh oneshot (append `SAXO_REFRESH_SERVICE`). Append two suites:
```python
class TestBrokerManagerUnit(unittest.TestCase):
    def test_service_exists(self) -> None:
        self.assertTrue(BROKER_MANAGER_SERVICE.is_file(), f"missing at {BROKER_MANAGER_SERVICE}")

    def test_service_is_simple_daemon_with_restart(self) -> None:
        text = BROKER_MANAGER_SERVICE.read_text()
        self.assertIn("Type=simple", text)
        self.assertRegex(text, re.compile(r"^Restart=on-failure\s*$", re.MULTILINE))
        self.assertRegex(text, re.compile(r"^RestartSec=\d+\s*$", re.MULTILINE))

    def test_execstart_runs_host_venv_manage(self) -> None:
        self.assertRegex(
            BROKER_MANAGER_SERVICE.read_text(),
            re.compile(r"^ExecStart=%h/AlphaLens/\.venv/bin/alphalens\s+broker\s+manage\b"
                       r"[^\n]*--poll-seconds\s+\d+", re.MULTILINE),
            "ExecStart must run host-venv `broker manage --poll-seconds N` (no --once).")

    def test_execstart_is_not_once(self) -> None:
        self.assertNotIn("--once", BROKER_MANAGER_SERVICE.read_text())

    def test_env_file_fail_loud_and_documents_allow_orders_arm(self) -> None:
        text = BROKER_MANAGER_SERVICE.read_text()
        self.assertRegex(text, re.compile(r"^EnvironmentFile=/etc/alphalens/env\s*$", re.MULTILINE))
        self.assertIn("ALPHALENS_BROKER_ALLOW_ORDERS", text)

    def test_resource_caps_present(self) -> None:
        text = BROKER_MANAGER_SERVICE.read_text()
        self.assertRegex(text, re.compile(r"^MemoryMax=\S+\s*$", re.MULTILINE))
        self.assertRegex(text, re.compile(r"^TasksMax=\d+\s*$", re.MULTILINE))

    def test_working_dir_and_install(self) -> None:
        text = BROKER_MANAGER_SERVICE.read_text()
        self.assertIn("WorkingDirectory=%h/AlphaLens", text)
        self.assertRegex(text, re.compile(r"^\[Install\]\s*$", re.MULTILINE))

    def test_daemon_excluded_from_emit_hook_glob(self) -> None:
        self.assertNotIn(BROKER_MANAGER_SERVICE, ACTIVE_SERVICES)


class TestSaxoRefreshUnit(unittest.TestCase):
    def test_service_is_oneshot_refresh(self) -> None:
        text = SAXO_REFRESH_SERVICE.read_text()
        self.assertIn("Type=oneshot", text)
        self.assertRegex(text, re.compile(
            r"^ExecStart=%h/AlphaLens/\.venv/bin/alphalens\s+broker\s+auth\s+--refresh\s*$",
            re.MULTILINE), "ExecStart must run `broker auth --refresh`.")

    def test_service_env_fail_loud_and_working_dir(self) -> None:
        text = SAXO_REFRESH_SERVICE.read_text()
        self.assertRegex(text, re.compile(r"^EnvironmentFile=/etc/alphalens/env\s*$", re.MULTILINE))
        self.assertIn("WorkingDirectory=%h/AlphaLens", text)

    def test_timer_fires_inside_40min_window_persistent(self) -> None:
        text = SAXO_REFRESH_TIMER.read_text()
        self.assertRegex(text, re.compile(r"^OnUnitActiveSec=20min\s*$", re.MULTILINE))
        self.assertRegex(text, re.compile(r"^Persistent=true\s*$", re.MULTILINE))

    def test_timer_carries_install_section(self) -> None:
        text = SAXO_REFRESH_TIMER.read_text()
        self.assertRegex(text, re.compile(r"^\[Install\]\s*$", re.MULTILINE))
        self.assertRegex(text, re.compile(r"^WantedBy=timers\.target\s*$", re.MULTILINE))
```
   > Match the file's existing helpers (`SYSTEMD_DIR`, `re`, the `ACTIVE_SERVICES` shape, the `test_every_active_service_wires_emit_hook` glob). If `StartLimit*` placement is asserted elsewhere (`TestStartLimitInUnitSection`), keep the broker-manager `StartLimit*` keys in `[Unit]`.

2. Run it, expect FAIL:
```bash
.venv/bin/python -m unittest discover -s apps/alphalens-research/tests \
    -t apps/alphalens-research -p test_deploy_systemd_units.py -v
```

3. Create `deploy/systemd/alphalens-broker-manager.service`:
```ini
[Unit]
# SIM auto-manager daemon (ADR 0013 T6 IN-FLIGHT / T7 EXIT live consumer).
# Always-on polling loop: kill-gate -> session-keeper -> orphan sweep (start)
# -> drain armed picks -> place in-band subset + standalone disaster stop ->
# reconcile -> manage each base position to terminal. SIM-only (SaxoClient
# refuses non-SIM base URL). Design:
# docs/research/saxo_automanager_mvp_design_2026_07_21.md.
#
# Install as a user unit:
#   cp deploy/systemd/alphalens-broker-manager.service ~/.config/systemd/user/
#   systemctl --user daemon-reload
#   systemctl --user enable --now alphalens-broker-manager.service
# Emergency stop (instant): touch ~/.alphalens/broker_orders/KILL
# Disarm placement (needs restart): unset ALPHALENS_BROKER_ALLOW_ORDERS in
#   /etc/alphalens/env, then `systemctl --user restart ...`.
# Inspect: journalctl --user -u alphalens-broker-manager.service -f
# Persistence: sudo loginctl enable-linger "$USER"
Description=AlphaLens SIM auto-manager (poll + reconcile + exit management)
After=network-online.target
Wants=network-online.target

# Cap crash loops. [Unit]-section keys — under [Service] modern systemd
# silently ignores them (TestStartLimitInUnitSection).
StartLimitIntervalSec=600
StartLimitBurst=5

[Service]
Type=simple
WorkingDirectory=%h/AlphaLens

# Shared secrets. Needs the OAuth chain (SAXO_APP_KEY/SAXO_APP_SECRET/
# SAXO_AUTH_REDIRECT_URL), the Telegram alert pair, and — to place orders —
# ALPHALENS_BROKER_ALLOW_ORDERS=1. No leading `-`: fail loud on a missing env
# file. UNSET the arm + restart to run inert (reconcile + read only).
EnvironmentFile=/etc/alphalens/env

# Node_exporter textfile dir for the per-tick heartbeat gauge (Task 13).
Environment=ALPHALENS_TEXTFILE_DIR=/var/lib/node_exporter/textfile

# Daemon: NO --once (a clean exit would not be relaunched by Restart=on-failure).
ExecStart=%h/AlphaLens/.venv/bin/alphalens broker manage --poll-seconds 45

Restart=on-failure
RestartSec=30

StandardOutput=journal
StandardError=journal

MemoryMax=1G
TasksMax=32

[Install]
WantedBy=default.target
```

4. Create `deploy/systemd/alphalens-saxo-refresh.service`:
```ini
[Unit]
# Idle OAuth keep-alive. During active monitoring the 45s poll refreshes the
# token as a side effect; this oneshot covers idle stretches (no open bracket).
# Fires every ~20min, well inside the ~40min refresh-token window. Re-creates
# the alphalens-saxo-refresh unit ADR 0012 removed. Design memo "Session lifecycle".
#
#   cp deploy/systemd/alphalens-saxo-refresh.{service,timer} ~/.config/systemd/user/
#   systemctl --user daemon-reload
#   systemctl --user enable --now alphalens-saxo-refresh.timer
#
# Single-refresher invariant: run this ONLY on the VPS that also runs the
# broker-manager daemon (two hosts sharing the token store burn each other's
# rotation chains — the TokenStore flock is per-host).
Description=AlphaLens Saxo OAuth idle keep-alive (refresh_now)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=%h/AlphaLens

# Needs the OAuth env + TELEGRAM_* for the _chain_lost alert. Fail loud on a
# missing env file.
EnvironmentFile=/etc/alphalens/env

ExecStart=%h/AlphaLens/.venv/bin/alphalens broker auth --refresh

# Cron-health metrics (last_success refreshes each ~20min fire -> the
# AlphalensJobStale{job=saxo-refresh} rule catches "keep-alive stopped").
ExecStopPost=%h/AlphaLens/deploy/systemd/bin/alphalens-emit-job-metrics saxo-refresh

StandardOutput=journal
StandardError=journal

MemoryMax=512M
TasksMax=16

[Install]
WantedBy=default.target
```

5. Create `deploy/systemd/alphalens-saxo-refresh.timer`:
```ini
[Unit]
Description=AlphaLens Saxo OAuth keep-alive timer (~20min, inside the 40min window)

[Timer]
# 20min << 40min refresh-token life -> two consecutive misses still leave a
# margin. OnBootSec re-arms promptly after reboot; Persistent=true self-heals a
# single missed window on the next boot.
OnBootSec=3min
OnUnitActiveSec=20min
Persistent=true

[Install]
WantedBy=timers.target
```

6. Run it, expect PASS (same command as step 2). Also confirm `test_every_active_service_wires_emit_hook` passes now that saxo-refresh carries the `ExecStopPost` hook.
7. Commit:
```bash
git add deploy/systemd/alphalens-broker-manager.service \
        deploy/systemd/alphalens-saxo-refresh.service \
        deploy/systemd/alphalens-saxo-refresh.timer \
        apps/alphalens-research/tests/test_deploy_systemd_units.py
git commit -m "feat(brokers): systemd units for the SIM auto-manager daemon + OAuth keep-alive timer"
```

---

## Task 13 — health/metrics (per-tick heartbeat + Prometheus alert rules)

Replace the Task 11 placeholder `_default_emit_heartbeat` with the real textfile emitter and add the Prometheus rules. A `Type=simple` daemon rarely triggers `ExecStopPost`, so its health signal is the per-tick heartbeat gauge, not `last_success`.

**Files:** modify `.../automanager/control_loop.py`; modify `deploy/monitoring/prometheus/rules/alphalens.yaml`; modify `.../automanager/test_control_loop.py` + `apps/alphalens-research/tests/test_deploy_systemd_units.py`.

**Interfaces:**
- Consumes: `alphalens_pipeline.observability.textfile.emit_domain_metrics(job:str, metrics:Mapping[str,float|int]) -> Path` (writes `alphalens_domain_<job>.prom`, honors `ALPHALENS_TEXTFILE_DIR`); `run_daemon` (Task 11, `heartbeat_fn` default).
- Produces: real `_default_emit_heartbeat`; alerts `AlphalensBrokerManagerHeartbeatStale` (>300s), `AlphalensBrokerManagerHeartbeatMissing`, `AlphalensJobStale{job=saxo-refresh}` (>3600s), `AlphalensJobMetricMissing{job=saxo-refresh}`.

**Steps:**

1. Append the FAILING heartbeat tests to `test_control_loop.py`:
```python
class TestHeartbeatEmitter(unittest.TestCase):
    def test_default_emit_heartbeat_writes_gauge_to_textfile_dir(self) -> None:
        import os
        from tempfile import TemporaryDirectory
        from alphalens_pipeline.brokers.automanager import control_loop as cl
        with TemporaryDirectory() as d:
            old = os.environ.get("ALPHALENS_TEXTFILE_DIR")
            os.environ["ALPHALENS_TEXTFILE_DIR"] = d
            try:
                cl._default_emit_heartbeat()
            finally:
                if old is None:
                    os.environ.pop("ALPHALENS_TEXTFILE_DIR", None)
                else:
                    os.environ["ALPHALENS_TEXTFILE_DIR"] = old
            written = Path(d) / "alphalens_domain_broker-manager.prom"
            self.assertTrue(written.is_file())
            body = written.read_text()
            self.assertIn("alphalens_broker_manager_last_tick_timestamp_seconds", body)
            self.assertIn('job="broker-manager"', body)

    def test_run_daemon_uses_default_heartbeat_signature(self) -> None:
        import inspect
        from alphalens_pipeline.brokers.automanager import control_loop as cl
        sig = inspect.signature(cl.run_daemon)
        self.assertIs(sig.parameters["heartbeat_fn"].default, cl._default_emit_heartbeat)
```
   Append the FAILING rules suite to `test_deploy_systemd_units.py`:
```python
class TestBrokerManagerHealthRules(unittest.TestCase):
    RULES = REPO_ROOT / "deploy" / "monitoring" / "prometheus" / "rules" / "alphalens.yaml"

    def setUp(self) -> None:
        self.text = self.RULES.read_text()

    def test_broker_manager_heartbeat_stale_rule(self) -> None:
        self.assertRegex(self.text, re.compile(
            r"time\(\)\s*-\s*alphalens_broker_manager_last_tick_timestamp_seconds"
            r"\{job=\"broker-manager\"\}\s*>\s*300\b"),
            "Missing AlphalensBrokerManagerHeartbeatStale (>300s).")

    def test_broker_manager_heartbeat_missing_rule(self) -> None:
        self.assertRegex(self.text, re.compile(
            r"absent\(alphalens_broker_manager_last_tick_timestamp_seconds\{job=\"broker-manager\"\}\)"))

    def test_saxo_refresh_job_stale_and_missing_rules(self) -> None:
        self.assertRegex(self.text, re.compile(
            r"time\(\)\s*-\s*alphalens_job_last_success_timestamp_seconds"
            r"\{job=\"saxo-refresh\"\}\s*>\s*3600\b"))
        self.assertRegex(self.text, re.compile(
            r"absent\(alphalens_job_last_success_timestamp_seconds\{job=\"saxo-refresh\"\}\)"))
```
   > Reuse the file's existing `REPO_ROOT` constant; if it is named differently, use that.

2. Run both, expect FAIL:
```bash
.venv/bin/python -m unittest discover -s apps/alphalens-research/tests/brokers/automanager \
    -t apps/alphalens-research -p test_control_loop.py -v
.venv/bin/python -m unittest discover -s apps/alphalens-research/tests \
    -t apps/alphalens-research -p test_deploy_systemd_units.py -v
```

3. Replace the placeholder `_default_emit_heartbeat` in `control_loop.py`:
```python
def _default_emit_heartbeat() -> None:
    """Write the per-tick Prometheus heartbeat gauge. A Type=simple daemon rarely
    triggers ExecStopPost, so the emit-job-metrics last_success clock is the
    wrong health signal — this gauge (watched by AlphalensBrokerManagerHeartbeatStale)
    is. Best-effort: a textfile-dir hiccup must never crash the loop."""
    import time as _time

    from alphalens_pipeline.observability.textfile import emit_domain_metrics

    try:
        emit_domain_metrics("broker-manager", {HEARTBEAT_METRIC: int(_time.time())})
    except OSError:
        logger.warning("broker-manager heartbeat emit failed", exc_info=True)
```
   > `HEARTBEAT_METRIC` stays as defined in Task 11. If `emit_domain_metrics` expects a bare metric name rather than a labelled one, pass the bare name and let the emitter attach `job="broker-manager"` — verify against the shipped `observability.textfile` signature and keep the test's `job="broker-manager"` assertion satisfied.

4. Add the alert rules to `deploy/monitoring/prometheus/rules/alphalens.yaml`, mirroring the shipped `feedback-shadow-returns` Stale/Missing block shape, inside the same `groups[].rules` list:
```yaml
      # broker-manager is a Type=simple daemon: ExecStopPost fires only on
      # crash/restart, so health = the per-tick heartbeat gauge the control loop
      # writes each ~45s tick. 300s = 5min ~ 6-7 missed ticks.
      - alert: AlphalensBrokerManagerHeartbeatStale
        expr: time() - alphalens_broker_manager_last_tick_timestamp_seconds{job="broker-manager"} > 300
        for: 2m
        labels:
          severity: warning
          route: telegram
          unit: broker-manager
        annotations:
          summary: "broker-manager heartbeat stale > 5m (expected ~45s ticks)"
          description: "Last control-loop tick was {{ $value | humanizeDuration }} ago — the SIM auto-manager may be wedged or stopped."

      - alert: AlphalensBrokerManagerHeartbeatMissing
        expr: absent(alphalens_broker_manager_last_tick_timestamp_seconds{job="broker-manager"})
        for: 5m
        labels:
          severity: warning
          route: telegram
          unit: broker-manager
        annotations:
          summary: "broker-manager heartbeat metric missing"
          description: "No heartbeat gauge for broker-manager. The daemon has not ticked since metrics were enabled, or the textfile collector / node_exporter scrape is broken. Clears after the first tick."

      # saxo-refresh is a timer-driven oneshot (~20min keep-alive), so the
      # standard last_success clock applies. 3600s = 1h = 3x the 20min cadence.
      - alert: AlphalensJobStale
        expr: time() - alphalens_job_last_success_timestamp_seconds{job="saxo-refresh"} > 3600
        for: 5m
        labels:
          severity: warning
          route: telegram
          unit: saxo-refresh
        annotations:
          summary: "saxo-refresh stale > 1h (expected 20min cadence)"
          description: "Last successful OAuth keep-alive was {{ $value | humanizeDuration }} ago — the refresh chain will die at the 40min window; re-run `alphalens broker auth`."

      - alert: AlphalensJobMetricMissing
        expr: absent(alphalens_job_last_success_timestamp_seconds{job="saxo-refresh"})
        for: 5m
        labels:
          severity: warning
          route: telegram
          unit: saxo-refresh
        annotations:
          summary: "saxo-refresh success metric missing"
          description: "No success metric for saxo-refresh. The keep-alive timer has not fired since metrics were enabled, or the scrape is broken. Clears after the first refresh."
```
   > The live Prometheus rules file is hand-synced + HUP'd on the VPS, NOT auto-mounted from the repo (memory `reference_prometheus_live_rules_not_repo_mounted`) — the repo change is the SoT; deploying it is a separate operator step in the PR Test plan. If the repo already defines `AlphalensJobStale`/`AlphalensJobMetricMissing` as generic templated rules that match all jobs, do not duplicate for `saxo-refresh`; instead adjust the test to assert the generic rule covers it — but prefer explicit per-job rules if the shipped file uses per-job blocks.

5. Run both, expect PASS.
6. Full group regression before commit:
```bash
.venv/bin/python -m unittest discover -s apps/alphalens-research/tests/brokers \
    -t apps/alphalens-research -v
```
7. Commit:
```bash
git add apps/alphalens-pipeline/alphalens_pipeline/brokers/automanager/control_loop.py \
        apps/alphalens-research/tests/brokers/automanager/test_control_loop.py \
        deploy/monitoring/prometheus/rules/alphalens.yaml \
        apps/alphalens-research/tests/test_deploy_systemd_units.py
git commit -m "feat(brokers): broker-manager heartbeat gauge + Prometheus health rules"
```

---

## After all tasks (project doctrine)

Push the branch, open the PR, run `mcp__zen__codereview` with `deepseek/deepseek-v4-pro` (`thinking_mode="high"`) — this touches the live-infra broker surface + systemd + Prometheus — apply findings as ADDITIONAL commits, wait for CI green on the latest commit, then merge. The PR body must carry a `## Known issues` section covering: (1) `build_default_deps` factory internals (`_default_oauth_provider` / `_make_place_pick` / `_make_position_view_builder` / `_make_standalone_stop_placer`) are validated only by the SIM live probe, not hermetic unit tests; (2) the ~30–60 s post-fill unprotected window (memo Risk 1); (3) portfolio caps are un-tuned operator policy (memo Risk 7); (4) `>40 min` VPS outage kills the chain (attended recovery only, memo Risk 3).

## Deferred follow-ups (not tasks in this plan)

- **SIM live probe (`SAXO_LIVE_TEST=1`)** — the spec's §TDD SIM-live probes (end-to-end arm→place→fill→standalone-stop→terminal, `place_standalone_stop` re-probe, unattended-session probe, `__nextPoll` non-regression, weekly `live-probes` job) is referenced by Task 11's `build_default_deps` but has **no drafted TDD task**. It is the only validation of the `build_default_deps` factory internals and must be authored as a separate opt-in `skipUnless` probe under `apps/alphalens-research/tests/live/test_saxo_live.py` before the daemon is trusted on SIM. Flagged as the top follow-up.
- Phase-B items (far-TP standalone limit-sells, cancel-on-fill OCO synthesis, resize-on-partial PATCH, TP→breakeven ratchet, 42-session time-stop, `StreamingFillSource`, web arm button) stay deferred per memo §Phasing.
