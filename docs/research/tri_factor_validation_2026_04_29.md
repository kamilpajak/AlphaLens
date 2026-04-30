# Tri-factor combo (mom + lowvol + ROE) — extended-IS verdict

**Date:** 2026-04-29
**Strategy under test:** `score = z(mom_12_1m) − vol_w × z(vol_60d) + roe_w × z(roe_ttm)`
**Verdict:** **FAIL.** Tri-factor is 2-year-OOS-luck. Fallback to mom+lowvol baseline.

## Why we revisited

`tri_factor_combo.md` (earlier 2026-04-29) reported OOS 2023-2026 Sharpe net 1.04 with α t=2.08 — first config in the entire investigation to clear nominal t > 2.0. The IS for that result was constrained to **2 years (2021-2022)** by SimFin's data floor of 2020-Q2. Per Perplexity peer review, "2-year IS is approximately 30-40% of academic minimum acceptable standard. The strategy should not be advanced to forward validation without IS extension."

This session: build PIT-correct EDGAR companyfacts ROE store (deliverable 1, see `alphalens/data/fundamentals/edgar_companyfacts.py` + 13 tests + 82.8% R2000-PIT coverage @ 2015-01-31), re-run tri-factor on **8-year IS (2015-2022)** with EDGAR-backed ROE, then split IS into 4y halves for stability check.

## Results

### Full 8-year IS + OOS

`docs/research/tri_factor_edgar_extended_is.md` (4 configs × 2 cost levels = 16 rows):

| Period | roe_w | ADV | cost | Sharpe gross | Sharpe net | excess net | α 4F | t (4F) |
|---|---:|---|---:|---:|---:|---:|---:|---:|
| IS 2015-2022 | 0.5 | $5M | 5bp | 0.45 | 0.26 | +18.9% | +30.0% | +1.51 |
| IS 2015-2022 | 0.5 | $20M | 5bp | 0.46 | 0.29 | +16.2% | +27.2% | +1.54 |
| IS 2015-2022 | 1.0 | $5M | 5bp | 0.49 | 0.31 | +20.0% | +30.5% | +1.65 |
| IS 2015-2022 | 1.0 | $20M | 5bp | 0.36 | 0.20 | +10.4% | +20.8% | +1.26 |
| OOS 2023-2026 | 0.5 | $20M | 5bp | 1.16 | **0.98** | +41.5% | +66.0% | **+2.13** |
| OOS 2023-2026 | 1.0 | $20M | 5bp | 1.32 | **1.12** | +43.9% | +65.0% | **+2.22** |

**At face value**, 8y IS looks promising: positive Sharpe, α 30%/y, t≈1.5. Not at the t>2.0 gate but consistent positive direction. OOS 2023-2026 reproduces the original result: Sharpe net 0.98–1.12, α t > 2.0 for ADV $20M.

### Subsample stability check — locked universe, 2015-2018 vs 2019-2022

Both halves use the **same screener universe** (2015-2022 PIT union, 1395 tickers) so apples-to-apples. `docs/research/tri_factor_edgar_subsample_halves.md`:

| Period | roe_w | ADV | Sharpe gross | excess gross | α 4F | t (4F) |
|---|---:|---|---:|---:|---:|---:|
| 2015-2018 | 0.5 | $5M | 0.14 | +9.8% | +5.8% | +0.23 |
| 2015-2018 | 0.5 | $20M | 0.23 | +12.1% | +8.7% | +0.40 |
| 2015-2018 | 1.0 | $5M | -0.04 | +0.8% | -3.4% | -0.14 |
| 2015-2018 | 1.0 | $20M | 0.19 | +10.0% | +6.4% | +0.31 |
| **2019-2022** | 0.5 | $5M | -0.13 | -20.9% | -18.7% | -0.45 |
| **2019-2022** | 0.5 | $20M | -0.01 | -10.7% | -7.4% | -0.19 |
| **2019-2022** | 1.0 | $5M | -0.29 | -34.6% | -30.2% | -0.75 |
| **2019-2022** | 1.0 | $20M | -0.09 | -17.5% | -12.9% | -0.34 |

**Half 1 (2015-2018):** all four configs t-stat ≤ +0.40, half show negative gross excess after adjusting for momentum/lowvol contribution. Strategy has **no detectable edge in this 4-year window**.

**Half 2 (2019-2022):** every single config shows **negative** Sharpe, **negative** α, **catastrophic** −10% to −34% gross excess vs SPY per year. This is the period that includes 2019 late-cycle growth, 2020 COVID, 2021 junk rally, 2022 bear — and the tri-factor underperforms across all of it.

