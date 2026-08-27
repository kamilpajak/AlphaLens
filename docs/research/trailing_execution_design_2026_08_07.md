# Trailing execution design — E / TP / SL

**Status:** DRAFT; **§3 SUPERSEDED 2026-08-27** by `breakeven_trail_live_policy_design_2026_08_27.md` — the adopted live policy's phase-2 trail is the lens's FRACTIONAL GIVEBACK (`entry + 0.6×(peak − entry)`), not the `0.6·ATR` offset §3 sketches, and it is bot-amend on the resting `StopIfTraded` (no native `TrailingStopIfTraded` conversion), so the §3 native-trailing mapping does not apply. The §8b/§8b.1 probe evidence stays valid and historically useful. Original status: adversarial zen review DONE; SIM **GATE + HIGH + finish probes DONE 2026-08-07** (results §8b / §8b.1). Net: the trailing-distance PARAMETER is amendable in place (memo assumed immutable), but amend does NOT reposition the current stop level (SIM static price) — so **cancel+replace stays the +0.5R break-even route** (~576ms, no 2-sells race). Server-side conditional entries confirmed; over-hedge rejected; prices must be tick-rounded. Ready to LOCK; LIVE confirms native ratchet/fire + whether a live tick lets narrow-amend reposition the level. Supersedes the "trailing = deferred/pluggable" note in `live_market_execution_model_design_2026_08_05.md`.

**Committed post-hoc 2026-08-23** as probe evidence: this memo lived untracked on the workstation while its essentials were restated in `entry_trailing_design_2026_08_12.md` (whose §9 records this file as absent). The unique content preserved here is the SIM probe results (§8b/§8b.1).


**Author aligned with operator 2026-08-07** through two Perplexity research rounds (trailing algorithms; Saxo order-type mechanics) + one adversarial review. Every empirical Saxo claim below is docs-cited and MUST be re-confirmed by a SIM probe before this memo is LOCKED.

---

## 1. Goal

