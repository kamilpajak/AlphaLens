# Fundamental gate — 60d holding horizon validation (issue #15)

**Date:** 2026-04-21
**Window:** 2021-04-19 → 2026-04-17 (5y, 944 trading days at 60d-hold / 971 at 5d-hold)
**Universe:** 113 tickers (Layer 2b themed curated)
**Rebalance:** top-5, linear weighting, daily
**Gate config:** `--fundamental-gate --fundamentals-source simfin --with-prices` (PIT P/S + runway + OCF + net_income penalties, multiplicative on technical composite)

## Hypothesis (recap)

Phase 2 (issue #14) wykazał, że fundamental gate niszczy 5d-hold Sharpe (−35%) i FF3 α t (−54%). Hipoteza #15: Layer 3's fundamental edge manifestuje się na długim horyzoncie — spread accepted vs rejected α rośnie ~55× między 5d (+0.87pp) a 120d (+48.13pp), z 60d jako pierwszym horyzontem, w którym rejected α staje się ujemna (−4.30%). Gate w 60d-hold powinien więc przywrócić edge, jeśli signal faktycznie jest długoterminowy.

## Wyniki

### Momentum scorer

| Run | Sharpe (gross) | Sharpe (mod 100bps) | FF3 α ann | FF3 α t HAC | Carhart α t HAC | IC t | Turnover |
|---|---:|---:|---:|---:|---:|---:|---:|
| baseline 5d  | +1.488 | +1.473 | +94.50% | +2.62 | +2.62 | +3.97 | 39.9% |
| gate 5d      | +0.763 | +0.746 | +34.08% | +1.20 | +1.21 | +5.19 | 35.5% |
| **baseline 60d** | **+1.567** | **+1.553** | **+98.22%** | **+2.66** | **+2.66** | **+3.51** | **39.7%** |
| **gate 60d**     | **+0.763** | **+0.745** | **+35.27%** | **+1.20** | **+1.23** | **+2.56** | **35.1%** |

Ratio **gate 60d / baseline 60d**:

- Sharpe (mod): 0.745 / 1.553 = **0.480** → próg 0.95 → **FAIL** (−52.0%)
- FF3 α t HAC: 1.20 / 2.66 = **0.451** → próg 0.90 → **FAIL** (−54.9%)

### Early-stage scorer

| Run | Sharpe (gross) | Sharpe (mod 100bps) | FF3 α ann | FF3 α t HAC | Carhart α t HAC | IC t | Turnover |
|---|---:|---:|---:|---:|---:|---:|---:|
| baseline 5d  | +1.135 | +1.118 | +60.97% | +1.86 | +1.88 | +1.66 | 76.2% |
| gate 5d      | +0.981 | +0.962 | +48.95% | +1.60 | +1.61 | +3.76 | 71.7% |
| **baseline 60d** | **+1.201** | **+1.184** | **+65.12%** | **+1.95** | **+1.96** | **+1.66** | **76.0%** |
| **gate 60d**     | **+1.010** | **+0.992** | **+49.40%** | **+1.60** | **+1.59** | **+2.24** | **71.3%** |

Ratio **gate 60d / baseline 60d**:

- Sharpe (mod): 0.992 / 1.184 = **0.838** → próg 0.95 → **FAIL** (−16.2%)
- FF3 α t HAC: 1.60 / 1.95 = **0.821** → próg 0.90 → **FAIL** (−17.9%)

## Decision matrix

| Scorer | Sharpe ratio (gate60/base60) | ≥ 0.95? | α t ratio | ≥ 0.90? | Verdict |
|---|---:|:---:|---:|:---:|:---:|
| momentum    | 0.480 | NO | 0.451 | NO | **FAIL** |
| early-stage | 0.838 | NO | 0.821 | NO | **FAIL** |

**Both scorers FAIL both criteria.**

## Key observations

1. **Gate is absolutely capped.** Momentum gate Sharpe is identical at 5d and 60d (0.763 / 0.745 vs 0.763 / 0.745). Baseline momentum *improves* with longer horizon (1.473 → 1.553). The gate's score-multiplier caps the upside; removing top-ranked momentum names replaces them with fundamentally-cleaner but weaker-trend names, and the trade never recovers regardless of holding period.
2. **Early-stage degrades less but still fails.** Early-stage gate Sharpe 0.962 → 0.992 between 5d and 60d (tiny improvement) while baseline climbs 1.118 → 1.184. Gap stays wider than 5%.
3. **IC rises with gate for early-stage (1.66 → 2.24) but that does not translate to portfolio Sharpe.** The gate tightens rank-ordering (higher IC) but loses the high-magnitude names that drive returns — classic "pick-better, lose-bigger" trade.
4. **No horizon at which the gate wins on our universe.** The Layer 3 historical-acceptance asymmetry (rejected α turns negative at 60d, very negative at 120d) does exist, but the gate's heuristic score is not a close enough proxy for Layer 3's accept/reject decision to capture it. Score 0.4 doesn't mean "Layer 3 would reject"; it means "cheaper on P/S and more cash runway", which over this 113-ticker curated universe is not the fundamental dimension that Layer 3 exploits.

## Decision: **FAIL — close fundamental-gate family**

Per issue acceptance criteria, both scorers fail both metrics at 60d-hold. Combined with Phase 2 FAIL at 5d-hold, the fundamental-gate family has no horizon at which it outperforms baseline on our curated universe.

**Actions:**

- Close issue #15 with final verdict FAIL.
- Do **not** promote 60d-rebalance strategy to production — baseline 60d has stronger metrics than any gated variant, but the 60d vs 5d comparison is a separate question (baseline 60d Sharpe 1.553 vs baseline 5d Sharpe 1.473 is only marginal; turnover drops from ~40% daily @ 5d to ~40% daily @ 60d because rebalance is still daily over a 60d holding, so savings are not obvious). Defer that question.
- **Keep** existing `--fundamental-gate` CLI flag as opt-in research tool (per issue #14 close-out). Do not enable by default anywhere.
- Retire the fundamental-gate hypothesis family. Next experiments in other directions: alternative data sources (insider, short interest), ML-based ranker, or Layer 3 rejection-prediction classifier trained directly on the historical-acceptance panel rather than hand-crafted fundamental heuristics.

## Artifacts

- Baseline 60d: [`docs/backtest/baseline_momentum_hold60.md`](../backtest/baseline_momentum_hold60.md), [`docs/backtest/baseline_early_stage_hold60.md`](../backtest/baseline_early_stage_hold60.md)
- Gate 60d: [`docs/backtest/fundamental_gate_momentum_hold60.md`](../backtest/fundamental_gate_momentum_hold60.md), [`docs/backtest/fundamental_gate_early_stage_hold60.md`](../backtest/fundamental_gate_early_stage_hold60.md)
- 5d reference (Phase 2, issue #14): [`docs/backtest/compare_momentum_2026-04-21.md`](../backtest/compare_momentum_2026-04-21.md), [`docs/backtest/compare_early_stage_2026-04-21.md`](../backtest/compare_early_stage_2026-04-21.md), [`docs/backtest/fundamental_gate_momentum_with_ps.md`](../backtest/fundamental_gate_momentum_with_ps.md), [`docs/backtest/fundamental_gate_early_stage_with_ps.md`](../backtest/fundamental_gate_early_stage_with_ps.md)
