# Paper-Trading Capital Sizing — Design Memo v1

**Date:** 2026-05-28
**Status:** **LOCKED (v2)** — locks operational params for Phase A. Adversarial review (zen `deepseek-v4-pro` + Perplexity `sonar-deep-research`) completed pre-lock; both inputs reconciled in §2. v2 (2026-05-28) supersedes v1 per-candidate-cap sizing with global multiplicative scaling after a second-round zen review (`64622363-774f-40f8-a98d-f010d7165e38`) flagged composition bias in v1; see §2.3.
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

### §2.3 Why we side with Perplexity on sizing — and how

Inspection of `apps/alphalens-pipeline/alphalens_pipeline/thematic/trade_setup/sizing.py:56-80` showed `suggested_size_pct` is computed as:

```
suggested_size_pct = min(
    risk_budget_pct × Σ(q_i × E_i / (E_i − S)),
    _MAX_EXPOSURE_PCT  # 25.0
)
```

with `risk_budget_pct = 1.0%` and equal weights `q_i = 1/n` per tier. Inputs are purely price-based (entry tiers, stop). **Semantics: equal-risk sizing — every candidate risks 1% of book to its stop.** It is not signal-quality weighting, not vol-conditional, not a placeholder. Tight stop → larger position; wide stop → smaller.

**This matches Perplexity's argument**: `suggested_size_pct` encodes a real per-position risk budget, and arbitrarily replacing it with $2k flat breaks the relationship between entries / stop / TP that the design memo (`thematic_trade_setup_v1_design_2026_05_27.md` §7.3) calibrated.

**BUT the design assumed cherry-pick context (1–3 concurrent positions per user)**. Batch paper-trading 6–8/day × longer hold operates in a *different* portfolio context where naive `suggested_size_pct` × L blows up.

#### Why per-candidate `min(suggested, 1/N_FIXED)` is the wrong reconciliation

The v1 reconciliation (now superseded — see zen review 2026-05-28) used a per-candidate ceiling: `effective_size_pct = min(suggested_size_pct, 100/N_FIXED)`. Adversarial review (deepseek-v4-pro, continuation `64622363-774f-40f8-a98d-f010d7165e38`) flagged this as a **composition bias**: the cap binds for ~95% of candidates (because typical `suggested_size_pct` is 5–8% while `100/N_FIXED ≈ 0.278%`), so almost every position is flattened to the same uniform notional ~$2,778. The relative differences encoded by equal-risk sizing (wide stop → larger; tight stop → smaller) collapse, contradicting the stated objective of honoring Perplexity's measurement-validity argument.

#### Locked v2 reconciliation: global multiplicative scaling

Replace the per-candidate cap with a **daily global scale factor** that preserves inter-candidate ratios while bounding aggregate steady-state gross:

```
daily_target_gross_dollars    = STEADY_STATE_GROSS_FRAC × paper_equity
                                  / EXPECTED_AVG_HOLD_DAYS
aggregate_uncapped_notional   = Σ_i (suggested_size_pct_i / 100) × paper_equity
                                  for all plannable candidates today
scale_factor                  = min(1.0, daily_target_gross_dollars
                                          / aggregate_uncapped_notional)
final_size_pct_i              = suggested_size_pct_i × scale_factor
total_notional_i              = final_size_pct_i / 100 × paper_equity
per_tier_qty                  = floor(total_notional_i × alloc_pct_i / 100
                                       / limit_price_tier)
```

With `STEADY_STATE_GROSS_FRAC = 0.667` and `EXPECTED_AVG_HOLD_DAYS = 30` the **steady-state aggregate gross** matches v1's Little's Law derivation (the equivalence: `STEADY_STATE_GROSS_FRAC / EXPECTED_AVG_HOLD_DAYS = 0.0222 ≈ L / N_FIXED / W`, integrated over W=30d hold ≈ 0.667 = L/N_FIXED). The per-candidate notional in v2 NO LONGER matches v1's flat $2,778 — that uniform value was the v1 bug. Instead, each candidate's final size is proportional to its raw `suggested_size_pct`: **a candidate with `suggested = 8%` gets a position 33% larger than one with `suggested = 6%`**, instead of both being flattened to the cap. The same average per-candidate notional only emerges if every brief contains candidates with identical `suggested_size_pct`, which is not the case in practice.

If `aggregate_uncapped_notional` is zero (no plannable candidates today), `scale_factor` defaults to `1.0` — the value is moot since the planner will not consume it for any candidate, but the default avoids a division-by-zero edge case in `compute_daily_scale_factor`.

**Daily-variance trade-off, accepted:** on quiet days (3 candidates) the scale is 1.0 and each gets full suggested size; on busy days (15 candidates) the scale tightens. This is the inverse of v1, where the per-candidate cap forced the same size every day. The observation framing (R-multiple is size-independent) holds either way; the scale-based version additionally measures whether the trade_setup's heterogeneous calibration adds signal.

