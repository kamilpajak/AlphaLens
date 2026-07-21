# Saxo auto-manager — Stage 1 (broker-state-truth STOP-ONLY protection) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Kill Bug A (failed-stop retry suppression → permanent naked position) and Bug B (`SellOrdersAlreadyExistForOwnedContracts` double-sell) by making protection a pure function of live broker state, keyed per-uic, with a guaranteed standalone `StopIfTraded` sized to netted owned qty.

**Architecture:** Protection status is derived each tick from `get_long_positions()` + `list_working_sell_orders()` (correlated by uic), NOT from any journal line. The journal keeps only *plan prices*. Entry brackets are placed entry-only (no TP child), so the total live SELL commitment on any uic is `≤ owned` by construction. Rung 0 (naked) → rung 1 (one `StopIfTraded`, `Amount == owned`) is the Stage-1 terminal rung; rung 2 (OCO+TP) is Stage 2 and stays dark.

**Tech Stack:** Python, `apps/alphalens-pipeline/alphalens_pipeline/brokers/`, `unittest.TestCase`, Saxo OpenAPI (SIM), `uv`, `pyright`, `ruff`, `bandit`.

**Design memo (THE SPEC — read it first, it is in the worktree):** `docs/research/saxo_oco_exit_target_design_2026_07_21.md`. Every task cites its section. The memo carries the full signatures, the `reconcile_protection`/`_reconcile_long` pseudocode (§6), the executor (§6), the journal changes (§7), the crash/retry matrix (§9), and the contract/Action changes (§10).

## Global Constraints

- **Stage 1 is STOP-ONLY.** Do NOT wire rung 2. `place_oco_exit` / `SupportsOcoExit` / `UpgradeToOco` are Stage 2. In Stage 1, `_reconcile_long` MUST NOT emit `UpgradeToOco` — `_oco_enabled()` returns `False` and the covered branch returns `NoOp`. (Defining the `UpgradeToOco` dataclass is fine; wiring the arm is not.)
- **Keep `place_standalone_stop` exactly as-is** — it IS rung 1 (SIM-proven shape, OrderId 5039296412). Do not delete or change its signature.
- **Protection = broker truth only.** No journal line may confer "protected". `kind="intent"` is removed entirely; `kind="placed"` survives only as an append-only audit breadcrumb that nothing reads into a decision.
- **Keying is per-uic** everywhere (positions, sell legs, planned exits). Never per-`client_request_id` for protection.
- **Tests are `unittest.TestCase`** (pytest-style is silently skipped in CI). All new tests under `apps/alphalens-research/tests/brokers/`. Diff-coverage ≥80%.
- **Never bare `>=` on float qty** — use `_QTY_EPS = 0.5` tolerance.
- **Per-action `BrokerError` boundary** in the control loop: one uic's failure must not abort the tick or other uics.
- **Append-only journals** (never rewritten). **SIM-only structural rail** stays untouched.
- **Dependency direction:** `alphalens_pipeline.*` must not import `alphalens_research.*` at top level.
- **CI is more than the automanager tests:** the research job also runs whole-tree `uv run pyright`, `bandit`, `ruff check apps/...`, and repo-wide guard tests. The final task runs the full suite + pyright + bandit + ruff before hand-off.
- **Worktree:** all work in `.claude/worktrees/saxo-oco-exit` (branch `feature/saxo-oco-exit`) off fresh `origin/main`, with its **own `uv sync`** (verify `alphalens_pipeline.__file__` points into the worktree). The design memo is copied into the worktree and committed as part of this branch.

---

## File Structure

| File | Responsibility (Stage 1) |
|---|---|
| `apps/alphalens-pipeline/alphalens_pipeline/brokers/contract.py` | `OrderState` +5 fields; `OrderRejectedError.error_code`; `_is_sell_orders_already_exist` / `_is_too_far_from_entry` classifiers |
| `apps/alphalens-pipeline/alphalens_pipeline/brokers/saxo/broker.py` | `_to_order_state` maps the new fields; `get_long_positions` / `list_working_sell_orders` / `get_positions_by_uic`; `error_code` set in `_precheck_or_raise` |
| `apps/alphalens-pipeline/alphalens_pipeline/brokers/reconcile.py` | per-uic filled-tier match + `Σ FilledAmount == owned` cross-check (Stage-1 BLOCKER) |
| `apps/alphalens-pipeline/alphalens_pipeline/brokers/automanager/placement_planner.py` | entry-only brackets (`take_profit=None` always); surface tier `tp` + `tier_index`; `_journal_tier` records stop+tp+tier_index+gen |
| `apps/alphalens-pipeline/alphalens_pipeline/brokers/automanager/position_manager.py` | `PlannedExit`; `ProtectionView`; new Actions `PlaceStop`/`CancelSellLegs`/`UpgradeToOco`; pure `reconcile_protection` + `_reconcile_long` (rung 0↔1) |
| `apps/alphalens-pipeline/alphalens_pipeline/brokers/automanager/control_loop.py` | `_fold_planned_exits`; gen-ref helpers; `build_protection_view`; `_make_protection_executor` (ordered cancel/place, execute-time re-check, KILL-allows-stop, sell-exist defer); alert throttle by `(uic,reason)`+backoff; `run_once` protection pass; `TickReport.exits_placed` |

