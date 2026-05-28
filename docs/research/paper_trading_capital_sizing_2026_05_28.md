# Paper-Trading Capital Sizing — Design Memo v1

**Date:** 2026-05-28
**Status:** **LOCKED** — locks operational params for Phase A. Adversarial review (zen `deepseek-v4-pro` + Perplexity `sonar-deep-research`) completed pre-lock; both inputs reconciled in §2.
**Track:** Thematic event-driven decision-support tool (parallel to factor-paradigm-search). **NOT** a paradigm test under doctrine 3.5 — paper trading is a FORWARD-OBSERVATION harness for the deterministic `brief_trade_setup` ladder shipped in PR #262. No real capital. No edge claim.
**Companions:** [`thematic_trade_setup_v1_design_2026_05_27.md`](thematic_trade_setup_v1_design_2026_05_27.md), [`thematic_outcome_tracking_v1_design_2026_05_26.md`](thematic_outcome_tracking_v1_design_2026_05_26.md), [`paradigm14_pead_cost_model_audit_2026_05_14.md`](paradigm14_pead_cost_model_audit_2026_05_14.md) (Little's Law precedent).

---

## §0. Problem statement

The daily thematic pipeline emits ~6–8 verified candidates per session, each with a deterministic `brief_trade_setup` (3-tier entry ladder, 2–3 TP tranches, disaster stop, `suggested_size_pct`). The user-facing intent is **cherry-pick** (WhatsApp group picks 1–2 names, discusses, each member decides). We now want a **forward-observation harness** that paper-trades **every** verified candidate and tracks:

- fill rate per entry tier
- TP-hit / SL-hit / time-stop frequency
- realized R-multiple distribution
- whether 4-gate-pass correlates with positive outcomes

The harness uses Alpaca paper sandbox (`paper-api.alpaca.markets`). The point is **observation quality**, not return maximization, not edge proof.

## §1. Capital math problem

Naive sizing collapses within ~14 sessions:

- λ ≈ 6–8 verified candidates/day
- W = position hold time (TP / SL / time-stop)
- L = λ·W (Little's Law) = peak concurrent positions
- naive `suggested_size_pct ≈ 5–8%` × L = several hundred % gross book

This is the **same shape** as the paradigm-14 PEAD v2 cost-model audit (`paradigm14_pead_cost_model_audit_2026_05_14.md` §5). The proven response there:

> N_FIXED = peak_concurrent + 50% safety margin
> per-position weight = 1 / N_FIXED
> no forced rebalancing — peak overlap absorbed by pre-allocated capacity

That doctrine carries over with two adjustments specific to paper-trade:

1. We **honor** `suggested_size_pct` as the upper bound (Perplexity's measurement-validity argument — see §2), capped at `1/N_FIXED`. For ~95% of candidates the cap binds; for very-wide-stop names the natural smaller size carries through.
2. There is no real cost-amplification risk (paper). The bound is purely about **gross capacity**, not about churn cost.

## §2. Adversarial review summary

Both reviewers ran 2026-05-28 (continuation IDs in session transcript). Reconciliation:

### §2.1 Convergent findings (both reviewers agree)

| Question | Verdict | Evidence |
|---|---|---|
| Shorten time-stop 21d → 5d to control concurrency? | **REJECT** | Perplexity: 30–40% edge decay; PEAD literature places 30–40% of drift in days 4–10. Zen: changes the strategy being observed. |
| Hard cap `N_OPEN_MAX = 50` with skip-when-full? | **REJECT** | Perplexity quantifies 15–25% selection bias under normal vol, escalating to 60–70% exclusion in high-vol regimes (exactly when signal is strongest). Zen: first-come-first-serve biases dataset toward early-week briefs. |
| Default $100k paper equity? | **TOO SMALL** | Both: request $1M–$2M; Alpaca paper grants are routine. |

### §2.2 Divergent findings

**Sizing methodology** — Zen recommends fixed-dollar $2k/tier (simple, equal observation per trade, R-multiple is size-independent). Perplexity recommends honoring `suggested_size_pct` (literature: arbitrary sizing introduces 15–20% measurement error in R-multiple distribution; preserves paper→live consistency).

**Same-ticker overlap** — Zen recommends independent bets, separated by `candidate_id` (clean per-candidate R-multiple). Perplexity recommends scale-in/scale-out (literature: same-ticker signals exhibit correlation 0.85–0.95 → treating as independent inflates volatility 35–40% without return benefit).

### §2.3 Why we side with Perplexity on sizing — but with a critical caveat

Inspection of `apps/alphalens-pipeline/alphalens_pipeline/thematic/trade_setup/sizing.py:56-80` showed `suggested_size_pct` is computed as:

```
suggested_size_pct = min(
    risk_budget_pct × Σ(q_i × E_i / (E_i − S)),
    _MAX_EXPOSURE_PCT  # 25.0
)
```

with `risk_budget_pct = 1.0%` and equal weights `q_i = 1/n` per tier. Inputs are purely price-based (entry tiers, stop). **Semantics: equal-risk sizing — every candidate risks 1% of book to its stop.** It is not signal-quality weighting, not vol-conditional, not a placeholder. Tight stop → larger position; wide stop → smaller.

**This matches Perplexity's argument**: `suggested_size_pct` encodes a real per-position risk budget, and arbitrarily replacing it with $2k flat breaks the relationship between entries / stop / TP that the design memo (`thematic_trade_setup_v1_design_2026_05_27.md` §7.3) calibrated.

**BUT the design assumed cherry-pick context (1–3 concurrent positions per user)**. Batch paper-trading 6–8/day × longer hold operates in a *different* portfolio context where naive `suggested_size_pct` × L blows up. The reconciliation: **honor the relative sizing between candidates, but scale absolute sizes via a global N_FIXED cap so peak gross stays bounded**.

### §2.4 Why we side with Zen on same-ticker — pragmatic, not statistical

Perplexity's correlation-0.85 argument is statistically correct in general event-driven literature. For our universe specifically, same-ticker overlap within 60d is rare (thematic catalysts are usually one-shot per ticker; insider-form-4 reactivations cluster but are minority). MVP simplification: **skip the new candidate if any open position exists on that ticker; shadow-log the skip for retrospective evaluation**. Revisit after first month of data — if skip rate >10%, upgrade to scale-in/out per Perplexity.

## §3. Locked operational parameters

| Parameter | Value | Source |
|---|---|---|
| Paper venue | `paper-api.alpaca.markets` | Alpaca sandbox; live URL structurally rejected |
| Target paper equity | $1,000,000 | Alpaca paper grant request; default $100k acceptable interim with scaled positions |
| Universe filter | verified-only (4-gate pass) | matches what a user would seriously consider |
| Time-stop W | **60 days from first fill** | §4 decision |
| Concurrency peak L (Little's Law) | λ·W = 8 · 30 (realistic avg hold) = 240 | conservative: most positions exit on TP/SL before time-stop |
| Safety margin | +50% | paradigm-14 doctrine |
| **N_FIXED** | **360** | L · 1.5 |
| Per-position size | `min(suggested_size_pct, 1 / N_FIXED) × equity` | §2.3 reconciliation |
| Per-position cap @ $1M equity | $1,000,000 / 360 ≈ **$2,778** | binding for ~95% of candidates |
| Gross safety guard | block new orders if `cumulative_gross_notional + new > 1.0 × equity` | belt-and-suspenders; should rarely bind |
| Same-ticker policy | **skip new candidate if open position exists; shadow-log** | §2.4 |
| Entry order type | limit-GTC at each tier price | matches `brief_trade_setup.entry_tiers[].limit` |
| Entry order TTL | `order_ttl_days` from `brief_trade_setup` (default 10) | cancel unfilled limits after TTL |
| Exit order plumbing | OCO sells per TP tier (sized by `tranche_pct`) + single stop-loss at `disaster_stop` | matches `brief_trade_setup.tp_tranches` + `disaster_stop` |
| Position selection bias | **no hard cap, no skip-when-full** | §2.1 |
| Shadow log | **every** verified candidate logged (including same-ticker skipped + (rare) gross-guard blocked) | retrospective analysis without rerun |

## §4. Time-stop = 60 days — rationale

`brief_trade_setup` ships **no position time-stop** (only entry-limit TTL `order_ttl_days = 10`). The design (§3 of trade_setup memo) treats positions as TP-or-SL exits indefinitely. For paper-trade observation that is structurally problematic — without a backstop, `W → ∞` in Little's Law and L diverges; zombie positions accumulate forever.

Three options were considered:

1. **No time-stop (design-faithful)** — accept that 1–2% of positions never exit. Distorts N_FIXED upward without bound.
2. **30-day time-stop** — aligns with generic news-catalyst half-life. Conservative.
3. **60-day time-stop** ← **CHOSEN**. Empirical event-driven horizons:
   - PEAD drift literature: bulk of drift complete by day 60 (Bernard-Thomas 1989, Chordia-Shivakumar 2006)
   - thematic-momentum decay: 30–90d typical (varies by theme freshness)
   - matches the longest catalyst-floor horizon used by L4 scoring

60d is the **upper realistic bound** under which a thematic position still carries the original catalyst's information. Beyond that, exit is mechanical risk control, not signal capture. Position closes at next-session market open via market sell.

If empirical avg hold (measured after first month) drifts much above 30d, revisit — but the **decision-rule structure** (TP > SL > time-stop) is locked.

## §5. Position-overlap policy

```
on new verified candidate C for ticker T:
  if exists open paper position P where P.ticker == T:
    shadow_log(C, reason='same_ticker_open', existing_position=P.id)
    skip
  else:
    proceed to plan
```

The shadow_log preserves the candidate row + its `brief_trade_setup` so retrospective analysis can ask "what would have happened if we had scaled in instead of skipped?" without rerunning the pipeline. Phase B may upgrade to scale-in/out per Perplexity §2.2 once we have evidence on real overlap frequency.

## §6. Gross safety guard

The N_FIXED-based per-position cap should keep peak gross ≤ ~66% under Little's Law assumptions. The guard is a defense against:

- realized λ spikes above 8/day during burst news periods
- N_FIXED estimation error (we picked W=30 avg; if real avg is 50+ the math breaks)
- compounded same-ticker overlaps (rare but possible across themes)

Rule: at order-planning time, if `current_gross_notional + planned_notional > 1.0 × paper_equity`, **block** all of the new candidate's orders and shadow-log with reason `gross_cap_block`. Do **not** partial-fill the ladder. Do **not** scale down to fit (would silently distort sizing). The block is loud — it surfaces a planning issue rather than silently degrading observation.

Expected binding frequency: <1% of planned candidates under normal operation. If >5% over the first 30 days, recompute N_FIXED with empirical W and increase paper_equity.

## §7. R-multiple computation

Per-position R-multiple is computed at exit:

```
R = (avg_fill_price_exit − blended_entry_price) / (blended_entry_price − disaster_stop)

# blended_entry_price = volume-weighted across actually-filled tiers
# exit pathways:
#   TP-fill: sells executed at TP tranches → blended exit price
#   SL-fill: single market sell at SL trigger price
#   time-stop: market sell at next-session open after 60d from first fill
#   unfilled (no entry tier hit within order_ttl_days): R = NaN, log fill_status='no_entry'
```

R-multiple is **size-independent**. Scaling all positions via N_FIXED does not bias R-multiple measurements (per Zen's observation). This is the primary metric for evaluating whether the deterministic trade-setup design has structural quality.

## §8. Implementation

PR sequence (this memo = PR 0):

| PR | Scope | Locks |
|---|---|---|
| **PR 0** | This memo (doc-only) | All §3 params |
| **PR 1** | `alpaca_client.py` canonical wrapper + `alpaca-py` dep + `test_no_raw_alpaca_http.py` enforcement | Vendor client doctrine |
| **PR 2** | `apps/alphalens-pipeline/alphalens_pipeline/paper/` — SQLite ledger (`~/.alphalens/paper_ledger.db`) + `alphalens paper plan` reading verified candidates from Django DB | Sizing math, shadow-log schema |
| **PR 3** | `alphalens paper submit` + `alphalens paper reconcile` — limit-GTC entries, OCO exits, 60d time-stop, dedup | Order lifecycle |
| **PR 4** | `alphalens paper report` CLI — per-candidate R-multiple, fill rate, hit rate distributions | Phase A complete |
| **Phase B** | Django endpoint + web tab for outcomes; possible upgrade to scale-in/out same-ticker per §2.4 | Deferred until ≥10 closed positions |

## §9. What this memo does NOT do

- **Does NOT claim edge.** Paper-trade observation is a measurement instrument, not a strategy validation. Verdicts about whether the deterministic ladder has positive expected R require ≥100 closed positions and adversarial review of any claimed bias.
- **Does NOT change the pipeline.** `suggested_size_pct` calculation in `trade_setup/sizing.py` is unchanged; the paper-trade layer interprets it under a different portfolio context (batch vs cherry-pick).
- **Does NOT replace the WhatsApp group decision flow.** The thematic tool's tool-of-augmentation doctrine (per `project_thematic_tool_pivot_2026_05_16`) is unaffected. Paper-trade is sidecar telemetry.
- **Does NOT touch real capital.** Alpaca live API base URL is structurally rejected by the client wrapper (PR 1).

## §10. References

- `docs/research/thematic_trade_setup_v1_design_2026_05_27.md` — source of `brief_trade_setup` schema + `suggested_size_pct` semantics
- `docs/research/paradigm14_pead_cost_model_audit_2026_05_14.md` — Little's Law precedent + N_FIXED + 1/N + no forced rebalancing doctrine
- `docs/research/thematic_outcome_tracking_v1_design_2026_05_26.md` — companion outcome-tracking instrument (different angle: CAR not R-multiple, all candidates not just paper-filled)
- Bernard, V. L. & Thomas, J. K. (1989). "Post-earnings-announcement drift: Delayed price response or risk premium?" *Journal of Accounting Research* 27 (Suppl.), 1–36.
- Chordia, T. & Shivakumar, L. (2006). "Earnings and price momentum." *Journal of Financial Economics* 80(3), 627–656.
- Adversarial review transcripts: session `77abc9d4-0e29-42f9-b367-4864c72e3181`, zen continuation `5eae14a9-69e2-4e56-8b8a-406476d86246`, Perplexity tool-result `toolu_011MRxHDWJ7ppJct6c2PjY7C`.