**N_FIXED retained as cross-check, not as binding constraint:** §3 still cites N_FIXED=360 for Little's Law derivation, but the planner uses `STEADY_STATE_GROSS_FRAC / EXPECTED_AVG_HOLD_DAYS` directly. The two formulations are equivalent at steady state; v2 just routes through the input that makes the ratio-preservation explicit.

### §2.4 Why we side with Zen on same-ticker — pragmatic, not statistical

Perplexity's correlation-0.85 argument is statistically correct in general event-driven literature. For our universe specifically, same-ticker overlap within 60d is rare (thematic catalysts are usually one-shot per ticker; insider-form-4 reactivations cluster but are minority). MVP simplification: **skip the new candidate if any open position exists on that ticker; shadow-log the skip for retrospective evaluation**. Revisit after first month of data — if skip rate >10%, upgrade to scale-in/out per Perplexity.

## §3. Locked operational parameters

| Parameter | Value | Source |
|---|---|---|
| Paper venue | `paper-api.alpaca.markets` | Alpaca sandbox; live URL structurally rejected |
| Target paper equity | $1,000,000 | Alpaca paper grant request; default $100k acceptable interim with scaled positions |
| Universe filter | verified-only (4-gate pass) | matches what a user would seriously consider |
| Time-stop W (max hold) | **60 days from first fill** | §4 decision |
| Expected avg hold (`EXPECTED_AVG_HOLD_DAYS`) | **30 days** | TP / SL typically exits before time-stop; revisit after first month |
| **`STEADY_STATE_GROSS_FRAC`** | **0.667** | matches paradigm-14 Little's Law target (peak concurrent 240 × 1/N_FIXED) |
| Per-candidate sizing | `final = suggested × scale_factor` (see §2.3) | global daily scaling preserves ratios |
| `scale_factor` formula | `min(1.0, (STEADY × equity / W) / Σ_i suggested_notional_i)` | global multiplicative; binds only when daily aggregate exceeds target |
| N_FIXED (historical cross-check, not binding) | 360 = L · 1.5 with L = λW = 8 · 30 | retained for cross-validation; planner does not use directly |
| Gross safety guard | block new orders if `cumulative_gross_notional + new > 1.0 × equity` | belt-and-suspenders; should rarely bind even on busy days |
| Same-ticker policy | **skip new candidate if open position exists; shadow-log** | §2.4 |
| Intra-run duplicate ticker policy | **skip second occurrence in same brief; shadow-log with `duplicate_ticker_in_brief`** | post-zen 2026-05-28 fix |
| Entry order type | limit-GTC at each tier price | matches `brief_trade_setup.entry_tiers[].limit` |
| Entry order TTL | `order_ttl_days` from `brief_trade_setup` (default 10) | cancel unfilled limits after TTL |
| Exit order plumbing | per-tranche limit-sells + single stop-loss at `disaster_stop` | multi-tranche TP ladder doesn't fit Alpaca's single-leg BRACKET; reconciler orchestrates |
| Position selection bias | **no hard cap, no skip-when-full** | §2.1 |
| Shadow-log reasons | Complete enumeration: `not_verified`, `no_trade_setup`, `unplannable_setup`, `same_ticker_open`, `gross_cap_block`, `duplicate_ticker_in_brief` | every reason has a structured `details_json` blob; query with `SELECT reason, COUNT(*) FROM shadow_log GROUP BY reason` |

## §4. Time-stop = 60 days — rationale

`brief_trade_setup` ships **no position time-stop** (only entry-limit TTL `order_ttl_days = 10`). The design (§3 of trade_setup memo) treats positions as TP-or-SL exits indefinitely. For paper-trade observation that is structurally problematic — without a backstop, `W → ∞` in Little's Law and L diverges; zombie positions accumulate forever.

Three options were considered:

1. **No time-stop (design-faithful)** — accept that 1–2% of positions never exit. Distorts N_FIXED upward without bound.
2. **30-day time-stop** — aligns with generic news-catalyst half-life. Conservative.
3. **60-day time-stop** ← **CHOSEN**. Empirical event-driven horizons:
   - PEAD drift literature **as an analogy** (NOT a direct fit — our candidates are second-order beneficiaries of thematic news, not post-earnings drift): bulk of measurable drift complete by ~day 60 (Bernard-Thomas 1989; Chordia-Shivakumar 2006). The PEAD horizon is the closest formal literature anchor for "the catalyst's price impact has largely played out."
   - **Chan, Jegadeesh & Lakonishok 1996 — closest primary fit:** examines momentum in stocks sorted on past returns AND on news / earnings events. Findings: news-driven stock-specific drift decays over ~6 months; reaction is front-loaded. Single-stock universe + news catalyst = direct map to our setup.
   - **Moskowitz, Ooi & Pedersen 2012 (time-series momentum):** documents ~30–90d momentum persistence in equity index futures, commodity, currency and bond futures. Bridging assumption needed — MOP is a macro time-series result, not cross-sectional single-stock — but the horizon evidence transfers as an upper-bound on the relevant persistence window.
   - Matches the longest catalyst-floor horizon used by L4 scoring (`apps/alphalens-pipeline/alphalens_pipeline/scorers/catalyst_floor.py`).

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