Test files (all `apps/alphalens-research/tests/brokers/`): `test_saxo_broker.py`, `test_broker_contract.py`, `test_reconcile.py`, `test_placement_planner.py`, `test_position_manager.py`, `test_control_loop.py`.

---

## Task 1: Adapter enrichment — `OrderState` fields, `error_code`, filters, classifiers

**Memo:** §4.1, §4.2, §10 (contract.py).

**Files:**
- Modify: `apps/alphalens-pipeline/alphalens_pipeline/brokers/contract.py`
- Modify: `apps/alphalens-pipeline/alphalens_pipeline/brokers/saxo/broker.py` (`_to_order_state`, `_precheck_or_raise`, add filter helpers)
- Test: `apps/alphalens-research/tests/brokers/test_saxo_broker.py`, `.../test_broker_contract.py`

**Interfaces:**
- Produces: `OrderState.{uic, side, order_type, amount, external_reference}` (all `| None = None`); `OrderRejectedError.error_code: str | None = None`; `broker.get_long_positions() -> list[Position]`; `broker.list_working_sell_orders() -> list[OrderState]`; `broker.get_positions_by_uic(uic) -> Position` (netted, for execute-time re-check); `_is_sell_orders_already_exist(e)`, `_is_too_far_from_entry(e)`.
- Consumed by: Tasks 5–7.

- [ ] **Step 1: Write failing test — `_to_order_state` surfaces the new fields.** In `test_saxo_broker.py::TestToOrderStateSurfacesFields`, feed a `/orders/me` fixture row (`{"OrderId":"1","Uic":43070,"BuySell":"Sell","OpenOrderType":"StopIfTraded","Amount":46.0,"ExternalReference":"crid-stop-0","Status":"Working","FilledAmount":0.0}`) through `_to_order_state`; assert `uic==43070`, `side=="SELL"`, `order_type=="StopIfTraded"`, `amount==46.0`, `external_reference=="crid-stop-0"`.
- [ ] **Step 2: Run it — fails** (`AttributeError`/`TypeError` on the missing fields).
- [ ] **Step 3: Add the 5 defaulted fields to `OrderState`** (memo §4.1 code block) and map them in `_to_order_state` from the Saxo row (`BuySell`→upper, `OpenOrderType`, `Uic`, `Amount`, `ExternalReference`). Keep `filled_quantity` mapping.
- [ ] **Step 4: Run it — passes.**
- [ ] **Step 5: Write failing tests — `error_code` + classifiers.** `test_saxo_broker.py::TestPrecheckSetsErrorCode` (a precheck payload with `ErrorInfo.ErrorCode="SellOrdersAlreadyExistForOwnedContracts"` raises `OrderRejectedError` whose `error_code` equals that string) and `test_broker_contract.py::TestErrorClassifiersPositiveControl` (positive control: an `OrderRejectedError(error_code="SellOrdersAlreadyExistForOwnedContracts")` → `_is_sell_orders_already_exist` True; a plain `BrokerError()` → False; same for `_is_too_far_from_entry` / `"TooFarFromEntryOrder"`).
- [ ] **Step 6: Run — fail.**
- [ ] **Step 7: Add `error_code` to `OrderRejectedError`, set it verbatim from `ErrorInfo.ErrorCode` in `_precheck_or_raise`, add the two classifiers** (memo §4.2). Each classifier keeps its positive-control test so it cannot rot to always-False.
- [ ] **Step 8: Run — pass.**
- [ ] **Step 9: Write failing tests — filter helpers.** `TestGetLongPositionsFiltersFlatAndShort` (a positions list with qty {+46, 0, −5} → only the +46 returned); `TestListWorkingSellOrders` (mixed orders → only SELL with status WORKING/PARTIALLY_FILLED); `TestGetPositionsByUicNetted` (returns the netted Position for a uic, zero-qty sentinel when absent).
- [ ] **Step 10: Run — fail. Step 11: Implement the three helpers** (memo §4.1 filter block + a `get_positions_by_uic` that nets `get_positions()` by uic). **Step 12: Run — pass.**
- [ ] **Step 13: Commit** — `feat(brokers): enrich OrderState + error_code + broker read filters (saxo-oco stage1)`.

