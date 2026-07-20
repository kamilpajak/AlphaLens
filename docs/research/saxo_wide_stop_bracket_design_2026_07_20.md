# Saxo wide disaster-stop — bracket placement design (T4 follow-up)

**Status:** DESIGN (no implementation). Produced by the design-first workflow 2026-07-20 (Saxo-rule research + code audit + setup-geometry) cross-checked against an independent Perplexity deep-research pass. Triggered by T4: production `broker submit` rejected 0/3 with `TooFarFromEntryOrder` because the ADR-0013 T7 disaster stop sits 20-30% below entry, beyond Saxo's (undocumented, instrument-specific) bracket child-distance band; precheck was false-green.

---

# Saxo wide disaster-stop: bracket-child rejection — design memo

**Status:** DRAFT · 2026-07-20 · author: research session · Related: PR #872 (`__nextPoll`), PR #873 (closedposition field names), ADR 0013 (trade-side layers), ADR 0014 (broker contract), `docs/research/saxo_broker_layer_design_2026_07_17.md`

## Problem

Live Saxo SIM run `alphalens broker submit S --date 2026-07-13 --equity 2000 --execute` was **REJECTED 0/3** with HTTP 400 `TooFarFromEntryOrder: Order price is too far from the entry order`. Nothing was placed; account stayed flat.

The setup decomposed into 3 laddered tiers:

| tier | entry limit | shared disaster stop | stop distance | take-profit |
|---|---|---|---|---|
| 0 | 18.08 | 12.57 | −30.5% | 18.81 |
| 1 | 16.68 | 12.57 | −24.6% | 19.30 |
| 2 | 15.81 | 12.57 | −20.5% | 21.40 |

Each tier is emitted as one 3-way OCO bracket (`decompose_setup_plan`, `brokers/execution.py:235-246`), the **same** disaster-stop scalar `12.57` copied onto every tier (`stop_loss=setup_plan.disaster_stop`, line 241). `_build_bracket_body` (`brokers/saxo/broker.py:710-721`) turns each `stop_loss` into a `StopIfTraded` OCO **child** inside `body["Orders"]`. That child sits 20–30% below its tier entry — far beyond Saxo's child-distance band — and Saxo 400s it per-leg.

**Two failures, not one:**
1. The wide stop is attached as a **bracket child** of each entry, but geometrically it is a whole-ladder invalidation level, never a per-entry protective stop.
2. **Precheck is a genuine FALSE-GREEN.** `POST /trade/v2/orders/precheck` does not run the child-distance business rule, so `_precheck_or_raise` (`broker.py:745-760`) returned `Ok` for all 3 while the real POST 400s. Earlier first-fill/T1/T2 tests only passed because they used hand-crafted ~3% stops that happened to fit the band.

Capital deployment is **off-table** (no live capital). Priority is correctness, cleanliness, and an honest fail-fast — not feature speed. There is no urgency.

## Saxo constraint (from rule research)

