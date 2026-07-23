# Saxo SIM auto-manager — OCO/standalone exit-management redesign

**Status: DRAFT**
**Goal:** replace the journal-`intent`-driven standalone-stop protection with **broker-state-truth protection keyed to netted positions**, layered as a staged ladder (guaranteed standalone stop first, OCO-with-TP upgrade second) so a filled position is never left permanently naked (Bug A) and never double-commits the sell side (Bug B), while the whole thing sizes to realized/netted qty and self-heals across restarts, manual cancels, and partial fills.

Author: broker automanager track. Scope: `apps/alphalens-pipeline/alphalens_pipeline/brokers/`. SIM-only (SaxoClient refuses non-SIM base URLs). Supersedes the standalone-stop design in `project_saxo_first_fill_experiment_2026_07_20`.

---

## 1. Problem

Two defects were reproduced live on Saxo SIM on 2026-07-21 (worked example: BIO, 3 entry tiers 297.5 / 287.8 / 281.8, common disaster stop 216.48, per-tier TPs; only tier-1 filled +46 @ ~296).

### Bug A — retry suppression (permanent naked position)

`_make_standalone_stop_placer` writes an `intent` journal line **before** it POSTs the standalone stop, then POSTs. `_fold_standalone_stop_journal` (control_loop.py:353-386) treats **both** `intent` and `placed` lines as conferring protection, keyed by entry `client_request_id`. `position_manager._advance_filled` (position_manager.py:104) returns `NoOp` the instant `request_id in protected_request_ids`.

**Root cause:** if the stop POST raises `BrokerError`, it propagates to `run_once`'s try/except (alerts Telegram, skips the tick) and the `placed` line is never written — but the orphan `intent` line makes `_fold` mark the entry protected **forever**. `advance` returns `NoOp` every subsequent tick and never retries. A failed POST is silently converted into a permanently naked downside. The "intent confers protection" rule only ever reasoned about a crash *after* a successful POST (to avoid a double stop); it never reasoned about a *failed* POST.

### Bug B — `SellOrdersAlreadyExistForOwnedContracts` double-sell (structural, not transient)

The standalone disaster-stop POST fails precheck with Saxo `ErrorCode SellOrdersAlreadyExistForOwnedContracts`.

**Root cause:** the in-band TP bracket child (a SELL limit, promoted by `placement_planner`) already commits the full owned qty to the sell side. The disaster stop (also a SELL, same full qty) would commit **2×** the owned shares. Under FifoRealTime netting Saxo checks sell orders against the **netted owned qty**, so it rejects the second sell. Whenever a lone TP sell exists for a long, the standalone disaster stop is **structurally blocked** — there is no transient retry that fixes it.

---

## 2. Confirmed Saxo facts vs open questions

### Confirmed (verified read-only or by live placement on SIM, 2026-07-20/21)

1. `/port/v1/orders/me` returns `ExternalReference` on every order (= the `x-request-id` / `client_request_id` used at placement). A promoted TP child inherits the **entry's** `ExternalReference`.
2. `/port/v1/positions` `PositionBase` returns `ExternalReference` (= source/oldest-FIFO tier crid) + `SourceOrderId` (entry `OrderId`) + `RelatedOpenOrders` + `Uic` + netted signed `Amount`.
3. A **standalone** `StopIfTraded` SELL sized to owned, ~27% below entry, **places fine** (OrderId 5039296412) — no `TooFarFromEntryOrder` (it has no entry-child parent) and, being the *only* sell, no `SellOrdersAlreadyExistForOwnedContracts`. **This is the guaranteed protective shape the whole Stage-1 design rests on.**
4. The child-distance limit (~15%, HTTP 400 `TooFarFromEntryOrder`) applies to IfDone **entry-child** orders (child price validated against the parent entry price). It does **not** have a parent to anchor to on a standalone order.
5. Netting profile = **FifoRealTime**: multiple fills on the same uic net into ONE position. A multi-tier entry ladder produces ONE netted position per uic. Sell orders are checked against the **netted** owned qty (that is what `SellOrdersAlreadyExistForOwnedContracts` enforces).
6. Saxo `x-request-id` dedup window is ~15s: a re-POST with the same `ExternalReference` inside 15s is deduped to the prior order.
7. `place_bracket_order` / `place_standalone_stop` exist; there is **no OCO-exit primitive** and **no PATCH primitive** (`_send_write` wires POST/DELETE only).
8. Cancelling one member of a related-order group cascades to its sibling (Saxo related-order cascade, per `cancel_order` docstring).

### OPEN QUESTIONS — must be resolved by a live SIM probe before the gated code path is enabled