---

## Task 2: `reconcile.py` per-uic multi-tier fix (Stage-1 BLOCKER)

**Memo:** §8 (last bullet), §12 (`test_reconcile.py`).

**Why first-after-adapter:** without it, laddered fills make every non-source filled tier fall through `_reconcile_filled` to `divergence=True` → a per-tick `AlertOnly` storm, and a FIFO mapping flip can un-protect. The per-uic protection loop tolerates it but the storm/flip do not.

**Files:** Modify `apps/alphalens-pipeline/alphalens_pipeline/brokers/reconcile.py`; Test `test_reconcile.py`.

- [ ] **Step 1: Write failing test — `TestSecondFilledTierNotDivergent`.** Two filled tiers on ONE uic (source tier crid returned by `get_open_position_references`, plus a second-tier crid whose audit `FilledAmount>0`); assert the non-source filled tier resolves `FILLED` (NOT `divergence=True`), and a `Σ FilledAmount == owned` cross-check passes for the netted position.
- [ ] **Step 2: Run — fail** (today the second tier is `divergence=True`).
- [ ] **Step 3: Implement the per-uic match** — a filled tier is "position open" iff its uic has owned>0 AND its audit `FilledAmount>0`; add the `Σ FilledAmount == owned` correlation validator; stop emitting `divergence=True` for non-source filled tiers (memo §8 last bullet).
- [ ] **Step 4: Run — pass.** Re-run the existing `test_reconcile.py` suite; fix any assertions that encoded the old per-crid divergence behavior.
- [ ] **Step 5: Commit** — `fix(brokers): reconcile filled tiers per-uic, drop false divergence (saxo-oco stage1)`.

---

## Task 3: `placement_planner.py` — entry-only brackets + journal enrichment (Bug-B at source)

**Memo:** §10 (placement_planner), §7 (planned line carries tp+tier_index+gen), §12 (`TestEntryBracketIsEntryOnly`).

**Files:** Modify `placement_planner.py`; Test `test_placement_planner.py`.

**Interfaces:**
- Produces: entry brackets with `take_profit=None AND stop_loss=None` on every tier; `_journal_tier` writes `planned {entry_crid, uic, tier_index, side, stop_price, take_profit, gen}`; each tier surfaces its in-band `tp` + `tier_index` (`tp_planned_in_oco`, replacing `tp_placed_as_child`).
- Consumed by: Task 4 (fold reads these `planned` lines).

- [ ] **Step 1: Write failing test — `TestEntryBracketIsEntryOnly`.** Plan a 2-tier pick; assert every placed entry bracket request has BOTH `take_profit is None` AND `stop_loss is None`.
- [ ] **Step 2: Run — fail** (planner promotes the in-band TP to a child today).
- [ ] **Step 3: Set `take_profit=None` always** in the entry bracket build; rename `tp_placed_as_child`→`tp_planned_in_oco`; surface each tier's in-band TP + `tier_index` on `PlacementPlan`/`TierPlacement`.
- [ ] **Step 4: Run — pass.**
- [ ] **Step 5: Write failing test — `TestJournalTierRecordsStopTpTierIndex`.** After `_journal_tier`, the `planned` line for a tier carries `stop_price`, `take_profit`, `tier_index`, `gen`, `uic`, `entry_crid`.
- [ ] **Step 6: Run — fail. Step 7: Extend `_journal_tier`** to record tp+tier_index+gen (today it writes stop only). **Step 8: Run — pass.**
- [ ] **Step 9: Confirm `TestDisasterStopExactlyOnce` still passes**; update `_operator_report` wording (TP is operator-managed in Stage 1). Run the whole `test_placement_planner.py`.
- [ ] **Step 10: Commit** — `feat(brokers): entry-only brackets + journal tp/tier_index (saxo-oco stage1, kills Bug-B source)`.

---

## Task 4: Journal fold → `_fold_planned_exits` + `PlannedExit` + gen-stamped refs

