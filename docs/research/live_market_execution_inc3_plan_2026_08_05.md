# Live-Market Execution — INC-3 (Live TP-Tranche Exit Engine) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the "brain" of live take-profit — a deterministic engine that, given a live price and a position's `tp_tranches`, decides which tranche(s) to realize and executes each as **shrink-the-SL-by-the-tranche → market-SELL the tranche**. Fully hermetic (driven by an injected price feed + the acceptance FakeBroker); **INERT** — not wired into the live daemon tick (that + the real Saxo feed are INC-2 and a later wiring increment).

**Architecture:** A minimal `PriceFeed` interface (broker_contract) whose `latest(uic)` returns `None` when there is no fresh price (the stream-health veto). A pure decision function `plan_tranche_exits(...)` (no I/O, trivially tested with fake prices). An executor `execute_tranche_exit(...)` that reproduces the SIM-probed safe sequence (amend SL down FIRST, then market sell ≤ owned) against the `Broker` + `SupportsAmendStop` + `SupportsMarketOrders` capabilities. A per-(uic,tag) fired-tranche journal for idempotency. A thin `run_live_exits(...)` orchestrator that ties them together per managed position (still inert).

**Tech Stack:** Python 3.12, `unittest`, `broker_contract` leaf + `alphalens_pipeline.brokers.automanager`.

## Global Constraints

- SIM-only; every real placement stays behind `ALPHALENS_BROKER_ALLOW_ORDERS=1` (already enforced inside `place_market_order`/`amend_stop_amount`).
- The SIM-proven safe order (2026-08-05, ticker F): on a netting account you MUST **amend the SL down by the tranche BEFORE the market sell** — a market SELL while the SL still commits full owned is rejected `SellOrdersAlreadyExistForOwnedContracts`. Never reorder these two.
- Sell **≤ live owned** (re-snapshot immediately before the sell) — a market sell of ≤ owned cannot flip the netting position short.
- Research tests MUST subclass `unittest.TestCase` (bare functions are silently skipped). English-only.
- Commits: Conventional Commits, `git commit -s`, sign-off `Kamil Pająk <kamilpajak@users.noreply.github.com>` (diacritic required); NO AI mention.
- `alphalens_pipeline` ↛ `alphalens_research`; `broker_contract` stays a dependency-free leaf (no vendor imports, no `alphalens_*` imports).
- Run tests from `apps/alphalens-research` with the workspace venv: `../../.venv/bin/python -m unittest <dotted.path> -v`.
- INERT: no task wires the engine into `control_loop`'s tick. `run_live_exits` exists and is tested but has no live caller yet.

## File Structure

- `apps/alphalens-broker-contract/broker_contract/price_feed.py` (NEW) — `PricePoint` + `PriceFeed` Protocol.
- `apps/alphalens-pipeline/alphalens_pipeline/brokers/automanager/live_exit_engine.py` (NEW) — `TrancheExit`, `plan_tranche_exits`, `execute_tranche_exit`, `run_live_exits`, tranche-fired journal helpers.
- Tests under `apps/alphalens-research/tests/brokers/automanager/` (new files per task).

---

### Task 1: `PriceFeed` interface + `PricePoint`

**Files:**
- Create: `apps/alphalens-broker-contract/broker_contract/price_feed.py`
- Modify: `apps/alphalens-broker-contract/broker_contract/__init__.py` (export if the package re-exports; else skip)
- Test: `apps/alphalens-research/tests/brokers/test_price_feed.py`

**Interfaces:**
- Produces: `PricePoint(uic:int, price:float, asof:datetime)` (frozen); `PriceFeed` Protocol with `latest(self, uic:int) -> PricePoint | None` (None = no fresh price → the veto).

- [ ] **Step 1: failing test** — `test_price_feed.py`:

```python
from __future__ import annotations
import datetime as dt
import unittest
from broker_contract.price_feed import PriceFeed, PricePoint

class TestPriceFeed(unittest.TestCase):
    def test_pricepoint_is_frozen_and_carries_price(self):
        p = PricePoint(uic=486, price=14.36, asof=dt.datetime(2026, 8, 5, tzinfo=dt.UTC))
        self.assertEqual(p.price, 14.36)
        with self.assertRaises(Exception):
            p.price = 1.0  # frozen

    def test_protocol_runtime_checkable(self):
        class _F:
            def latest(self, uic): return None
        self.assertIsInstance(_F(), PriceFeed)
        class _N: pass
        self.assertNotIsInstance(_N(), PriceFeed)

if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: run, expect FAIL** (`ModuleNotFoundError: broker_contract.price_feed`)

Run: `cd apps/alphalens-research && ../../.venv/bin/python -m unittest tests.brokers.test_price_feed -v`

- [ ] **Step 3: implement** — `price_feed.py`:

```python
"""Broker-agnostic live price feed — the trigger source for live-market E/TP.

Dependency-free leaf. ``latest(uic)`` returns ``None`` when there is no FRESH
price (disconnect / staleness / halt) — the engine treats ``None`` as "do not
fire" (the stream-health veto). The real Saxo streaming feed (INC-2) implements
this; tests use an in-memory fake.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class PricePoint:
    uic: int
    price: float
    asof: dt.datetime  # UTC


@runtime_checkable
class PriceFeed(Protocol):
    def latest(self, uic: int) -> PricePoint | None: ...
```

- [ ] **Step 4: run, expect PASS**
- [ ] **Step 5: commit** — `feat(broker-contract): PriceFeed interface for live-market triggers`

---

### Task 2: `plan_tranche_exits` (pure decision)

**Files:**
- Create: `apps/alphalens-pipeline/alphalens_pipeline/brokers/automanager/live_exit_engine.py`
- Test: `apps/alphalens-research/tests/brokers/automanager/test_live_exit_decision.py`

**Interfaces:**
- Consumes: `TpTranchePlan` (from `broker_contract.sizing` — fields `target_price:float`, `tranche_pct:float`, `r_multiple:float`).
- Produces: `TrancheExit(tag:str, qty:int, target_price:float)` (frozen); `plan_tranche_exits(*, price, tp_tranches, reference_qty, owned, already_fired) -> list[TrancheExit]`. `tranche_tag(index) -> str` = `f"tp{index+1}"`.

- [ ] **Step 1: failing test** — `test_live_exit_decision.py`:

```python
from __future__ import annotations
import unittest
from broker_contract.sizing import TpTranchePlan
from alphalens_pipeline.brokers.automanager.live_exit_engine import (
    plan_tranche_exits, TrancheExit, tranche_tag,
)

def _tr(target, pct): return TpTranchePlan(target_price=target, tranche_pct=pct, r_multiple=1.0)
_LADDER = (_tr(16.0, 0.5), _tr(18.0, 0.3), _tr(20.0, 0.2))  # TP1/TP2/TP3 of 100 shares

class TestPlanTrancheExits(unittest.TestCase):
    def test_no_touch_no_exits(self):
        out = plan_tranche_exits(price=15.0, tp_tranches=_LADDER, reference_qty=100, owned=100, already_fired=frozenset())
        self.assertEqual(out, [])

    def test_first_target_touched_fires_tp1_only(self):
        out = plan_tranche_exits(price=16.5, tp_tranches=_LADDER, reference_qty=100, owned=100, already_fired=frozenset())
        self.assertEqual(out, [TrancheExit(tag="tp1", qty=50, target_price=16.0)])

    def test_gap_through_two_targets_fires_both_within_owned(self):
        out = plan_tranche_exits(price=18.5, tp_tranches=_LADDER, reference_qty=100, owned=100, already_fired=frozenset())
        self.assertEqual([e.tag for e in out], ["tp1", "tp2"])
        self.assertEqual([e.qty for e in out], [50, 30])

    def test_already_fired_is_skipped(self):
        out = plan_tranche_exits(price=18.5, tp_tranches=_LADDER, reference_qty=100, owned=50, already_fired=frozenset({"tp1"}))
        self.assertEqual(out, [TrancheExit(tag="tp2", qty=30, target_price=18.0)])

    def test_qty_clamped_to_available_owned(self):
        # owned only 20 left but tp1(50)+tp2(30) both triggered -> fire tp1=20, tp2=0(skip)
        out = plan_tranche_exits(price=18.5, tp_tranches=_LADDER, reference_qty=100, owned=20, already_fired=frozenset())
        self.assertEqual(out, [TrancheExit(tag="tp1", qty=20, target_price=16.0)])

    def test_tranche_tag(self):
        self.assertEqual([tranche_tag(i) for i in range(3)], ["tp1", "tp2", "tp3"])

if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: run, expect FAIL**
- [ ] **Step 3: implement** — start `live_exit_engine.py`:

```python
"""Live take-profit engine (the 'brain') — decide + execute TP-tranche exits.