Extend the shipped live-market execution model (INC-1 market adapter #988, INC-2a yfinance feed #990, INC-3 engine #989, INC-5 daemon wiring #993 + fix #998) with **trailing** for entries (E), take-profits (TP), and the stop-loss (SL) — because trailing needs a trigger that MOVES each tick, which a static resting broker order cannot express. The trade setup keeps defining the LEVELS (E1/E2/E3, TP1/TP2/TP3, SL); execution decides HOW each is realized.

**Non-negotiable second goal: DATA-VALIDATE trailing before trusting it.** The evidence that trailing beats fixed levels is genuinely MIXED (helps in momentum/serial-correlation regimes; degrades near random-walk or when too tight — whipsaw + costs). So every execution stamps telemetry from day one and a `/edge` lens compares fixed-level vs trailing-market realized outcomes. Trailing is adopted only if the data on OUR names/regime says it wins net of slippage.

## 2. The core principle — protection & entries stay server-side; only TP execution + trailing dynamics depend on the always-on bot

The adversarial review killed the naive framing "SL resting, E and TP both bot-executed." Refined via the empirically-probed Saxo `SupportedOrderTypes` for a US stock on our FifoRealTime intraday-netting account (`Market, Limit, StopIfTraded, TrailingStopIfTraded, StopLimit, TriggerStop, TriggerBreakout, TriggerLimit`):

| Layer | Execution | Survives bot-down? |
|---|---|---|
| **SL** | Hybrid: bot stages the +0.5R / break-even transition, then a **native `TrailingStopIfTraded`** trails server-side. | Yes — after the transition the broker trails on its own |
| **E** (entries) | **Server-side** — resting `Limit` (today) or conditional `TriggerBreakout`/`TriggerStop` buy. **Bot-managed ONLY for a trailing entry** (dynamic level). | Yes for static/breakout/pullback entries |
| **TP** (tranches) | **Bot-managed marketable-limit** (netting forbids a resting TP sell alongside the SL sell). | No — bot-down = missed TP (bounded by the trailing SL) |

**Why entries are server-side, TPs are not** (the key netting asymmetry): a BUY entry establishes new exposure, so it does NOT collide with the "single working sell against owned" rule (`SellOrdersAlreadyExistForOwnedContracts`) nor the OCO ban (`ExplicitCloseNotAllowedForIntradayNetting`). A resting TP SELL would be a second sell against the owned long alongside the protective sell-stop → rejected. Therefore **the TP is the only leg genuinely forced to be bot-executed**; the SL and entries can stay at the broker.

**Course-correction (important):** the earlier plan "INC-4 = convert all entries to live-market bot BUYs" is REJECTED. Today's entries are resting `Limit+GTD` → already server-side → already survive bot-down. Converting a STATIC entry to a bot-fired market order would be a robustness REGRESSION (a missed entry can be a real loss, esp. because a single bot process is more likely down during stress — the exact moments that matter) and pays needless spread/slippage at a fixed level a resting limit fills exactly. **Live-market/bot entries are justified ONLY for a TRAILING entry** (dynamic bounce level). Static entries stay server-side.

## 3. The `break-even +0.5R · trail 0.6` policy and how native trailing maps to it

The `[in_sample]` exit policy `break-even +0.5R · trail 0.6` is **two-phase**:
1. **Before +0.5R:** the SL rests at the initial disaster level and does NOT trail (gives the trade room; the activation threshold is what prevents a fresh position being whipsawed out on noise).
2. **At +0.5R:** jump the SL to break-even (a discrete move), then trail by 0.6·ATR.

Native `TrailingStopIfTraded` is **single-phase**: it trails by an offset (`TrailingStopDistanceToMarket` + `TrailingStopStep`) from placement, server-side. **CORRECTION (probed 2026-08-07, §8b): the distance IS amendable in place** — the memo originally assumed it was immutable (change = cancel+replace). It is not: `PATCH` of `TrailingStopDistanceToMarket` returns 200 and takes effect. Only the order TYPE is immutable (`StopIfTraded` → `TrailingStopIfTraded` still needs cancel+replace). A native trailing stop placed naively at entry would still trail from the start (no +0.5R activation, no BE jump) → premature whipsaw; the mapping below controls that with the activation phase, now via amend rather than cancel+replace.

**Correct mapping — the SL hybrid.** Two routes. **Cancel+replace is the confirmed route for the +0.5R break-even JUMP; the amend route is NOT yet established** (the narrow-amend that would drive it did not reposition the stop level on SIM — §8b):
- **Cancel+replace route (confirmed):** Phase 1 plain resting `StopIfTraded` at the disaster level; at +0.5R the bot **cancel+replaces** it with a `TrailingStopIfTraded` anchored at break-even (offset frozen at the transition). Measured window ~576ms (§8b), brief and SAFE (price well above the stop in profit; a failed replace is re-covered by the never-naked reconcile; the cancel commits before the replace — no 2-sells race, §8b B2).
- **Amend route (UNCONFIRMED — do not rely on it yet):** place the disaster stop AS a `TrailingStopIfTraded` from entry (wide distance) and AMEND the distance narrower at +0.5R to pull the stop up. **Probed 2026-08-07 and it did NOT work on SIM: the amend is accepted (200) but the current stop LEVEL stayed put** — the level is anchored to the ratcheted high-water mark and only recomputes on a price TICK, which SIM's static price never delivered. So the +0.5R reposition-by-amend is unverified; it may work on LIVE (next live tick recomputes `level = high_water − distance`) or may not. **Confirm on LIVE before using this route. What IS confirmed: the trailing distance/step is amendable in place (accepted) — useful for adjusting the give-back as ATR evolves, NOT for the BE jump.**
- Phase 2 (both routes): the native trailing stop ratchets server-side thereafter — survives bot-down and keeps trailing (native ratchet itself also LIVE-only — SIM price was static, §8b).

So **the +0.5R activation and the break-even jump stay bot-managed (one transition); the continuous trail becomes native (robust).** The policy behaviour is preserved AND gains bot-down robustness for the trail phase.

**Residual limitation:** the native trail keeps 0.6·ATR FROZEN at the transition (native offset is fixed). Re-adapting the trail distance as ATR evolves would need further bot cancel+replaces — a refinement, not the first cut.

**Comparison variant to measure:** a **fully bot-dynamic** SL (bot amends a plain `StopIfTraded` level each step per the exact policy, incl. ATR re-adaptation) — flexible but fragile (frozen when the bot is down, amend rejects/latency). The data decides native-hybrid (robust, offset frozen) vs fully-bot-dynamic (adaptive, fragile).

## 4. Trailing algorithms (ATR-based, fits the existing `atr_bracket_1p5` geometry)

- **Trailing TP trigger (Chandelier for a long):** `peak = max(peak, price)`; `trigger = max(trigger, peak − k·ATR)` (ratchets up only); fire the tranche (shrink SL → marketable-limit sell) when `price ≤ trigger`. `k` is the give-back knob.
- **Trailing entry (bounce, long):** `trough = min(trough, price)`; `trigger = trough + m·ATR`; buy when `price ≥ trigger`. Differs from a fixed limit (exact level) by buying on a confirmed rebound off the low.
- **Activation / arming:** do NOT trail from entry — arm only after ≥ activation_offset in favour (e.g. ≥1 ATR or ≥1R). Before arming, the fixed level / disaster stop governs.
- **Whipsaw mitigation:** trail on bar-close (not raw ticks) OR a min-move filter (update only on moves > ε) OR time-confirmation; use **coarser trailing steps** (discrete ATR increments larger than the noise+latency band) so amends/cancel-replaces are meaningful and not tick-by-tick.

All parameters (`k`, `m`, activation offset, `0.6`, `+0.5R`) are `[in_sample]` hypotheses — measured, not trusted.

## 5. Execution mechanics — marketable limits, not pure market

For E (trailing) and TP tranche fires, use **marketable `Limit` orders with an ε slippage cap** (limit = trigger ± ε), NOT pure `Market`. Trailing triggers fire on inflection (entry after a bounce, TP after a rollover) where liquidity thins and spread widens — pure market at that tick is systematic adverse selection ("overpaying to be perfectly fast"). A marketable limit caps the slippage; if price runs past the cap the fill is missed and the trailing SL catches the reversal. For SMALL single-name tranches, marketable limits outperform Saxo's execution algos (VWAP/Implementation-Shortfall/Iceberg/Dark) — those carry min-size / algo constraints not worth it at our tranche size. (Static entries already fill exactly as resting limits — no slippage.)

## 6. Telemetry & measurement (start NOW, before trailing)

Stamp on **every** E/TP fire, from day one (re-introducing execution-quality metrics removed with the broker chain in ADR 0012):
- **Implementation shortfall:** `decision_price` (the trigger level) vs `fill_price` (actual marketable-limit fill) → per-fire slippage. This is unambiguous and accrues now.
- **Missed-exit-as-loss bucket:** when the bot is down at a TP window and the SL is above break-even, treat the converted-open-profit as a LOSS event (not "opportunity cost") and log its P&L impact — per the adversarial review, a real left-tail, not forgone edge.

**Fixed-vs-trailing comparison — NOT a per-trade counterfactual (adversarial-zen finding: category error).** You never run both arms on one trade, and the trailing outcome depends on the price PATH (did it make a new high before retracing?), which the fixed-level outcome does not — so `realized(trailing) − realized(fixed)` on a single trade is ill-defined. Two valid frameworks instead:
- **A/B randomization** (primary): randomly assign fixed-level vs trailing per trade (or per pick), compare AGGREGATE realized outcomes across many trades. Standard A/B, no per-trade counterfactual needed, unbiased — at the cost of needing volume + giving up per-trade info.
- **Path-replay** (secondary, faster): replay each trade's RECORDED price path (the population-monitor / ladder-replay already does this) under both policies. The TRIGGER levels are deterministic functions of the path → well-defined; the SLIPPAGE for the counterfactual arm is MODELLED from the observed implementation-shortfall distribution (only the live arm's slippage is observed). Path-replay estimates the trigger-point difference; A/B measures the true realized difference incl. slippage.