These are ranked. **None of them gate Stage 1** (Stage 1 uses only confirmed fact #3). They gate the OCO/reduce-only end-state.

- **Q1 (PIVOTAL, gates OCO rung) — does a 2-leg SELL OCO on a filled long commit owned ONCE?** Place OCO {near TP +~3.6%, far `StopIfTraded` −~27%}, both `Amount == owned`, `OrderRelation:"Oco"`. Confirm precheck + placement PASS (no `SellOrdersAlreadyExistForOwnedContracts`). **If this fails, the TP is never broker-side protection — ship stop-only permanently.**
- **Q2 (gates OCO rung) — does the far OCO STOP leg escape `TooFarFromEntryOrder`?** Standalone wide stops already pass (fact #3); verify the wide stop survives being OCO-linked to a near TP. The whole point of OCO is to carry a near TP + a far stop together.
- **Q3 (PIVOTAL for oversell safety) — does a `PositionId`-linked exit order behave as reduce-only?** A raw `uic + Amount` standalone stop is **NOT** reduce-only for a shortable equity: if it triggers when live owned < Amount it can **open a short**. Confirm (a) whether the SIM cash-equity account can go short at all, and (b) whether attaching the exit to `PositionId` makes Saxo clip the fill to owned. This decides whether the robust end-state is `PositionId`-linked reduce-only exits (target) or the "keep Amount ≤ owned by construction" floor (Stage 1).
- **Q4 — does Saxo auto-decrement the resting OCO sibling when its partner partially fills?** Decides whether the over-hedge/downsize arm ever fires.
- **Q5 — do two OCO groups (or two standalone stops) on ONE netted uic, summing to owned, coexist without cross-counting the sell side?** Gates additive-on-growth (place a delta stop for a newly-filled tier without cancelling the existing one). If Saxo cross-counts, growth must use cancel-then-replace with place-residual-first ordering.
- **Q6 — DELETE clear latency.** After cancelling a sell leg, how many ticks until a re-place precheck stops seeing the cancelled qty as committed. Decides two-tick vs same-tick resize.
- **Q7 — per-leg vs top-level `ExternalReference` on an OCO `Orders` array.** If only a top-level ref is honored, protection correlation falls back to `uic + BuySell + OrderType + Amount` matching (still broker-state truth, just coarser).
- **Q8 — is PATCH/modify wired-able and does it preserve OrderId on Amount change?** Gates the atomic-resize end-state (no naked window on grow).

**Probe rail:** all probes SIM-only, one instrument, behind `ALPHALENS_BROKER_ALLOW_ORDERS=1` + KILL-file, read-back-and-cancel. Land them as opt-in `SAXO_LIVE_TEST=1` probes under `apps/alphalens-research/tests/live/` (never gating CI), same pattern as the existing L4 vendor probes.

---

## 3. Target architecture — broker-state-truth protection, keyed to netted positions

Two truths, cleanly separated by what each can legitimately know. This split is what makes both bugs unrepresentable:

| Fact | Source of truth | Why |
|---|---|---|
| **owned qty per uic** (netted, realized) | `/port/v1/positions` `PositionBase.Amount` | broker is the only authority on realized/netted fills |
| **live protective exits** (uic, type, qty, ref, filled qty) | `/port/v1/orders/me` | broker is the only authority on what protection actually exists |
| **plan prices** (disaster stop, in-band TP) | append-only journal `planned` line | broker cannot know intended stop/TP prices |
| **fill attribution** (which entry crid filled how much) | reconcile audit log | refines the exit `ExternalReference` — **never gates a place** |

### The two structural moves

1. **Protection status is a per-tick pure function of live broker state** (`get_long_positions()` + `list_working_sell_orders()`), correlated **by uic**. No journal line asserts "protected". → kills **Bug A**.
2. **Total live SELL commitment on any uic ≤ owned_qty at every instant** — exactly one standalone stop, or one OCO group whose mutually-exclusive legs count once, and every transition cancels/places so the sum never exceeds owned. The entry bracket is placed **entry-only** (no TP child), so the lone-TP shape is never created at source. → kills **Bug B**.

### The staged protection ladder (per uic)

```
rung 0  NAKED         no protective SELL on the uic
          │  place standalone StopIfTraded (SIM-proven, fact #3)   ── ALWAYS reachable, zero unproven facts
          ▼
rung 1  STOP_ONLY     one SELL StopIfTraded, Amount == owned_qty   ◀── GUARANTEED INVARIANT (Stage 1 end-state)
          │  (upgrade, gated: Q1+Q2 green AND per-instrument capability flag ON)
          ▼
rung 2  OCO_PROTECTED one OCO {StopIfTraded, Limit TP}, combined commit == owned_qty   (Stage 2 end-state)
```

Downside is covered at rung ≥1 at all times. The TP is upside only; its absence is never "unprotected". The rung 1→2 upgrade is a controlled transition (details in §6); **any** OCO failure degrades back to the proven rung-1 stop and marks the instrument OCO-unsupported (persisted).

### The loop drives off live positions, not journal verdicts

**Critical correction over the naive design:** the protection pass iterates `broker.get_long_positions()`, NOT the journal-derived `verdicts` list. A position the journal doesn't yet know about (entry POSTed 200 + filled, daemon crashed before `append_submission_record`) still gets protected/alerted. This honors "protection = broker truth" end to end. Verdicts and the journal supply only the *plan prices*, joined by uic.

### Single consistent snapshot + execute-time re-check

`build_protection_view` takes ONE portfolio snapshot per tick (positions + open orders read back-to-back, ideally one `/port/v1/positions?FieldGroups=…` + one `/port/v1/orders/me`). Before any `place`, the executor re-reads owned qty and skips/rescopes if the position no longer covers the intended sell. This closes the round-trip-close race that would otherwise plant a stop on a just-flattened uic (which could later fire into a short).

---

## 4. New broker primitives + exact signatures

### 4.1 Enrich the open-orders read (prerequisite, additive)

`OrderState` today drops the fields the per-uic accounting needs. Extend it (no-backward-compat doctrine permits additive defaulted fields; the frozen base `Broker` Protocol is untouched):

```python
# contract.py
@dataclass(frozen=True)
class OrderState:
    order_id: str
    status: OrderStatus
    instrument: InstrumentRef | None
    filled_quantity: float
    raw_status: str
    uic: int | None = None                            # NEW — row["Uic"]
    side: Literal["BUY", "SELL"] | None = None        # NEW — from row["BuySell"]
    order_type: str | None = None                     # NEW — Saxo OpenOrderType ("StopIfTraded"|"Limit"|...)
    amount: float | None = None                       # NEW — row["Amount"] (RESTING qty, not filled)
    external_reference: str | None = None             # NEW — row["ExternalReference"]
```

`filled_quantity` already exists and is load-bearing for identifying a partially-executed OCO leg (fixes critique-B S5). Map in `_to_order_state` from fields Saxo already returns — mapping-only, no new HTTP surface.

Two thin filter helpers (mapping-only over already-fetched payloads):

```python
def get_long_positions(self) -> list[Position]:
    return [p for p in self.get_positions() if p.quantity > _QTY_EPS]

def list_working_sell_orders(self) -> list[OrderState]:
    return [o for o in self.list_open_orders()
            if o.side == "SELL" and o.status in (OrderStatus.WORKING, OrderStatus.PARTIALLY_FILLED)]
```

### 4.2 Structured error code on rejection (prerequisite)

Safety branches must **not** parse Saxo error strings (brittle — fixes critique-C S9). Attach the code at the adapter boundary:

```python
# contract.py
@dataclass(frozen=True)
class OrderRejectedError(BrokerError):
    error_code: str | None = None          # NEW — verbatim Saxo ErrorCode, set in _precheck_or_raise
    ...

def _is_sell_orders_already_exist(e: BrokerError) -> bool:
    return isinstance(e, OrderRejectedError) and e.error_code == "SellOrdersAlreadyExistForOwnedContracts"

def _is_too_far_from_entry(e: BrokerError) -> bool:
    return isinstance(e, OrderRejectedError) and e.error_code == "TooFarFromEntryOrder"
```

Each classifier gets a positive-control test so it cannot rot to always-False.

### 4.3 Keep `place_standalone_stop` — it IS rung 1

`SupportsStandaloneStop.place_standalone_stop(uic, side, qty, stop_price, request_id=None)` stays **exactly as-is**. It is the SIM-proven, always-reachable protective shape and the graceful-degradation floor. We do NOT delete it.

### 4.4 New `place_oco_exit` — rung 2 upgrade only (Stage 2)

```python
@runtime_checkable
class SupportsOcoExit(Protocol):
    """Standalone OCO EXIT pair on an existing position. No entry parent.

    Both legs SELL, same uic, Amount == qty, so the mutually-exclusive OCO group
    commits `qty` to the sell side ONCE (Saxo counts it as one commitment — Q1).
    Deterministic per-leg ExternalReference for x-request-id dedup + broker-state
    correlation. Off the frozen base Broker Protocol (capability-protocol pattern).
    """
    def place_oco_exit(
        self,
        uic: int,
        side: str,                       # exit side; "SELL" for a long
        qty: float,                      # REALIZED netted owned qty
        stop_price: float,               # disaster StopIfTraded leg (may be 15-30% away)
        take_profit: float,              # in-band Limit TP leg
        request_id: str,                 # generation-stamped base; legs derive -stop/-tp
        position_id: str | None = None,  # Q3: link for reduce-only if SIM confirms it clips to owned
    ) -> PlacedOrder: ...                # PlacedOrder(entry_order_id="", exit_order_ids=(stop_id, tp_id))
```

`_build_oco_exit_body`: two sibling SELL legs, `OrderRelation:"Oco"`, GTC, `ManualOrder:false`, no parent, `StopIfTraded @ stop_price` + `Limit @ take_profit`, each `Amount == qty`. Routed through the existing `_precheck_or_raise` + `client.place_order` (body is opaque — **no new client method, no new HTTP verb**). **Must NOT** pass through `_validate_price_relations`' 15% child-distance guard (that guard is an entry-child fail-fast; the OCO exit is precisely the wide-stop escape). Keep only the degenerate-ordering check `stop < market < tp`.

### 4.5 Generation-stamped deterministic request-ids (one constant, two consumers)

```python
def _exit_stop_ref(entry_crid: str, gen: int) -> str: return f"{entry_crid}-stop-{gen}"
def _exit_tp_ref(entry_crid: str, gen: int)   -> str: return f"{entry_crid}-tp-{gen}"
```

`gen` is a monotonic counter incremented **only when the size changes** (a resize), persisted in the journal. A crash-retry of the *same* size reuses the same ref → dedup-safe. A resize is a *distinct* request → never falsely deduped to the stale (cancelled, smaller) order. This resolves the ref tension flagged in every critique (A-S3, B-F5, C-S8): stable for retry, distinct for resize.

---

## 5. Idempotent cancel + alert throttling (cross-cutting prerequisites)

- **Idempotent cancel** (fixes A-S5): `cancel_order` treats `404 / OrderNotFound / already-cancelled` as success, so cascade-cancelling one OCO leg then attempting to cancel the already-gone sibling does not raise / alert / thrash.
- **Alert throttling** (fixes A-S2, B-S3, C-S10/S11): all protection alerts route through a throttle keyed by `(uic, reason)` with a re-alert interval (e.g. 30 min) and a per-uic consecutive-failure counter. After N consecutive identical placement failures on one uic, emit ONE escalated CRITICAL ("NAKED — manual action required") and back off (exponential) rather than paging every 45s forever. A stuck position must never drown out the *next genuine* naked alert (Telegram 429).

---

## 6. Per-tick reconcile algorithm

Framed as **desired-vs-actual diff** per netted uic (Approach B's declarative structure), so a single tick can emit cancels AND places together, cancels ordered first (fixes critique-B S4 single-arm clean-and-grow).

```python
STOP_TYPES = {"StopIfTraded", "Stop", "TrailingStopIfTraded"}   # a SELL leg that PROTECTS
TP_TYPES   = {"Limit"}                                          # a SELL leg that is UPSIDE only
_QTY_EPS   = 0.5    # half a share — tolerance, NEVER bare >= on floats (A-S6, B-S2, C-M12)

# ---------- assembled ONCE per tick by control_loop.build_protection_view (I/O) ----------
@dataclass(frozen=True)
class ProtectionView:
    long_positions:  Mapping[int, Position]              # uic -> netted long, quantity > _QTY_EPS
    all_positions:   Mapping[int, Position]              # includes flats/shorts (for orphan + short arms)
    sell_legs_by_uic: Mapping[int, tuple[OrderState, ...]]
    planned_by_uic:  Mapping[int, PlannedExit]           # folded from journal 'planned', joined by uic
    oco_unsupported: frozenset[int]                      # persisted capability flags (Q1/Q2 failed here)

def reconcile_protection(view: ProtectionView) -> list[Action]:
    actions: list[Action] = []
    # 1. every netted LONG -> ensure downside covered
    for uic, pos in view.long_positions.items():
        actions.extend(_reconcile_long(uic, pos, view))
    # 2. ORPHAN SWEEP (fixes C-FATAL-2 / A-S4): a working SELL on a uic with no long -> cancel it,
    #    else it can later fire into a naked SHORT.
    for uic, legs in view.sell_legs_by_uic.items():
        if uic not in view.long_positions and legs:
            actions.append(CancelSellLegs(uic, tuple(l.order_id for l in legs),
                                          reason=f"uic {uic}: exit legs on flat/absent position — orphan sweep"))
    # 3. NEGATIVE-POSITION arm (fixes B-F4): never silently ignore an accidental short.
    for uic, pos in view.all_positions.items():
        if pos.quantity < -_QTY_EPS:
            actions.append(AlertOnly(f"uic {uic}: unexpected SHORT {pos.quantity} — manual intervention"))
    return actions

def _reconcile_long(uic, pos, view) -> list[Action]:
    owned = pos.quantity                                 # netted realized qty (STRUCTURAL — never planned)
    plan  = view.planned_by_uic.get(uic)
    legs  = view.sell_legs_by_uic.get(uic, ())

    # multi-plan / un-journaled-share guard (fixes A-S1 / B-M1): refuse to merge conflicting arms.
    if plan is None:
        return [AlertOnly(f"uic {uic}: long {owned} open but no journaled disaster-stop plan — cannot protect")]
    if plan.conflicting:                                 # >1 active plan folded to this uic
        return [AlertOnly(f"uic {uic}: {plan.n_plans} active plans on one netted position — refusing to merge")]

    stop_qty = sum(l.amount or 0 for l in legs if l.order_type in STOP_TYPES)
    tp_qty   = sum(l.amount or 0 for l in legs if l.order_type in TP_TYPES)
    total    = stop_qty + tp_qty

    # (A) OVER-HEDGE: an exit leg partially filled, or the position shrank -> total sell > owned.
    #     Place a residual-sized stop FIRST (never a naked repair window — fixes C-S3), then cancel the
    #     over-committed group. Identify the shrunk group by its leg.filled_quantity (fixes B-S5).
    if total > owned + _QTY_EPS:
        bad = _group_with_partial_fill(legs) or _newest_group(legs)
        gen = plan.next_gen()
        return [
            PlaceStop(uic, "SELL", owned, plan.stop_price, _exit_stop_ref(plan.entry_crid, gen),
                      supersede_ids=bad.stop_leg_ids),          # keep-old-until-new-confirmed on downsize
            CancelSellLegs(uic, bad.order_ids, reason="over-hedge repair (post-place)"),
        ]

    # (B) DOWNSIDE DEFICIT: naked, grew, lone-TP Bug-B shape, or stale partial stop.
    if stop_qty + _QTY_EPS < owned:
        deficit = owned - stop_qty
        # Additive-on-growth (Q5): if a covering stop already holds `stop_qty`, add a stop for the
        # delta ONLY (no cancel, no naked window). If Q5 is unconfirmed, fall back to cancel-replace
        # with place-residual-first ordering (place full-owned stop, THEN cancel the small one).
        gen = plan.next_gen()
        if stop_qty > _QTY_EPS and uic not in view.oco_unsupported and ADDITIVE_STOPS_CONFIRMED:
            return [PlaceStop(uic, "SELL", deficit, plan.stop_price, _exit_stop_ref(plan.entry_crid, gen),
                              cancel_conflicting=_tp_only_leg_ids(legs))]  # cancel a lone TP first (Bug-B)
        return [PlaceStop(uic, "SELL", owned, plan.stop_price, _exit_stop_ref(plan.entry_crid, gen),
                          supersede_ids=tuple(l.order_id for l in legs))]  # cancel-replace, place-first

    # (C) DOWNSIDE COVERED. Consider the rung 1 -> 2 TP-capture upgrade (Stage 2, gated).
    if tp_qty + _QTY_EPS >= owned:
        return [NoOp()]                                  # rung 2 already: full-qty TP present
    if plan.tp_price is None or uic in view.oco_unsupported or not _oco_enabled():
        return [NoOp()]                                  # stop-only is the accepted terminal rung
    return [UpgradeToOco(uic, "SELL", owned, plan.stop_price, plan.tp_price, plan.entry_crid,
                         plan.next_gen(), supersede_ids=_stop_leg_ids(legs))]
```

### Executor (control loop, per-action `BrokerError` boundary, execute-time owned re-check)

```python
def execute(broker, action, alert, kill):
    match action:
      case NoOp(): return
      case AlertOnly(msg): alert(msg); return             # THROTTLED by (uic, reason)

      case CancelSellLegs(uic, ids, reason):
          for oid in ids: broker.cancel_order(oid)        # idempotent, cascade-safe
          alert(reason)

      case PlaceStop(uic, side, qty, price, ref, supersede_ids=(), cancel_conflicting=()):
          # KILL: a protective stop only REDUCES exposure -> allowed under KILL (fixes B-S1).
          for oid in cancel_conflicting: broker.cancel_order(oid)   # a lone TP, cleared BEFORE place
          live = broker.get_positions_by_uic(uic)                   # EXECUTE-TIME re-check (fixes B-F3/A-S4)
          if live.quantity + _QTY_EPS < qty: qty = max(live.quantity, 0)   # never oversell
          if qty <= _QTY_EPS: return                                 # position gone — do NOT plant a stop
          try:
              broker.place_standalone_stop(uic, side, qty, price, ref)     # GUARANTEED shape
              _journal_placed(uic, ref, qty)                                # audit breadcrumb (A-M2)
          except OrderRejectedError as e:
              if _is_sell_orders_already_exist(e):
                  alert(f"uic {uic}: stop deferred — sell-commit not yet released"); return  # retry next tick
              raise
          for oid in supersede_ids: broker.cancel_order(oid)          # cancel the OLD stop AFTER new confirmed

      case UpgradeToOco(uic, side, qty, stop, tp, crid, gen, supersede_ids):
          if kill: return                                             # upgrade is optional -> skip under KILL
          try:
              broker.place_oco_exit(uic, side, qty, stop, tp, _exit_stop_ref(crid, gen), position_id=...)
              for oid in supersede_ids: broker.cancel_order(oid)      # cancel lone stop AFTER OCO confirmed
          except BrokerError as e:                                    # BROAD catch (fixes C-S4): 202/rate/reject
              _mark_oco_unsupported(uic)                              # PERSISTED, on ANY failure (fixes C-FATAL-1/S7)
              alert(f"uic {uic}: OCO upgrade failed ({e}); staying rung-1 stop-only")
              # rung-1 stop is UNTOUCHED (supersede runs only on success) -> never naked (fixes C-FATAL-1)
```

Two load-bearing ordering rules, both fixing "cancel strands naked":
- **Place the new/larger protective stop BEFORE cancelling the old/smaller one** (`supersede_ids` cancelled only after the place succeeds). A grow or an over-hedge repair never opens a naked window on the shares that were already covered (fixes A-F2, C-S3, C-S4). The transient overlap is `owned + delta` on the sell side for one round-trip — acceptable *only if* Q5 (or execute-time clip) keeps it ≤ owned; the execute-time re-check + `PositionId` reduce-only (Q3) is the belt for that. On a pure grow the overlap is bounded by the delta and cleared same tick.
- **The OCO upgrade never touches the rung-1 stop until the OCO POST succeeds.** A failed upgrade leaves the proven stop live and marks the instrument unsupported — no churn, no naked window (fixes C-FATAL-1).

---

## 7. Journal role changes

**KEPT — the plan prices the broker cannot know:**

```
kind="planned"  {entry client_request_id, uic, tier_index, side, stop_price, take_profit, gen}
```

`_fold_standalone_stop_journal` → `_fold_planned_exits(lines) -> dict[int, PlannedExit]`, keyed by **uic**, returning ONLY plan params (drop the `frozenset[str] protected` return entirely). `_journal_tier` already writes a `planned` line at entry placement; it must now **also carry `take_profit` and `tier_index`** (today it writes stop only, because the entry bracket nulls the TP). `tier_index` lets the governing-TP rule pick the shallowest filled tier (§8). `gen` (the resize counter) is journaled here too, append-only.

```python
@dataclass(frozen=True)
class PlannedExit:
    uic: int
    entry_crid: str          # governing (shallowest-filled) tier crid, for the deterministic ref
    side: str                # "SELL"
    stop_price: float
    tp_price: float | None
    conflicting: bool        # True if >1 distinct active plan folded to this uic (refuse-to-merge)
    n_plans: int
    # next_gen() reads/increments the persisted per-uic resize counter
```

**DROPPED as a protection signal:**
- `kind="intent"` — **removed entirely.** The placer no longer writes it. This is the Bug-A source; "intent confers protection" is deleted.
- `kind="placed"` — **no longer consulted** for protection. `BrokerView.protected_request_ids` and the `frozenset[str]` half of the fold are deleted; `_advance_filled`'s `request_id in protected_request_ids` branch is deleted. `placed` survives only as an append-only **audit breadcrumb** — nothing reads it into a protection decision.

The OCO-unsupported capability flag is persisted as its own append-only journal line (`kind="oco_unsupported"`, keyed by uic/instrument), read back into `view.oco_unsupported` so the flag survives systemd `Restart=on-failure` (fixes C-S7). Journals stay append-only (never rewritten).

---

## 8. Netting + partial-fill + multi-tier rules

**Keying is per-uic** — the unit Saxo enforces sell-qty against and the unit FifoRealTime netting collapses to. This is the central correctness move; the current per-`client_request_id` design is the root of both bugs plus the reconcile divergence.

- **Multi-tier ladder → one netted position.** `owned = pos.quantity` from the netted `PositionBase.Amount`, so protection is always sized to the true netted qty regardless of how many tiers filled or in what order. Zero planned-qty guessing — the "size to realized fill, never planned" rule becomes a **structural property**, not a code path.
- **Governing disaster stop:** a single brief scalar, identical on every tier by construction. Defensive: if journaled tiers disagree, take the **max** stop for a long (tightest) and alert (documented risk judgment — critique-A M1/C-M12).
- **Governing TP price (rung 2):** the target of the **shallowest FILLED tier** = `min(tier_index over filled tiers)`'s `tp_price`. Deterministic, fill-order-independent, monotone-stable (tiers fill shallow-first under descending limits, so the first fill sets it and later deeper fills never change it → no churn). Far TP tranches (deeper indices) stay operator-managed / phase-B.
- **Another tier fills (grow):** owned rises above the covering stop's Amount → deficit arm. **Additive** (place a stop for the delta only, no naked window) if Q5 confirms same-uic stops sum cleanly; otherwise cancel-replace with **place-full-owned-stop-first, cancel-small-stop-after** ordering (bounded overlap, no naked window on the already-covered shares).
- **Exit leg partial-fills (rung 2):** the TP leg fills `q_p < owned`; FIFO drops owned to `owned − q_p`; the paired stop still rests at the old Amount → over-hedge arm → **place a residual-sized stop first**, then cancel the over-committed OCO group. If Q4 shows Saxo auto-decrements the sibling, `total == owned` → NoOp, no action. Either outcome self-heals; the reconciler only ever compares `total`/`stop_qty` to `owned`.
- **Resize ordering (load-bearing):** on a **downsize/repair** always place the correctly-sized replacement BEFORE cancelling the oversized one (no naked window). On a lone-TP Bug-B shape, cancel the TP BEFORE placing the stop (the TP holds the conflicting sell commitment). These are opposite orderings for opposite reasons; the algorithm encodes each explicitly (`supersede_ids` = cancel-after; `cancel_conflicting` = cancel-before).
- **reconcile.py multi-tier fix (Stage-1 BLOCKER, not adjacent — fixes C-S6):** `get_open_position_references()` returns one `ExternalReference` per netted row (the source/oldest tier crid), so every *other* filled tier falls through `_reconcile_filled` to `divergence=True` → a per-tick `AlertOnly` storm, and a later FIFO mapping flip can un-protect. Fix: match a filled tier to a position **by uic** (tier is "position open" iff its uic has owned > 0 and its audit `FilledAmount > 0`), add a `Σ FilledAmount == owned` cross-check as the correlation validator, stop emitting `divergence=True` for non-source filled tiers. Ship this in Stage 1 — the per-uic protection loop tolerates it, but the alert storm and the un-protect flip do not.

---

## 9. Idempotency + restart + crash-window guarantees

Explicit invariants (for adversarial review), each with the sequence it defends:

- **I1 — Downside always covered (bounded latency).** For every netted long with `owned > 0` and a journaled plan, within ≤1 tick (fresh place) or ≤1 tick (place-first resize) there is a live SELL `StopIfTraded` on the uic with `Σ stop Amount ≥ owned`. Exceptions: the accepted ~1-tick fill→place window and broker-unreachable ticks. *Defends:* entry fills at t → next tick positions shows owned → deficit → place.
- **I2 — No over-commit (Bug B unrepresentable).** Total live SELL `Amount` on any uic is `≤ owned` at every instant: one stop, or one OCO group (commit = owned, counted once), and every transition places/cancels so the sum never exceeds owned; the entry bracket commits nothing on the sell side. *Defends:* lone-TP shape → cancel TP before stop; grow → additive/place-first; upgrade → OCO counted once (Q1) or degrade.
- **I3 — Protection = broker truth only.** The place/cancel/no-op decision reads only live positions + live orders (+ journal *prices*). No journal line asserts protection. *Defends:* Bug A.
- **I4 — Idempotent placement.** Deterministic gen-stamped `ExternalReference` → a sub-15s crash-retry of the *same size* hits Saxo dedup; a resize is a distinct ref (no false dedup to the stale small stop); a cross-window re-derivation sees the live leg → NoOp. *Defends:* A-S3/B-F5/C-S8.
- **I5 — Self-healing across restart.** Manual cancel, daemon restart, crash mid-transition all re-derive from live broker state. The OCO-unsupported flag is persisted, so a restart does not resume a structurally-impossible upgrade churn. *Defends:* C-S7; the crash-before-journal case (position on broker, no verdict) is caught because the loop iterates positions, not verdicts (C-S5).
- **I6 — Upgrade never strands below rung 1.** `place_oco_exit` failure (any `BrokerError`) leaves the proven rung-1 stop live (supersede runs only on success) and persists the unsupported flag. *Defends:* C-FATAL-1/S4.
- **I7 — No plant on a flat/closing uic.** The execute-time owned re-check + orphan sweep prevent a stop landing on (or lingering on) a uic that closed between the snapshot and the place — which could otherwise fire into a short. *Defends:* A-S4/B-F3/C-FATAL-2.

Crash / retry matrix:

| event | broker state after | next-tick re-derivation | outcome |
|---|---|---|---|
| stop/OCO POST raises (Bug-A trigger) | no new SELL leg on uic | `stop_qty < owned` → deficit | **retries** — no journal marker can lie |
| crash after successful stop POST | SELL stop live | `stop_qty ≥ owned` → covered | **NoOp** — no double |
| operator cancels the stop | leg gone | deficit | re-places |
| daemon restart, position never journaled | position on broker, no verdict | loop iterates positions → deficit | protects (or alerts if no plan) |
| inherited lone-TP (Bug-B shape) | TP present, stop 0 | deficit + lone-TP | cancel TP → place stop; owned committed once |
| crash between cancel-TP and place-stop | no SELL legs | NAKED → deficit | self-heals in one tick |
| OCO upgrade fails (Q1 false) | rung-1 stop still live | mark unsupported (persisted) → NoOp | no churn, no naked, no re-attempt after restart |
| TP leg partial-fills, sibling not decremented | stop Amount > owned | over-hedge | place residual stop first → cancel oversized |
| round-trip close between snapshot and place | position flat, stale exits maybe | execute-time re-check skips; orphan sweep cancels | no stop planted on flat uic |

---

## 10. contract.py Protocol + position_manager Action changes

**contract.py**
- `OrderState` gains `uic / side / order_type / amount / external_reference` (additive, defaulted). `filled_quantity` already present.
- `OrderRejectedError` gains `error_code`.
- Add `SupportsOcoExit` Protocol (Stage 2). **Keep** `SupportsStandaloneStop`.
- `__all__` += `SupportsOcoExit`. Base `Broker` Protocol unchanged (frozen).

**position_manager.py**
- `DisasterStop` → `PlannedExit(uic, entry_crid, side, stop_price, tp_price, conflicting, n_plans)`.
- `BrokerView` → `ProtectionView(long_positions, all_positions, sell_legs_by_uic, planned_by_uic, oco_unsupported)` — **no `protected_request_ids`**, **no journal-derived `disaster_stops`**. `working_children` retained for the surviving terminal/round-trip `CancelRemaining` path.
- New `ProtState` classify + new Actions:
  ```python
  @dataclass(frozen=True) class PlaceStop:      uic:int; side:str; qty:float; stop_price:float; request_id:str; supersede_ids:tuple[str,...]=(); cancel_conflicting:tuple[str,...]=()
  @dataclass(frozen=True) class UpgradeToOco:   uic:int; side:str; qty:float; stop_price:float; tp_price:float; entry_crid:str; gen:int; supersede_ids:tuple[str,...]
  @dataclass(frozen=True) class CancelSellLegs: uic:int; order_ids:tuple[str,...]; reason:str
  Action = PlaceStop | UpgradeToOco | CancelSellLegs | CancelRemaining | AlertOnly | NoOp
  ```
  `PlaceStandaloneStop` is removed as an Action (the place is inside `PlaceStop`). `CancelRemaining / AlertOnly / NoOp` unchanged.
- New pure `reconcile_protection(view) -> list[Action]` + `_reconcile_long` (§6). `advance` keeps only the non-protection verdict routing (divergence/unresolved → `AlertOnly`; terminal / round-trip-closed → `CancelRemaining`); its FILLED branch no longer places a stop.

**control_loop.py**
- `LoopDeps.place_standalone_stop` retained; add `place_oco_exit` (Stage 2) + `build_protection_view`.
- `_make_position_view_builder` → reads `get_long_positions()` + `list_working_sell_orders()` + folds only `planned` (prices) + `oco_unsupported`; single snapshot.
- `run_once`: after the existing terminal/cancel `advance` loop, run one protection pass — `for a in reconcile_protection(view): execute(...)` each inside its own `BrokerError` boundary.
- `_fold_standalone_stop_journal` → `_fold_planned_exits` (prices only). `_make_standalone_stop_placer` → `_make_protection_executor` (no intent/placed-as-protection writes; adds the ordered cancel/place executor + `_is_sell_orders_already_exist` / `_is_too_far_from_entry` + `_mark_oco_unsupported` persist + `_oco_enabled`).
- Alert throttle wrapper by `(uic, reason)` + per-uic failure counter/backoff. Idempotent `cancel_order`.
- `build_default_deps` — capability gates (`SupportsStandaloneStop` always; `SupportsOcoExit` for Stage 2). `TickReport.stops_placed` → `exits_placed`.

**placement_planner.py (Bug-B fix at source)**
- Entry brackets already have `stop_loss=None`; also set `take_profit=None` **always** — the in-band TP no longer places as a bracket child (that child is the Bug-B lone-TP sell). `tp_placed_as_child` → `tp_planned_in_oco`. `PlacementPlan`/`TierPlacement` surface each tier's in-band TP + `tier_index` so `_journal_tier` records them. `_operator_report` wording updates; "disaster stop appears exactly once" invariant stays.

**reconcile.py** — per-uic filled-tier match + `Σ FilledAmount == owned` cross-check (§8, Stage-1 blocker).

---

## 11. Staged plan (TDD, in a git worktree)

Per multi-session discipline, all implementation happens in a **dedicated `git worktree`** off fresh `origin/main` (`git worktree add -b feature/saxo-oco-exit .claude/worktrees/saxo-oco-exit origin/main`), with its **own `uv sync`** (a worktree editing pipeline code needs its own local venv — verify `alphalens_pipeline.__file__` points into the worktree). Every stage is red→green→refactor. Zen pre-merge review (`deepseek/deepseek-v4-pro`, thinking=high) before each merge.

### Stage 1 — kill Bug A + Bug B with STOP-ONLY protection (independently shippable, zero unproven probes)

The minimal increment that stops the naked-position bug. Uses only confirmed fact #3 (standalone stop). No OCO, no TP as broker-side protection (TP stays operator-managed exactly as the far-TP path is today).

1. `reconcile.py` per-uic multi-tier fix + `Σ FilledAmount == owned` (BLOCKER — un-protect flip + alert storm).
2. Enrich `OrderState` (uic/side/type/amount/external_reference) + `_to_order_state` mapping.
3. Structured `error_code` on `OrderRejectedError` + `_is_*` classifiers with positive-control tests.
4. `placement_planner` entry-only brackets (`take_profit=None`, `stop_loss=None`) — Bug-B at source.
5. Journal: drop `intent`/`placed`-as-protection; `_fold_planned_exits` (prices + tier_index, keyed by uic).
6. `reconcile_protection` + `_reconcile_long` with only the deficit / over-hedge / orphan-sweep / negative / no-plan / conflicting arms — rung 0↔1 only (no `UpgradeToOco`).
7. Executor: loop iterates `get_long_positions()`; single snapshot; execute-time owned re-check; place-first resize ordering; broad `BrokerError` catch; idempotent cancel; alert throttle + backoff; KILL allows protective stop.
8. Deterministic gen-stamped refs.

**End state of Stage 1:** every filled long is covered by a live standalone `StopIfTraded` sized to netted owned within ≤1 tick; a failed POST retries; the lone-TP double-sell cannot occur; orphans/shorts/flat uics are swept or alerted. Both bugs dead. Fully shippable and testable without any live SIM probe (Stage-1 correctness is proven by unit tests against fakes; a one-shot SIM smoke of the BIO geometry validates end-to-end but is not a code gate).

### Stage 2 — OCO rung-2 (TP-capture upgrade), gated on Q1 + Q2

Add `place_oco_exit` + `_build_oco_exit_body` + `SupportsOcoExit`; add the `UpgradeToOco` arm behind `_oco_enabled()` (env flag) AND the persisted per-instrument `oco_unsupported` capability flag. Land the Q1/Q2/Q7 SIM probes first; enable per-instrument only after green. Any OCO failure degrades to rung-1 + persists unsupported. Ship dark (flag OFF) until probes pass.

### Stage 3 — robustness end-state (SUPERSEDED — see the locked Stage-3 memo)

> **SUPERSEDED 2026-07-23.** The original Stage-3 plan below hinged on `PositionId`-linked reduce-only exits (Q3). That plan is **REFUTED**: a live SIM POST proved Saxo rejects a `PositionId` on an OCO master (HTTP 400), so reduce-only cannot be attached and the oversell-into-short guard stays the cash-account `NotOwned` backstop (Q3a, live-confirmed). **Q8 is CONFIRMED live** — PATCH preserves `OrderId` on an `Amount` change — so the atomic-resize path is real. The user chose **option X** (reach OCO only at the fresh-fill moment via `UpgradeToOco(supersede_ids=())`, converge by turnover; NO rung1->2 upgrade of a resting standalone stop, which is unsafe by construction). The locked Stage-3 design is now **[`saxo_stage3_oco_amend_design_2026_07_23.md`](saxo_stage3_oco_amend_design_2026_07_23.md)** — read it for the authoritative signatures, branch conditions, mitigations, and the two dark env flags (`ALPHALENS_BROKER_OCO_ENABLED` for B0, `ALPHALENS_BROKER_AMEND_ENABLED` for the PATCH-amend resize arms). The bullets below are retained only as the historical framing.

- ~~`PositionId`-linked reduce-only exits (Q3)~~ — **REFUTED** (400 on the OCO master); replaced by option X (OCO-direct-on-fill) + the cash-account `NotOwned` oversell backstop.
- Additive-on-growth same-uic stops (Q5) — shipped in Stage 1.5 (#879) as the always-correct fallback; the Stage-3 PATCH-amend UP composes with it (amend one clean standalone stop in place, fall to B1 additive otherwise).
- PATCH/modify-Amount atomic resize (Q8 — **CONFIRMED live**) — no cancel/replace on resize; PATCH wired into the never-blind-retry write lane (`idempotent=False`). Attended SIM amend probe: `apps/alphalens-research/tests/live/test_saxo_amend_probe_live.py` (flag `SAXO_LIVE_AMEND_PROBE=1`), which rests a standalone stop, amends its `Amount` UP then DOWN, and confirms the `OrderId` is preserved and the order never leaves the book — the gate before flipping `ALPHALENS_BROKER_AMEND_ENABLED=1`.

Each is an independent, dark-shipped increment; none block Stage 1 or 2.

---

## 12. TDD test plan (unittest.TestCase)

All research-side tests under `apps/alphalens-research/tests/brokers/`, **`unittest.TestCase`** (pytest-style is silently skipped in CI). Pure `reconcile_protection` / `_reconcile_long` tested against hand-built `ProtectionView` stubs; the executor against a `FakeBroker` that raises mapped `OrderRejectedError(error_code=...)`. Diff-coverage ≥80%.

### Red-first reproductions of the two live bugs (must FAIL on current code, PASS after)

- `test_position_manager.py::TestBugARetryAfterFailedPost` — GIVEN a netted long `owned=46`, a journaled plan, and **no SELL leg on the uic** (a prior POST raised) — WHEN `_reconcile_long` runs — THEN it returns `PlaceStop(qty=46)` (retry), NOT `NoOp`. Companion `test_control_loop.py::TestFailedPostLeavesNoProtectionAndRetries` — executor tick 1 `place_standalone_stop` raises `BrokerError`; assert **no** protection recorded and tick 2 (fresh view, still no live stop) re-issues the place. *(On today's code the orphan `intent` line makes tick 2 a permanent `NoOp` — this is Bug A.)*
- `test_position_manager.py::TestBugBLoneTpForcesCancelBeforeStop` — GIVEN a netted long `owned=46` with **one live SELL Limit (TP, Amount 46) and no stop** — WHEN `_reconcile_long` runs — THEN `PlaceStop(qty=46, cancel_conflicting=(tp_id,))` (cancel the TP *before* placing the stop), NOT `NoOp`. *(On today's code this position reads "protected" and is left naked-downside — this is the shape behind Bug B.)*
- `test_placement_planner.py::TestEntryBracketIsEntryOnly` — the placed entry bracket has **both** `take_profit is None` AND `stop_loss is None` on every tier. *(Fails today — planner promotes the in-band TP to a child, the Bug-B source.)*
- `test_saxo_broker_oco_exit.py::TestOcoBodyCommitsOwnedOnce` (Stage 2) — `_build_oco_exit_body` produces two SELL legs both `Amount==owned` under one POST, `OrderRelation:"Oco"`.

### `reconcile_protection` decision table (pure)

- `TestNakedPlacesStopSizedToNettedOwned` — no legs → `PlaceStop(qty=owned)`.
- `TestCoveredIsNoOp` — live stop `Amount≥owned` → `NoOp`.
- `TestCrashAfterPlaceIsNoOp` — live stop present → `NoOp` (no double).
- `TestGrowUnderCoversResizesPlaceFirst` — owned grew past stop Amount → `PlaceStop(qty=owned, supersede_ids=(old,))`; assert supersede cancelled AFTER place in the executor test.
- `TestOverHedgePlacesResidualBeforeCancel` — `total > owned` (TP leg `filled_quantity>0`) → `PlaceStop(residual)` emitted BEFORE `CancelSellLegs(bad_group)`; `_group_with_partial_fill` selects the group by `filled_quantity` (fixes B-S5).
- `TestOrphanExitOnFlatUicSwept` — sell legs on a uic with no long → `CancelSellLegs` (fixes C-FATAL-2).
- `TestNegativePositionAlerts` — `quantity < 0` → `AlertOnly("unexpected SHORT")` (fixes B-F4).
- `TestNoPlanAlerts` / `TestConflictingPlansRefuseMerge` — `plan is None` → alert; `>1` plan on a uic → `AlertOnly` refuse-to-merge (fixes A-S1).
- `TestSizesToNettedOwnedNotPlanned` — position qty ≠ any single planned tier qty → stop sized to `pos.quantity`.
- `TestFloatToleranceNoFlicker` — `owned=46.0`, `stop_qty=45.9999999` → `NoOp` (tolerance, not bare `>=`; fixes A-S6/B-S2).
- `TestGenStampedRefChangesOnResize` — a resize increments `gen` → distinct ref; a same-size retry keeps `gen` (fixes A-S3/B-F5/C-S8).

### Executor / degradation tests (FakeBroker)

- `TestUpgradeOcoFailKeepsRung1AndMarksUnsupported` (Stage 2) — `place_oco_exit` raises `SellOrdersAlreadyExist` → rung-1 stop untouched, `oco_unsupported` persisted, no re-attempt next tick (fixes C-FATAL-1); assert persistence survives a rebuilt view (fixes C-S7).
- `TestUpgradeOcoBroadCatch` (Stage 2) — `place_oco_exit` raises plain `BrokerError` (202/rate-limit) → caught, rung-1 kept, no naked (fixes C-S4).
- `TestSellOrdersAlreadyExistDefersNotCrashes` — place raises `SellOrdersAlreadyExist` after a same-tick cancel → alert + defer, tick survives, retries next tick.
- `TestExecuteTimeRecheckSkipsFlatUic` — snapshot showed owned=46, position flat at execute → place skipped, no stop planted (fixes B-F3/A-S4).
- `TestKillAllowsProtectiveStopBlocksUpgrade` — under KILL, `PlaceStop` executes, `UpgradeToOco` skipped (fixes B-S1).
- `TestIdempotentCancelNoThrash` — cancelling an already-cancelled sibling returns success, no alert (fixes A-S5).
- `TestAlertThrottleByUicReason` — the same `(uic, reason)` within the interval alerts once; N consecutive failures escalate once + back off (fixes A-S2/B-S3/C-S10).
- `TestPerCallBrokerErrorBoundary` — one uic's broker error does not prevent other uics being processed.
- `TestLoopIteratesPositionsNotVerdicts` — a position with owned>0 and NO journal verdict → protected/alerted (fixes C-S5).

### View-builder / journal / broker-adapter / reconcile

- `TestFoldPlannedExitsPricesOnly` — fold returns `PlannedExit` (stop+tp+tier_index), **no** protected set; `intent`/`placed` ignored for protection.
- `TestProtectionDerivedFromLiveOrders` — a live `{crid}-stop-{gen}` SELL → classified covered; absent → not covered.
- `test_saxo_broker.py::TestToOrderStateSurfacesFields` — `_to_order_state` surfaces `uic/side/order_type/amount/external_reference/filled_quantity` from a `/orders/me` fixture row; `OrderRejectedError.error_code` mapped in `_precheck_or_raise`.
- `test_reconcile.py::TestSecondFilledTierNotDivergent` — two filled tiers on one uic → the non-source tier resolves FILLED (not `divergence=True`); `Σ FilledAmount == owned` (fixes C-S6).

### Existing suites to migrate/delete

- `test_control_loop.py::TestStandaloneStopJournalFold` (intent/placed→protected) and `TestStandaloneStopPlacerRecovery` (intent-before-post) — **deleted/rewritten** around broker-state protection.
- `test_position_manager.py` — `DisasterStop`/`PlaceStandaloneStop`/`disaster_stops`/`protected_request_ids` → `PlannedExit`/`PlaceStop`/`ProtectionView`.
- `test_placement_planner.py` — `tp_placed_as_child` → `tp_planned_in_oco`; `TestDisasterStopExactlyOnce` survives.
- `test_broker_contract.py` — `OrderState` field defaults so existing constructions pass; `FakeBroker` implements `get_long_positions`/`list_working_sell_orders` (+ `place_oco_exit` for Stage 2).

---

## Appendix — BIO worked example under the target design (Stage 1)

Tiers 297.5 / 287.8 / 281.8 (`tier_index` 0/1/2), common stop 216.48, `tp_tranches[0].target = 306.72`, only tier-0 filled +46 @ ~296.

- Entry brackets placed **entry-only** (no TP child). `planned` lines journal stop=216.48 + tp=306.72 + tier_index per tier.
- reconcile: tier-0 → FILLED (owned=46); tier-1/2 → WORKING. One netted position uic=BIO, owned=46 (per-uic match, no false divergence for a later second fill).
- Protection pass: `long_positions[BIO]` owned=46, `sell_legs=()`, plan present → deficit → `PlaceStop(SELL, 46, 216.48, "{tier0_crid}-stop-0")`. Committed=46==owned → no `SellOrdersAlreadyExist` (**Bug B gone**; there is no TP sell). POST fails → nothing on broker → next tick re-derives deficit → retries (**Bug A gone**). Crash after POST → live stop → covered → NoOp (no double).
- Price falls, tier-1 (287.8) fills +Q2: owned=46+Q2, stop covers 46 → deficit=Q2 → place-first resize to `owned` (or additive delta if Q5 confirmed), old 46-stop cancelled only after the new stop is confirmed — no naked window on the original 46.
- TP is operator-managed at 306.72 in Stage 1 (surfaced in the operator report, not placed as broker-side protection). Stage 2 turns it into the OCO rung once Q1+Q2 are green.
