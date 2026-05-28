# Paper-trading playbook — 3-tier entry × 3-tier TP

**Status:** REFERENCE
**Date:** 2026-05-28
**Scope:** Decision tree for the paper-harness exit_manager state machine. Codifies how a disciplined investor manages a 3-tier ladder entry + 3-tier take-profit ladder + disaster stop + time-stop. Drives concrete follow-up PRs on top of the PR #277/#279 paper harness.

Long-only event-driven (AlphaLens thematic trade-setup). Notation:
- **E1 > E2 > E3** — entry limits, progressively deeper pullback
- **TP1 < TP2 < TP3** — profit-take limits, progressively further targets
- **SL** — disaster stop below E3, defined as *thesis invalidation*, NOT a % offset
- **Time-stop** — N days (60d default) if neither SL nor TP1 hit
- **Tranches** — equal thirds (⅓/⅓/⅓) — no Kelly weighting, no pyramiding

## 1) Entry scenarios (before first TP)

| # | Market action | Basket state | Action | Why |
|---|---|---|---|---|
| E0 | Never tags E1 | 0/3, no position | Hold GTC until `order_ttl_days` elapsed (default 10d per `paper/constants.py::DEFAULT_ORDER_TTL_DAYS`), then **auto-cancel** | No chasing — thesis required pullback. No fill = no risk = valid outcome. ⚠ TTL enforcement currently absent in reconciler (see §5.6). |
| E1 | Tag E1 then bounce, no deeper pullback | 1/3 filled | Keep E2, E3 GTC | ⅓ position is enough if thesis already materialising. |
| E2 | Pullback to E2 | 2/3 filled | Keep E3 GTC, **do NOT raise SL** | E3 is the deeper-pullback reserve without changing invalidation. |
| E3 | Pullback to E3 | 3/3 (full size) | Position fully allocated, SL unchanged | Avg basis ≈ planned; max risk = (avg − SL) × N₀. |
| Eg↓ | **Gap-down** through E1 and E2 simultaneously | E1+E2 filled at **worse** price than planned (slippage) | Accept slippage; **cancel E3** if gap > 2× ladder span | Catastrophic gap = thesis possibly already invalidated; do not pour the third tranche into a falling knife. |
| Eg↑ | **Gap-up** above entry zone | 0/3 | Cancel all entry; consider *separate brief* after re-tag | Average entry now well above plan — R/R destroyed. |
| Ex | **Event invalidated** (M&A pulled, downgrade on thesis) | 0–2/3 | Cancel all unfilled tiers; market-close anything already filled | Thesis-side invalidation, not price-side — SL won't save it. |

## 2) Exit scenarios (position open)

Notation: state = (entry tiers filled) × (TP tranches sold).

| # | Start state | Market action | Action | After |
|---|---|---|---|---|
| X1 | any / 0 TP | Price tags **TP1** | Sell ⅓ limit-on-tag | TP1 realised, runner = ⅔ |
| X2 | after TP1 | — | **Cancel unfilled E2/E3** and **move SL → break-even** (avg basis) | Trade is now "free": max loss on remaining ⅔ = 0 |
| X3 | after TP1 | Price to **TP2** | Sell another ⅓ | TP2 realised, runner = ⅓ |
| X4 | after TP2 | — | Move SL → **TP1** (lock gain) or trailing ATR | Runner has guaranteed profit |
| X5 | after TP2 | Price to **TP3** | Sell final ⅓ | Position closed, full target |
| X6 | after TP1 | Price retraces to **BE-stop** | Market-out the remainder; cancel unfilled entry | PnL = +(⅓ × (TP1−avg)). Positive outcome without full target. |
| X7 | 0 TP | Price tags **SL** | Stop-market full position | Max planned loss; NO averaging-down below SL. |
| X8 | 0 TP | **Catastrophic gap-down** through SL | Stop-market at open; **do NOT** wait for bounce | Slippage cost of discipline. Repeated gap-throughs = process review, not per-trade. |
| X9 | 0 TP | **Time-stop** (N days, neither SL nor TP1) | Market-sell full | Capital re-use > optionality of one more day. Event-edge decays. |
| X10 | after TP1 | Time-stop on runner | Market-sell runner OR hold 1 day "post-event drift" — **per pre-registration** | Decision must be pre-decided in brief, not ad-hoc. |
| X11 | after TP2 | Sudden reversal, trail SL hit | Market-sell runner | Lock partial profit; don't trade past signal expiry. |
| X12 | any | **News invalidation** (downgrade, profit warning, SEC subpoena) | Market-sell **everything** ignoring TP/SL | Price-action SL does not protect against thesis news. |