The comparison is a **registry-driven `/edge` lens** over this telemetry. Baseline slippage accrues now (fixed-trigger + marketable-limit); the trailing benefit accrues once trailing is live; the lens compares via A/B (primary) + path-replay (sanity).

## 7. Failure modes carried from the adversarial review + mitigations

- **Frozen stop when bot-down** → the native trailing stop keeps trailing server-side after the +0.5R transition (the whole reason for the hybrid). Before the transition, the static disaster stop protects.
- **Missed TP = real loss (not opportunity cost)** when deep in profit + reversal → bounded by how tightly the trailing SL has ratcheted; the native trailing SL is the safety net. Measured via the missed-exit bucket.
- **Market-on-reversal slippage** → marketable limits with ε cap (§5).
- **Fast-move oversell race** (bot fires a market TP while the server-side stop fires → net short on netting) → shrink-SL-before-sell (INC-5) reduces it, but an amend-latency window remains: disarm TP locally once the SL is suspected triggered, reconcile broker state after any fast move, and probe whether it can be induced (§9).
- **Cancel+replace naked window at the +0.5R transition** (still the confirmed route — the amend route does NOT reposition the level, §8b.1 N1) → measured ~576ms; the cancel commits before the replace so there is no 2-sells race (§8b B2). Mitigations stand: a **restart state-recovery path** that detects the "cancelled-but-not-yet-replaced" state and re-protects, plus **idempotency** (client-order-id query) so a restart that can't tell whether the cancel or the replace succeeded does NOT place a duplicate. **Kill-9 test stays required** (this is the primary BE-jump path). (Partial-fill-during-cancel does NOT bite the SL transition — at +0.5R price is above the stop, so the stop is not filling; it bites only working limits.)
- **Off-tick price rejection** (§8b.1 F2): Saxo 400s any price not on the tick grid (it does not round) → the bot must round every level/distance to the instrument tick (0.01, 0.0001 <$1) before every place/amend.
- **Naked window on an entry ADD (never-naked-on-add)** (NEW, §8b): Saxo does not auto-extend a standalone SL when a later entry tier fills and grows the position → owned > SL-covered until the bot amends the SL `Amount` up. Mitigation: after every entry-tier fill, amend the SL amount to the new owned qty (INC-5's reconcile already does this on owned>covered; the window = poll latency). Same discipline as the +0.5R transition, applied to adds.
- **Native-vs-bot trail-state divergence after an outage** (adversarial-zen HIGH) → while the bot is down the native stop keeps trailing, so the bot's LOCAL trail level goes stale. On restart the bot must **QUERY the broker for the actual native order + trail level and treat the broker as the source of truth (local = cache); it must NOT re-place / re-anchor the native trail at its last-known (looser) level**, and must NOT assume the native order is still alive (some brokers expire it on disconnect — verify). **Probe (§9): can the Saxo API return the CURRENT native-trail level/state? If not, native trailing across a bot restart is UNSAFE and must not be used.**
- **Restart trailing state** → derive canonical truth from the BROKER on restart (position, avg price, active orders); reconcile journal→broker; a restart-safe "simple mode" for the bot-side trailing state (peak/armed) that falls back to a conservative fixed rule until fresh live data re-establishes the high-water mark. INC-5 already does broker-state-truth reconcile + idempotent `tranche_fired`; the peak/armed state needs the same discipline.
- **Amend rejects / latency on the SL** → `OnWrongSideOfMarket` when the stop approaches market (our known dead-end); trailing keeps the stop BELOW market so it should be safe, but probe. Rate-limit adjustments (coarse steps); prefer native trailing over per-tick bot amends.
- **Marketable-limit missed fill in a fast market** (quote moves between price-check and order arrival) → short TTL + aggressive re-price / re-place (the reconcile loop handles unfilled orders); do NOT switch to pure market (reintroduces the adverse-selection slippage). A missed TP fill falls to the trailing SL (which is why the SL safety-net + the missed-exit bucket matter).
- **R-multiples / break-even anchored to the WRONG price** → `+0.5R`, the break-even level, and the trail distance must be computed from the **realized average fill** (blended across filled entry tiers), NOT the planned entry blend. Anchoring the stop/trail to the planned price when the actual fill diverges puts the stop at the wrong risk distance — a correctness (risk) bug, not an approximation.
- **Single-process single-point-of-failure** → for the eventual LIVE arc, consider splitting a minimal hardened execution agent from the strategy process; not for the SIM phase.

## 8. Saxo capabilities (empirically probed 2026-08-07 for a US stock on SIM)

`SupportedOrderTypes = [TriggerStop, TriggerBreakout, TriggerLimit, StopLimit, StopIfTraded, TrailingStopIfTraded, Limit, Market]`. `OrderDistances` carries default distance config (StopLoss/TakeProfit % distances). `SupportedStrategies` (algos): VWAP, TWAP, Implementation Shortfall, Iceberg, Dark, Liquidity Seeking, MOC, LOC, Price Peg. Account: `PositionNettingProfile: FifoRealTime, Intraday`. Docs (Perplexity): `TrailingStopIfTraded` can be standalone ("sleeping order"); `Trigger*` are server-side conditional orders; buy-side entries are exempt from the single-sell constraint. **Probe corrections (§8b): its distance is NOT fixed — it is amendable in place;** server-side ratchet + fire could NOT be confirmed on SIM (static price) and move to LIVE.

## 8b. SIM probe results (2026-08-07) — empirical corrections

GATE + HIGH batteries ran attended, daemon paused, on a throwaway Ford (uic 486) long (`probe_gate.py`, `probe_high.py` — the population-monitor SIM account, cleaned first). Verdicts:

**Confirmed as designed:**
- **Standalone `TrailingStopIfTraded` SELL accepted on netting** (precheck `Ok`, place 200) — §9.1 placement ✓.
- **Native-trail level readback ✓** via `GET /port/v1/orders/me` (returns `Price` = current stop level + `TrailingStopDistanceToMarket`). Per-order `GET /port/v1/orders/{ClientKey}/{OrderId}` came back empty — **read the trail level from `/orders/me`, not per-id.** §9.2 gate PASSES: the live trailed level IS queryable → native trailing across a restart is not blocked on readback.
- **Single-sell netting confirmed**: a 2nd SELL (`Limit` TP) alongside the SL sell → 400 `SellOrdersAlreadyExistForOwnedContracts` (the §2 asymmetry).
- **Server-side entry coexists with the SL**: a resting `Limit` BUY placed alongside the SL sell → 200 (1 buy + 1 sell) — §9.3 core hypothesis ✓ (via `Limit`; `TriggerBreakout` still pending the correct schema, below).

**PARTIAL CORRECTION: `TrailingStopIfTraded` distance is amendable in place — but amending it does NOT reposition the current stop level (walked back below).** §3/§7/§8 assumed the distance was immutable (change = cancel+replace). That is FALSE — `PATCH /trade/v2/orders` on `TrailingStopDistanceToMarket` returns 200 and the distance parameter changes. But the second probe pass (below) showed the accepted amend does NOT move the current stop LEVEL on SIM, so it CANNOT drive the +0.5R break-even jump. What amend-in-place buys:
- **Adjusting the trailing distance/step** as ATR evolves (accepted; future trailing behaviour changes). NOT a level reposition.
- Order TYPE is still not amendable — `StopIfTraded` → `TrailingStopIfTraded` needs cancel+replace, **measured window ~576ms** (cancel-ack 53ms + place-confirm 523ms), and the cancel commits before the replace (no 2-sells race — B2 below). This stays the route for the +0.5R jump.

**NEW failure mode — naked window on an entry ADD (never-naked-on-add):** Saxo does NOT auto-extend a standalone SL when a later entry tier fills and grows the position. Probed: SL covering 6, a `+4` market BUY → owned 10 but the SL `Amount` stayed 6 = **4 shares uncovered until the bot amends**. Amending the SL `Amount` 6→10 (`AmendStop` absolute-target) returned 200 and re-covered. The never-naked invariant must therefore extend to ADDS: after each entry-tier fill, the bot amends the SL amount up to the new owned qty. (INC-5's reconcile already grows stops on owned>covered; this quantifies the window = poll latency.)

**SIM is NOT representative for slippage:** a marketable SELL `Limit` fills immediately (does not rest — §5 mechanic ✓), but SIM filled BOTH a deep-through (limit 13.52) and a near-touch (limit 13.79) at `ExecutionPrice = 13.82 = reference` — zero modelled spread. The §6 implementation-shortfall baseline therefore **cannot come from SIM; it accrues only on LIVE.** SIM proves the mechanic, not the cost.

**Unverifiable on SIM — deferred to LIVE:** native server-side RATCHET (§9.1) and native trailing FIRE could not be induced — SIM's price for the instrument was static across the observation windows (frozen 3 min / 30 s). Both need LIVE's real-time feed (or a longer moving window). Placement / readback / amend all passed; only the price-driven dynamics are open.

**Follow-up (not gating — `Limit` entry already validates the server-side-entry model):**
- `TriggerBreakout` BUY body needs a proper trigger schema — the naive `{OrderType: TriggerBreakout, OrderPrice}` returned 400 `InvalidTriggerPriceType`. Saxo `Trigger*` orders require a trigger-price-type spec (`TriggerOrderData` / `PriceType` = LastTraded|Bid). Discover the full body before using conditional breakout entries (§9 F4). Resting `Limit` entries (proven) cover the requirement meanwhile.
- No minimum trailing distance surfaced at precheck (accepted `dist=0.01`); a real min may be tick-based / enforced at placement, not precheck.

### 8b.1 Second probe pass — remaining SIM-answerable items (`probe_sim_finish.py`, 2026-08-07)

- **N1 — narrow-amend does NOT reposition the stop level (the walk-back).** Placed `TrailingStopIfTraded` dist=1.00 (level 12.91 = ref−1.00), then amended dist→0.40→0.05: each amend returned 200 but the **level stayed 12.91** (never rose toward market). The trailing level is anchored to the ratcheted high-water mark and only recomputes on a price TICK; SIM's static price delivered none. So the "amend route" for the +0.5R break-even jump is UNVERIFIED — it may recompute `level = high_water − distance` on the next LIVE tick, or may not. **Cancel+replace is the confirmed BE-jump route (§3).** Amend-in-place is confirmed only for changing the distance PARAMETER, not for moving the level.
- **F4 — server-side conditional entries CONFIRMED.** The correct `Trigger*` body needs `BreakoutTriggerUpPrice` / `BreakoutTriggerDownPrice` (the bare `{OrderType, OrderPrice}` was what 400'd). Precheck `Ok` for `TriggerBreakout` (+Up, +Up+PriceType, +Up+Down), `TriggerLimit` (+Up), `TriggerStop` (+Down), all with `TriggerPriceType`. Trigger price types this stock advertises: **`LastTraded`, `Open`** (no Bid/Ask). So §9.3 conditional entries (breakout/limit/stop) are placeable — the model's server-side-entry side is validated beyond plain `Limit`.
- **D4 — standalone `StopLimit` SELL accepted** (`OrderPrice` = stop trigger + `StopLimitPrice` = limit) — a slippage-capped disaster-stop alternative is available.
- **A9 — over-hedge REJECTED (`NotOwned`).** A sell-stop with `Amount` > owned is refused. Consequence: never-naked-on-add MUST be reactive — you cannot pre-arm a stop for the full intended size before the shares are owned; the bot amends the SL `Amount` UP only after each entry-tier fill (confirms §8b grow-SL + §7 add bullet).
- **B2 — no cancel→replace race.** Cancelling the disaster stop then IMMEDIATELY placing the trailing (no wait) returned 200 — no `SellOrdersAlreadyExist`. The cancel commits before the replace, so the transition needs no explicit confirm-cancel gate (SIM; re-verify latency on LIVE).
- **F2 — off-tick price REJECTED (`PriceNotInTickSizeIncrements`).** Saxo does NOT round — an off-tick order price 400s. **The bot MUST round every price/level/distance to the instrument tick (0.01, 0.0001 <$1) before placing.** Precheck did not enforce a minimum trailing distance (accepted 0.01), so the tick is the binding constraint, not a min-distance.

## 9. Probes required BEFORE lock (SIM, attended, same pattern as the shrink-sell probe)

**Status 2026-08-07: probes 1–3 RUN (see §8b) — 1 placement/readback ✓ (ratchet/fire → LIVE), 2 ✓ (readback via `/orders/me`), 3 ✓ via `Limit` (`TriggerBreakout` schema follow-up). Probes 4–5 (oversell race, kill-9 recovery) still pending; the kill-9 recovery is now lower priority since the amend route removes the cancel+replace it guards.**

1. **`TrailingStopIfTraded`**: standalone placement for a long sell-stop on netting; confirm server-side ratchet; confirm the distance is fixed (amend rejected → cancel+replace required); no netting/wrong-side reject when placed below market.
2. **Native-trail state readback (GATES the native-trail design)**: confirm the Saxo API returns the CURRENT trailed stop level / high-water state for a live `TrailingStopIfTraded` (via the orders/positions read), so the bot can reconcile broker-truth on restart. If the current trailed level is NOT queryable, native trailing across a bot restart is UNSAFE → fall back to bot-managed trailing or a static stop.
3. **Conditional BUY entry** (`TriggerBreakout` / `TriggerLimit`) resting server-side; confirm it coexists with the protective SL sell (1 buy + 1 sell, NOT 2 sells) without `SellOrdersAlreadyExist`.
4. **Fast-move oversell race**: attempt to induce the bot-TP + server-side-stop double-sell → net short; confirm the shrink-first sequence + reconcile prevent a persisted short.
5. **Cancel+replace kill-9**: kill the bot mid cancel→replace at the +0.5R transition; confirm the restart recovery path detects cancelled-but-not-replaced and re-protects, with no duplicate order.

## 10. Build sequence (each: TDD + zen pre-merge; probes gate the trailing builds)

1. **Execution-quality telemetry** on the existing INC-5 TP fires (implementation shortfall: decision vs fill) — ships NOW to start the baseline dataset; no trailing yet.
2. **Probes** (§9) — attended, market-open.
3. **SL trailing** as an ExitPolicy: the hybrid (bot-stage +0.5R/BE → native `TrailingStopIfTraded`) + the fully-bot-dynamic comparison variant, both flag-gated + measured.
4. **Trailing TP trigger** (Chandelier `peak − k·ATR`) replacing the fixed geometry TP level in the engine's `plan_tranche_exits`; marketable-limit fills.
5. **Trailing entry** (server-side conditional where static; bot-managed for the dynamic bounce level) — only when trailing is turned on.
6. **`/edge` fixed-vs-trailing lens** over the accrued telemetry.

## 11. Open questions — resolved by data, not by faith

- Native-hybrid SL (robust, 0.6·ATR frozen at transition) vs fully-bot-dynamic SL (ATR-adaptive, fragile) — which realizes better net of the bot-down cost?
- `k` (TP trail), `m` (entry bounce), activation offset, the `+0.5R` / `0.6` params — all `[in_sample]`.
- Marketable-limit ε (slippage cap) — tight (more misses, less slippage) vs loose (fewer misses, more slippage).
- Do trailing entries earn their added fragility vs plain resting-limit / breakout-trigger entries?
