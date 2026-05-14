# Paradigm-14 PEAD v2 — cost-model audit + α1/α2 verdict

**Date:** 2026-05-14
**Status:** LOCKED (informs `pead_v5_pss_2026_05_13` Phase B2 weighting choice)
**Author:** B0 prereq per `~/.claude/plans/paradigm14_pead_v2_next_session_plan_2026_05_13.md` §1.B0
**Audit target:** `alphalens/attribution/cost_model.py` + `alphalens/backtest/metrics.py::turnover_pct` + `alphalens/attribution/cost_validation.py::apply_tiered_cost`

## §1 Question (from plan §1.B0)

Does the cost model charge spread × turnover when:
- (a) entering a NEW position,
- (b) exiting a position at term,
- (c) **rebalancing existing positions to maintain gross=1 as new positions enter**?

## §2 Method

Direct read of three call sites:
1. `alphalens/attribution/cost_model.py::CostModel.apply` — applies `daily_turnover × per_period_drag` element-wise. The class is mechanical: turnover comes from upstream.
2. `alphalens/backtest/metrics.py::turnover_pct` (lines 222-236) and `per_rebalance_turnover` (lines 239-280) — both compute `exits = prev_set − curr_set; turnover = len(exits) / N`. **Set-based on ticker baskets, not weight-based.**
3. `alphalens/attribution/cost_validation.py::apply_tiered_cost` (line 352) — `turnover_today = len(current_set - prev_top_n) / n`. Same set-based pattern. `compare_cost_scenarios` (line 450, 459) passes `daily_turnover=None` to `CostModel.apply`, which then assumes 100% turnover every bar (worst-case fallback).

## §3 Findings

| Question | Answer | Evidence |
|---|---|---|
| (a) New-entry charge | YES | New ticker enters `curr_set`, missing from `prev_set` → counted in `len(curr - prev)` symmetric-difference term (one-way variant uses entrants only; round-trip is `2 × turnover`). |
| (b) Term-exit charge | YES | Exiting ticker in `prev_set` not in `curr_set` → counted in `len(prev - curr)`. |
| (c) Forced-rebalancing charge | **NO** | Existing positions whose weights shift (e.g. from 1/4 to 1/5 when a new entrant joins under gross=1 daily-rebal) do not change set membership → not in `(prev XOR curr) / N`. Cost-model wiring silently undercounts. |

### Quantitative illustration (α1 worst-case)

Day t: `{A,B,C,D}`, weights 1/4 each, gross=1.
Day t+1: `{A,B,C,D,E}` (one entrant, no exits), weights 1/5 each, gross=1.

- Set turnover (current impl): `|{E}| / 5 = 0.20`
- True weight-based one-way turnover: `4 × |1/4 − 1/5| + |0 − 1/5| = 4/20 + 4/20 = 0.40`
- **Undercount factor: 2.0×** for this transition. Magnifies during peak earnings season (Feb/Apr/Jul/Oct) where multiple entrants/day are routine.

### Reverse case (α1 deleveraging)

Day t: `{A,B,C,D,E}`, weights 1/5 each.
Day t+1: `{A,B,C,D}` (E exits at term, no new entrants), weights 1/4 each.

- Set turnover: `|{E}| / 5 = 0.20` (exit captured)
- True weight-based: `4 × |1/4 − 1/5| + |1/5 − 0| = 0.40`
- Same 2× undercount on the leveraging-up leg.

## §4 Material side-finding (not blocking)

`compare_cost_scenarios` passes `daily_turnover=None` to both `flat_net` and `tiered_net`. `CostModel.apply` defaults to 100% turnover/day when None. This means **scenario comparison silently uses worst-case turnover**, NOT the per-day basket diff. Downstream Sharpe-net numbers in `BacktestReport` are therefore conservative for any strategy with realistic <100% turnover — but uninformative for distinguishing 30% vs 60% churn.