## 3) PnL sign matrix

| Entry filled | Exit event | Net PnL sign | R-multiple (typical) |
|---|---|---|---|
| 1/3 (only E1) | TP1+TP2+TP3 | + | +1.5R to +2.5R |
| 1/3 | SL | − | −0.3R (smaller pos → smaller loss) |
| 1/3 | Time-stop | ≈0 | −0.1R to +0.1R |
| 2/3 | TP1+TP2+TP3 | ++ | +2.0R to +3.0R |
| 2/3 | SL | −− | −0.66R |
| 3/3 | TP1+TP2+TP3 (full pass) | +++ | **+R_planned** (best case) |
| 3/3 | SL on full size | −−− | **−1R** (max planned loss) |
| 3/3 | TP1 + BE-stop rest | + | +0.3R to +0.6R |
| 3/3 | TP1 + TP2 + trail-stop | ++ | +1.0R to +1.8R |
| 3/3 | TP1 + gap-down catastrophe | − to 0 | −0.4R to −0.7R |

## 4) Rules of engagement (invariant)

| # | Rule | Anti-pattern |
|---|---|---|
| R1 | **Never lower SL** after E2/E3 fill — invalidation defined once | "Give it more room" → SL drift, max loss explodes |
| R2 | **Never raise TP1** mid-trade | Greed; corrupts pre-registration |
| R3 | After TP1 → **cancel unfilled entry tiers** | Adding to a winner raises avg basis and kills R/R |
| R4 | After TP1 → SL to BE (or TP1−ε with slippage buffer) | Trade must NOT be able to turn into a loss now |
| R5 | After TP2 → SL to TP1 (lock gain) or trailing | Runner loses "free option" if it retraces to BE |
| R6 | Time-stop = **market-out**, not GTC | Capital tied up = capital not in next trade |
| R7 | News invalidation > price action | Stop-loss protects against *price*, not *thesis* |
| R8 | "Hold or exit" discretion → only if **pre-decided** in brief | Ad-hoc discretion = leakage from forward-test to retrospective |
| R9 | Slippage > 20 bps on entry → **cancel rest of ladder** | Gap = new regime, planned prices stale |
| R10 | Simultaneous TP1 + partial E3 fill → priority TP1, cancel E3 | Auto-rebalance that grows the winning trade is a bug |

## 5) AlphaLens implementation gap vs. ideal