**Memo:** §4.5 (ref helpers), §7 (fold + PlannedExit), §12 (`TestFoldPlannedExitsPricesOnly`).

**Files:**
- Modify: `position_manager.py` (define `PlannedExit`), `control_loop.py` (`_fold_planned_exits`, `_exit_stop_ref`, `_exit_tp_ref`, per-uic `gen` persistence)
- Test: `test_control_loop.py` (fold + refs)

**Interfaces:**
- Produces: `PlannedExit(uic, entry_crid, side, stop_price, tp_price, conflicting, n_plans)` + `next_gen()` (reads/increments the persisted per-uic resize counter); `_fold_planned_exits(lines) -> dict[int, PlannedExit]` (keyed by uic, prices only, NO protected set); `_exit_stop_ref(crid, gen)="{crid}-stop-{gen}"`, `_exit_tp_ref(crid, gen)="{crid}-tp-{gen}"`.
- Consumed by: Tasks 5, 6.

- [ ] **Step 1: Write failing test — `TestFoldPlannedExitsPricesOnly`.** Given `planned` lines (incl. tp+tier_index) for two tiers on one uic, `_fold_planned_exits` returns `{uic: PlannedExit(...)}` with the stop/tp prices and `conflicting=False`; and it returns NO `frozenset` protected set. An `intent`/`placed` line contributes NOTHING to the result.
- [ ] **Step 2: Run — fail** (`_fold_standalone_stop_journal` still returns the protected set / keys by crid).
- [ ] **Step 3: Add `PlannedExit` to position_manager.py; write `_fold_planned_exits` keyed by uic** returning prices only; drop the `frozenset[str] protected` return entirely. Fold `conflicting`/`n_plans` when >1 distinct active plan folds to a uic. Governing stop = max stop for a long (tightest) with an alert flag if tiers disagree (memo §8).
- [ ] **Step 4: Run — pass.**
- [ ] **Step 5: Write failing test — `TestGenStampedRefChangesOnResize`.** A resize increments `gen` → distinct ref; a same-size retry keeps `gen` (same ref). `next_gen()` persists append-only per uic.
- [ ] **Step 6: Run — fail. Step 7: Implement `_exit_stop_ref`/`_exit_tp_ref` + persisted per-uic `gen`** (memo §4.5). **Step 8: Run — pass.**
- [ ] **Step 9: Commit** — `feat(brokers): planned-exits fold (prices only) + gen-stamped refs (saxo-oco stage1)`.

---

## Task 5: `position_manager` — `reconcile_protection` + `_reconcile_long` + new Actions (rung 0↔1)

**Memo:** §6 (algorithm), §10 (Actions), §12 (decision table + BOTH bug repros). **This is the core of the fix.**

**Files:** Modify `position_manager.py`; Test `test_position_manager.py`.

**Interfaces:**
- Produces: `ProtectionView(long_positions, all_positions, sell_legs_by_uic, planned_by_uic, oco_unsupported)`; Actions `PlaceStop(uic,side,qty,stop_price,request_id,supersede_ids=(),cancel_conflicting=())`, `CancelSellLegs(uic,order_ids,reason)`, `UpgradeToOco(...)` (defined, NOT emitted in Stage 1); pure `reconcile_protection(view) -> list[Action]` + `_reconcile_long`.
- Consumes: `PlannedExit` (Task 4), enriched `OrderState`/`Position` (Task 1).
- Note: `PlaceStandaloneStop` Action is removed (the place is inside `PlaceStop`). `advance`'s FILLED branch no longer places a stop; `advance` keeps only divergence/terminal/round-trip routing.

- [ ] **Step 1: Write the two red-first BUG REPRODUCTIONS (must fail on current code):**
  - `TestBugARetryAfterFailedPost` — GIVEN a netted long `owned=46`, a journaled plan, and **no SELL leg on the uic** — WHEN `_reconcile_long` runs — THEN it returns `PlaceStop(qty=46, ...)` (retry), NOT `NoOp`.
  - `TestBugBLoneTpForcesCancelBeforeStop` — GIVEN `owned=46` with **one live SELL Limit (TP, Amount 46), no stop** — WHEN `_reconcile_long` runs — THEN `PlaceStop(uic, "SELL", 46, stop_price, ref, cancel_conflicting=(tp_id,))` (cancel the lone TP before placing the stop), NOT `NoOp`.