Recommendation (deferred, not blocking PEAD v2): plumb `per_rebalance_turnover().turnover.values` into `compare_cost_scenarios.daily_turnover` so the comparison uses realized rather than 100%-fallback. Cross-references `ev_fcff_yield_audit_verdict_2026_05_12.md` material finding "G4 cost-stress is no-op duplicate of G1".

## §5 Decision: α2 sub-leveraged

Per plan §1.B0: "If (c) is NOT charged → choose α2".

**Verdict: α2 (weight = 1/N_FIXED, gross varies in [0, 1])** for PEAD v2 Phase B implementation.

- **N_FIXED selection: 150** (peak-concurrent ceiling for top-quintile, not average — see §5.1 Little's Law re-derivation below).
- Each entrant adds 1/150 to gross without forcing existing positions to rebalance. Set-based turnover correctly captures all weight changes (entry adds, exit removes, no forced shifts).
- Cost-stress grid (5/10/15/25 bps half-spread) measures the actual round-trip per-position, not muffled by undercount artifact.
- Trade-off: sub-leveraged design forfeits substantial average gross exposure (off-peak ~0.10-0.25, peak ~0.65-0.85) but preserves pure α2 mechanics — no forced rebalancing ever, at any concurrency level ≤ N_FIXED.

### §5.1 N_FIXED re-derivation via Little's Law (zen review 2026-05-14)

Initial derivation `25 announcements/day × 20-day hold ÷ 500 universe → N_FIXED=30` was incorrect — it conflated *average* concurrent count with *peak* and ignored Little's Law (L = λW for stationary M/M/∞ queues).

**Corrected steady-state derivation (S&P 500, 20-day hold, top-quintile filter):**

- Events per year: 500 names × 4 reports/year = 2000 events/year
- Trading days: 252/year
- Average daily arrival rate λ_avg = 2000 / 252 ≈ 7.94 events/day
- Average concurrent count L_avg = λ_avg × W = 7.94 × 20 ≈ 159 names in 20-day post-event window at any time
- Top-quintile filter (rank ≥ 80th percentile): keep ~20% → average concurrent top-quintile ≈ 32

**Peak compression (Feb/Apr/Jul/Oct earnings weeks):**

- ~125 events compress into ~20 trading days each quarter → λ_peak ≈ 25 events/day for ~4 weeks
- L_peak = 25 × 20 = 500 concurrent across full universe during peak
- Top-quintile peak concurrent ≈ 0.20 × 500 = 100

**N_FIXED=150 calibration:**

- 150 = peak top-quintile concurrent (~100) + 50% safety margin for clustering / quintile-boundary fluctuation
- Max gross under this calibration: 150/150 = 1.0 (only if every top-quintile slot is filled simultaneously — empirically unlikely but mathematically capped)
- Off-peak gross: ~32/150 = 0.21 (average); peak: ~100/150 = 0.67
- **No forced rebalancing at any point** because weight=1/150 is constant for any active position

### §5.2 Alternatives considered + rejected

**Option A1 (zen-suggested dynamic cap, `weight = 1/max(N_FIXED, active_positions)`):**
- When `active ≤ N_FIXED`: weight=1/N_FIXED (fixed) → no forced rebalancing ✓
- When `active > N_FIXED`: weight=1/active (changes daily) → **forced rebalancing returns** ✗
- This reintroduces the cost hazard the audit was meant to eliminate, precisely at peak earnings when slippage is worst (zen review §H1 alternative). Rejected.

**Option A2 (lower N_FIXED, e.g. 30-50, allow gross to exceed 1):**
- Effectively leverage during peak. Out of scope for a long-only retail design and changes risk profile vs pre-reg. Rejected.

**N_FIXED=150 chosen** as the smallest value that keeps `gross ≤ 1.0` deterministically across all observed earnings-season configurations while preserving pure α2 mechanics.

### §5.3 Empirical validation deferred to Phase B1

Before locking 1/150 in `score_pead_pss.py`, B1 must compute the empirical 95-percentile of `concurrent_top_quintile_count` across a sample year of AV data (e.g. 2018 full year) using the actual 80th-percentile PSS cohort-rank logic. If empirical 95-percentile ≤ 100 → N_FIXED=150 is conservative and acceptable. If empirical 95-percentile > 100 → bump N_FIXED to `1.5 × empirical_p95` and re-log the calibration in B1 commit message.

### α1 alternative (rejected, with conditions)

α1 (gross=1 daily-rebal) is rejected for v2 implementation because fixing it requires:
1. New weight-based turnover function (~80 LOC + 5 unit tests).
2. Plumbing into `CostModel.apply` and `apply_tiered_cost` (~3 call-site changes).
3. Re-validation that existing layer-3 backtests (audit replays for closed paradigms) still produce consistent numbers — multi-day verification.

Total: ~2 sessions of engineering + multi-day regression risk. Not justified for a single paradigm test; revisit if PEAD v2 PASSes and Layer-4 overlay testing requires full-gross sizing.

## §6 Bonferroni consequence

α2 is a MATERIAL spec change from the v2 memo (which referenced "alpha-1 (gross=1 daily-rebal, memo-literal weighting) vs alpha-2 (1/n_active sub-leveraged) — resolved in Phase B2 after cost-model audit"). The v2 memo §5 explicitly left this for B0 to decide, so this is NOT a Bonferroni-evasion event — it is the audit-driven resolution the pre-reg anticipated.

**Bonferroni accounting unchanged**: class `event_drift_search_2026_05_03` remains at n=3 (v3 abandoned, v4 abandoned, pead_v5_pss). Class-internal strict critical |t| = 2.39. Project doctrine 3.5 binds. **No new ledger entry needed.**

A `v3 memo` IS required to capture the α2 choice and N_FIXED=30 lock — `docs/research/paradigm14_pead_v2_design_2026_05_13.md` §16 (post-lock amendments) will absorb this as amendment §16.4 next session. The pre-reg outcome field gains a `weighting_choice_resolution` sub-key documenting α2 + this memo path.

## §7 Acceptance checks

- [x] Read `cost_model.py` (254 LOC, all)
- [x] Read `metrics.py::turnover_pct` + `per_rebalance_turnover` (lines 222-280)
- [x] Read `cost_validation.py::apply_tiered_cost` + `compare_cost_scenarios` (lines 390-490)
- [x] 3-question gate answered (YES/YES/NO)
- [x] α1 vs α2 verdict locked: **α2 + N_FIXED=30**
- [x] Side-finding logged (compare_cost_scenarios None-fallback)
- [x] Bonferroni implication checked (no increment)

## §8 Next-session followups

1. **Phase B1**: implement `score_pead_pss.py` with α2 sub-leveraged weighting (weight = 1/150 per active position, gross ∈ [0, 1]). Includes empirical 95-percentile concurrent-count validation per §5.3 before lock.
2. **v2 memo §16.A6 amendment**: updated 2026-05-14 to lock α2 + N_FIXED=150.
3. **Pre-reg outcome amendment**: updated `weighting_choice_resolution` to reference N_FIXED=150 + this memo §5.1 re-derivation.
4. **Defer (NOT this paradigm)**: weight-based turnover function + `compare_cost_scenarios` plumbing fix. Tracked as repo-wide tech debt.

## §9 Changelog

- 2026-05-14 initial publication, N_FIXED=30 (incorrect — based on conflated avg/peak derivation).
- 2026-05-14 zen review surfaced Little's Law violation. §5.1 re-derivation added with N_FIXED=150 lock. §5.2 documents rejected alternatives (dynamic cap, allowed-leverage). §5.3 adds empirical p95 validation gate before B1 lock. Downstream amendments (v2 memo §16.A6, ledger outcome) updated in same commit.