| Area | Today (PR #277 + #279 paper harness) | Ideal | Follow-up PR |
|---|---|---|---|
| 3 entry tiers (BUY limit) | submitter sends 3 limit orders post-brief-fill | as ideal | — |
| 3 TP tiers (SELL limit) | bracket children at each entry fill | as ideal | — |
| Disaster SL | stop-market below E3 | as ideal | — |
| Time-stop 60d | exit_manager cron daily; cancel-and-market-sell | as ideal | — |
| **Cancel unfilled entries after TP1** (R3) | ❌ not implemented | hook into exit_manager TP1-fill handler | PR-TBD |
| **SL → BE after TP1** (R4) | ❌ not implemented | replace stop-market with new stop at avg basis | PR-TBD |
| **SL → TP1 after TP2** (R5) | ❌ not implemented | replace stop again on TP2 fill | PR-TBD |
| News invalidation kill-switch | manual CLI (`paper cancel`) | OK on MVP | — |
| Catastrophic-gap detector | ❌ none | nice-to-have post-MVP | — |

R3/R4/R5 are the three obvious follow-ups. All modify state inside `exit_manager.py`; none need new Alpaca API surface. Sequence them AFTER 3–5 days of stable manual flow on TEST account so the baseline behaviour is observed before adding state transitions.

## 5) Open issues from zen review (2026-05-28)

Zen `mcp__zen__codereview` with `deepseek/deepseek-v4-pro` + `thinking_mode="high"` flagged five categories of gaps against the playbook above. Captured here verbatim as warnings; existing R1-R10 are NOT modified. Each block translates into either a rule extension, a fork-decision, or an implementation contract.

### 5.1 Logic holes (4 real gaps)

| ID | Hole | Required fix |
|---|---|---|
| L1 | **TP1 + SL same bar** — playbook covers TP1+E3 (R10) but is silent on TP1 vs SL on a wide-range bar. | Add rule: "If TP1 and SL would both trigger on the same execution step, TP1 limit takes priority — cancel the stop BEFORE it fires." Without this, harness may fire SL first and turn a winner into a loss. |
| L2 | **Race TP1-fill vs E2/E3-fill** — entry-fill notification can arrive between TP1 confirmation and broker cancel-ack. | Atomic invariant: once TP1 fires, position is locked — any subsequent E2/E3 fill notifications are discarded/voided. Don't rely on cancel timing. |
| L3 | **Eg↓ "2× ladder span" undefined** — span = (E1−E3)? (E1−SL)? Between adjacent tiers? | Pin span = (E1 − E3) and cutoff = 2× explicitly in implementation, OR replace with a price-vs-yesterday-close ratio (e.g., gap > 5%). |
| L4 | **Partial-fill on SL stop-market** — playbook assumes full fill; in low-liquidity stocks Alpaca can fill partially. | Exit_manager must detect partial fill and either cancel-and-market-out the residual or convert to limit. Residual position = silent invariant violation. |

### 5.2 Anti-pattern smell on R4 (and R5)

**R4 (SL → BE after TP1) may systematically degrade expectancy** on trend-following / event-edge strategies. Normal post-event retracements knock out the BE-stop before the runner makes the real move. The original disaster SL was thesis-based — after TP1 the thesis hasn't changed, so tightening to BE encodes a price-action rule on top of a thesis-action stop.

Three alternatives to consider before implementing R4 as written:
- **A — Leave SL unchanged after TP1.** ⅔ runner stays on the original thesis-SL.
- **B — Partial lock.** Move SL to (avg − 0.5R), locking in part of the initial-risk budget but giving the runner more breathing room than BE.
- **C — Trailing ATR engaged only after larger cushion.** Don't engage until price reaches, say, TP1.5 — gives the runner room until it's already deeper in profit.

R5 (SL → TP1 after TP2) inherits the same critique with smaller magnitude. **Decision deferred** — defaults from R4 carry into R5.

This is a **fork-decision before implementing the PRs**. Pick A/B/C/original-R4 with an empirical justification (backtest on the verified-candidate history would resolve it).

### 5.3 Missing rules (R11-R16 candidates)

| ID | Rule | Rationale |
|---|---|---|
| R11 | **Max concurrent positions per GICS sector ≤ 2** | Event-driven thematic-news clusters into sectors (e.g., multiple biotech catalysts same week). Without a cap, portfolio becomes inadvertently sector-bet. |
| R12 | **Daily loss circuit-breaker** — if cumulative day-PnL ≤ −X% of equity, halt new entries for 24h + cancel pending GTC entries | Catches regime-change days where multiple stops fire; mimics discretionary "step back and reassess". |
| R13 | **Liquidity floor** — ADV (in $) > Y AND median bid-ask < Z bps; reject otherwise | Prevents partial-fill / slippage issues. Also useful sanity check before any future real-capital deployment. |
| R14 | **Dividend / split / corporate-action handling** — on ex-div, do NOT lower SL (dividend compensates); if ex-div size > 50 bps of entry-zone width, cancel the trade pre-ex-date | Otherwise SL fires on cash-distribution mechanics, not thesis. |
| R15 | **Extended-hours stop eligibility policy** — explicit decision: stops active only in RTH, or also in pre/post-market | Affects whether X8 (catastrophic gap) is even possible — if stops only RTH, gap-through fires at open as designed; if extended, fills may print at wider spreads. |
| R16 | **No-re-entry window after Eg↑ cancel** — 5 trading days minimum | Otherwise tempted to re-engage on every minor pullback below cancelled E1; overtrading. |

R11-R16 are **candidates**, not yet locked. R11 + R12 are the highest-priority (correlation + regime-change protection). R13-R16 can ride as a single rules-PR later.

### 5.4 Alpaca operational gotchas

| ID | Gotcha | Implementation contract |
|---|---|---|
| A1 | **OCO bracket-orders cancel TP2/TP3 when you modify SL** — Alpaca's bracket OCO ties TP/SL legs to the parent; modifying the stop tears down the TP siblings. | **Do NOT use OCO for 3 TP tiers.** Submit 3 separate GTC SELL-limit orders + 1 standalone GTC stop-market. PATCH `stop_price` to update SL in-place; fall back to cancel-and-replace on PATCH failure. |
| A2 | **Out-of-order fill notifications** — WebSocket fills can arrive non-monotonic in clock-time, especially on gap bars. | Buffer fill events ~50ms then apply in priority order: TP1 > SL > entry. Time-stop / news-invalidation override all. |
| A3 | **Cancel-storm vs rate limit** — 200 req/min/key. A catastrophic-gap event may need to cancel dozens of GTC entries + stops at once. | Use `/v2/orders` batch cancel-all endpoint when wiping the basket; pace per-order cancels otherwise. |
| A4 | **Time-stop racer** — cron-based time-stop fires asynchronously vs. live exit events. | Time-stop handler must check "position still open?" right before submitting market-sell; once market-sell is sent, ignore any subsequent GTC fills and cancel TP/SL siblings. |

### 5.5 Implications for the R3/R4/R5 PR sequence

Zen recommends **single bundled PR** for the exit state machine over 3 separate PRs. Reason: TP1-handler must atomically `{cancel E2/E3, replace stop}`; splitting leaves an intermediate state where R3 ships but the disaster SL still covers the full ⅔ runner — internally inconsistent for several days.

**If splitting anyway** (granular review), the order **R3 → R4 → R5 is correct** (NOT a different order). R3 must precede R4 because if R4 ships first and an unfilled E2/E3 fills between TP1-confirmation and cancel-ack (L2 race), the new shares get a BE-stop set above their cost basis = guaranteed lock-in loss on those shares.

The 5.2 fork on R4 must be resolved BEFORE either path. If A (leave SL unchanged) wins, then R4 collapses to a no-op and the sequence becomes R3 → R5 only (with R5 also possibly collapsing).

### 5.6 Entry-TTL not yet enforced (impl gap discovered 2026-05-28)

**Symptom:** All 54 NEW orders on the live TEST account sit as GTC indefinitely. None will auto-cancel after their nominal `order_ttl_days` elapses.

**Why:** `paper/constants.py::DEFAULT_ORDER_TTL_DAYS = 10` and `trade_setup/builder.py::_DEFAULT_ORDER_TTL_DAYS = 10` lock the intended horizon. Planner persists `order_ttl_days` on every `plans` row (`paper/ledger.py:81`). But neither `paper/reconciler.py` nor `paper/exit_manager.py` contains code that reads back `order_ttl_days` and cancels expired entry orders — grep for `order_ttl_days` / `days_since` / `ttl_expired` returns zero matches in those two files. The docstring contract at `alpaca_client.py:192` ("Phase A uses GTC for entries — cancel via TTL in reconciler") records the intent but not the implementation.

**Impact (today):**
- Entries that never reach their limit price stay alive forever instead of clearing the basket on day 10.
- Capital-cap accounting (gross-notional) keeps reserving size for stale tiers that the playbook says should already be free.
- Operator must manually cancel via `alphalens paper cancel` (or Alpaca dashboard) to free capital — adds discretionary touch the playbook forbids (R8).

**Fix:** Add a TTL-sweep step inside `reconciler.reconcile_orders`. For every PLANNED row, compute `days_since = (today − created_at).days`; if `days_since ≥ order_ttl_days` AND status ∈ {SUBMITTED, PARTIALLY_FILLED} on any ENTRY-side order for that plan, cancel those Alpaca orders + write `outcome=UNFILLED_TTL` (new outcome kind) for plans with zero entry fills, OR proceed to attach exits + close-out the partial position per the existing UNFILLED-with-fills branch.

**Sequence:** This is **pre-requisite for R3/R4/R5** — the same TTL-sweep code path will also house the R3 "cancel unfilled entries after TP1" logic (both are "cancel-unfilled-entry" actions, just triggered by different conditions). Implement TTL-sweep as the structural foundation, then R3 hooks into the same helper with `trigger=TP1_FILL` instead of `trigger=TTL_EXPIRED`.

**Status:** Open. TODO sub-tasks to be tracked once user OKs the work.