INERT: no live caller wires this into the daemon tick yet (INC-2 supplies the
real price feed; a later increment wires it). Everything here is driven by an
injected PriceFeed + the Broker capabilities, and is exercised hermetically.

Safe sequence (SIM-probed 2026-08-05, netting account): shrink the standalone SL
by the tranche FIRST, THEN market-sell the tranche (a sell while the SL commits
full owned is rejected SellOrdersAlreadyExistForOwnedContracts); sell <= live
owned so the position can never flip short.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from broker_contract.sizing import TpTranchePlan

logger = logging.getLogger(__name__)

_PRICE_EPS = 1e-9  # a long tranche fires when price >= target (within eps)


def tranche_tag(index: int) -> str:
    return f"tp{index + 1}"


@dataclass(frozen=True)
class TrancheExit:
    tag: str
    qty: int
    target_price: float


def plan_tranche_exits(
    *,
    price: float,
    tp_tranches: tuple[TpTranchePlan, ...],
    reference_qty: float,
    owned: float,
    already_fired: frozenset[str],
) -> list[TrancheExit]:
    """Which not-yet-fired tranches a LONG at ``price`` should realize now.

    ``reference_qty`` is the tranche-sizing base (the intended/peak filled
    position); tranche qty = round(reference_qty * tranche_pct), cumulatively
    clamped so the batch never exceeds live ``owned``. Order preserved.
    """
    available = int(round(owned))
    out: list[TrancheExit] = []
    for i, t in enumerate(tp_tranches):
        tag = tranche_tag(i)
        if tag in already_fired:
            continue
        if price + _PRICE_EPS < t.target_price:
            continue  # target not touched
        qty = min(int(round(reference_qty * t.tranche_pct)), available)
        if qty <= 0:
            continue
        out.append(TrancheExit(tag=tag, qty=qty, target_price=t.target_price))
        available -= qty
    return out
```

- [ ] **Step 4: run, expect PASS**
- [ ] **Step 5: commit** — `feat(brokers): plan_tranche_exits pure TP-ladder decision`

---

### Task 3: `execute_tranche_exit` (the safe amend-down → market-sell)

**Files:**
- Modify: `apps/alphalens-pipeline/alphalens_pipeline/brokers/automanager/live_exit_engine.py`
- Test: `apps/alphalens-research/tests/brokers/automanager/test_live_exit_executor.py`

**Interfaces:**
- Consumes: `Broker` + `SupportsAmendStop` + `SupportsMarketOrders`; `OrderState` (the resting SL leg — fields `order_id`, `amount`, `order_type`, `side`, `uic`); the FakeBroker (which implements all three + `get_positions_by_uic`, `list_working_sell_orders`); `_sole_standalone_stop` from `position_manager`.
- Produces: `execute_tranche_exit(broker, *, uic, exit, sl_leg, stop_price, request_ref) -> bool` (True if the sell landed). Amends SL to `sl_leg.amount - exit.qty` then market-sells `min(exit.qty, live_owned)`.

- [ ] **Step 1: failing test** — `test_live_exit_executor.py` (use the acceptance FakeBroker; set a position + a resting standalone StopIfTraded, then execute):

```python
from __future__ import annotations
import unittest
from tests.brokers.automanager.acceptance.fake_broker import FakeBroker
from alphalens_pipeline.brokers.automanager.live_exit_engine import TrancheExit, execute_tranche_exit

class TestExecuteTrancheExit(unittest.TestCase):
    def _setup(self, owned=100, sl_qty=100):
        b = FakeBroker()
        uic = b.uic_of("KO")
        b.set_position("KO", owned, avg_price=15.0)
        sl_id = b.add_resting_sell("KO", sl_qty, 13.0, order_type="StopIfTraded")
        sl = next(o for o in b.list_working_sell_orders() if o.order_id == sl_id)
        return b, uic, sl

    def test_amends_sl_down_then_sells_tranche(self):
        b, uic, sl = self._setup(owned=100, sl_qty=100)
        ok = execute_tranche_exit(b, uic=uic, exit=TrancheExit("tp1", 40, 16.0), sl_leg=sl, stop_price=13.0, request_ref="KO-g0")
        self.assertTrue(ok)
        self.assertEqual(b.get_positions_by_uic(uic).quantity, 60.0)  # 100 - 40 sold
        sl_now = next(o for o in b.list_working_sell_orders() if o.order_type == "StopIfTraded")
        self.assertEqual(sl_now.amount, 60.0)  # SL shrunk 100 -> 60

    def test_sell_clamped_to_live_owned(self):
        b, uic, sl = self._setup(owned=30, sl_qty=30)
        ok = execute_tranche_exit(b, uic=uic, exit=TrancheExit("tp1", 50, 16.0), sl_leg=sl, stop_price=13.0, request_ref="KO-g0")
        self.assertTrue(ok)
        self.assertEqual(b.get_positions_by_uic(uic).quantity, 0.0)  # sold min(50,30)=30, never short

    def test_noop_when_live_owned_zero(self):
        b, uic, sl = self._setup(owned=100, sl_qty=100)
        b.set_position("KO", 0, avg_price=15.0)  # closed out from under us
        ok = execute_tranche_exit(b, uic=uic, exit=TrancheExit("tp1", 40, 16.0), sl_leg=sl, stop_price=13.0, request_ref="KO-g0")
        self.assertFalse(ok)