- [ ] **Step 2: Run — both fail** (today: no `_reconcile_long`; the naive path reads "protected").
- [ ] **Step 3: Implement `ProtectionView`, the new Actions, `reconcile_protection`, and `_reconcile_long`** exactly per memo §6: arms = deficit (naked/grow/lone-TP/stale-partial → place-first `supersede_ids`, or `cancel_conflicting` for a lone TP), over-hedge (place residual stop FIRST then `CancelSellLegs`), orphan-sweep (SELL legs on a uic with no long → `CancelSellLegs`), negative-position (`AlertOnly` short), no-plan (`AlertOnly`), conflicting (`AlertOnly` refuse-to-merge). **Covered branch returns `NoOp` in Stage 1** (`_oco_enabled()` False → no `UpgradeToOco`). Use `_QTY_EPS=0.5` tolerance everywhere.
- [ ] **Step 4: Run the two bug repros — pass.**
- [ ] **Step 5: Write the decision-table tests** (memo §12): `TestNakedPlacesStopSizedToNettedOwned`, `TestCoveredIsNoOp`, `TestCrashAfterPlaceIsNoOp`, `TestGrowUnderCoversResizesPlaceFirst` (asserts `supersede_ids`), `TestOverHedgePlacesResidualBeforeCancel` (residual `PlaceStop` before `CancelSellLegs`; `_group_with_partial_fill` selects by `filled_quantity`), `TestOrphanExitOnFlatUicSwept`, `TestNegativePositionAlerts`, `TestNoPlanAlerts`, `TestConflictingPlansRefuseMerge`, `TestSizesToNettedOwnedNotPlanned`, `TestFloatToleranceNoFlicker` (`owned=46.0`, `stop_qty=45.9999999` → `NoOp`).
- [ ] **Step 6: Run — pass.** Migrate any `test_position_manager.py` tests referencing `DisasterStop`/`PlaceStandaloneStop`/`disaster_stops`/`protected_request_ids` to the new types; delete ones that asserted the deleted protection-from-journal behavior.
- [ ] **Step 7: Commit** — `feat(brokers): broker-state reconcile_protection kills Bug A + Bug B (saxo-oco stage1)`.

---

## Task 6: `control_loop` — executor + `build_protection_view` + throttle + `run_once` wiring

**Memo:** §6 (executor), §5 (idempotent cancel + throttle), §9, §10 (control_loop). §12 executor tests.

**Files:** Modify `control_loop.py`; Test `test_control_loop.py`.

**Interfaces:**
- Produces: `build_protection_view(broker, records) -> ProtectionView` (ONE snapshot: `get_long_positions()`+`list_working_sell_orders()`+`_fold_planned_exits`+persisted `oco_unsupported`); `_make_protection_executor` (executes `PlaceStop`/`CancelSellLegs`/`AlertOnly`/`NoOp` per memo §6, `UpgradeToOco` guarded to skip when not enabled); `run_once` protection pass after the terminal/cancel `advance` loop, each action inside its own `BrokerError` boundary; alert throttle by `(uic, reason)` + per-uic consecutive-failure counter/backoff; idempotent `cancel_order`; `TickReport.exits_placed`.
- Removes: `_make_standalone_stop_placer` (the intent-before-post placer), `_fold_standalone_stop_journal`, the `intent`/`placed`-as-protection writes.