**Revisit trigger — operator runbook**. Run this query at the end of the first month of paper-trading:

```sql
SELECT
    SUM(CASE WHEN reason = 'same_ticker_open' THEN 1 ELSE 0 END) * 1.0
        / NULLIF(COUNT(*), 0) AS skip_rate
FROM shadow_log
WHERE brief_date >= date('now', '-30 days');
```

If `skip_rate > 0.10`, escalate to a Phase-B follow-up PR implementing Perplexity's scale-in / scale-out (correlation-0.85 evidence becomes relevant). Below 10% the skip-and-shadow policy continues to dominate on simplicity grounds.

## §6. Gross safety guard

The v2 global scaling (§2.3) keeps daily aggregate gross at-or-below `STEADY_STATE_GROSS_FRAC / EXPECTED_AVG_HOLD_DAYS × equity` ≈ 2.2% of book per day. The guard is a defense against:

- realized λ spikes above 8/day during burst news periods (still bounded by `scale_factor ≤ 1.0`, but checks at insert time)
- `EXPECTED_AVG_HOLD_DAYS` estimation error (we picked 30; if real avg is 50+ the steady-state target under-projects)
- compounded same-ticker overlaps (rare but possible across themes)

Rule: at order-planning time, if `current_gross_notional + planned_notional > 1.0 × paper_equity`, **block** all of the new candidate's orders and shadow-log with reason `gross_cap_block`. Do **not** partial-fill the ladder. Do **not** scale down to fit (would silently distort sizing — the scale-down is the planner's job, computed once globally per day; per-candidate downsizing here would double-count). The block is loud — it surfaces a planning issue rather than silently degrading observation.

Expected binding frequency: <1% of planned candidates under normal operation. If >5% over the first 30 days, recompute `STEADY_STATE_GROSS_FRAC` with empirical W and increase paper_equity.

#### §6.1 Behaviour after a planning crash + on partial fills

The planner uses per-candidate transactions inside one ledger session (`insert_planned` wraps each candidate's plans + plan_entries + plan_exits in a single `BEGIN/COMMIT`). A mid-batch crash leaves the candidates processed so far committed and the rest unwritten. Recovery: re-run with `--force`, which deletes that date's rows from `plans` + `shadow_log` before re-planning. The `UNIQUE(brief_date, ticker)` constraint protects against accidental duplicate inserts on crash-then-rerun-without-`--force`.

The gross guard is a **plan-time** check only; it does NOT re-evaluate after fills. A partial fill that brings live exposure above the cap is NOT a guard violation — the guard's job is bounding NEW planned notional, not pruning existing positions. This is by design: post-fill re-balancing introduces churn and violates paradigm-14's "no forced rebalancing" doctrine.

**But detection of slow-creep drift cannot be "operator observes" handoff** (zen review §6.1 HIGH 2026-05-28). Cumulative partial fills can creep past 1.0× equity over weeks without any single day's planner run noticing. Two concrete operator-actionable detection paths:

**Path A — operator weekly query** against the paper ledger (run during the Sunday review):
```sql
SELECT
    SUM(qty * limit_price)
      FROM plan_entries pe
      JOIN plans p ON p.plan_id = pe.plan_id
      WHERE p.brief_date >= date('now', '-60 days')  -- positions still potentially open
        AND p.status = 'PLANNED'
    AS planned_aggregate_60d;
-- compare to paper_equity (current Alpaca account.equity). If planned_aggregate_60d
-- / paper_equity > 1.0 over multiple weeks, drift is happening — escalate.
```

**Path B — automated in the PR 3 reconciler** (cleaner, mandatory before PR 3 ships): at the end of each reconcile cycle, compute live `total_filled_notional = SUM(filled_qty × fill_price)` from the upcoming `fills` table, divide by `equity` from `AlpacaClient.get_account()`, and `logger.warning("live gross %.2f%% of equity (over 100%%)", ratio * 100)` if `> 1.0`. This is the closed-loop check the §6.1 gap currently lacks.

PR 3 will land Path B; until then, Path A is the operator's manual fallback. If sustained drift is observed by either path, escalate to a Phase-B follow-up tightening `STEADY_STATE_GROSS_FRAC`.

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