if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: run, expect FAIL**
- [ ] **Step 3: implement** — append to `live_exit_engine.py`:

```python
from broker_contract.contract import Broker, OrderState  # add to imports at top


def execute_tranche_exit(
    broker: Broker,
    *,
    uic: int,
    exit: TrancheExit,
    sl_leg: OrderState,
    stop_price: float,
    request_ref: str,
) -> bool:
    """Realize ONE tranche: shrink the standalone SL by the tranche, THEN market
    sell it. Re-snapshots live owned first (never sell more than owned → cannot
    flip short). Returns True iff the sell was sent. Callers MUST hold a
    per-uic lock so this never races the never-naked reconcile.
    """
    live = broker.get_positions_by_uic(uic)
    owned = max(live.quantity, 0.0)
    qty = min(int(exit.qty), int(round(owned)))
    if qty <= 0:
        logger.info("tranche %s uic %s: position gone (owned=%.2f) — no sell", exit.tag, uic, owned)
        return False
    new_sl_qty = max(float(sl_leg.amount or 0.0) - qty, 0.0)
    # 1) shrink the SL FIRST (a sell while the SL commits full owned is rejected).
    broker.amend_stop_amount(
        uic=uic,
        order_id=sl_leg.order_id,
        side=sl_leg.side or "SELL",
        order_type=sl_leg.order_type or "StopIfTraded",
        new_qty=new_sl_qty,
        stop_price=stop_price,
        request_id=f"{request_ref}-{exit.tag}-amend",
    )
    # 2) market-sell the freed tranche.
    broker.place_market_order(uic, "SELL", qty, request_id=f"{request_ref}-{exit.tag}-sell")
    logger.info("tranche %s uic %s: SL %.0f->%.0f, market-sold %d", exit.tag, uic, sl_leg.amount or 0.0, new_sl_qty, qty)
    return True
```

- [ ] **Step 4: run, expect PASS**
- [ ] **Step 5: commit** — `feat(brokers): execute_tranche_exit (amend-down SL then market-sell tranche)`

---

### Task 4: fired-tranche journal (idempotency)

**Files:**
- Modify: `live_exit_engine.py` (add journal helpers mirroring the standalone-stop journal in `control_loop`)
- Test: `apps/alphalens-research/tests/brokers/automanager/test_live_exit_journal.py`

**Interfaces:**
- Produces: `mark_tranche_fired(uic, tag)` (append-only), `fold_fired_tranches(lines) -> dict[int, frozenset[str]]`. Reuse the existing append-only journal writer used by `_append_standalone_stop_journal` if importable; otherwise define a local writer keyed by the same file. (Implementer: `grep -n "_append_standalone_stop_journal\|def _read_standalone" control_loop.py` and reuse the reader/writer seam.)

- [ ] **Step 1: failing test** — `test_live_exit_journal.py`:

```python
from __future__ import annotations
import unittest
from alphalens_pipeline.brokers.automanager.live_exit_engine import fold_fired_tranches

class TestFoldFiredTranches(unittest.TestCase):
    def test_folds_lines_into_per_uic_tag_sets(self):
        lines = [
            {"kind": "tranche_fired", "uic": 486, "tag": "tp1"},
            {"kind": "tranche_fired", "uic": 486, "tag": "tp2"},
            {"kind": "tranche_fired", "uic": 999, "tag": "tp1"},
            {"kind": "oco_placed", "uic": 486},  # ignored
            {"kind": "tranche_fired", "uic": 486},  # malformed (no tag) ignored
        ]
        out = fold_fired_tranches(lines)
        self.assertEqual(out[486], frozenset({"tp1", "tp2"}))
        self.assertEqual(out[999], frozenset({"tp1"}))

if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: run, expect FAIL**
- [ ] **Step 3: implement** — append to `live_exit_engine.py`:

```python
from collections.abc import Iterable, Mapping
from typing import Any


