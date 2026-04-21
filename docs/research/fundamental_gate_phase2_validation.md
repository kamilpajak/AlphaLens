# Phase 2 — Fundamental Gate Validation (issue #14)

**TL;DR: FAIL. Gate destroys momentum alpha, marginally degrades early-stage. Do not promote to production.**

## Setup

Same 5-year backtest window as the canonical comparison
(`docs/backtest/compare_*_2026-04-21.md`):

- Window: 2021-06-01 → 2026-04-17, 1226 trading days, 999 daily snapshots
- Universe: 113 themed tickers (Layer 2b curated YAML, 94/113 covered by SimFin)
- Top-N: 5, linear weighting, 5-day holding
- Benchmark: SPY

Gate components (all active):
- Cash runway TTM avg OCF (hard reject <3mo, soft penalty 3-12mo)
- P/S ceiling 100 for pre-profit names (PIT-safe via SimFin shareprices)
- Consecutive negative OCF ≥4 quarters
- Floor multiplier 0.3

Fundamentals source: SimFin bulk CSV (free tier; ~435MB shareprices CSV
downloaded on VPS, rsync'd to laptop).

## Headline

| Scorer | Metric | Baseline | Gate (no P/S) | Gate (with P/S) | Δ baseline→full-gate |
|---|---|---:|---:|---:|---:|
| **Momentum** | Sharpe (gross) | 1.488 | 0.720 | **0.763** | **−49%** |
| Momentum | Sharpe (net moderate) | 1.473 | 0.703 | 0.746 | −49% |
| Momentum | FF3 α ann | 94.50% | 32.26% | 34.08% | −64% |
| Momentum | **FF3 α t-stat (HAC)** | **2.62** | 1.10 | **1.20** | **−54%** |
| Momentum | Carhart-4F α t | 2.62 | 1.10 | 1.21 | −54% |
| Momentum | Turnover | 39.9% | 35.5% | 35.5% | −11% |
| **Early-stage** | Sharpe (gross) | 1.135 | 0.959 | **0.981** | **−14%** |
| Early-stage | Sharpe (net moderate) | 1.118 | 0.941 | 0.962 | −14% |
| Early-stage | FF3 α ann | 60.97% | 48.49% | 48.95% | −20% |
| Early-stage | **FF3 α t-stat (HAC)** | **1.86** | 1.58 | **1.61** | **−13%** |
| Early-stage | Turnover | 76.2% | 72.0% | 71.7% | −6% |

## Phase 2 gate criteria (from plan)

Pass requires ALL:

| Criterion | Threshold | Momentum | Early-stage |
|---|---|:-:|:-:|
| FF3 α t-stat spadek | < 10% | ❌ (−54%) | ❌ (−13%) |
| Sharpe (net moderate) spadek | < 5% | ❌ (−49%) | ❌ (−14%) |
| Pick overlap w/ baseline | > 60% | not computed | not computed |

**Verdict: FAIL both scorers.** Phase 3 threshold sweep NOT promoted.

## Why it failed

The gate was designed assuming that Layer 3's 87% fundamental-adjacent rejections
(`docs/research/rejection_analysis.md`) were fundamentally sound filters the
scorer should adopt. Backtest disagrees — systematically:

1. **Momentum picks are correctly short-term profitable despite weak
   fundamentals.** Names like INVZ, MARA, BELFB, QUBT in the thematic
   universe *do* rally +20-164% in 20-day windows even with negative OCF
   and near-zero revenue. Gate penalizes exactly these, costing most of
   the alpha.

2. **Early-stage picks less affected** because they're inherently earlier
   in the cycle — less extended, lower P/S already — so fewer gate
   triggers. But still a 14% Sharpe drop and 13% t-stat drop below
   plan threshold.

3. **The P/S component adds ~+0.04 Sharpe and +0.11 t-stat vs no-P/S.**
   Marginal. The 435MB SimFin download is not worth it for this.

4. **Perplexity literature (CAN SLIM mutual funds)** specifically warned
   that quality-filtered momentum funds underperform despite academic
   backtests — our failure replicates that warning.

## What the failure tells us about Layer 3

Layer 3 is correctly identifying *fundamental* risk factors when it
rejects picks. But in a thematic momentum universe (AI/quantum/biotech),
the market systematically rewards exactly those risks during bubble
regimes. Layer 3 is "right on textbook fundamentals, wrong on regime."

The 120d forward spread (+48pp accepted vs rejected) from the
historical-acceptance study suggests Layer 3's fundamental view *is*
predictive at longer horizons — but our strategy rebalances at 5d
holding, where momentum dominates. A longer-holding-period strategy
might use Layer 3 fundamentals as designed; our 5d momentum pipeline
should not.

## Decision

1. **Do not promote gate to production** (`launchd/bin/alphalens-themed`
   stays with just `--scorer early-stage --analyze`, no `--fundamental-gate`).
2. **Retain code as opt-in** — `--fundamental-gate` CLI flag + config
   `fundamental_gate_enabled=False` default remain in place for future
   research (e.g. longer-horizon or quality-momentum hybrid strategy).
3. **Close issue #14 as "validated, rejected"** with link to this report.
4. **Lessons carried over to memory** (`project_fundamental_gate_fail.md`)
   for future strategies that might re-consider.

## Alternatives not explored in Phase 2 (follow-up issues possible)

- **Soften thresholds** — runway 6mo + P/S 200 (less aggressive)
- **Asymmetric deploy** — gate OFF for momentum, ON for early-stage
  (where damage is marginal, may net-out with longer paper trade)
- **Invert sign** — treat high-dilution as a BUY signal (bubble-regime
  aware contrarian). Perplexity's "price of quality" QMJ finding: when
  quality is cheap, junk outperforms near-term.
- **Longer holding period** — 60d instead of 5d. Matches Layer 3's
  120d alpha horizon better.

## Artifacts

- `docs/backtest/fundamental_gate_momentum.md` — no-P/S run
- `docs/backtest/fundamental_gate_early_stage.md` — no-P/S run
- `docs/backtest/fundamental_gate_momentum_with_ps.md` — full gate
- `docs/backtest/fundamental_gate_early_stage_with_ps.md` — full gate
- CSVs for each above
- Reference baselines: `docs/backtest/compare_*_2026-04-21.md`

## Infrastructure notes (retained)

SimFin integration works. `SimFinFundamentalsStore` with pre-split
per-ticker price dict + searchsorted lookup gave ~11m per 5y backtest,
down from "hung forever" with the naive implementation. This
infrastructure is reusable for future fundamental-aware research even
though the current gate is rejected.

Pre-CR-fix issue (OVERVIEW snapshot leaking today's TTM values into
2022 backtest) was caught + fixed in commit `9ad1d20`. Validated
against the P/S numbers in this report — backtest uses PIT close ×
shares from SimFin daily prices, not today's OVERVIEW TTM.
