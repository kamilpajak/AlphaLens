# Trailing-execution SIM probe runbook — 2026-08-07 ~13:30 UTC (open)

**Status:** GATE + HIGH batteries RUN 2026-08-07 (results below; full analysis + memo corrections in `trailing_execution_design_2026_08_07.md` §8b). Companion to that memo §9. Attended, market-open, **daemon PAUSED** (operator runs `systemctl --user stop/start alphalens-broker-manager` — the classifier blocks the assistant from `systemctl`). Every probe uses a small THROWAWAY position on a liquid US name (Ford, uic 486), with a hard `finally` flatten. SIM / virtual money.

**Committed post-hoc 2026-08-23** as probe evidence (companion of `trailing_execution_design_2026_08_07.md`): preserves the raw GATE + HIGH battery results of 2026-08-07.


---

## Results (2026-08-07) — `probe_gate.py` + `probe_high.py`

| Probe | Verdict | One-line |
|---|---|---|
| A1 standalone TrailingStopIfTraded | ✅ PASS | precheck Ok, place 200 — accepted on netting |
| A5 trail-level readback | ✅ PASS | `/orders/me` returns `Price` + `TrailingStopDistanceToMarket`; per-id `/orders/{ck}/{id}` empty — read from `/orders/me` |
| A4 amend trailing distance | ⚠️ distance param amendable, **but level NOT repositioned** | PATCH accepted (200) changes the distance; narrow-amend (N1 below) did NOT raise the level (SIM static) → cancel+replace stays the +0.5R BE-jump route |
| N1 narrow-amend raises level | ❌ NO (SIM) | dist 1.00→0.40→0.05 all 200 but level stayed 12.91 — recompute needs a price TICK → **LIVE** |
| F4 Trigger* schema | ✅ PASS (precheck) | `BreakoutTriggerUpPrice`/`DownPrice` (+`TriggerPriceType` LastTraded\|Open) → Breakout/Limit/Stop all Ok — server-side conditional entries confirmed |
| D4 StopLimit standalone | ✅ PASS | placed (`OrderPrice` stop + `StopLimitPrice` limit) — slippage-capped stop alternative |
| A9 over-hedge (stop>owned) | ❌ REJECT `NotOwned` | can't pre-arm full-size stop → never-naked-on-add is reactive (amend after fill) |
| B2 cancel→place race | ✅ PASS | immediate replace after cancel = 200, no 2-sells race |
| F2 off-tick price | ❌ REJECT `PriceNotInTickSizeIncrements` | Saxo does NOT round → bot must tick-round (0.01) before place/amend |
| A2 / A8 native ratchet + fire | ❓ INCONCLUSIVE | SIM price static (frozen 3 min / 30 s) — no ratchet/fire inducible → **LIVE** |
| C1 2nd SELL (Limit TP) | ✅ PASS | 400 `SellOrdersAlreadyExistForOwnedContracts` |
| C2 resting BUY entry + SL | ✅ PASS | 200, 1 buy + 1 sell coexist (server-side entry survives bot-down) |
| C5 grow-SL on add | ✅ PASS (+ NEW failure mode) | +4 add → owned 10, SL still 6 = **4 naked until amend**; amend `Amount` 6→10 = 200 |
| B1 StopIfTraded→Trailing cancel→replace | ✅ PASS | naked window **~576ms** (cancel 53ms + place 523ms) |
| D2 marketable SELL Limit | ✅ mechanic (SIM synthetic) | fills immediately; both fills at `ExecutionPrice = ref` (zero spread) → **slippage baseline needs LIVE** |
| F1 min trailing distance | ○ INFO | precheck `Ok` even at dist=0.01 (no min at precheck) |
| C3 TriggerBreakout BUY | ❌ body malformed | 400 `InvalidTriggerPriceType` — needs `TriggerOrderData`/`PriceType`; follow-up F4 (not gating, Limit entry covers it) |

**Headline:** model buildable. Cancel+replace stays the +0.5R BE-jump route (~576ms, no 2-sells race) — amend changes the distance param but does NOT reposition the level on SIM (N1). Server-side conditional entries confirmed (F4); over-hedge rejected (A9); prices must be tick-rounded (F2). Deferred to LIVE: native ratchet/fire (A2/A8), real slippage (D2), whether a live tick lets narrow-amend reposition the level (N1).

