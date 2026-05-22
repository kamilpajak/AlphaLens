# Idiosyncratic Momentum (paradigm #15) — Audit Verdict + Postmortem

**Verdict:** **FAIL** (joint-FAIL: every window IS / OOS / FL fails independently)
**Date:** 2026-05-14 (audit ran 2026-05-14 17:27-17:57 UTC on runpod)
**Class:** `price_factor_search_2026_04_29` — paradigm test #15 (5th member of class)
**Ledger ID:** `idiosyncratic_momentum_2026_05_14_v1`
**Canonical JSON:** `docs/research/idiosyncratic_momentum_audit_2026-05-14.json`
**Pod:** community-cloud CPU pod EU-RO-1, 2 vCPU / 256 GB shared, $0.07/hr (~$0.04 total)
**Wall:** 30.4 min total (IS 7.8 min / OOS 9.5 min / FL 13.2 min)

---

## 1. One-line summary

Idiosyncratic momentum (Blitz-Huij-Martens 2011 canonical, FF3 36-month rolling residualisation, σ_36 standardisation, top-decile equal-weight long-only on S&P 1500 PIT union) produces **monotonically-strengthening positive signal across IS → OOS → FL** (Carhart-4F αt mean +0.02 / +0.71 / +1.58, every phase positive in OOS+FL, IS effectively null) — **below the project Bonferroni 3.5 doctrine threshold** AND below class-internal n=5 critical |t|=2.57 on all three windows. Paradigm fails clean; honest prior from memo §9 (10-15% PASS probability, most likely αt 1.5-2.5 below threshold) was approximately right.

## 2. Per-window summary

| Window | Period | n_phases | Verdict | αt mean | αt range | αt_net @15bps | β_market |
|---|---|---|---|---|---|---|---|
| IS | 2010-01-01 → 2017-12-31 | 5 | FAIL | +0.02 | -0.04..+0.12 | -0.67 | 1.11-1.12 |
| OOS | 2018-01-01 → 2021-12-31 | 5 | FAIL | +0.71 | +0.41..+0.96 | +0.24 | 1.16-1.17 |
| FL | 2022-01-01 → 2024-12-31 | 5 | FAIL | +1.58 | +1.29..+1.84 | +0.91 | 0.97 |

Phase variation tight in every window (std/mean ratios 0.05-0.20 within OOS/FL; IS is essentially noise about zero). FL window had the strongest IM signal — opposite to memo §9 anticipated penalty from 2023-2024 momentum crisis.

## 3. Gate-by-gate evaluation

Per memo §8 success criteria:

| Gate | Threshold | IS | OOS | FL |
|---|---|---|---|---|
| G1: full-sample αt | ≥ 3.5 | 0.02 ✗ | 0.71 ✗ | 1.58 ✗ |
| G2: mean αt per-phase | ≥ 2.5 | 0.02 ✗ | 0.71 ✗ | 1.58 ✗ |
| G3: positive αt each phase | > 0 | min=-0.04 ✗ | min=+0.41 ✓ | min=+1.29 ✓ |
| G4: net αt @15bps | ≥ 2.0 | -0.67 ✗ | +0.24 ✗ | +0.91 ✗ |

Joint-PASS rule (memo §8): every window in {IS, OOS, FL} must clear all 4 gates. Zero windows clear all 4 → overall FAIL.

PASS_MARGINAL band (αt ∈ [2.5, 3.5] + G3 + G4): not entered on any window — all windows fall below 2.5.

## 4. Material findings (memo §5.1 mandatory diagnostics)

### 4.1 BAB-confound check (memo §5.1.1): NOT TRIGGERED

Realised β_market on the IM portfolio:
- IS: 1.11-1.12
- OOS: 1.16-1.17
- FL: 0.97

All values above the memo flag threshold β < 0.8. The Blitz canonical 1/σ_36 standardisation did **NOT** inject a material low-vol tilt on the S&P 1500 universe — the IM portfolio is slightly **high-beta** in IS/OOS and ≈ market in FL. This invalidates the memo §10 risk-register prediction (BAB confound at 40-60% probability). The Arnott-Beck 2023 BAB-confound hypothesis (that residual momentum is implicitly low-vol-tilted) is **not supported** on this universe.

### 4.2 FF5+UMD attenuation check (memo §5.1.2): borderline material in OOS

Per-phase attenuation (Carhart α → FF5+UMD α):
- IS: meaningless (Carhart α ≈ 0 → denominator near zero; per-phase reports range -2197% to +506%)
- OOS: 24% / 31% / 26% / 31% / 48% → mean ~32% (borderline above flag threshold 30%)
- FL: -7% / -4% / -4% / -4% / -4% → no attenuation (Carhart α slightly **lower** than FF5+UMD α)

OOS attenuation ~32% indicates RMW+CMA absorb roughly 1/3 of the OOS Carhart α — a borderline-material confound. FL is fully robust (negative attenuation = adding RMW/CMA strengthens the α, not weakens it). NOT material to verdict (everything below 3.5 anyway), but worth flagging if a future v2 attempts a sector-neutral or quality-controlled variant.

### 4.3 Hyper-turnover check (memo §4 + pre-reg `turnover_logging_mandate`): NOT TRIGGERED

