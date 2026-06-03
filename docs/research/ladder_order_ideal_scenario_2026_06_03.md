# Ladder order lifecycle — ideal scenario + feedback-replay design

**Status:** DRAFT
**Date:** 2026-06-03
**Scope:** How a candidate's 3-entry / 3-TP / 1-SL ladder *should* behave (north-star — the fully dynamic, literature-grounded version), why Alpaca cannot deliver it, how the simplified static version we emit today diverges from that ideal, and how we capture the "did the setup work" feedback signal **without a broker** via a deterministic price-path replay.

**Two ideals, kept separate on purpose:**
- The **live-execution north-star** (§2) is highly dynamic: a ratcheting stop, a trailing runner, confirmation-gated and cancellable entries, ATR-normalised targets, graduated time-stops. This is what a disciplined trader does with a perfect broker (Van Tharp, Raschke, Elder, Turtle). It is the target for real capital (Saxo endgame), **not** the thing we replay for feedback.
- The **feedback measurement** (§5) deliberately does **NOT** emulate that dynamic trader. It replays the ladder **as the tool actually specifies it** plus a policy-free price-path substrate, because the goal is to measure *our* candidate + *our* setup geometry — not a smarter management policy the tool never ships. Adding dynamic management would measure free parameters we don't emit and confound attribution. See §5.0.

---

## 1. Purpose

Every thematic candidate carries a deterministic trade setup (`brief_trade_setup`, from `thematic/trade_setup/ladder.py`): up to **3 entry tiers** (E1/E2/E3, staggered limit prices below close — scale-in on dips), up to **3 take-profit tranches** (TP1/TP2/TP3, ascending) and **1 disaster stop** (SL).

This memo first defines the **ideal** behaviour of that ladder in a perfect broker (the spec any execution path is judged against), then records the decision that — because our goal is **feedback collection, not real capital** — the right way to measure it is a **broker-free price-path replay**, not a live broker.

---

## 2. Ideal scenario (perfect broker — Saxo-like native if-done/OCO per tier)

Assumes a broker where resting scale-in BUYs and resting protective SELLs **coexist** on the same symbol with no wash-trade constraint, and protection lives entirely on the broker book (no client process is load-bearing for safety). It also grants every contingent-order wish: server-side trailing stops (ATR / structure), if-done chains, OCO groups, conditional triggers on price/time, automatic position-average recalculation, partial-fill-aware children.

> **Correction over the first draft of this memo.** The first version described a *static* ideal: one fixed shared SL held at its initial level for the whole life of the trade, and fixed TP targets that only moved because the blend moved. The trading literature (Van Tharp R-multiples, Raschke market-structure stops, Elder triple-screen, Turtle pyramiding) is unanimous that this is the **simplistic static model an ideal trader does NOT use**. With a broker that grants every wish, the genuinely ideal management is **more dynamic**: the stop **ratchets** as risk falls, the last tranche **trails**, deeper entries are **confirmation-gated and cancellable**, and **time** is a co-equal exit dimension. §2 below is the corrected dynamic ideal; §2.6 records the simplified static version we actually emit today and why it diverges.

### 2.1 Worked example

Candidate `XYZ`, prior close `100.00`, ATR `4.00`.

- Entry tiers (descending, ≥ 0.5·ATR apart, ≥ 0.5·ATR above stop): **E1 = 99.00, E2 = 97.00, E3 = 95.00**, 100 sh each (full position 300).
- Disaster stop (the final safety net, NOT the primary stop): **SL_disaster = 92.00**.
- TP tranches sized as thirds of the *currently filled* position, ascending, priced by r-multiple `r = (target − blended_entry) / (blended_entry − stop)`, with r ≈ 1 / 2 / 3.

### 2.2 The three load-bearing invariants

1. **PROTECTION INVARIANT** — at every instant where `filled_qty > 0`, the broker holds a live SELL stop covering *exactly* `filled_qty`. No window, however small, where held shares are unprotected.
2. **BLEND-TRACKING INVARIANT** — stop quantity and all live TP prices/quantities stay consistent with the current qty-weighted blended entry and `filled_qty`. Every fill (entry or partial TP) recomputes the blend and re-sizes / re-prices the resting exits **atomically** with that fill.
3. **RATCHET INVARIANT** *(the one the first draft missed)* — the **primary** stop only ever moves in the risk-reducing direction (up, for a long), and it advances on **achieved milestones**: each entry tier that fills lowers blended risk, and each TP tranche that hits converts captured profit into a tighter floor. The disaster stop is the conservative backstop *beneath* the primary stop; in a healthy trade the primary stop tightens so far that the disaster stop is never reached.