def fold_fired_tranches(lines: Iterable[Mapping[str, Any]]) -> dict[int, frozenset[str]]:
    """Fold append-only ``tranche_fired`` journal lines into per-uic tag sets.
    Non-``tranche_fired`` and malformed (missing uic/tag) lines are ignored."""
    acc: dict[int, set[str]] = {}
    for line in lines:
        if line.get("kind") != "tranche_fired":
            continue
        uic, tag = line.get("uic"), line.get("tag")
        if uic is None or not tag:
            continue
        acc.setdefault(int(uic), set()).add(str(tag))
    return {u: frozenset(t) for u, t in acc.items()}


def mark_tranche_fired(uic: int, tag: str) -> None:
    """Append one ``tranche_fired`` marker (idempotency: a fired tranche never
    re-fires). Writes via the shared append-only standalone-stop journal seam."""
    from alphalens_pipeline.brokers.automanager.control_loop import _append_standalone_stop_journal
    _append_standalone_stop_journal({"kind": "tranche_fired", "uic": int(uic), "tag": str(tag)})
```

(Implementer: verify `_append_standalone_stop_journal` is importable from `control_loop`; if it lives elsewhere, import from there. This lazy import inside the function keeps `live_exit_engine` free of a heavy top-level `control_loop` import.)

- [ ] **Step 4: run, expect PASS**
- [ ] **Step 5: commit** — `feat(brokers): tranche-fired journal for TP-ladder idempotency`

---

### Task 5: `run_live_exits` orchestrator (inert, end-to-end hermetic)

**Files:**
- Modify: `live_exit_engine.py`
- Test: `apps/alphalens-research/tests/brokers/automanager/test_run_live_exits.py`

**Interfaces:**
- Consumes: everything above + `_sole_standalone_stop` (from `position_manager`), a `PriceFeed`, and a per-uic managed spec `ManagedExit(uic, tp_tranches, reference_qty, stop_price, already_fired)`.
- Produces: `run_live_exits(broker, feed, managed) -> int` (count of tranches fired). For each managed uic: `feed.latest(uic)` — None → skip (veto); else plan → for each exit, execute under the per-uic guarantee, `mark_tranche_fired`. Returns fired count. NOT wired to the daemon tick.

- [ ] **Step 1: failing test** — `test_run_live_exits.py` (FakeBroker + a fake feed; INC-3 does NOT persist markers across the call in-memory, so pass `already_fired` explicitly and assert the fire):

```python
from __future__ import annotations
import datetime as dt
import unittest
from broker_contract.price_feed import PricePoint
from broker_contract.sizing import TpTranchePlan
from tests.brokers.automanager.acceptance.fake_broker import FakeBroker
from alphalens_pipeline.brokers.automanager.live_exit_engine import run_live_exits, ManagedExit

class _FakeFeed:
    def __init__(self, prices): self._p = prices  # {uic: price|None}
    def latest(self, uic):
        px = self._p.get(uic)
        return None if px is None else PricePoint(uic=uic, price=px, asof=dt.datetime(2026,8,5,tzinfo=dt.UTC))

def _tr(target, pct): return TpTranchePlan(target_price=target, tranche_pct=pct, r_multiple=1.0)

class TestRunLiveExits(unittest.TestCase):
    def _mk(self, price):
        b = FakeBroker(); uic = b.uic_of("KO")
        b.set_position("KO", 100, avg_price=15.0)
        b.add_resting_sell("KO", 100, 13.0, order_type="StopIfTraded")
        feed = _FakeFeed({uic: price})
        managed = [ManagedExit(uic=uic, tp_tranches=(_tr(16.0,0.5),_tr(18.0,0.3)), reference_qty=100, stop_price=13.0, already_fired=frozenset())]
        return b, uic, feed, managed

    def test_touch_fires_tranche_and_shrinks_sl(self):
        b, uic, feed, managed = self._mk(price=16.5)
        n = run_live_exits(b, feed, managed)
        self.assertEqual(n, 1)
        self.assertEqual(b.get_positions_by_uic(uic).quantity, 50.0)

    def test_stale_price_vetoes_all_fires(self):
        b, uic, feed, managed = self._mk(price=None)  # feed.latest -> None
        n = run_live_exits(b, feed, managed)
        self.assertEqual(n, 0)
        self.assertEqual(b.get_positions_by_uic(uic).quantity, 100.0)