## Decision per `project_next_session_edgar_backfill.md` matrix

| Result | Verdict | Next |
|---|---|---|
| Per-subperiod t > 2.0 in 2 of 2 halves; R² stable | PASS | Forward-walk Sharpe ≥ 0.9 gate |
| One half t > 2.0, the other marginal | MID | Forward-walk Sharpe ≥ 0.7, regime-conditional sizing |
| **Both halves t < 1.5 OR catastrophic in any half** | **FAIL** | **Fallback to mom+lowvol; document tri-factor as 2-year-OOS-luck** |

Locked-universe halves: max IS-half t = +0.40 (≪ 1.5); half 2 catastrophic. → **FAIL.**

## Where the apparent 8y IS Sharpe came from

Math sanity: full IS shows excess gross +18.0%/y (rw=0.5 ADV $20M). The two locked-universe halves show +12.1% and −10.7%, averaging +0.7%/y. The 17pp gap is **not** universe size (verified — both runs use 1395 tickers); it is **carry-over edge effects** from the engine's holding-period model.

`holding_period=60` with `stride=5` means roughly 12 layered cohorts of positions are open at any time. In a single 8-year run, positions opened in late 2018 keep generating returns through 2019-Q1 (the high-payoff 2019 mean-reversion / vaccine rally pre-COVID); positions opened in late 2022 carry into 2023-Q1 (the early 2023 small-cap value rally). The two clean 4-year halves get neither benefit at their boundaries (2018→2019, 2022→2023).

The full-period Sharpe is therefore a **biased upward** view of "what running this strategy in this period would have delivered" — it benefits from cross-period continuity that a deployed-from-scratch backtest cannot recreate. The halves' Sharpe is the cleaner estimator.

## Why the OOS 2023-2026 result was so strong

Half 2 (2019-2022) shows the strategy bleeding ~20–35% per year for 4 years straight. Then OOS 2023-2026 — same engine, same scorer, same universe — reverses to +40%/y excess. This is regime amplification: small-cap quality + momentum was deeply out of favour 2019-2022 (mega-cap tech dominance, COVID dislocations, junk rally), then sharply rotated back into favour starting 2023.

A 3.3-year OOS window that picks up exactly this rotation will show a strong Sharpe — and is **not** evidence of stable alpha. The 4-year preceding window of equally severe under-performance is.

## Caveats / methodology notes

1. **Low Carhart R² flag.** Full IS R² 0.024–0.025 and OOS R² 0.008–0.011 with high α suggest most strategy variance is orthogonal to FF/Mom factors — small-cap idiosyncratic. Per `feedback_low_r2_high_alpha_diagnostic.md`, IS α >50%/y + R² <0.01 is a distributional-artifact red flag. We are below the >50% α threshold but the pattern (α 30%/y, R² 0.025) is in the same suspicious neighbourhood.
2. **Halves use locked universe.** First subsample run (no `--lock-universe`) showed half 1 universe = 969 tickers vs half 2 = 1360, biasing comparison. Locked rerun standardised both to 1395 (full 2015-2022 PIT union). Result picture identical, magnitudes shifted slightly.
3. **R² subperiod stability.** Full IS R² 0.025; half 1 R² 0.005–0.009; half 2 R² 0.063–0.093. Half 2 explained ~10× better by Carhart factors than half 1 — factor exposures clearly regime-dependent.
4. **EDGAR coverage.** 82.8% (2015) → 89.7% (2022) of R2000-like PIT universe has resolvable TTM ROE. Failures cluster on REITs/BDCs needing PartnersCapital fallback (not implemented), historical taxonomy migrations (e.g., ANF NetIncomeLoss tag only since 2015-09), and small-cap data sparsity.

## Action items

1. **Mark tri-factor CLOSED** for capital deployment. Retain code as research replay tooling per `docs/adr/0005-closed-layers-as-anti-pattern-catalog.md`.
2. **Fallback to mom+lowvol baseline** per `experiment_momentum_lowvol_combo.py`. OOS Sharpe 0.55 with documented 2017-2022 regime hole. Lower ambition, higher robustness.
3. **Pre-register before any further iteration.** Tri-factor was not pre-registered; multiple-testing burden is now non-trivial across the 30+ strategy variants explored 2026-04-28/29. Per Harvey-Liu-Zhu 2016, future tests need formal pre-registration.
4. **Re-evaluate the engine's carry-over treatment** in a follow-up. The ~17pp full-vs-halves gap is a methodology issue worth understanding: should the backtest report period-Sharpe in a way that excludes mid-period carry-over, or is the layered-cohort treatment correct? This affects how all multi-period validation results are interpreted.