**Legend:** ⛔GATE = the native-trail design cannot be locked/built unless this passes (a fail forces a fallback). ★HIGH = strong design input. ○INFO = nice-to-know / parameter discovery.

---

## Session order

0. **Market open (13:30 UTC).** Operator: `systemctl --user stop alphalens-broker-manager` (removes daemon fighting + 429 contention). Verify `is-active` = inactive.
1. **Clean up current SIM** — flatten the 5 legacy positions (YEXT/TTD/FCN/FTRE/DFIN) now that the market is open + the daemon is paused (the earlier attempt failed on 429 + daemon re-placing + closed market). Then clean their journal lines (already scripted).
2. **Run the probe battery** below, one throwaway position at a time, each self-flattening.
3. **Operator: `systemctl --user restart alphalens-broker-manager`** — back to normal.

---

## A. Native `TrailingStopIfTraded` mechanics (the trailing SL core)

- **A1 ⛔GATE — standalone placement.** Place a `TrailingStopIfTraded` SELL for a small long, below market, NOT position-attached. Expected: accepted (HTTP 20x), no `ExplicitCloseNotAllowedForIntradayNetting`, no wrong-side reject. (Fail → native trailing SL is off the table.)
- **A2 ⛔GATE — server-side ratchet.** Leave A1's order untouched (bot not managing it); watch the stop level over a few minutes as price rises. Expected: the stop level ratchets UP on its own (server-side), never loosens. Read the order repeatedly. (Fail → it's not really native trailing.)
- **A3 ○INFO — parameter units + bounds.** Inspect `TrailingStopDistanceToMarket` + `TrailingStopStep` accepted units (%, price, ticks, currency) and the min/max the API accepts vs `OrderDistances` (`StopLossDefaultDistance` etc.). Needed to express `0.6·ATR` as a valid native distance.
- **A4 ★HIGH — amend the distance → reject.** Try to AMEND the trailing distance/step after placement. Expected: rejected (order-type/strategy immutable) → confirms the policy needs cancel+replace to change the offset (drives the SL-hybrid staging).
- **A5 ⛔GATE — current-trail-level READBACK.** After A2 has ratcheted, read the order/position and confirm the API returns the CURRENT trailed stop level (and ideally the high-water state). Expected: readable. **This gates restart safety** — if the live trailed level is NOT queryable, native trailing across a bot restart is unsafe (§7 native-vs-bot divergence) → fall back to bot-managed trailing.
- **A6 ★HIGH — survives a control gap.** With the daemon paused (already), leave the native trailing stop for a while as price moves; confirm it is still present + trailed (proves bot-down survival). (Optional: briefly kill the assistant's session too — the point is nothing local touches it.)
- **A7 ○INFO — trigger price type.** From `SupportedOrderTriggerPriceTypes`, confirm which price the stop triggers on (LastTraded / Bid). Matters for fill timing.
- **A8 ★HIGH — fire behaviour.** Place the native trailing stop CLOSE below market so a small dip triggers it; observe: does it fire as a market sell, at what price vs the stop level (gap/slippage on the trigger)? Flatten if it doesn't fire.
- **A9 ○INFO — qty on partial exit.** If a tranche sells part of the position, does the native trailing stop's qty auto-adjust to remaining owned, or stay stale? (netting qty behaviour).

## B. The +0.5R cancel→replace transition (disaster `StopIfTraded` → `TrailingStopIfTraded`)

- **B1 ⛔GATE — measure the naked window.** Long + resting disaster `StopIfTraded`. Cancel it, then place the `TrailingStopIfTraded`. Time the cancel-ack → place-confirm window (mirror the shrink-sell probe). Expected: seconds, price far above the stop → low risk. Confirm no netting reject on the sequential cancel→place.
- **B2 ★HIGH — transient "2 sells"?** Confirm the cancel is fully processed before the place, i.e. no `SellOrdersAlreadyExist` if the place races the cancel. If it can race, the transition needs a confirm-cancel-before-place gate.
- **B3 ★HIGH — kill-9 recovery (simulated).** Simulate the crash: do the cancel, then DON'T place the replacement; confirm the position is naked and that a subsequent reconcile pass (or the restart recovery path, once built) re-protects it. Also confirm no duplicate order if you then run the recovery + the normal path.
- **B4 ○INFO — cancel reject near trigger.** Place a stop just below market; as price approaches, try to cancel — does Saxo reject the cancel of an in-flight/triggering stop? Handle-path check.

## C. Netting single-sell constraint + SERVER-SIDE entries

- **C1 ○INFO — re-confirm the constraint.** Long + resting SL sell; try to place a 2nd SELL (a `Limit` TP or `TriggerLimit` TP). Expected: `SellOrdersAlreadyExistForOwnedContracts` (re-confirms TPs can't rest alongside the SL).
- **C2 ⛔GATE — resting BUY entry coexists with the SL sell.** Long + resting SL sell; place a resting BUY entry (`Limit` and/or `TriggerBreakout`) for a further tier. Expected: accepted (1 buy + 1 sell, NOT 2 sells) — validates server-side entries survive bot-down. (Fail → entries also can't rest → revisit the model.)
- **C3 ★HIGH — `TriggerBreakout` BUY fires.** Place a conditional breakout BUY (trigger above market); when price breaks it, confirm it fires + fills server-side. Discover the exact params (trigger price, resulting order type, duration).
- **C4 ○INFO — `TriggerStop` BUY (pullback entry).** Place a pullback-style conditional buy; confirm trigger semantics.
- **C5 ★HIGH — grow-SL window on a server-side entry fill.** When a server-side entry fills and ADDS to the position (netting), is the existing SL auto-extended to cover the new shares, or is there a window where owned > SL-covered until the bot grows the stop? Measure the exposure window (never-naked on adds).

## D. Execution quality / oversell / slippage

- **D1 ★HIGH — oversell race (best-effort).** With a long + SL, fire a bot market/marketable-limit SELL while the SL sits close; try to induce the double-sell → net short. Confirm the shrink-SL-first sequence + a post-fire reconcile prevent a persisted short. (Hard to force; at least verify the sequence timing.)
- **D2 ★HIGH — marketable-limit fill quality.** Fire a marketable SELL `Limit` (limit through the bid by ε); confirm immediate fill + record fill vs limit vs mid = the implementation-shortfall baseline. Repeat with a couple of ε values.
- **D3 ○INFO — marketable-limit MISS.** Fire a marketable limit with a very tight ε on a fast-moving name; see if it misses (no fill) → informs the ε / TTL / re-price policy.
- **D4 ○INFO — `StopLimit` standalone.** Confirm `StopLimit` works standalone (a slippage-capped stop alternative), in case we want to bound the disaster-stop fill.

## E. Anchoring (mostly code, one live observation)

- **E1 ○INFO — realized-avg vs planned anchor.** Fill a multi-tier entry at DIFFERENT prices; record the realized blended avg fill; confirm (against the code) whether the SL / break-even / R levels anchor to the realized avg or the planned blend (§7 "wrong-price anchor" bug). Mostly a code check + one live fill to make it concrete.

## F. Parameter discovery (informational)

- **F1 ○INFO — min stop/trail distances.** From `OrderDistances` + a rejected too-tight order, learn the broker's minimum stop/trail distance (a `0.6·ATR` that's too tight may reject).
- **F2 ○INFO — tick-size rounding.** Confirm levels/distances round to `TickSizeScheme` (0.01 default) — the bot must round.
- **F3 ○INFO — algos reject small size.** Try routing a small tranche via an execution algo (VWAP / Implementation Shortfall) — expect a min-size reject (confirms "algos not worth it at our size").
- **F4 ○INFO — Trigger* full param set.** Capture the full request/response shape of `TriggerBreakout` / `TriggerLimit` (trigger price type, the placed order's type + duration) for the entry-order builder.

---

## Minimum GATE set to run first (if time is short)
**A1, A2, A5** (native trailing works + is readable) · **A4** (amend→cancel+replace) · **C1, C2** (TP can't rest, entry CAN rest) · **B1** (transition window). These decide whether the memo's native-trail + server-side-entry model is buildable as written, or must fall back (bot-managed trailing / no server-side entry). The ★HIGH probes sharpen the build; ○INFO fills parameters.

## Safety notes
- Daemon PAUSED for the whole battery (no re-placing / 429 contention).
- One throwaway position at a time, small size, hard `finally` flatten (cancel resting sells → market sell).
- Throwaway on a liquid name; do NOT reuse a uic across probes (avoids the 2-plans conflict) and clean journal lines after.
- All order placement via the canonical `SaxoBroker`; all reads via `SaxoClient` (keep `test_no_raw_saxo_http` green if any of this graduates into the repo).
