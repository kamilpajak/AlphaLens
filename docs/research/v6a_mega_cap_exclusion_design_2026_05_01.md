# v6a-revised — Non-mega-cap retrained Lasso with cap-matched benchmark

**Status:** REVISED 2026-05-01 PM after zen + perplexity adversarial review
KILLED original v6a as mathematically self-defeating (SPY benchmark in
mega-cap rally vs non-mega-cap subset = structural −35% gap before any
alpha test). User policy `feedback_no_passive_pivot.md` overrides
"wait for fresh OOS." This is **EXPLORATORY DIAGNOSTIC** on burnt holdout;
PASS verdict requires prospective walk-forward 2026-05+ confirmation
before any escalation.

**Class:** in-class continuation `alt_data_screener_search_2026_04_30`
(reopens 5/5 closed class with 6th hypothesis test).

**Threshold:** Romano-Wolf approximation under m≈50 effective hypotheses
(11 prior + variants on selection/model/preprocessing) → **primary |t|≥3.5**,
**stretch |t|≥4.0**. NOT simple Bonferroni n=12.

## Adversarial review synthesis (one paragraph)

zen flagged FATAL: comparing non-mega-cap subset vs SPY in 2024-2025
mega-cap rally is mathematical sabotage; SPY ≈ +60-70%/y includes mega-cap
drift, non-mega-cap subset ≈ +25-30%/y absolute → -35% gap before any
alpha test. Carhart SMB can't absorb in extreme regime. perplexity flagged
FATAL: HARKing irreducible (designed against v5 diagnostic on same
holdout); top-50 = specification mining; cache reuse = distribution
mismatch (Lasso trained on full universe applied to subset); n=12
Bonferroni understated for true effective m≈50; underpowered. Five
mitigations adopted in v6a-revised: (1) **MDY benchmark** (cap-matched,
NOT SPY); (2) **percentile-based universe filter** (exclude top 10% by
mkt-cap, exogenous to observed regime); (3) **retrain Lasso on
non-mega-cap train pool only** (still cheap via cached features filter);
(4) **threshold |t|≥3.5** primary (Romano-Wolf m≈50); (5) **explicit
EXPLORATORY framing** with fresh-OOS confirmation as gate to escalation.

## Hypothesis (refocused)

**H₁:** Predictive factors exist in non-mega-cap universe when Lasso is
trained on that subset directly. Tests: "does the alt-data signal stack
work in mid/small-cap names where mega-cap concentration doesn't dominate?"

**H₀:** No alpha in non-mega-cap subset even with retrained Lasso. v5
settled was correct: this 10-feature stack doesn't generate alpha,
period — independent of universe restriction.

## Design (revised)

| Variable | v5 | v6a (REJECTED) | v6a-revised | Rationale |
|---|---|---|---|---|
| Universe filter at asof | post-ADV ≥$5M | + drop top-50 mkt-cap | + drop **top-10%** mkt-cap (~150-160 names) | exogenous percentile, not arbitrary count |
| Train pool | full universe (~221k rows) | full | **non-mega-cap subset** (~199k rows after filter) | retrain on subset (zen + perplexity) |
| Lasso fit | global, target='rank' | reused (cached coefs) | **refit on subset train pool** | cache reuse → distribution mismatch (perplexity ★★★) |
| Holdout scoring | full universe | non-mega-cap subset | non-mega-cap subset | unchanged |
| Selection rule | long top-decile EW | long top-decile EW | **long top-decile EW** | unchanged |
| Benchmark (PRIMARY) | SPY | SPY (rejected) | **MDY** (S&P MidCap 400) | cap-matched (zen ★★★) |
| Benchmark (descriptive) | n/a | MDY descriptive | **SPY** descriptive | continuity vs v5 reporting |
| Cost | 30 bps RT, 1 leg | unchanged | unchanged | |
| Train/holdout windows | 2018-2024 / 2024-2026 | unchanged | unchanged | burnt holdout invariant |
| Stride / holding | 5d / 20d | unchanged | unchanged | |
| Threshold (primary) | 2.81 | 2.85 | **3.5** | Romano-Wolf m≈50 |
| Threshold (stretch) | 3.2 | 3.2 | **4.0** | conservative |
| Frame | confirmatory | confirmatory | **EXPLORATORY** | PASS requires fresh-OOS confirmation |

## Mkt-cap computation at asof (PIT-correct)

```
shares = latest_shares_as_of(ticker, asof)  # SEC EDGAR companyfacts (existing infra, used in v4 SI%)
close = history_store.close_as_of(ticker, asof, lookback=5)
mkt_cap = shares * close if both defined else NaN
```

Top-10% exclusion: at each asof, rank `mkt_cap_at_asof` desc, drop top
10% (count varies with cross-section size, ~150-160 in late holdout).
NaN-mkt-cap tickers RETAINED in long pool (absence-of-evidence ≠
evidence-of-absence). Pre-reg locks this convention.

**Why 10% (locked):**
- Exogenous percentile, scales with cross-section size dynamically.
- Captures mega-cap concentration without arbitrary tuning to observed
  Mag-7 distribution.
- Robust against cross-section size variation across asofs.

## Primary metric (CHANGED — critical)

Carhart-4F regression of `(long_only_return − MDY_return)` on
Mkt-RF/SMB/HML/Mom with HAC maxlags=5, subtract_rf=False.

**MDY = SPDR S&P MidCap 400 ETF.** Closest cap-matched ETF to our
post-filter universe (top 51-450 by mkt-cap). NOT perfect (covers ~400
mid-caps; our pool has ~1100-1400 names), but materially closer than SPY.