Mean monthly turnover:
- IS: 18.9%
- OOS: 26.6%
- FL: 35.9%

All values **far below** the memo §10 anticipated 60-80%/mo "hyper-turnover" range. The cost-drag risk that motivated the §4 turnover-logging mandate did not materialise. Cost drag at 15bps half-spread (G4 stress) is dominated by the modest gross α, not by turnover-driven cost amplification.

### 4.4 Sharpe-vs-raw-momentum diagnostic (memo §5.1.3)

Not on the verdict critical path. Sharpe of the IM portfolio (gross):
- IS: 1.06
- OOS: 0.66
- FL: 0.59-0.70

Blitz's Sharpe ≥ 0.5 primary claim met on all windows. The raw-momentum comparator was run in parallel within each phase (`_RawMonthlyMomentumScorer` in the experiment script) — per-phase raw-momentum Sharpe values were written to the per-phase JSON outputs but not aggregated into the canonical orchestrator JSON. Detailed Sharpe-improvement-over-raw analysis deferred to a future research diagnostic if material.

## 5. Interpretation notes

### 5.1 IS effective sample = 5.4y, not registered 8y

The IS window was registered as 2010-01-01 → 2017-12-31 (8 years). The audit's per-phase log reports `n=1357-1361` daily portfolio observations, which is **~5.4 years** of in-market data, not 8. Source of the gap:

1. `IdiosyncraticMomentumScorer.MIN_BARS_REQUIRED = 900` requires each ticker to have ≥ 900 daily bars before the engine even attempts to score it.
2. The 36-month rolling FF3 residualisation needs 36 month-end observations BEFORE the rebalance date.
3. At early-2010 rebalances, many S&P 1500 tickers do not have 36 months of pre-IS-start history → scorer drops them → top-decile becomes empty → portfolio_return is NaN → daily_continuous_returns excludes that day.

The effective IS sample is closer to 2013-01-01 → 2017-12-31. The IS αt ≈ 0 measurement should be read as "IM had no detectable signal in 2013-2017", not "no signal in 2010-2017".

This is a **design consequence**, not a bug. The pre-reg memo did not explicitly address it. Future paradigms with rolling-window residualisation should either (a) document the effective vs registered sample mismatch, or (b) require an explicit pre-IS-start warm-up data buffer.

### 5.2 Monotonic IS → OOS → FL strengthening

The αt mean trajectory 0.02 → 0.71 → 1.58 is striking. Possible interpretations:

- **Real signal evolution**: IM mechanism may genuinely be gaining ground in modern markets (post-2018). This contradicts both Blitz's update finding ("idiosyncratic momentum weakens after the early 2000s") AND memo §9 anticipated FL penalty from 2023-2024 momentum crisis. If real, it is suggestive but not actionable (FL αt = 1.58 still far below 3.5).
- **Survivorship-bias artifact**: Universe is the current S&P 1500 PIT-union snapshot (memo §3 known limitation). Survivorship bias inflates α magnitudes upward more for forward periods than back periods, mechanically producing IS<OOS<FL. Magnitude consistent with 100-300 bps/y bias.
- **Cost-model artifact**: Turnover rises 19→27→36% across windows. Higher turnover means more cost drag eats into net Sharpe, but in this audit `α 4F` is the gross-returns regression — cost effects appear in `α-net 4F` (G4 channel) but the gross-Carhart αt trajectory is unaffected by this mechanism. Unlikely.

Most likely combination: real modest mechanism strengthening + survivorship-bias amplification.

### 5.3 FL period strongest, opposite to memo §9 prediction

Memo §9 anticipated "2023-2024 momentum crisis hit on FL phase: subtracts ~5pp from above". The audit shows FL is the **strongest** window. Possibilities:

1. The 2023-2024 momentum crisis affected RAW momentum more than RESIDUAL momentum — exactly what Blitz et al 2011 predicted as IM's drawdown-resilience claim.
2. Modern factor crowding has displaced raw-momentum strategies, leaving residual variants relatively unscathed.
3. Survivorship-bias inflation is highest on the most recent window.

Material observation for future paradigms in the same class.

## 6. Module + ledger updates applied

- `alphalens/screeners/idiosyncratic_momentum/__init__.py`: `__status__` → `CLOSED`, `__closed_date__=2026-05-14`, full `__closed_reason__` + 7-gate `__closed_evidence__` mapping.
- `docs/research/preregistration/ledger.json`: entry `idiosyncratic_momentum_2026_05_14_v1` status `registered` → `completed` with full `outcome` block.
- Class `price_factor_search_2026_04_29` member count: 4 → 5 with this entry. Class **remains OPEN** per project doctrine — future paradigms in the class continue to register against the existing class roster.

## 7. Reference

- Design memo: `docs/research/idiosyncratic_momentum_v1_design_2026_05_14.md`
- Implementation PRs: #124 (scorer + experiment script), #125 (orchestrator + launcher)
- Pre-reg: PR #122
- ev_fcff_yield precedent (paradigm #13 same-pattern FAIL): `docs/research/ev_fcff_yield_audit_verdict_2026_05_12.md`
- Blitz, D., Huij, J., Martens, M. (2011). "Residual Momentum." *Journal of Banking & Finance* 35(8): 1949-1956.