### 2.3 Lifecycle / state machine (dynamic, ratcheting stop)

- `RESTING` — 3 entry limits live; no position; exit group dormant.
- **E1 fills (→ PARTIALLY_SCALED):** `filled=100`, blend = 99.00. **Primary stop = SL_disaster = 92.00** for 100 sh, live at the fill instant (denominator 7.00 → TP1/2/3 = 106 / 113 / 120, split 33/33/34). **E2/E3 stay resting** (perfect-broker property: BUYs coexist with SELL exits). Scale-in alive.
- **E2 fills:** `filled=200`, blend = 98.00. Atomic re-size + re-price: stop 100 → **200** (never <200 covered). Denominator 6.00 → TP1/2/3 re-priced **down** to 104 / 110 / 116, re-split 67/67/66 (cheaper tier lowers the blend → absolute TPs fall though r is fixed). The deeper fill *reduced* blended risk; the primary stop may stay at 92.00 (disaster floor) until a profit milestone justifies tightening. E3 stays resting.
- **E3 fills (→ FULLY_SCALED):** blend = 97.00, stop 300 sh, denominator 5.00 → TP1/2/3 = 102 / 107 / 112 × 100. Position full + fully protected.
- **TP1 hits (→ SCALING_OUT) — RATCHET STEP:** TP1 fill decrements stop quantity to the new `filled_qty` in the same atomic transaction. **AND the primary stop ratchets up to break-even+ (blended entry, or entry + the profit just banked on TP1).** From here the remaining position cannot produce a net loss. Un-hit TP prices do **not** move — a *sale* does not change the held lot's cost basis (contrast entry fills, which move the blend).
- **TP2 hits — RATCHET STEP:** stop quantity decrements again; primary stop ratchets up further, locking in the TP1 profit and protecting the gain on what remains.
- **Runner (after TP2, optionally TP3):** the last tranche need not be a fixed limit — the ideal converts it to a **trailing stop** (≈1.5–2× ATR, evaluated on session close not intrabar, or anchored to the last confirmed swing-low) so an extended move is captured beyond the fixed TP3. Fixed-TP3 vs trailing-runner is a setup-design choice, not a broker limitation.
- **Disaster / primary stop hit (`filled_qty > 0` → CLOSED_SL):** the stop fires for the **entire current** `filled_qty`; OCO simultaneously cancels any still-resting entries (you do not scale further into a name that just stopped out) and all live TP tranches.
- **Confirmation-gated, cancellable entries:** deeper tiers are not unconditional limits. A genuinely ideal manager fills E2/E3 only while the thesis holds, and **cancels** pending deeper tiers on a thesis-invalidating move (gap below the pre-entry range, swing-low break) regardless of whether the tier price was reached — so scale-in stays "adding under strength," not averaging-down into a loser. (Our deterministic setup cannot evaluate this discretionary signal — see §2.6.)
- **TTL on unfilled entries:** a tier that never fills before its TTL is dropped; the position simply ends smaller. No fill ever → group expires, clean no-op.
- **Graduated time-stop (from first fill):** beyond a single 42-day hard flatten, the ideal tightens with elapsed time for an event-driven trade whose catalyst is decaying — partial profit-take / tighter stop as the thesis window closes, full flatten at the horizon.

### 2.4 What the dynamic ideal adds over the static draft (summary)

| Dimension | Static draft (first version) | Dynamic ideal (this version) |
|---|---|---|
| Primary stop | one fixed level (92) the whole trade | **ratchets** up on each TP milestone (BE+ after TP1, lock-in after TP2) |
| Disaster stop role | the only stop | the **backstop beneath** an advancing primary stop |
| Runner (TP3) | fixed limit | optional **trailing stop** to capture extended moves |
| Deeper entries | unconditional limits | **confirmation-gated + cancellable** on thesis break |
| Targets | fixed prices | **ATR-normalised** R-multiples |
| Time | single 42-day flatten | **graduated** decay for event catalysts |

### 2.5 The one sentence the dynamic ideal guarantees

*The filled position is ALWAYS protected, the protective stop only ever ratchets toward more safety as tiers fill and TPs hit (so a winner is locked progressively and can no longer turn into a loss), scale-in continues under that protection only while the thesis holds, and price-or-time lifts the position out — never an unprotected window and never a stop that drifts the wrong way.*

### 2.6 The simplified STATIC version we emit today — and why it diverges

`brief_trade_setup` (from `thematic/trade_setup/ladder.py`) is deterministic and emits a **static** ladder: one fixed disaster stop, fixed R-multiple TP targets, unconditional entry limits, single 42-day time-stop. It deliberately omits every dynamic dimension in §2.4, because each of those depends on **discretionary, real-time signals the tool does not produce** (structure retest, momentum divergence, sector RS, volume absorption). This is a conscious scope choice, not an oversight:

- We ship a **decision-support artifact**, not an execution policy. The group member decides how to manage the position; the tool gives the levels.
- A deterministic generator cannot honestly encode "cancel E3 if the thesis breaks" without a thesis-break signal it does not compute.

The consequence — and this is the useful part for feedback — is that **our emitted geometry is exactly the "static model" the literature flags as sub-optimal.** Measuring how much the static design leaves on the table vs a ratchet variant is therefore a real signal about the *generator*, captured cheaply in replay (§5, the "what-if" overlay) — without ever shipping the dynamic policy.

---

## 3. Why Alpaca cannot deliver the ideal

Alpaca's **wash-trade block** (error `40310000`, HTTP 403) rejects a new BUY whenever *any* opposite-side SELL is open on the same symbol — limit, stop, or stop-limit — evaluated **at submit time**, no carve-out for non-marketable or stop legs. So in the entry window the choice is **binary: resting scale-in BUYs OR resting protective SELLs — never both.**

Consequences in the current live code:

- `exit_manager` (#401) **cancels the unfilled entry tiers (E2/E3) on the first fill** so it can attach the protective exit ladder. Scale-in is abandoned by construction.
- The just-deployed `trade_updates` WS daemon detects the first fill **in ~1–3 s** (vs up to 30 min for the poll), so it cancels E2/E3 almost instantly → **sequential intraday scale-in effectively never happens** on Alpaca (only a deep gap-at-open that fills E1+E2+E3 in the same instant survives).
- Native bracket/OTO/OCO do not help: one TP + one SL per parent, children held until the parent *fully* fills — no multi-entry-shared-stop construct exists.

There is no Alpaca configuration that gives "scale-in under continuous protection." The only honest path to the ideal is **Saxo native per-tier if-done/OCO** (each tier carries its own server-side protection; tiers coexist) — which is a separate, later track (Saxo is far off + the demo token may not survive >20 min).

---

## 4. Decision: for FEEDBACK we do not need a broker

The Alpaca paper apparatus exists to answer one question: **did the setup work, and in what order did the levels resolve?** That question is answered more cleanly by **replaying the price path against the deterministic ladder** than by any broker:

- **No wash-trade, no #401, no protection gap, no slippage, no always-on process.** The replay is post-hoc, deterministic, backfillable.
- **Cleaner telemetry.** A market/paper broker would fill entries at market (slippage on thin small-caps) → blended entry drifts → realized R is measured against an execution model the brief never specified. The replay uses **exact level prices** (the resting-limit prices the geometry assumes), so the realized R is the clean, slippage-free number.
- **REST historical aggregates, not a live feed.** The data we need is in the past (a matured candidate's path already happened); a WebSocket streams the present and would re-introduce an always-on daemon + live dependency + daemon-death fragility — the opposite of where we are going. (If sub-minute crossing order ever matters, fetch REST second/tick aggregates — still no daemon.)

This makes the whole Alpaca paper **chain** (`plan → submit → reconcile → exit_manager`) + the `trade_updates` daemon **redundant for the feedback purpose.** Its only remaining value is operational rehearsal of a real execution path before live capital — a separate concern from feedback.

---

## 5. Broker-free price-path replay — model spec

### 5.0 What to measure — the priority order (the key decision)

The feedback question decomposes into three things, and they want different measurements. Conflating them — especially layering the §2 dynamic ideal onto the replay — pollutes the signal. Priority order:

1. **Policy-free path substrate = the ground-truth layer (store once, never re-fetch).** Independent of *any* execution policy: the ordered crossing sequence, plus **MFE / MAE** (max favourable / adverse excursion over the horizon) and **forward return at fixed horizons**. This answers *"was the candidate / theme a good pick?"* with **zero ladder assumptions** (the same directional signal `shadow_return` already captures), and lets any policy be re-derived later without touching Polygon again.
2. **As-specified replay = the headline outcome.** Replay the ladder **exactly as `brief_trade_setup` specifies it** (static stop, fixed TP targets). This is the *only* number that maps 1:1 to what the tool actually emits → it is the honest *"did OUR setup work."* It answers the **setup-geometry** question (Q2): given the path, did our levels capture it.
3. **Ratchet variant = an optional, explicitly-labelled "what-if" overlay.** Re-run the same path under a §2-style ratcheting stop (BE+ after TP1, lock-in after TP2, optional trailing runner) to **quantify how much the static geometry leaves on the table** (Q3 → a signal about the generator). It is computed cheaply from the same substrate. It is **never** reported as the tool's score, because we do not ship ratchet management — a ratchet P&L would flatter or penalise the tool for a policy it never executes.

**Explicitly OUT of the feedback core: the full dynamic ideal of §2** (confirmation-gated entries, momentum/structure/volume-based stop moves, sector-RS cancellation). It depends on discretionary real-time signals the tool does not emit; replaying it would measure free parameters we never ship and confound attribution. It lives in the live-execution north-star (Saxo), not in measurement.

> One-line rule: **measure what the tool emits (as-specified) over a policy-free substrate; treat the ratchet ideal as a labelled comparison, never as ground truth.**

### 5.1 Mechanics

**Enumeration (broker-free):** iterate matured **feedback decisions** (`brief_date` + `ticker`); look up the ladder from the **brief parquet** `brief_trade_setup` keyed `(date, ticker)`; fetch the intraday path over the hold horizon via **Polygon REST minute aggregates** (`get_agg_range`, the same fetch `backfill-shadow-returns` already uses); replay; write the outcome to the feedback ledger. No paper ledger, no broker.

**Fill model (only assumption):** touch = executed-at-that-level.
- Entry E(n) fills on `bar.low ≤ limit` (a dip), at `limit`.
- TP(m) hits on `bar.high ≥ target`, at `target`.
- SL hits on `bar.low ≤ disaster_stop`, at `disaster_stop`.

**Replay rules:**
- Walk bars ascending by timestamp. Entries are checked first each bar (you cannot exit before entering); multiple tiers can fill in one bar (gap-down through several limits).
- Track running MFE/MAE (highest high / lowest low vs blended entry) across the whole horizon — this is path-substrate, computed regardless of which levels trigger.
- Once `filled_qty > 0`: a bar where **both** SL and a TP are crossable is **ambiguous** (minute granularity hides intra-bar order) → resolve **conservatively SL-first** and flag the bar (`ambiguous_bars`), so the pessimistic bias is auditable.
- Record the ordered **crossing sequence** `(level_id, kind, price, bar_ts)`, e.g. `E1→E2→TP1→SL`.

**Outputs (to feedback ledger):**
- *Substrate (layer 1):* `sequence` (ordered crossings) + compact string, `mfe`, `mae` (in R and/or %), `forward_return` at the fixed horizon.
- *As-specified (layer 2 — headline):* `classification` (`NO_FILL` / `OPEN` / `TP_FULL` / `PARTIAL_TP_OPEN` / `PARTIAL_TP_THEN_SL` / `SL_HIT`), `blended_entry` (qty-weighted over filled tiers, alloc_pct weights), `realized_r` (R = blended − stop; exited tranches at target, SL closes remainder at stop, any horizon-open remainder marked to last close + `horizon_open` flag).
- *Ratchet what-if (layer 3 — optional, clearly namespaced e.g. `ratchet_realized_r`):* same path under the §2 ratcheting stop. Never overrides the layer-2 headline.
- `ambiguous_bars` count.

**Skip conditions:** trade setup absent / `status != "OK"` / no `disaster_stop` / no entry tiers → `NO_STRUCTURE` (same conditions under which the live exit_manager falls back to no structured ladder). Horizon not yet closed → not matured, skip (Polygon serves only past sessions).

**Where it runs:** a deterministic post-hoc step in the nightly feedback job (reuses the Polygon fetch + maturity/session helpers from `shadow_return.py`). Read-only against decisions + brief parquet + Polygon; the only write is the new ladder-outcome columns on the feedback `decisions` row. Stays click-orthogonal (reads no click columns), consistent with the v3 feedback-ledger orthogonality discipline.

---

## 6. Sequencing (decommission only after proof)

1. **Build** the broker-free replay engine (pure, deterministic) + its nightly enumeration/write. **(this memo's deliverable; engine drafted.)**
2. **Prove** it produces the feedback signal over real matured candidates for a few weeks (and answers the empirical question: how often does sequential intraday scale-in even occur vs gap-or-never).
3. **Then decommission** the Alpaca chain + `trade_updates` daemon (they stay running untouched until step 2 passes — broker is the safety net until the replacement is proven).
4. **Saxo (later):** the only path that delivers the §2 ideal for real capital (per-tier server-side OCO). Validate the Saxo if-done/OCO path on the demo token in the interim; do not build Alpaca scale-in machinery Saxo would delete.

---

## 7. Open question to answer with the data (cheap, first)

Before any further execution work: does sequential intraday scale-in (E1 at open, E2/E3 on later dips) actually happen often enough to matter, or do candidates gap-or-never? The replay's `entries_filled` sequence answers this directly from real paths. If tiers rarely fill sequentially intraday, the entire scale-in-emulation question is moot and protection-first is simply correct.