if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: run, expect FAIL**
- [ ] **Step 3: implement** — append to `live_exit_engine.py`:

```python
from broker_contract.price_feed import PriceFeed  # add to top imports


@dataclass(frozen=True)
class ManagedExit:
    uic: int
    tp_tranches: tuple[TpTranchePlan, ...]
    reference_qty: float
    stop_price: float
    already_fired: frozenset[str]


def run_live_exits(broker: Broker, feed: PriceFeed, managed: list[ManagedExit]) -> int:
    """One live-exit pass over managed positions. Stale/absent price -> veto (skip).
    INERT: no daemon caller yet. Returns the number of tranches fired."""
    fired = 0
    for m in managed:
        point = feed.latest(m.uic)
        if point is None:
            continue  # stream-health veto
        live = broker.get_positions_by_uic(m.uic)
        legs = tuple(broker.list_working_sell_orders())
        legs = tuple(leg for leg in legs if leg.uic == m.uic)
        sl = _sole_standalone_stop(legs)
        if sl is None:
            logger.info("uic %s: no sole standalone SL — skipping live exits this pass", m.uic)
            continue
        exits = plan_tranche_exits(
            price=point.price, tp_tranches=m.tp_tranches, reference_qty=m.reference_qty,
            owned=live.quantity, already_fired=m.already_fired,
        )
        for ex in exits:
            if execute_tranche_exit(broker, uic=m.uic, exit=ex, sl_leg=sl, stop_price=m.stop_price, request_ref=f"u{m.uic}"):
                mark_tranche_fired(m.uic, ex.tag)
                fired += 1
    return fired
```

Add `from alphalens_pipeline.brokers.automanager.position_manager import _sole_standalone_stop` (lazy inside the function if a top-level import creates a cycle — verify with `test_module_dependencies`).

- [ ] **Step 4: run, expect PASS**; also run the whole automanager suite to confirm no regression.
- [ ] **Step 5: commit** — `feat(brokers): run_live_exits orchestrator (inert live TP-ladder pass)`

---

## Final gate

- [ ] `cd apps/alphalens-research && ../../.venv/bin/python -m unittest discover -s tests/brokers -t . -q` — all green.
- [ ] `../../.venv/bin/python -m unittest tests.test_module_dependencies -v` — DAG intact (watch for a `live_exit_engine` ↔ `control_loop`/`position_manager` cycle; use lazy imports as noted).
- [ ] `../../.venv/bin/ruff check ../alphalens-broker-contract ../alphalens-pipeline/alphalens_pipeline/brokers` — clean.
- [ ] Open PR; zen `deepseek/deepseek-v4-pro` (thinking=high) pre-merge; apply findings as commits; merge on green CI.

## Self-review

- **Spec coverage:** memo §3.4 (live-trigger engine) + §5 INC-3 (live TP ladder) → Tasks 2/3/5; §4 safety (per-instrument lock note, stream veto, owned re-snapshot, idempotent request_id) → Tasks 3/4/5; the price-source seam (§3.3 prep) → Task 1. Entries (§ INC-4) explicitly OUT of scope.
- **Placeholder scan:** none — decision + executor + orchestrator carry full code; two implementer verifications flagged (journal writer import, `_sole_standalone_stop` import cycle) are concrete grep/verify steps, not placeholders.
- **Type consistency:** `TrancheExit(tag, qty, target_price)`, `plan_tranche_exits(...)`, `execute_tranche_exit(broker, *, uic, exit, sl_leg, stop_price, request_ref)`, `ManagedExit(...)`, `run_live_exits(broker, feed, managed)` consistent across tasks.
- **Inert:** no task edits `control_loop`'s tick to call `run_live_exits`. The per-uic lock is a documented CALLER obligation (the wiring increment adds it); INC-3 tests exercise single-threaded.
- **Known deferrals (PR "behaviour notes"):** reference_qty sourcing (peak/intended filled qty) is passed in here; the wiring increment derives it. The ~2s amend→sell uncovered window (SIM-probed) is inherent; mitigated live by the stream-gate (fire only when price is at target, far above the SL).
