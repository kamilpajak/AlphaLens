# v10 design — analyst-event features (Path γ, post-v9 LightGBM FAIL)

**Status:** LOCKED 2026-05-01 PM after zen + perplexity adversarial
review and user delegation of all open questions ("zdecyduj sam").

**Adversarial synthesis (one paragraph):** zen flagged yfinance survivorship
(★★★), flat 60d window dilution (★★★), action-string magnitude loss
(★★), multicollinearity gating (★) — recommended Path β dominates Path γ.
Perplexity flagged drift attenuation post-zero-commission (★★★),
yfinance vs IBES gap (★★★), multicollinearity (★★★), n=10 Bonferroni
insufficient under Romano-Wolf m≈50 → |t|≥3.0-3.4 (★★★) — recommended
REJECT both paths on burned holdout, wait 6m for fresh OOS. User policy
(`feedback_no_passive_pivot.md`, explicit "nie ma mowy o czekaniu")
overrides "wait 6m" recommendation; capital deploy already off-table
per `feedback_burnt_holdout_multiplicity.md`. Five mitigations adopted:
(1) survivorship probe = HARD-block + auto-pivot to Path β on FAIL;
(2) exponential 10d decay on Feature 1 + 2 (Womack drift profile);
(3) VIF + cross-fold Lasso stability gate in Phase A;
(4) stretch threshold |t|≥3.2 added alongside primary 2.81;
(5) action-mapping kept direction-only (perplexity's post-RegFD
parsimony argument overrides zen's magnitude argument).

**Class:** NEW `analyst_alt_data_search_2026_05_01` (n=1 in-class).

**Program-level Bonferroni:** n=10 prior tests on burnt 2024-2026 holdout
→ pass threshold **|t| ≥ 2.81**. Per `feedback_burnt_holdout_multiplicity.md`,
fresh class on identical features+selection+holdout window does not cleanse
multiplicity. v10 swaps **feature space only** — selection, holdout, target
all unchanged → program-level count applies.

**Burnt-holdout caveat:** capital deploy off-table on this window
regardless of v10 verdict. v10 is a diagnostic test of "features are
bottleneck" hypothesis settled across 9 prior tests + 3 model classes.

## What's the design

ONE variable changes vs v4: feature space grows from 10 alt-data → 13
(10 alt-data unchanged + 3 analyst-event features).

| Variable | v4 (FAIL αt=−2.57) | v10 | Source |
|---|---|---|---|
| Feature count | 10 | **13** (+3 analyst) | feature-bottleneck hypothesis |
| Analyst features | none | 3 yfinance-derived | this memo |
| Selection rule | decile L/S + SI≤15% | unchanged | v4 (settled rank-blindness) |
| Model class | linear-Lasso CV | unchanged | v4 (clean ablation) |
| Target | 20d forward return rank | unchanged | v3/v4 |
| Train | 2018-01-01 → 2024-04-29 | unchanged | v4 |
| Holdout | 2024-04-30 → 2026-04-30 | unchanged | v4 (BURNT) |
| Stride / holding | 5d / 20d | unchanged | v4 |
| HAC maxlags | 5 | unchanged | v4 |
| Cost | 60bps + 1.5% borrow | unchanged | v4 |
| Threshold | program-level n=4 (|t|≥2.50) | **program-level n=10 (|t|≥2.81)** | burnt-holdout multiplicity |

## PIT audit — yfinance analyst data (2026-05-01)

Empirical probe across {AAPL, NVDA, PLTR, GME, NRIX, OUST, WOLF}:

| yfinance field | PIT-correct? | Notes |
|---|---|---|
| `upgrades_downgrades` | ✅ YES | event-indexed by `GradeDate` (timestamp), backfill 2012+ for AAPL, 40+ events for microcap OUST since 2023 |
| `recommendations` / `recommendations_summary` | ❌ NO | only current 4-month bucket snapshot (0m, -1m, -2m, -3m offsets to "now"); not historical |
| `analyst_price_targets` | ❌ NO | dict with current consensus only (mean/median/high/low) |
| `earnings_estimate` | ❌ NO | current consensus snapshot per period (0q/+1q/0y/+1y) |
| `eps_revisions` | ❌ NO | trailing 7d/30d up/down counts but only as of "now"; not snapshotted historically |

**Conclusion:** only `upgrades_downgrades` is usable for retrospective
backtest. Original Path γ spec (`analyst_eps_revision_60d`,
`analyst_target_premium`, `analyst_rating_score_change_30d` from
recommendations buckets) revised to 3 derived features below.

**Alpha Vantage fallback:** their `EARNINGS` endpoint has historical
reportedEPS/estimatedEPS pairs but that's already encoded in
`earnings_sue_naive_4q_decayed`. No free-tier PIT analyst-revision
endpoint. Premium IBES / Refinitiv out of scope.

## Three analyst-event features (PIT contract per event timestamp)

All three computed at `asof = t` from `upgrades_downgrades` events with
`GradeDate ≤ t − 1 BD` (one-day dissemination embargo to avoid
look-ahead from intraday timestamps).

### Feature 1 — `analyst_action_net_decay10_60d`

Signed sentiment shift over 60-day trailing window with **exponential
recency decay** (τ=10 calendar days) per zen rec on Womack 1996 drift
profile (steepest decay 5-15d post-event).

```
events_60d = events with t − 60 cal days ≤ GradeDate ≤ t − 1 BD
weight(e) = exp(−(t − GradeDate.date()) / 10)
score(e) = action_to_score(e.Action)  # in {−1, 0, +1}
analyst_action_net_decay10_60d
    = sum(weight(e) × score(e) for e in events_60d)
      / sum(weight(e) for e in events_60d)
```

`action_to_score` mapping (yfinance Action column):
- "up" → +1
- "down" → −1
- "init" / "main" / "reit" → 0
- unmappable string → NaN propagates to the feature value (fail loud,
  don't impute zero)

**Magnitude-encoding rejected** — perplexity Q3 argues post-RegFD
attenuates ordinal-magnitude predictability and adds variance under
regime shift. Sticking with direction-only for parsimony.

Range: [−1, +1] when defined. NaN when zero events in window.

### Feature 2 — `analyst_target_revision_pct_decay10_60d`

Weighted-mean relative price-target change across 60d events with same
exp(−Δd/10) decay as Feature 1.

```
valid = events_60d where priorPriceTarget > 0 and currentPriceTarget > 0
weight(e) = exp(−(t − GradeDate.date()) / 10)
delta(e) = (e.currentPriceTarget − e.priorPriceTarget) / e.priorPriceTarget
analyst_target_revision_pct_decay10_60d
    = sum(weight(e) × delta(e) for e in valid)
      / sum(weight(e) for e in valid)
```

`priorPriceTarget = 0` indicates initiation (no prior target) — excluded
to avoid divide-by-zero and infinite revision artifacts.

NaN when zero valid events.

### Feature 3 — `analyst_attention_log_60d`

Coverage intensity (orthogonal to direction).

```
analyst_attention_log_60d = log1p(len(events_60d))
```

Always defined (≥0). Captures the "lots of analysts paying attention"
signal that may interact with directional features.

### Cross-sectional ranking

Per existing v4 convention, all three features ranked within each asof
slice; raw values + ranks both fed to Lasso (same as 10-feature
treatment). NaN propagates.

## Hypothesis

**H₁:** Analyst-event flow surfaces signal that the 10-feature alt-data
joiner missed. Mechanism: rating actions and target revisions are
discrete information events with documented post-event drift
(Womack 1996, Stickel 1995, Jegadeesh-Kim 2006); they encode broker
research synthesis that's orthogonal to fundamentals/short-interest/insider
signals already in the joiner.

**H₀:** Adding 3 analyst features doesn't lift Carhart α t-stat above
program-level Bonferroni 2.81. Either (a) yfinance backfill is
selection-biased (only large caps with continuous coverage), (b) signal
is dominated by squeeze regime in 2024-2026, or (c) features are
collinear with existing 10 (e.g., high-SI names get rated more often).

## What "PASS" means (pre-reg gates)

**Primary gates (all four must hold on holdout for PASS):**

1. Carhart-4F (Mkt-RF, SMB, HML, Mom) intercept HAC `|t| ≥ 2.81`
   (program-level Bonferroni n=10).
2. ≥1 of the 3 analyst features has nonzero Lasso coef (rules out
   "any signal coming entirely from existing 10 features" artifact —
   parity with v3/v4 zero-coef gate).
3. In-CV mean IR ≥ 0.5 (fold stability per v3+ pre-reg standard).
4. MaxDD on holdout ≥ −40% (v4-style sanity; squeeze blowup risk).

**Stretch gate (Romano-Wolf approximation per perplexity Q6):** if all
four primary gates PASS, additionally check `|t| ≥ 3.2` (corresponds
to FDR-adjusted threshold under m≈50 effective hypotheses across full
program). Result classification:

- All-four PASS + stretch PASS → "robust diagnostic positive; replication
  required on fresh holdout 2026-11+ before any escalation"
- All-four PASS + stretch FAIL → "directional diagnostic positive
  consistent with primary gate but not robust to conservative
  multiplicity"
- Any primary gate FAIL → standard FAIL ledger entry, increment program
  count to n=11

Gate 2 is critical: if Lasso zeros the 3 new features and the strategy
"passes" on existing 10 alone, it's the existing 10 driving everything →
no diagnostic information about analyst features. Pre-reg this explicitly.

## Open risks for adversarial review

1. **Action-string normalization fragility.** yfinance Action column
   has discrete values (`'up'`, `'down'`, `'main'`, `'init'`, `'reit'`)
   but firm-specific noise possible. Need probe across full 1618-ticker
   universe to inventory unique values. If >5% NaN, feature value is
   degraded.

2. **Backfill survivorship.** Are events for delisted tickers retained
   in yfinance `upgrades_downgrades`? If yfinance only keeps active-
   ticker history, this introduces a survivorship bias missing from
   `pit_audit_2026_04_30_findings.md`. Critical to test before run.

3. **Multicollinearity with existing features.** `analyst_attention_log_60d`
   may correlate with `filing_density_4q` (firms filing more get covered
   more); `analyst_action_net_60d` may correlate with `earnings_pead_5d`
   (post-earnings rating actions). Need correlation matrix in Phase A
   sanity. Lasso will handle via regularization, but interpretation
   weakens if correlations >0.7.

4. **Yahoo source curation.** Yahoo's underlying data provider (Refinitiv?)
   may have selection criteria for which broker actions get listed.
   Compared to a paid IBES feed, yfinance has unknown coverage gaps.
   Cannot fully audit without external benchmark.

5. **Squeeze-regime confound.** v4 long-leg performed +20.6%/y across
   sub-periods; if analyst features add signal in long leg only, but
   short leg again gets squeezed (+108%/y like v4), L/S spread fails
   regardless of feature quality. Mitigation already locked: SI≤15%
   filter on short leg.

6. **One-day embargo sufficiency.** `GradeDate` includes intraday
   timestamps but trade execution happens at next-day open. T+1 BD
   embargo is conservative; T+0 might be defensible for events with
   GradeDate < 09:30 ET. Current spec uses T+1 BD for safety.

7. **Diminishing-returns multiplicity.** Even with cleanest possible
   v10 design, the 2024-2026 holdout has been touched 10 times. If
   feature-set choice is implicitly informed by what would have helped
   prior fails (e.g., "signal that wasn't squeeze-blind"), n=10 is a
   floor not a ceiling. Adversarial review should challenge feature-
   selection independence claim.

## Phase A sanity checks (pre-Phase B blocking)

1. **Action column coverage probe** (zen Risk #1): probe yfinance Action
   column unique values across 50-ticker stratified sample. If unmapped
   strings >5%, expand `action_to_score` map before Phase B.
2. **Survivorship HARD-block** (zen ★★★): probe upgrades_downgrades
   availability for the FULL set of known-delisted tickers in
   `pit_universe` (delisted between 2018-2026). Compare event-frequency
   distribution against active-ticker matched-cap controls.
   - If delisted/active event-rate ratio < 0.5 with z>2 (statistically
     significant under-coverage of delisted), **ABORT Path γ and
     auto-pivot to Path β** (long-only top-decile in alt_data class,
     n=5 → |t|≥2.58, ~1d effort).
   - If ratio ≥ 0.5, document residual tilt and proceed.
3. **13-feature joiner build**: 60-asof × 200-ticker sample. Verify
   coverage ≥70% (parity with v4 build, `phase_a_multi_source_2026_04_30.md`).
4. **VIF + correlation gate** (perplexity ★★★): compute pairwise
   rank-correlation matrix AND variance inflation factors for the 13
   features. Three sub-gates:
   - 4a. If max |ρ| pairwise ≤ 0.7 AND max VIF ≤ 3 → proceed.
   - 4b. If 0.7 < max |ρ| ≤ 0.85 OR 3 < max VIF ≤ 5 → orthogonalize
     analyst features against existing 10 via residualization, re-run
     correlation check on residuals.
   - 4c. If max |ρ| > 0.85 OR max VIF > 5 → ABORT (signal indistinguishable
     from existing features).
5. **Cross-fold Lasso stability** (perplexity ★★★): fit Lasso CV on first
   half of train (2018-2021) vs second half (2021-2024) separately.
   For analyst-feature coefs:
   - If sign-flips on any analyst feature → spurious selection, ABORT.
   - If magnitude differs >50% → flag as unstable; document but proceed.
   - If consistent sign and magnitude within 50% → proceed.
6. Re-run Phase A sanity driver (existing `phase_a_multi_source_2026_04_30.md`
   pattern) with new joiner.

Phase B blocked until Phase A passes all six gates (or auto-pivots
to Path β on gate 2 FAIL).

## Variables NOT touched (settled by prior experiments)

- Linear-Lasso model (v9 LightGBM-MSE FAIL settled "model class is not
  the bottleneck" with rank-IC sign-flip diagnostic).
- 5-fold time-series CV with embargo (v3+ standard).
- Selection rule decile L/S (rank-blindness diagnosed but cleanest
  test-isolation: keep selection fixed, vary features only).
- Train window 2018-2024 (PIT audit cleared).
- 60bps + 1.5% borrow cost model (v4 standard).
- HAC maxlags=5 (v3+ standard).

## Path β auto-pivot spec (triggered if Phase A gate 2 FAILs)

Per zen ★★★ on yfinance survivorship: if the FULL delisted-ticker probe
shows event-rate ratio <0.5 with z>2, abandon Path γ and execute Path β:

- **Class:** stays `alt_data_screener_search_2026_04_30` (n=5 → |t|≥2.58)
- **Features:** v4's 10 unchanged (no analyst additions)
- **Selection:** long top-decile EW vs SPY benchmark (no short leg)
- **Cost:** 30bps round-trip on long leg only
- **HARKing flag (explicit):** This selection rule was post-hoc
  designed against observed v4 long-leg performance (+20.6%/y across
  all sub-periods of 2024-2026 holdout). Per Kerr 1998 and Simmons
  et al. 2011, this is classical hypothesis-mining: the long-only
  pivot was motivated by results we already saw on the same window.
  We acknowledge the inference is conditional and capital deploy
  remains off-table regardless of any PASS verdict — fresh-OOS
  replication on data not yet observed is mandatory before any
  escalation.
- **Effort:** ~1d to implement + run + audit.
- **PASS verdict:** still requires fresh-OOS replication before any
  capital escalation (burnt-holdout caveat unchanged).

This auto-pivot is pre-registered: triggering condition is objective
(delisted/active event-rate ratio < 0.5 with z > 2 under bootstrap),
no experimenter discretion, no hypothesis-after-results.

## Estimated effort

- yfinance bulk-download analyst events for 1618 tickers: ~2-3h walk-time
  (yfinance rate limits + retries). Cache as parquet under
  `~/.alphalens/yfinance_analyst_events/`.
- Feature joiner extension + tests: ~3h.
- Phase A sanity: ~1h.
- Phase B Lasso fit + audit + memo: ~2-3h walk-time, ~1h compute.
- **Total: 1-2 working days** if PIT audit findings hold; +1d if
  survivorship probe surfaces material delisted-ticker coverage gap.

Materially shorter than original 3-5d estimate because Alpha Vantage
fallback is no longer needed (yfinance `upgrades_downgrades` covers the
PIT-correct slice).

## Pre-reg artifact (post-user-approval)

After user sign-off on this REVISED memo, lock in:
- `docs/research/preregistration/params_analyst_alt_data_v10_2026_05_01.json`
- Add via `alphalens preregister add --class analyst_alt_data_search_2026_05_01
  --strategy analyst_alt_data_v10 --threshold 2.81`
- Add stretch threshold metadata 3.2 to ledger entry.
- Pre-register Path β auto-pivot trigger so swap is not post-hoc.

Memo `Status:` field will flip to LOCKED with date on user approval.

## Decision log (post-user delegation 2026-05-01)

User delegated all five open questions with "zdecyduj sam":

1. **Path γ-primary with hard-block:** KEPT. zen's "β dominates" honored
   via objective auto-pivot trigger (no experimenter discretion at
   pivot point).
2. **Stretch threshold:** 3.2 (perplexity range midpoint of 3.0-3.4).
3. **Action mapping:** direction-only KEPT (perplexity post-RegFD
   parsimony argument).
4. **Decay τ:** 10d KEPT — event-driven signal time-scale matches
   Womack 1996 profile; 30d existing alt_data decay is for quarterly
   fundamentals, different scale.
5. **HARKing ack:** strengthened with explicit Kerr 1998 + Simmons et al.
   2011 framing for Path β auto-pivot.