- [ ] **Step 1: Write the red-first executor integration test — `TestFailedPostLeavesNoProtectionAndRetries`.** Tick 1: `place_standalone_stop` raises `BrokerError`; assert NO protection is recorded and the tick survives (per-action boundary). Tick 2: fresh `build_protection_view` still shows no live stop → the executor re-issues the place. *(On today's code the orphan `intent` line makes tick 2 a permanent `NoOp` — Bug A.)*
- [ ] **Step 2: Run — fail.**
- [ ] **Step 3: Implement `build_protection_view` + `_make_protection_executor`** per memo §6: for `PlaceStop` — cancel `cancel_conflicting` first (lone TP), execute-time owned re-check (`get_positions_by_uic`; clip/skip if position gone — never oversell/never plant on flat), `place_standalone_stop`, then cancel `supersede_ids` AFTER the place succeeds; `_is_sell_orders_already_exist` → alert+defer (retry next tick), other `OrderRejectedError` re-raise. KILL allows a protective `PlaceStop` (it only reduces exposure); `UpgradeToOco` is skipped (Stage 1). Wire the protection pass into `run_once` after the existing terminal/cancel `advance` loop.
- [ ] **Step 4: Run — pass.**
- [ ] **Step 5: Write the remaining executor tests** (memo §12): `TestExecuteTimeRecheckSkipsFlatUic`, `TestKillAllowsProtectiveStop`, `TestSellOrdersAlreadyExistDefersNotCrashes`, `TestIdempotentCancelNoThrash`, `TestAlertThrottleByUicReason`, `TestPerCallBrokerErrorBoundary`, `TestLoopIteratesPositionsNotVerdicts`.
- [ ] **Step 6: Run — pass.**
- [ ] **Step 7: Delete/rewrite the obsolete suites** — `test_control_loop.py::TestStandaloneStopJournalFold` and `TestStandaloneStopPlacerRecovery` (they encoded the deleted intent/placed→protected behavior). Make idempotent `cancel_order` treat `404/OrderNotFound/already-cancelled` as success.
- [ ] **Step 8: Commit** — `feat(brokers): broker-state protection executor + throttle in control loop (saxo-oco stage1)`.

---

## Task 7: Wiring, cleanup, and full-suite/pyright/bandit/ruff green

**Memo:** §10 (build_default_deps, TickReport), §12 (FakeBroker migration).

**Files:** `control_loop.py` (`build_default_deps`), `test_broker_contract.py` (`FakeBroker`), any remaining references; then full validation.

- [ ] **Step 1: Update `build_default_deps`** — wire `build_protection_view` + `_make_protection_executor`; keep the `SupportsStandaloneStop` capability gate; do NOT require `SupportsOcoExit` (Stage 2). Rename `TickReport.stops_placed` → `exits_placed` and fix all references.
- [ ] **Step 2: Migrate `FakeBroker`** in `test_broker_contract.py` to implement `get_long_positions` / `list_working_sell_orders` / `get_positions_by_uic` and the enriched `OrderState`; give `OrderState` field defaults so existing constructions still pass.
- [ ] **Step 3: Grep for stragglers** — `disaster_stops`, `protected_request_ids`, `PlaceStandaloneStop` (Action), `_make_standalone_stop_placer`, `_fold_standalone_stop_journal`, `standalone_stops.jsonl` intent/placed-as-protection reads — remove or migrate each.
- [ ] **Step 4: Run the automanager + broker suites** — `uv run python -m unittest discover -s apps/alphalens-research/tests/brokers -t apps/alphalens-research -v`. All green.
- [ ] **Step 5: Run the FULL research suite** — `uv run python -m unittest discover -s apps/alphalens-research/tests -t apps/alphalens-research` (catches dependency-direction, no-raw-http, layer-status guard tests).
- [ ] **Step 6: `uv run pyright` (whole workspace), `uv run bandit -c pyproject.toml -r apps/alphalens-pipeline/alphalens_pipeline/brokers`, `uv run ruff check apps/alphalens-pipeline apps/alphalens-research`.** Fix all findings (these are the CI gates that per-package test runs miss).
- [ ] **Step 7: Commit** — `chore(brokers): wire Stage-1 protection deps + suite/pyright/bandit/ruff green (saxo-oco stage1)`.

---

## Self-Review (author checklist — completed)

- **Spec coverage:** the 8 Stage-1 steps in memo §11.1 map to Tasks 1–7 (adapter=1, reconcile blocker=2, planner=3, fold+refs=4, reconciler=5, executor+wiring=6, finalize=7). The two headline bug repros (§12) are in Tasks 5 (pure) + 6 (integration); `TestEntryBracketIsEntryOnly` in Task 3.
- **No rung-2 leak:** Global Constraints + Task 5 Step 3 pin `NoOp` on the covered branch and `_oco_enabled()==False`; `place_oco_exit`/`SupportsOcoExit` are explicitly Stage 2 and not built here.
- **Type consistency:** `PlannedExit` defined in Task 4 (position_manager), consumed in 5/6; `ProtectionView`/`PlaceStop`/`CancelSellLegs` defined in Task 5, consumed in 6; enriched `OrderState`/`error_code`/filters defined in Task 1, consumed in 5/6/7. `next_gen()`/`_exit_stop_ref` in Task 4, used in 5.
- **Placeholders:** none — bulk code lives verbatim in the memo (present in the worktree); each task inlines its red-first test(s) and the exact interface it produces.

---

## Execution Handoff

Plan complete and saved. Execution: **subagent-driven-development** in the worktree `.claude/worktrees/saxo-oco-exit` (created off `origin/main` with its own `uv sync`; copy the design memo in and commit it first). Fresh implementer per task + task review (spec + quality), broad whole-branch review at the end, then zen pre-merge (`deepseek/deepseek-v4-pro`, thinking=high), CI green, merge.