- `TooFarFromEntryOrder` is a **business-rule** rejection (not `InvalidModelState`), returned **per-leg** during multi-leg validation on `POST /trade/v2/orders`. It fires ONLY on a related/child order inside an IfDone/OCO `Orders` array, measured against the **entry order price** as the assumed market (confirmed by Saxo's help note: related-order price is validated against the entry order price). A valid entry can be accepted while a too-distant child is rejected; the sibling then returns `OrderNotPlaced`.
- The exact cutoff is **NOT a documented fixed percentage**. Saxo publishes only *minimal* order distances (`InstrumentDetails.OrderDistances`) and *default* distances (`EntryDefaultDistance`/`StopLossDefaultDistance`, `DistanceType` Percent or Pips) — **no maximum / "too far" threshold anywhere**. The cap is instrument- and asset-class-specific, driven by per-instrument reference data plus Saxo internal risk rules, and is **not client-configurable** via OpenAPI. The team's empirical ~5% window is consistent with an undocumented instrument bound. **Do NOT hardcode "5%".**
- **A standalone protective stop far below market is normal and supported.** A `StopIfTraded` posted as its own `POST /trade/v2/orders` with **no `Orders` array and no entry** cannot trigger `TooFarFromEntryOrder` (there is no entry order to measure against); it is validated against current market, and a protective stop well below market is a standard stop-loss (not the `TooFarFromMarket` case, which the docs scope to limit orders unlikely to be met). This is Saxo's own recommended two-step pattern.
- **PositionId-related stops are netting-gated.** Relating a stop to an open position via `PositionId` works only under **End-of-Day netting**; the live SIM account is **Intraday / FifoRealTime**, where Saxo "no longer allows placing orders directly against a specific position." Not portable here.
- **IfDone/OCO does not help** — the child-distance window is exactly what fails. **Algo orders** (Iceberg/TWAP) change scheduling, not distance validation. **TrailingStopIfTraded** is market-referenced but trails, so it is not a fixed disaster stop.
- Rate limits: 1 order/sec/session; entry+2 related = one request; 15s duplicate-protection needs distinct `x-request-id`. Relevant to any two-step (entry then standalone stop) pattern.

## Setup intent (from geometry audit — the fix must stay faithful to this)

The disaster stop is a **HARD RISK BACKSTOP**: one catastrophe/invalidation stop for the **whole** laddered position, ADR 0013 **T7** position-side termination — NOT active trade management (T6 does nothing today) and NOT the intended normal exit (the TP tranches and the 42-session time-stop are). Geometry (`builder.py:152-173`): `stop = jitter(min(all entry candidates) − 1·ATR)`, floored at `blended_entry · 0.75` (`_DISASTER_FLOOR_FRAC`). It sits **below the deepest tier** by construction, so it is naturally 20–30% below the shallow tiers — **wide by design**.

One common wide stop is **required** for the equal-risk math to be coherent: the single risk unit is `R = blended_entry − S`, and `shares_i = (B·q_i)/(E_i − S)` with the **same S** for every tier (`sizing.py`). The position is sized so that if every tier fills and price reaches `S`, total loss ≈ a fixed 1% budget. A per-tier tight/OCO-child stop would **break** the equal-risk math (different R per tier) and the 1%-to-disaster budget.

> **The design never intended the wide stop as a bracket child of any tier.** It is a single invalidation level under the whole ladder. A per-tier tight stop is the real distortion. The distortion in the live path is attaching a whole-ladder backstop as an OCO child of each entry. **The wide stop should never be a bracket child.**

The current per-tier-bracket decomposition (memo `saxo_broker_layer_design_2026_07_17.md` §P2) chose order-attached exits *because* live netting (Intraday/FifoRealTime) kills fill-then-attach-exits via `PositionId`. That reasoning is sound for the **TP** child (a near, in-band limit) but the **stop** child inherits the child-distance band it structurally cannot satisfy.

## Options with honest trade-offs

### Option A — Local child-distance FAIL-FAST (guard, no placement change)

Add a client-side child-distance check in `_validate_price_relations` (`broker.py:109-134`) so a wide-stop bracket is **rejected locally, deterministically, before any network call**, on both the precheck and place paths (both route through `_build_bracket_body`, line 690-692). Closes the precheck false-green.

- **Unprotected window:** none — nothing is placed either way; this only converts a silent mid-batch 400 into a clean local reject that names the leg and the reason.
- **Orchestration cost:** ~zero. One guard function + one policy constant.
- **N tiers × FIFO:** N/A — no position is opened.
- **Reconcile shape:** untouched. No order changes.
- **Complexity:** minimal, fully hermetic-testable.
- **Honest limitation:** it does **not** place the wide-stop setups. It makes the failure honest and cheap instead of a false-green followed by a mid-batch 400. Given capital is off-table, "these setups are not placeable as brackets and we say so cleanly" is an acceptable end state on its own.

### Option B — Entry (+ optional near TP child) then STANDALONE position-level backstop after fill

Decouple the stop from the bracket. Emit brackets with `stop_loss=None` (entry + TP-only, or entry-only), carry `setup_plan.disaster_stop` out-of-band, and after the entry fills place ONE standalone `StopIfTraded` on the net filled position as its own `POST /trade/v2/orders` (no `Orders` array, no entry). This is **faithful to intent** — one backstop at 12.57 on the aggregate position is exactly what T7 describes.

- **Unprotected window (the core cost):** there is a real entry-fill → place-stop gap, and **zero orchestration exists today** (no fill monitor, no place-stop-after-fill anywhere; the only reconciliation is the read-only on-demand `alphalens broker reconcile`). Bounding options: (i) place the stop **synchronously immediately** after detecting the fill inside a new CLI place loop; (ii) accept the window (capital off-table → tolerable for SIM). A background fill-monitor loop/timer is **net-new infrastructure**.
- **N tiers × FIFO netting:** under Intraday FIFO only some tiers may fill (conditional fills run 2-3 deep). A full-planned-size standalone stop would over-hedge / flip short on the unfilled remainder, so it **must be sized to realized filled qty** — which requires knowing fills first (hence the fill-detection step).
- **OCO / double-exit:** a decoupled stop is **not in the OCO group** with the per-tier TP children, so a TP fill and a stop trigger can both execute (double-exit) unless a new OCO grouping or cancel-on-fill is built.
- **Reconcile shape:** the closedposition row is keyed by the **opening** leg (`OpeningExternalReferenceId == entry client_request_id`, `reconcile.py:497-504`); a standalone stop is a **closing** leg (`ClosingExternalReferenceId`), so the FIFO-pair join still matches per-entry and **does not regress #872/#873** field shape. BUT: (a) `compute_realized_r(ClosingPrice, entry, stop)` reads the **per-tier journaled `stop`** — with `stop_loss=None` on the bracket record, the disaster-stop price must be **journaled out-of-band** or realized_r breaks; (b) FIFO closes the **oldest lot first**, so one net stop makes per-tier r attribution ill-defined (cardinality caveat: closedposition **row count** may vary, row **shape** does not — allow N:M mapping, never assume 1:1).
- **Complexity:** high — new `SaxoBroker` standalone-stop method, new CLI place-then-stop orchestration, per-tier fill sizing, out-of-band stop journaling, a new cohort boundary (`execution_config_version` bump). **Not worth building now** for a no-capital project; document as the target design gated by a SIM live-probe.

### Option C — Bracket TP-only child + separate standalone stop placed immediately (no fill wait)

Variant of B that places the standalone stop right after the entry POST, without waiting for a fill. Removes the fill-detection loop but **re-introduces over-hedge risk** (stop sized to planned, not filled, qty → flips short if tiers do not fill) and leaves the stop live against an unfilled entry. Strictly worse than B for correctness; **rejected**.

### Option D — PositionId-related stop (EoD netting)

Relate the stop to the open position via `PositionId`. **Not available** under the live Intraday/FifoRealTime netting mode. **Rejected** (netting-gated, not portable).

### Option E — IfDone/OCO restructure · Option F — TrailingStopIfTraded · Option G — Algo (Iceberg/TWAP)

E does not help (child-distance is exactly what fails). F is market-referenced but trails, so it is not a fixed invalidation stop and breaks the equal-risk `R`. G changes execution scheduling, not distance validation. **All rejected.**

## Recommendation

**Adopt the architectural principle plainly: the wide disaster stop must never be a Saxo bracket child.** It is a whole-ladder invalidation level (ADR 0013 T7), and the equal-risk math shares one `S` across all tiers — a per-tier child stop is the distortion, not the wide distance.

Sequence the work as:

1. **Ship Option A now** (local child-distance fail-fast + close the precheck false-green). It is the correctness floor: cheap, deterministic, hermetic, zero network, and it guarantees we never emit a plan that 400s mid-batch again. Given capital is off-table, an honest "not placeable as a bracket" reject is a legitimate end state.
2. **Treat Option B as the target design, deferred.** It is the only option that actually protects the position and is faithful to intent, but it costs net-new fill-monitor orchestration, per-tier fill sizing under Intraday FIFO, a new OCO grouping vs the TP children, out-of-band stop journaling, and care to preserve the `client_request_id` round-trip that #872/#873 validated. **Do not build it before** an attended SIM live-probe confirms the standalone-`StopIfTraded`-far-below-market primitive passes, and a decision that the orchestration is worth it for a no-capital project.

This matches all three research inputs: the code audit's "honest near-term move is (A) as a guard plus a decision memo on whether (B) is worth the netting/reconcile complexity"; the rule research's "cleanest fix = standalone stop, respect the two-step pattern"; and the geometry audit's "standalone position-level backstop is faithful to intent; the child attachment is the distortion."

## Code changes

### Phase 1 — Option A (ship)

1. **`brokers/execution.py`** — add policy constant so it flows into `execution_config_version()` automatically (namespace sweep → ADR 0013 R3 forward-only cohort boundary; pre-fix and post-fix rows never pool):
   > **SHIPPED (PR #874):** the constant is a FRACTION `0.15` (15%) applied to BOTH
   > children, not the `4.0`% stop-only sketch below. 15% sits in the GAP between
   > legitimate hand-tight children (~3%, first-fill) and the disaster-stop-as-child
   > case (20-30%, T4) — it never false-rejects a normal near child and always catches
   > the architectural mistake, without pretending to mirror Saxo's exact undocumented
   > ~5% limit (borderline 5-15% children still rely on Saxo's own server-side check).
   ```python
   # Client-side guardrail ONLY — NOT Saxo's real engine bound (which is
   # instrument-specific and undocumented; never hardcode "5%"). Its job is to
   # convert a KNOWN-bad wide disaster stop into a clean local reject, chosen
   # conservatively in the GAP between legitimate ~3% children and 20-30% disaster
   # stops. A wide whole-ladder backstop must be placed standalone (Option B),
   # not as a bracket child.
   _MAX_CHILD_DISTANCE_FRAC = 0.15
   ```
   Add `"max_child_distance_frac": _MAX_CHILD_DISTANCE_FRAC` to the `config` dict in `execution_config_version()`.
2. **`brokers/saxo/broker.py::_validate_price_relations` (109-134)** — after the ordering checks, add a child-distance check on the QUANTIZED prices, applied symmetrically to BOTH the stop and take-profit child (fires for BUY and SELL):
   ```python
   for label, child_q in (("stop", stop_q), ("take-profit", tp_q)):
       if child_q is None:
           continue
       dist_frac = abs(entry_q - child_q) / entry_q
       if dist_frac > execution_policy._MAX_CHILD_DISTANCE_FRAC:
           raise OrderRejectedError(
               f"{symbol}: {label} child {child_q} is {dist_frac * 100:.1f}% from entry "
               f"{entry_q}, beyond the {execution_policy._MAX_CHILD_DISTANCE_FRAC * 100:.0f}% "
               "bracket child-distance guardrail — a wide disaster stop must be placed as a "
               "standalone position-level order, not an OCO child "
               "(saxo_wide_stop_bracket_design_2026_07_20; Saxo TooFarFromEntryOrder)"
           )
   ```
   Because both `precheck_bracket_order` (327) and `place_bracket_order` (302) call `_build_bracket_body` (which calls `_validate_price_relations` at 690-692), this closes the false-green on **both** paths before any network call. No new method, no contract change.
3. **No `contract.py` / `reconcile.py` change** in Phase 1.

### Phase 2 — Option B (design only, deferred; sketch for the decision memo)

- **`contract.py`** — either keep `BracketOrderRequest.stop_loss` and set it `None` at decompose time (entry+TP bracket), or add a separate `StandaloneStopRequest` dataclass `{instrument, side(opposite), quantity, stop_price, client_request_id}`. House rule = no back-compat, single commit.
- **`brokers/execution.py::decompose_setup_plan` (235-246)** — emit brackets with `stop_loss=None`; return the `disaster_stop` out-of-band alongside the bracket list so the CLI can place the standalone stop and **journal the stop price** (realized_r depends on it).
- **`brokers/saxo/broker.py`** — new `place_standalone_stop(instrument, side, quantity, stop_price, client_request_id) -> PlacedOrder` that POSTs a single `StopIfTraded` with **no `Orders` array**; `_build_bracket_body` already emits no stop child when `stop_loss is None` (710-721 guard).
- **`alphalens_cli/commands/broker.py::_place_and_record` (557-604)** — after placing entry brackets, detect realized filled qty (`broker.get_order` / `get_positions`), size ONE standalone stop to that qty, place it, and record `"stop"` per the out-of-band disaster-stop price so `compute_realized_r` still works.
- **`reconcile.py`** — no field renames; add a test (below) that N-tier open + standalone-stop close keeps closedposition/FinalFill shape and allows row-count to vary (no 1:1 tier↔closedposition assumption).

## Fail-fast plan (never send a plan that will 400 mid-batch)

- **Where:** `_validate_price_relations` (`broker.py:109-134`), reached from `_build_bracket_body` (690-692) on the quantized `entry_q`/`stop_q`. This is the single choke point both precheck and place traverse, so the guard closes the precheck false-green deterministically and spends no network call.
- **What it catches:** any bracket whose stop OR take-profit child is beyond `_MAX_CHILD_DISTANCE_FRAC` from its entry — i.e. exactly the S-2026-07-13 case (30.5% / 24.6% / 20.5% all rejected locally).
- **Message:** names the leg, the measured distance, the band, and directs to the standalone-stop design + the Saxo `TooFarFromEntryOrder` cause (see snippet above). Raised as `OrderRejectedError`, so `_place_and_record`'s `except BrokerError` journals the clean reject with the reconcile hint and the CLI exits non-zero — no partial placement.
- **Why not hardcode 5%:** the real cap is instrument-specific and undocumented. The constant is an intentionally conservative guardrail INSIDE the safe band whose only job is to turn a known-bad wide stop into a clean reject; it is not a model of Saxo's engine. If a precise number is ever needed, derive minimal distances from `InstrumentDetails.OrderDistances` and discover the max empirically per instrument or via Saxo support.
- **Optional hardening:** mirror the same check pre-decompose (over `setup_plan.disaster_stop` vs each `tier.limit_price`) so the CLI reports the whole plan unplaceable in one shot rather than tier-by-tier — nice-to-have, not required (the choke-point guard already prevents any 400).

## TDD + live-probe plan

**Hermetic `unittest.TestCase`, red-first** (research CI is `unittest discover`, NOT pytest — pytest-style tests silently skip):

Phase 1 (Option A):
1. `test_validate_price_relations_rejects_wide_stop_child` — BUY, entry 18.08, stop 12.57 → `OrderRejectedError` whose message contains the distance and "standalone". Red first (today only ordering is checked, so 12.57 < 18.08 passes).
2. `test_validate_price_relations_rejects_wide_stop_child_sell` — SELL symmetric case.
3. `test_validate_price_relations_allows_near_stop_child` — entry 18.08, stop 17.55 (~2.9%) → no raise (protect the T1/T2 hand-crafted-stop path).
4. `test_build_bracket_body_wide_stop_raises_before_any_client_call` — mock `SaxoClient`; assert `place_order`/`precheck_order` are NEVER called when the stop is too far (no network spent).
5. `test_precheck_wide_stop_also_rejects_locally` — the precheck path raises too → the false-green is closed on both paths.
6. `test_S_20260713_regression` — reconstruct the exact 3-tier plan (entries 18.08/16.68/15.81, stop 12.57), decompose, and assert all 3 brackets reject locally with the guard message. Anchors the incident.
7. `test_execution_config_version_bumps_with_new_constant` — adding `_MAX_CHILD_DISTANCE_FRAC` changes the poolability token (forward-only cohort boundary; existing rows never restamped).

Phase 2 (Option B — only if built):
8. `test_standalone_stop_body_has_no_orders_array_and_no_entry` — the standalone `StopIfTraded` body carries no `Orders` and no entry, so `TooFarFromEntryOrder` cannot fire.
9. `test_decompose_emits_none_stop_and_carries_disaster_stop_out_of_band`.
10. `test_realized_r_survives_none_bracket_stop` — with `stop_loss=None` on the bracket record, `compute_realized_r` still uses the out-of-band journaled disaster-stop price.
11. `test_reconcile_N_tiers_closed_by_standalone_stop_shape_unchanged` — open N tiers, close via one standalone stop; assert `closedposition`/`FinalFill` FIELD shape unchanged (the #872/#873 surface) while allowing closedposition ROW COUNT to vary (no 1:1 tier↔row).

**Attended SIM live-probe (cheap, decisive — capital off-table):**
- **Primary:** on an already-open SIM stock long, place a single Sell `StopIfTraded` ~25% below market as its OWN `POST /trade/v2/orders` (no `Orders`, no entry). Confirm it is **NOT** rejected with `TooFarFromEntryOrder`. This de-risks the entire Option B premise in one order.
- **Netting check:** read the SIM account `PositionNettingMode`/`PositionNettingProfile`; confirm Intraday/FifoRealTime (so Option D / PositionId-relation stays off the table).
- **If Option B is built:** open 2-3 tiers via entry limits, let them fill, place one standalone stop sized to REALIZED filled qty, run `alphalens broker reconcile`, and confirm the `OpeningExternalReferenceId` per-entry join still matches, realized_r computes from the out-of-band stop, and the `FinalFill`/closedposition shape is unchanged (row-count may differ from tier-count). Verify `__nextPoll` pagination (#872) and closedposition field names (#873) still parse.

## Risks + open questions

**Risks**
- The child-distance cap is instrument-specific and undocumented; the guard threshold may false-reject a legitimately near stop on a tight-band instrument or (less likely, since it is conservative) false-pass on an unusually strict one. It is a guardrail, not Saxo's engine — treat rejections as "route to standalone", not "Saxo would definitely 400".
- Option B's entry-fill → place-stop window has **zero orchestration today**; if built naively the position is momentarily unprotected. Capital off-table makes this tolerable on SIM but it is the core correctness cost.
- Partial-ladder fills under Intraday FIFO require sizing the standalone stop to realized qty; a planned-qty stop over-hedges / flips short.
- Decoupled stop + per-tier TP children are not in one OCO group → double-exit risk without cancel-on-fill or a rebuilt OCO group.
- One net standalone stop makes per-tier realized-r attribution ill-defined (FIFO closes oldest lot first); may force aggregate-only r for the standalone cohort (isolated by the token bump).

**Open questions**
1. One **net** standalone backstop (fewer orders, loses per-tier attribution) vs **per-tier** standalone stops (preserves attribution, N stops + N fill detections + N `x-request-id`s)?
2. Does the fail-fast belong only in `_validate_price_relations`, or also pre-decompose so the CLI reports the whole plan unplaceable in one shot?
3. Is Option B worth building at all while capital is off-table, or is Option A (clean reject) the correct terminal state until capital is on the table? (Recommendation: A now; B is a documented target, not scheduled.)
4. If Option B ships, is the unprotected window bounded by a synchronous place-after-fill in the CLI loop, or does it need a background fill-monitor timer (net-new infra)?
5. Should the guardrail constant later be replaced by a per-instrument bound discovered from `InstrumentDetails.OrderDistances` + empirical probing, or is a single conservative constant sufficient forever (since the real answer is "don't attach wide stops as children at all")?

---

## Cross-check — independent Perplexity deep-research (2026-07-20)

Perplexity (18 sources) and the workflow's Saxo-rule agent **converge**:
- `TooFarFromEntryOrder` is a **child-only, entry-referenced** rule; a **standalone stop against an open position escapes it** (no entry parent to compare). The exact limit is **undocumented, instrument/asset-class-specific — do NOT hardcode 5%**. Precheck is **false-green** (single-order precheck skips child-distance).
- A standalone/decoupled stop **does not change** the FinalFill/closedposition audit shape validated in #872/#873.

**Workflow-only refinements (stronger than Perplexity):**
- **PositionId-relation is END-OF-DAY-netting ONLY** — our account is `FifoRealTime` (Intraday), so a position-attached stop is **unavailable**; Option B must use a **plain standalone StopIfTraded**, not a PositionId relation.
- Cardinality: per-tier vs one net standalone stop changes the **closedposition row COUNT** (not shape) — reconcile must not assume 1:1 tier→closedposition. Rate limits: 1 order/sec, 15s `x-request-id` dedup.

**Perplexity-only lead worth verifying (added to open questions):**
- `POST /trade/v2/orders/**multileg/precheck**` may run the child-distance check that the single-order `/orders/precheck` skips — i.e. a **server-side** catch for the false-green. The workflow's Option A (client-side fail-fast) is deterministic and endpoint-independent, so it stays the primary fix; but if `multileg/precheck` does catch it, use it as an additional server-side guard. **Verify in SIM.**



---

## Option B primitive — SIM-validated (2026-07-20, live probe)

Decisive cheap probe run per the saxo-agent recommendation: opened a KO long qty 2 on SIM, then placed a
**standalone Sell `StopIfTraded` @ 61.36 (~25% below the ~81.82 market)** as its own `POST /trade/v2/orders`
(no `Orders` array, no entry parent), via the canonical `SaxoClient.place_order`.

- **precheck**: status 200, `PreCheckResult=Ok`.
- **place**: status 200, `OrderId=5039296412`, **no `ErrorInfo` — NO `TooFarFromEntryOrder`**.
- open-orders confirms: `StopIfTraded Sell 61.36 qty 2 Working relation=StandAlone`.

**Conclusion:** the `TooFarFromEntryOrder` child-distance rule does NOT fire on a standalone order — a wide
disaster stop IS placeable as a standalone position-level stop. Option B's PRIMITIVE is proven; the remaining
Option B work is purely the ORCHESTRATION (fill-monitor, per-tier sizing under Intraday FIFO, OCO grouping vs
TP children, out-of-band journaling for realized_r, unprotected-window bounding). Cleaned up: stop cancelled,
long closed, account flat.