**Why MDY not IWM:** our universe is Russell 1000 + Russell 2000 union
(~1618 tickers); after dropping top-10% (~150), remaining is ~1450
covering most of mid-cap and small-cap. MDY skews mid-cap, IWM skews
small-cap. MDY is a better center-of-mass match. zen suggested RSP
(equal-weight S&P) as alternative; MDY chosen because our selection rule
is also equal-weight, so cap-weighting drag is partially controlled in
both numerator and denominator.

## Secondary descriptive (NOT gating)

Carhart-4F regression of `(long_only_return − SPY_return)` for direct
v5 comparability. Reported alongside primary; does NOT contribute to
verdict.

## What "PASS" means (revised pre-reg gates)

**Primary EXPLORATORY (all must hold on holdout):**
1. Carhart-4F α t-stat (HAC=5) on (long − MDY) ≥ **3.5** (Romano-Wolf m≈50)
2. ≥1 Lasso nonzero coef (parity v3-v5)
3. in-CV mean rank-IR ≥ 0.5
4. Lo-adjusted Sharpe net (long − MDY) ≥ 0.5
5. α annualized ≥ 3%
6. MaxDD on (long − MDY) ≥ −25%
7. Holdout mean per-asof rank-IC > 0 (within non-mega-cap subset)

**Stretch:**
- Primary PASS + α t-stat ≥ 4.0 → "robust EXPLORATORY positive"

**Outcome classification (revised, exploratory frame):**
- All-7 PASS + stretch PASS → "robust exploratory positive; **MUST replicate
  on prospective walk-forward 2026-05-01+ at p<0.05 unadjusted before any
  capital escalation**"
- All-7 PASS + stretch FAIL → "directional exploratory positive, NOT robust
  to conservative multiplicity"
- Any primary FAIL → standard FAIL ledger entry, n=13, next |t|≥2.86

**No PASS at any level triggers capital deploy** per burnt-holdout policy.

## HARKing acknowledgment (refined)

v6a-revised is post-hoc designed against v5's regime-stratification
finding. Per Kerr 1998 + Simmons et al. 2011 this is hypothesis-mining.
Mitigations adopted on top of v5's:

1. **Exploratory framing** — verdict treated as hypothesis generation, not
   testing. PASS requires prospective walk-forward (perplexity Q7).
2. **Exogenous universe criterion** (top-10% percentile) — NOT post-hoc
   tuned to observed regime.
3. **Retrain Lasso** — eliminates "trained on contaminated full universe"
   confound (perplexity Q4).
4. **Cap-matched benchmark MDY** — eliminates structural −35% gap (zen).
5. **Threshold |t|≥3.5** Romano-Wolf approximation — m≈50 effective
   hypotheses (perplexity Q3).
6. **Capital deploy off-table** regardless of verdict.

The irreducible HARKing residual: even with all 5 mitigations, the choice
to test "non-mega-cap subset" was MOTIVATED by v5 regime diagnostic. This
is acknowledged. Confirmation requires prospective walk-forward; no claim
of confirmatory result possible from v6a-revised on burnt holdout alone.

## Phase A sanity (same cache, fast)

1. **Mkt-cap top-10% sanity** at recent asof: verify top names match
   expected mega-caps (NVDA/AAPL/MSFT/GOOGL/AMZN/META/TSLA/BRKB/JPM/WMT
   in top-50 of mkt-cap distribution).
2. **Sub-pool size**: at how many holdout asofs is non-mega-cap pool
   ≥30 (decile ≥3)? Should be all 94.
3. **Subset Lasso refit smoke test**: refit Lasso on filtered train (~199k
   rows). Verify nonzero coef count > 0; verify selected λ within reasonable
   range (compare against v5's λ=0.00289). Significant deviation is
   diagnostic information.
4. **Subset rank-IC sanity**: holdout mean rank-IC restricted to
   non-mega-cap. If ≤0, abort Phase B as guaranteed FAIL (perplexity
   warned this is a false-safeguard alone but still useful early-stop).

## Estimated effort

- v6a-revised driver: clone v5, add mkt-cap percentile filter, retrain
  Lasso on subset, change benchmark to MDY, ~45 min
- Phase A sanity: 5 min (cached features filter + new Lasso fit)
- Phase B run: 5 min (retrained Lasso + holdout backtest + Carhart vs MDY)
- Verdict memo + ledger close: 30 min

**Total: ~1.5h.**

## Settled (cannot change post-run)

| Variable | Locked value | Source |
|---|---|---|
| Universe percentile filter | top-10% drop | exogenous, this memo |
| Lasso retrain target | non-mega-cap train pool only | perplexity ★★★ |
| Primary benchmark | MDY | zen ★★★ |
| Primary threshold | |t| ≥ 3.5 | Romano-Wolf m≈50, perplexity ★★ |
| Stretch threshold | |t| ≥ 4.0 | this memo |
| Selection rule | long top-decile EW | unchanged from v5 |
| Cost | 30 bps RT 1 leg | unchanged from v5 |
| Train/holdout windows | 2018-2024 / 2024-2026 | unchanged from v5 |
| Stride/holding | 5d / 20d | unchanged from v5 |
| Frame | EXPLORATORY | perplexity Q7 |

## Open questions for future v6b/c (if v6a-revised PASS triggers them)

- v6b: same as v6a-revised but L/S market-neutral (zen alternative)
- v6c: sector-neutral within non-mega-cap pool
- v6d: continuous score-percentile-weighted instead of decile EW
- v6e: explicit Russell 2000 official membership filter (when membership
  history available)
