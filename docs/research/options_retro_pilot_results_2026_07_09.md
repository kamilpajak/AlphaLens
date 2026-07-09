# Options retro exploratory pilot — results

**Status:** COMPLETE — NULL (no Bonferroni survivor); exploratory, NOT class closure
**Date:** 2026-07-09 (run same day as the data pull)
**Ledger look:** `options_retro_pilot_2026_07` (outcome recorded)
**Design memo:** [`options_retro_firstlook_design_2026_07_09.md`](options_retro_firstlook_design_2026_07_09.md) (LOCKED; family and inference executed verbatim)
**Artifacts:** `~/.alphalens/options_retro_pilot_2026_07/{panel.parquet,results.json}`
**Runner:** `apps/alphalens-research/scripts/run_options_retro_pilot.py` (helpers TDD in `alphalens_research/diagnostics/options_retro.py`)

## 1. Verdict in one paragraph

None of the four pinned tests clears the family Bonferroni gate
(wild-cluster-bootstrap p < 0.0125). The closest is the **term slope**
(ivx180 − ivx30): β +0.598 on market-excess k=10 (+4.0 pp per 1 sd),
t_CR2 +2.43, p_WCB 0.039 — sign-stable across the sub-window split but
~3× short of the corrected threshold. Per memo §8 this is the **null
branch**: no acceleration of the options question; the September forward
first look (#774) proceeds as the properly powered stage; no further
retro spend. A null at effective N = 148 episodes / 26 day-clusters was
the expected outcome for |ρ| ≲ 0.1 — the pilot could only have surfaced
a large effect, and none is there.

## 2. Data funnel and coverage (memo §8 gate)

| Stage | N | Note |
|---|---|---|
| Banked (brief_date, ticker) pairs 2026-05-19 → 2026-07-08 | 537 | full population per memo §1 |
| Plannable | 405 (in-window) | dates 05-19..05-26 are 100% non-plannable (predate trade-setup stamping) — the plannable population starts 2026-05-27 |
| **Matured** (k=10 horizon ≤ newest grouped session 2026-07-07) | **319** | brief dates 2026-05-27 → 2026-06-23 |
| **Covered** (all 4 tests computable) | **317 (99.4%)** | ✅ far above the 70% HALT; the 2 misses are EEX + SBR (vendor ivp30 never populated) |
| Ticker episodes after chained 5-session dedup | **148** | top of the predicted 80-150 effective-N band |
| Brief-day clusters | 26 | memo assumed 51 (full window); maturity truncation halves it — WCB inference chosen precisely for this regime |

Features are `options_retro_ivol_smd_v1` from the immutable deep cache
(`~/.alphalens/ivolatility_smd_retro_2026_07_deep/`, archived in Nextcloud);
weekend calendar padding removed by the `ivp30`-non-null trading-day filter
(the v9D PIT convention); vendor ivp30 used directly (its 1y lookback is
computed vendor-side on history predating our window).

## 3. Collinearity gate

VIF across {ivx30, ivp30, term slope, ATR, log-mcap}: max **4.82** (ivx30),
ATR 4.28, all others < 2. Below the 10 threshold — no residualization
needed, the pinned specs ran unmodified.

## 4. The pinned family — primary outcome car_10 (N=148, 26 clusters)

| # | Test | β (read coef) | per 1 sd | t_CR2 | p_CR2 | **p_WCB** | Bonferroni (α=0.0125) |
|---|---|---|---|---|---|---|---|
| 1 | ivx30 level | −0.180 | −0.047 | −1.20 | 0.240 | **0.402** | ✗ |
| 2 | term slope | +0.598 | +0.040 | +2.43 | 0.023 | **0.039** | ✗ (closest) |
| 3 | VRP decomposed (ivx30 | hv20) | −0.175 | −0.046 | −1.79 | 0.086 | **0.132** | ✗ |
| 4 | ivp30 | −0.0009 | −0.023 | −1.43 | 0.166 | **0.179** | ✗ |

Inference: restricted (null-imposed) wild cluster bootstrap, Rademacher
weights, 9999 draws, clusters = brief days; CR2 reported alongside. Note
the consistent p_WCB > p_CR2 ordering — exactly the CR-downward-bias the
memo predicted at ~26 clusters; naive/CR1 p-values would have flattered
every row.

## 5. Stability split (hint, not a test)

Split at 2026-06-06 (74/74 episodes):

| Test | 1st half β (t) | 2nd half β (t) | Sign-stable? |
|---|---|---|---|
| ivx30 level | −0.347 (−4.03) | +0.312 (+1.46) | **NO — flips** |
| term slope | +0.291 (+1.07) | +0.860 (+2.50) | yes |
| VRP decomposed | −0.356 (−4.77) | +0.137 (+1.00) | **NO — flips** |
| ivp30 | −0.0006 (−0.71) | −0.0010 (−1.32) | yes (both null) |

The IV-level family (tests 1 and 3) flips sign between halves — its pooled
nulls are not "small but real" effects, they are unstable ones. The term
slope is the only coefficient that is both directionally stable and
individually significant in a half; still short of the family gate.

## 6. Descriptive only — terminal realized_r (fill-dependent, N=32)

ivx30 β −1.51 (se 0.78) and VRP β −1.26 (se 0.69): directionally
"high IV at brief → worse terminal R". Selection into fills confounds this
subset (memo §5); reported per the memo, no p-values, no verdict weight.

## 7. Decision mapping (memo §8)

- **Surfacing gate:** NOT met — nothing is promoted to a September
  directional hypothesis. The term-slope near-miss is explicitly NOT
  promoted (§7 anti-leakage: only Bonferroni survivors may be); it is
  recorded here descriptively. The forward telemetry already logs the term
  slope, so September's own pre-registered family (#774) covers the
  hypothesis without any pilot-derived priming beyond what the design memo
  pinned in advance.
- **Null branch consequence:** no acceleration; NO class closure (Type II
  is expected at this N); #774 proceeds ~2026-09 on forward yfinance
  telemetry with `chain_quality=OK` accumulation; **no further retro
  spend** — the deep smd cache stays archived for reuse, the trial is not
  extended for more options data.
- **Coverage HALT:** not triggered (99.4%).

## 8. Honest caveats

- 26 day-clusters, not the 51 the design assumed — maturity truncation cut
  the window to 05-27..06-23. WCB is the right tool here, but cluster-count
  this low widens everything; the September stage does not share this
  problem (it accrues clusters daily).
- One ~4-week regime, catalyst-selected high-IV subpopulation (collider
  caveats from memo §2 apply to any reading of the signs).
- The 05-19..05-26 non-plannable exclusion is population-mechanical (no
  trade setups stamped yet), not an analyst choice.
- Retro features are vendor-constructed (IVX30/HV20); a forward yfinance
  effect may differ (memo §10) — one more reason September stands alone.
