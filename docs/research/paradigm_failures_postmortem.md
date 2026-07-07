# Eleven Paradigm Failures — AlphaLens Active Alpha Postmortem (2026-04-18 → 2026-05-09)

**Author:** Solo retail quant, Polish tax resident on XTB
**Period covered:** 2026-04-18 → 2026-05-09 (22 days, 11 strategies + 2 INCONCLUSIVE retrospectives across 4 architectural layers; original 5 failures + post-pivot continuation through 2026-04-30 + further continuation 2026-05-04 → 2026-05-09)
**Status:** Project remains positioned as research / learning infrastructure (pivoted 2026-04-25). Capital deployment OFF-TABLE per project policy. Path B refined 2026-04-30 — **screener search stays open-ended under pre-registration discipline** (per `feedback_keep_searching_screeners.md`). Path B is "methodology bundle + Layer 1 watchdog stay live," NOT closure of the search.
**Note:** This document is a **failure ledger**. The pipeline did eventually admit one phase-robust positive (PASS_MARGINAL on Cohen-Malloy opportunistic Form-4, 2026-05-09) — that result has its own dedicated postmortem at [`insider_form4_opportunistic_phase_b_postmortem_2026_05_09.md`](insider_form4_opportunistic_phase_b_postmortem_2026_05_09.md) and is summarised briefly in the Continuation 2026-05-04 → 2026-05-09 section below.
**Audience:** Future-self when re-evaluating active strategies; potentially other retail quants who stumble into similar territory

## Contents

- [Executive summary](#executive-summary)
- [Detailed failure timeline](#detailed-failure-timeline) (failures 1-5)
  - Layer 2b themed (failure 1) — 2026-04-22
  - Layer 2d insider (failure 2) — 2026-04-24
  - Layer 2e rotation (failure 3) — 2026-04-24
  - Layer 2f events (failure 4) — 2026-04-25
  - Layer 2g guru (failure 5) — 2026-04-25
- [Common failure patterns 1-6](#common-failure-patterns-lessons-learned)
- [Reusable infrastructure inventory](#reusable-infrastructure-inventory)
- [Conditions for project re-activation](#conditions-for-project-re-activation)
- [Methodology principles](#methodology-principles-apply-to-next-idea-whenever-it-appears)
- [Continuation 2026-04-25 → 2026-04-30](#continuation--post-pivot-research-2026-04-25--2026-04-29)
  - Failure 6 — Tri-factor (2026-04-29)
  - Failure 7 — Mom+lowvol combo (2026-04-29 PM)
  - Failure 8 — Regime-gate rescue (2026-04-29)
  - [Common patterns from failures 6-8](#common-patterns-from-failures-6-8)
  - Failure 9 — Quality+momentum (2026-04-30)
  - Failure 10 — Vol-target overlay (2026-04-30)
  - [Common patterns from failures 9-10](#common-patterns-from-failures-9-10)
- [Path B refined (2026-04-30)](#path-b-refined-2026-04-30)

## Executive summary

Five active alpha paradigms tested with progressively rigorous methodology. All five failed empirical validation:

| # | Layer | Strategy | Verdict | Headline metric |
|---|---|---|---|---|
| 1 | 2b | Small-cap themed momentum (15-stock top-N, daily rebalance, 60-day hold) | **KILL** | IS Sharpe 1.71, Carhart t=2.62 → **OOS t=0.82** |
| 2 | 2d | Insider Form 4 cluster-buy (R2000 universe) | **KILL** | IS Carhart t=2.14 → **OOS t=0.68** |
| 3 | 2e | Tactical sector rotation (SPY/QQQ/IWM macro overlay, quarterly) | **KILL** | IS Sharpe 0.94 → **OOS Sharpe 0.83, α t=0.33**, R²=0.999 vs passive |
| 4 | 2f | 8-K event-driven go/no-go screen | **KILL** | Winsorized CAR negative across all Item types; typical 8-K = bad news |
| 5 | 2g | LLM-researcher GuruAgent (Buffett-style, Gemini 3.1 Pro) | **KILL** | Mean +82 bps over 4 years, 2/4 years underperform in growth bulls (2020 + 2024), R²=0.97 vs SPY |

**Total time:** 8 days. **Total LLM cost:** ~$0.85. **Capital deployed:** $0.

**Key finding:** Each strategy generated *some* positive evidence in-sample (Sharpe 0.9-1.7, Carhart t 1.7-2.6) but failed under one or more of:
- Out-of-sample regime change
- Sample size scaling
- Realistic transaction costs
- Multiple-testing correction
- Strict pre-committed gates (correlation to benchmark, min-year underperformance)

## Detailed failure timeline

### Layer 2b: Small-cap themed momentum (closed 2026-04-22)

**Setup:** 113-ticker curated YAML universe (quantum, AI, biotech themes), daily rebalance, top-15 by 7-metric momentum scorer (`MomentumScorer`), 60-day fixed hold, linear weighting.

**In-sample (2017-2022 train):** Sharpe 1.71, Carhart-4F α t=2.62 (HAC).

**Out-of-sample (true 2023-2026):** Carhart α t=**0.82**.

**Root cause analysis:**
1. **Universe survivorship bias** — 18 tickers added 2026-04-19 retrospectively contributed ~0.3 OOS α t. PIT-95 sub-universe drop OOS α t from 1.70 to 1.36.
2. **Multiple testing under-correction** — ~26 primary hypothesis tests w research program but Bonferroni applied only n=2.
3. **Cost model unrealistic** — `cost_model.py` flat 100bps "moderate" = ~100× optimistic vs real spread × turnover × frequency. Real daily-rebalance microcap cost ~100% ann drag.
4. **Bankruptcy sign flip** — augmented universe (delisted M&A targets included) flipped train α t from +0.80 to -0.86. Scorer picked biotech failures with similar features as winners.

**Documented:** `docs/research/layer2b_audit_final.md`, issues #14-18.

### Layer 2d: Insider Form 4 cluster-buy (closed 2026-04-24)

**Setup:** R2000+ universe, weekly rebalance, top-15 by cluster-buy score (≥3 insiders, ≥5% notional), 60-day hold.

**In-sample:** Carhart α t=2.14 (marginal).

**Out-of-sample:** Carhart α t=**0.68**.

**Root cause:** Same overfit signature as 2b. Insider Form 4 is publicly available at 10s latency post-filing — strategy crowded by HFT/quant funds.

### Layer 2e: Tactical sector rotation (closed 2026-04-24)

**Setup:** SPY/QQQ/IWM 60/30/10 core + 4 macro-rule overlay (yield curve, VIX decile, QQQ/IWM momentum spread), quarterly rebalance, max ±10% tilt per ticker.

**In-sample (2009-2020):** Sharpe 0.94 net, α=28 bps t=1.96, cumulative 573.9% over 12 years.

**Out-of-sample (2021-2026):** Sharpe 0.83 net, α=**7.9 bps t=0.33**.

**Sanity checks (added per Perplexity follow-up):**
- `passive_correlation`: **1.000** with passive 60/30/10 → no real differentiation
- `rolling_sharpe_stability`: min 252d Sharpe **−0.94** at 2022-12-28
- `per_regime_vs_passive`: passes (1-2 bps/d marginal in each regime)
- `overlay_alpha`: **7.9 bps t=0.33** (was 28 bps t=1.96 IS)

**Root cause:** ±5% max tilt × 5% allocation move = ±25 bps tilt magnitude. Daily ETF return std ~100 bps. Tilt is mathematically dominated by passive holdings → R²=0.999. The "tactical overlay" is structurally noise relative to core exposure.

**Lesson:** R² approaching 1.0 vs benchmark is a dealbreaker, NOT a feature. Should have caught at design stage, not after IS run.

### Layer 2f: 8-K event-driven go/no-go screen (failed 2026-04-25)

**Setup:** 150 random S&P 500 tickers, 2022-2024 window, compute CAR at +1/+5/+20/+60d after each 8-K filing, aggregate by Item type.

**Result:** All Items had winsorized mean CAR < 50 bps (or negative). Items 1.01 (Material Definitive Agreement), 5.02 (Executive Departure), 8.01 (Other Events), 9.01 (Financial Statements) all showed median CAR -100 to -250 bps. Outlier-dominated raw means (mostly M&A surprise spikes) inflated mean above thresholds, but winsorization at 5-95th percentile collapsed signal.

**Key insight:** Most 8-K filings are bad news for the filing company. Asymmetry: positive announcements rarely come via 8-K (they go in earnings or press release), but negative material events (loss disclosure, default, agreement termination, exec departure under pressure) often do.

**Lesson:** Always winsorize on outlier-prone signals. Raw mean is misleading on heavy-tailed distributions. T-stat plus median + winsorized mean give honest picture.

### Layer 2g: LLM-researcher GuruAgent Buffett (closed 2026-04-25)

**Inspiration:** GuruAgents paper (arXiv 2510.01664, 2025) claimed Buffett-style GPT-4o achieved 42.2% CAGR on NASDAQ-100 Q4 2023-Q2 2025. Methodology had 6 flaws (single bull window, NASDAQ-100 = current survivorship-biased, OOS buffer 0-3 months from training cutoff, transaction costs 0.01% unrealistic, prompt = Buffett's published criteria = crowded).

**Our pilot v2 design (improved methodology):**
- Universe: S&P 500 random 30 tickers/year (proxy PIT, survivorship caveat)
- Years: 2018, 2020, 2022, 2024 (4 distinct regimes)
- LLM: Gemini 3.1 Pro, simple prompt + Polygon-backed financial context
- Hold: 1 year equal-weight top-10 by conviction
- Pre-committed kill thresholds (locked before run)

**Results (4 years):**

| Year | Regime | Portfolio | SPY | Outperf |
|---|---|---|---|---|
| 2018 | quiet/down | +1.74% | -7.41% | **+9.15pp** |
| 2020 | COVID growth bull | +13.68% | +17.24% | **-3.56pp** |
| 2022 | bear | -15.26% | -18.36% | **+3.11pp** |
| 2024 | AI growth bull | +19.85% | +25.28% | **-5.43pp** |

**Aggregate:** Mean +82 bps. Min-year -5.43%. Correlation to SPY +0.97.

**Verdict (relaxed gate, min-year tolerance -5pp per Perplexity):** **KILL on all 3 gates**:
- mean +82 bps < 200 bps floor
- min-year -5.43% beyond -5pp tolerance
- correlation +0.97 > 0.95 threshold

**Critical insight from 2024:** Adding 2024 collapsed mean from +290 bps (3-year) to +82 bps. **Two of four years underperform**, both in growth-bull regimes (2020 + 2024). Buffett-style screen systematically avoids speculative growth (NVDA, TSLA, MSFT, META, GOOG) — in 2020s+ market regime where mega-cap tech dominates SPY, this is **structural** drag, not cyclical noise.

**Perplexity's structural ceiling argument:** CXO Advisory tracked 6,582 graded forecasts since 2006. Average accuracy 46.9% — barely above coin flip 50%. Has not improved in 20 years despite advances in technology. LLMs do not break this ceiling because the problem is market predictability ceiling, not analysis sophistication.

## Common failure patterns (lessons learned)

### Pattern 1: IS-OOS Carhart t-stat degradation 30-65%
- 2b: 2.62 → 0.82 (-69%)
- 2d: 2.14 → 0.68 (-68%)
- 2e: 1.96 → 0.33 (-83%)
- 2g: implied (290 bps → 82 bps mean)

If your IS Carhart α t-stat is between 1.5 and 3.0, **expect 50-70% degradation OOS**. Plan strategy economics around OOS expectations, not IS observations.

### Pattern 2: R² approaching 1.0 vs benchmark = signal dead
- 2e: R² 0.999 vs passive
- 2g: correlation 0.97 vs SPY

When your active strategy's daily returns correlate >0.95 with a passive benchmark, the "active alpha" is mathematically dominated by passive exposure. The differentiator is too small to matter after costs.

### Pattern 3: Outlier-dominated raw means deceive
- 8-K screen: raw mean +606 bps for Item 5.03 (delisting). Winsorized: +602 bps with std 5783, n=36. Most positive raw means came from rare large M&A spikes, not consistent positive drift.

Always report: median + winsorized mean + t-stat alongside raw mean. Especially for heavy-tailed distributions (events, momentum, M&A).

### Pattern 4: Value strategies underperform structurally in 2020s+
- 2g: Buffett-style fails 2020 (-3.56pp) AND 2024 (-5.43pp). Both growth bulls.
- Mega-cap tech concentration in SPY (NVDA + MSFT + GOOG + META + AMZN > 25% of cap) means "avoiding speculative growth" = "missing the index return".
- This is Perplexity's structural ceiling argument applied to value style.

### Pattern 5: Universe concentration → O(P/N) overfit coefficient
- 2b: 113 tickers / 15 picks = 13% concentration. Random sampling variance dominates signal.
- 2g: 30 tickers / 10 picks = 33% concentration. Even more vulnerable.

Larger universe + smaller relative concentration = lower overfit risk. But also: harder for LLM/scorer to find systematic signal.

### Pattern 6: Multiple-testing correction underestimated
- 2b: 26 hypothesis tests, Bonferroni applied to n=2. Should have been n≥15.
- True n should count: every config-changing commit, every parameter sweep, every gate variant tested.

## Reusable infrastructure inventory

These modules survive the project pivot and are usable for future research:

### Backtest harness
- `alphalens/backtest/engine.py` — top-N selection
- `alphalens/archive/rotation/overlay_engine.py` — overlay/tilt strategies
- `alphalens/attribution/factor_analysis.py` — Carhart-4F, FF5+UMD, Q4 attribution with Newey-West HAC
- `alphalens/backtest/multiple_testing.py` — Bonferroni + BH-FDR + t→p conversion
- `alphalens/backtest/sharpe.py` — autocorr-adjusted Sharpe (Lo 2002)

### Sanity check framework (4 gates pattern)
- `alphalens/archive/rotation/sanity_checks.py` — passive_correlation, rolling_sharpe_stability, per_regime_vs_passive, overlay_alpha
- Reusable for ANY future strategy before commitment to OOS

### Pre-commit discipline
- `alphalens/archive/rotation/precommit.py` — config fingerprinting + git SHA tracking
- `alphalens/archive/rotation/config.py` — `ConfigFingerprint` pattern (file SHA-256 + git SHA)
- `alphalens/archive/guru/prompt.py` — prompt fingerprinting (same pattern, applied to LLM prompt files)

### Data clients (production-grade, rate-limit-aware)
- `alphalens/data/alt_data/sec_edgar_client.py` — SEC EDGAR submissions, Form 4, fundamentals
- `alphalens/data/macro/fred_client.py` — FRED time series with disk cache
- `alphalens/archive/guru/polygon_fundamentals.py` — Polygon Stocks Starter PIT-correct financials (income/balance/cashflow)
- `alphalens/archive/screeners/lean/polygon_client.py` — Polygon market data (OHLCV, ticker reference)

### LLM infrastructure
- `alphalens/archive/guru/llm_scorer.py` — disk-cached GuruScorer with structured JSON output, cost tracking
- `alphalens_cli/commands/guru.py` — Layer 2f GuruAgent pilot (LangChain + Gemini directly, no shared wrapper)
- (TradingAgents-based per-stock multi-agent analysis removed 2026-04-30 per ADR 0008)

### Live systems
- Layer 1 SEC EDGAR watchdog (launchd, daily digest) — keeps eyes on real-time events without capital commitment

### Test coverage
- ~700 unittests covering all production modules
- TDD discipline enforced (`feedback_tdd_always.md`)
- Fixtures pattern: `tests/test_<module>.py`, mocked external APIs, no shared conftest

## Conditions for project re-activation

Return to active alpha generation mode if ANY of:

1. **New peer-reviewed publication** (2026+) with **multi-window OOS validation** showing retail-replicable edge in paradigm not yet tested (e.g., true volatility-managed strategies, specific event-driven sub-strategies with academic backing).

2. **Convert arb attractive enough** that user accepts IB Poland broker switch — Perplexity ranked #1 (50-65% OOS) with HFRX Convertible Arbitrage Index +10.1% (2023), strong 2024, +4% YTD May 2025.

3. **Market regime change** that invalidates "value-style structural underperformance" thesis — e.g., mega-cap tech multi-year drawdown, breakdown of 2020s growth concentration.

4. **Capital-deploy budget grows materially** allowing institutional-grade data subscriptions — OptionMetrics ($200-300/mo) + CBOE DataShop ($300-500/mo) + Bloomberg level-2 data — for rigorous backtest of strategies that fail in retail data.

## Methodology principles (apply to next idea, whenever it appears)

1. **Pre-commit kill thresholds in YAML/text BEFORE running any backtest** — config-fingerprint + git SHA. Any change = +1 to true_n_tests for Bonferroni.

2. **Always include sanity checks before claiming OOS validity:**
   - Correlation to passive benchmark < 0.95
   - Rolling Sharpe stability across windows
   - Per-regime decomposition (bull/bear/flat)
   - Realistic cost model (spread × turnover × frequency)

3. **Winsorize outlier-prone signals.** Report median + winsorized mean + t-stat alongside raw mean.

4. **Multiple-testing correction = `n` of config-changing commits**, not `n` of final variant count.

5. **OOS Carhart α t-stat threshold = 1.5, not 2.0** — OOS lower SNR than IS. But also expect 50-70% degradation, so target IS Carhart α t > 3.0 if you want OOS t > 1.5.

6. **Honest go/no-go screen BEFORE building infrastructure.** Layer 2f 8-K screen took 1 day to write, gave KILL verdict in 1 hour. Saved weeks of building event-driven trading strategy on top.

7. **External LLM consultations (Perplexity, etc.) are structural priors dressed as evidence.** Treat as "directional opinion" not "Bayesian update". Run own pilots with own thresholds.

## Final reflection

Five strategies in 8 days. Empirical evidence wins over theoretical confidence every time. The single most valuable artifact from this 8-day journey isn't any one strategy — it's the **standardized methodology for killing bad ideas quickly**:

1. Define hypothesis specifically
2. Pre-commit kill thresholds with file fingerprint
3. Build minimal pilot (20-30 tickers, 4 regimes if possible)
4. Run, evaluate against thresholds, accept verdict
5. Document failure modes for next idea

If you're a future self reading this, or another retail quant: **the goal isn't to find a strategy that works. The goal is to design a research framework that kills bad ideas with discipline before they consume your time and capital.** That framework now exists in this codebase. Whether or not you find a working strategy, you've built something more valuable: a discipline.

---

## Continuation — post-pivot research (2026-04-25 → 2026-04-29)

The 2026-04-25 pivot did not stop strategy prospecting; it shifted it from "search for one to deploy" to "search to learn what doesn't work." Three more candidates failed across these four days, bringing the cumulative count to eight. Each tightened the methodology before the next test.

### Failure 6 — Tri-factor (momentum × value × quality), 2026-04-29

**Setup:** R2000-PIT universe, top-decile composite of 12-1 momentum × P/B value × ROE quality, weekly rebalance, 5-15bp cost.

**Single-phase IS (2019-2022):** α 4F = 63%/y, t = 2.24, R² = 0.049 (Phase 4 of stride-5 grid).

**Multi-phase audit (5 phases at stride=5):** mean t = +0.34, mean excess net = −8.5%/y, dispersion 28pp across phases. Phase-4 outlier was misleading single-sample.

**Verdict:** FAIL phase-robust. The single-phase IS read of "α t=2.24" was a sample-of-one artifact of which day-of-week the rebalance grid started on. **Methodology insight that came out of this run:** strided multi-rebalance backtests are subject to **phase-aliasing** — running one phase produces a Sharpe / t-stat that's only meaningful if the phase happens to land on the period where signal coheres. A multi-phase audit pattern (run all stride-offset phases, aggregate mean ± std) is required for credible verdict.

**Documented:** `docs/research/methodology_audit_2026_04_29.md`, `docs/research/tri_factor_multi_phase_verdict.md`. Patches landed: `phase_offset` engine param, `--phase-offset` CLI flag, `alphalens.backtest.multi_phase` library, `scripts/audit_multi_phase.py` driver.

### Failure 7 — Momentum × low-volatility combo, 2026-04-29 PM

**Setup:** R2000-PIT universe, top-decile of 12-1 momentum × inverse-vol composite, weekly rebalance, same cost model.

**This was the first real test of the just-shipped pre-registration ledger** (`alphalens preregister`). Pre-registered as signal class `price_factor_search_2026_04_29` alongside pure-contrarian and pure-momentum (both already FAIL'd OOS earlier the same day).

**Multi-phase audit:** mean OOS αt = +0.49, mean excess net = −5.7%/y, **dispersion 44.5pp across phases**. The single-phase IS had previously suggested mom+lowvol was "the only surviving strategy" with positive OOS Sharpe. Multi-phase erased that.

**Verdict:** FAIL phase-robust. Class `price_factor_search_2026_04_29` ends 3/3 FAIL. Bonferroni gate within class held — pre-registration framework worked as designed.

**Documented:** memory `project_mom_lowvol_combo_failed_2026_04_29.md`, `docs/research/strategy_validation_playbook.md` (7-step pipeline canonicalised post-incident).

### Failure 8 — Regime-gate rescue attempt, 2026-04-29

**Setup:** screener-agnostic regime gate (`alphalens.gates.regime_gated_scorer`) wrapping mom+lowvol BASE, with five pre-registered classifiers post-Perplexity revision: yield curve inversion (C1), VIX>20 (C2), NFCI>+1 (C3), HY OAS>400bp (C5), cross-sectional dispersion (C6). Bonferroni n=5 → |t|≈2.58. Hypothesis: 2017-2022 failure was a regime-conditional drawdown that a sensible gate could exclude.

**Phase 1 coverage diagnostic** (1499 SPY trading days, IS 2017-2022) overruled the design before any backtest ran:

| Classifier | Predicted OFF % | Actual OFF % |
|---|---|---|
| C1 yield curve | ~5 | 8.7 |
| C2 VIX>20 | ~30 | 40.8 |
| C3 NFCI>+1 | ~10 | **0.0** (degenerate at literature threshold) |
| C5 HY OAS>400bp | ~5 | 11.5 in one 173-day run (window-split, not regime) |
| **C6 (Perplexity's primary candidate)** | **>40** | **4.5** with 2.5-day mean run = noise events |

**Cross-sectional alternatives tested:** four C6 spec variants topped out at 21.4% with 6.6-day mean runs — microstructure noise clustering, not regime structure.

**Perplexity Sonar Reasoning Pro 2026-04-29 PM** (consulted post-diagnostic) overruled the proposed n=2 Phase 2 redesign as HARKing: pre-registration bound n=5 and dropping classifiers based on Phase 1 coverage is post-hoc rationalization without an explicit pre-registered amendment clause. More importantly, it argued **Phase 2 is theater + multiplicity p-hacking** because Phase 1 already empirically falsified the hypothesis — the failure window was structurally normal on every macro stress dimension; the cross-sectional dispersion that actually killed mom+lowvol was a slow cumulative drift, not a daily-classification regime.

**Verdict:** FAIL Phase 1; Phase 2 cancelled. Phase 0 infrastructure (the screener-agnostic wrapper) is reusable as RESEARCH_ONLY plumbing, but no concrete classifier shipped.

**Documented:** `docs/research/regime_gate_phase1_diagnostic.md`, memory `project_regime_gate_phase1_diagnostic.md`. Diagnostic script: `scripts/regime_gate_phase1_diagnostic.py`. Perplexity transcript summarised at the bottom of the diagnostic memo.

## Common patterns from failures 6-8

### Pattern 7: Phase-aliasing in strided backtests

A backtest with a stride-5 weekly rebalance has FIVE phase offsets, only one of which gets reported by default. Single-phase Sharpe / t-stat is sample-of-one; phase-robust mean ± std is the honest summary. **Fix:** `alphalens.backtest.multi_phase` library + `audit_multi_phase` driver. **Lift to discipline:** strategy validation playbook step 4 mandates multi-phase audit before declaring PASS/FAIL.

### Pattern 8: Pre-registration bites the hand that built it

The pre-registration ledger (`alphalens preregister`) was designed to enforce class-conditional Bonferroni. Failure 7 was its first real test — and it killed the strategy that single-phase IS would have called a survivor. Working as designed.

### Pattern 9: Coverage diagnostics falsify regime-gating hypotheses without backtesting

Before running a multi-classifier regime-gate over a Bonferroni budget, measure each classifier's **OFF coverage** of the failure window. If the supposed-target classifier covers <5% of the window, no backtest can rescue the strategy — the gate is incoherent with the failure mechanism. **Fix:** `scripts/regime_gate_phase1_diagnostic.py` pattern: 30 minutes of coverage diagnostics saves 3-4 hours of theatrical pre-registered backtests.

### Pattern 10: Structural drift is not a daily-classification regime

Mega-cap-vs-small-cap dominance during 2017-2022 was a slow cumulative drift that compounded to enormous CAGR gaps without ever showing up as point-in-time dispersion strong enough to trip a sensible threshold. Macro / cross-sectional snapshot signals don't gate it; only universe selection does (per Path A reframing — but that's universe-shopping disguised as a fix unless explicitly framed as a new research question).

## Final tally and the Path B accept

8 strategies, 12 days, $0 deployed. Empirical evidence:

- 8/8 paradigm failures, including the first real test of pre-registration (PASS as a methodology — caught the FAIL).
- 4 anti-pattern mechanisms newly identified post-pivot (phase-aliasing, ledger discipline, coverage diagnostics, structural-drift-vs-regime distinction).
- Reusable methodology bundle extracted to `kamilpajak/phase-robust-backtesting` (MIT) — see `docs/anti_patterns.md` in that repo for the 5 failure mechanisms documented for community use.

**Path B confirmed 2026-04-29:** AlphaLens is research infrastructure + decision-support tooling, not active alpha. The methodology bundle is the actual product. Active strategy prospecting on this universe is closed; future tests would be inbound validation (replicating published strategies) rather than outbound prospecting.

### Failure 9 — Quality × momentum, 2026-04-30 (Path B course-correction test)

**Setup:** R2000-PIT + EDGAR-fundamentals universe, score = z(mom_12_1m) + z(roe_ttm), top-15, weekly stride, ADV ≥ $5M, 5bp cost. Pre-registered as 4th hypothesis in `price_factor_search_2026_04_29` class (Bonferroni n=4 → critical |t| ≈ 2.50). Triggered by user request "experiment with new screener better than mom+lowvol combo." Honored Path B by going through full pre-reg discipline.

**Multi-phase audit (5 phases at stride=5):**

| Phase | IS αt | IS excess net | OOS αt | OOS excess net |
|---|---|---|---|---|
| 0 | +1.55 | +35.4% | −0.63 | −55.6% |
| 1 | +0.11 | +5.5% | **+2.00** | **+112.2%** |
| 2 | −0.22 | −29.2% | −0.14 | −7.0% |
| 3 | +0.57 | +19.1% | −0.03 | −43.8% |
| 4 | +0.88 | −1.4% | +0.68 | +45.5% |
| **mean** | **+0.58** (±0.69) | +5.9% (±24pp) | **+0.38** (±1.02) | +10.3% (±69pp) |

**Verdict:** FAIL phase-robust on both IS and OOS. **OOS dispersion is 167.8pp** (range +112.2% to −55.6% on the same signal across 5 stride-offset phases) — not a strategy, a phase-roulette.

**The methodological lesson is the strongest empirical evidence yet for the phase-aliasing anti-pattern.** Phase 1's OOS αt = +2.00 would have looked like a discovery in single-phase reporting (passes Bonferroni 1.96 even). It's a sample-of-one artifact. Multi-phase mean αt = +0.38 — far below the 2.50 threshold the pre-registration imposed.

**Comparative outcome vs failure 7:** Quality+momentum is *worse* than mom+lowvol on phase-robust mean (αt 0.38 vs 0.49). Adding ROE quality factor failed to rescue the 2017-2022 hole AND introduced post-2022 underperformance — same structural drag as Layer 2g Buffett-style screening out mega-cap tech rally. **Documented:** memory `project_quality_momentum_failed_2026_04_30.md`, audit JSON at `docs/research/quality_momentum_multi_phase_audit.json`.

## Path B refined (2026-04-30)

The class `price_factor_search_2026_04_29` now stands 4/4 FAIL. Bonferroni-corrected critical |t| rises to 2.58 if a 5th test is added. Pattern is now overwhelming for the price-factor angle on R2000-PIT:
- 9 distinct strategy hypotheses on this universe in 13 days, all FAIL phase-robust.
- Phase-aliasing demonstrated empirically across two signal classes (mom+lowvol with 44.5pp dispersion and quality+momentum with 167.8pp dispersion).
- Pre-registration ledger working as designed: every test imposes a cost on subsequent tests, and 0/4 in the price-factor class achieved the threshold even before correction.

**Path B = methodology bundle is the durable artifact + Layer 1 watchdog and literature review stay live.** This does NOT mean strategy prospecting halts. The screener search remains open-ended — the next hypothesis must clear a 2.58 Bonferroni bar, the one after 2.65, and so on. Each failure narrows the next hypothesis; the bundle's discipline is what makes the iteration honest, not a meta-decision to stop.

The discipline that turned nine bad ideas into nine clean falsifications is the artifact; the methodology bundle is the product. The search continues.

### Failure 10 — Vol-target overlay on mom+lowvol (Layer 4 introduction), 2026-04-30

**Setup:** First test in a brand-new architectural layer (`alphalens/overlays/`, ADR 0007). Vol-targeting per Moreira & Muir 2017, *Journal of Finance*: scale gross exposure by `target_vol / realized_vol_rolling`. Wraps the mom+lowvol BASE (failure 7) with target_vol=0.10 ann, lookback=5 weekly periods (~1 month, parity with M-M), max_leverage=1.5. Pre-registered as `vol_target_mom_lowvol_2026_04_30` in fresh signal class `risk_management_overlay_2026_04_30` (Bonferroni n=1, |t|≥1.96). **Dynamic per-rebalance cost** (zen review fix): `turnover_t = base_turnover · scale_t + |scale_t − scale_{t-1}|` — accounts for both position-size scaling AND the turnover cost of leverage adjustments themselves.

Triggered by user question whether stop-loss / trailing limits could rescue the "least bad" screener. Pushed back with academic evidence (Kaminski-Lo 2014: stops on long-only momentum don't add value), proposed M-M vol-targeting as the academically supported variant, ran with full discipline.

**Multi-phase audit (5 phases):**

| Phase | IS αt | IS excess net | OOS αt | OOS excess net |
|---|---|---|---|---|
| 0 | +1.32 | −2.3% | +1.23 | +7.2% |
| 1 | +1.45 | +10.7% | +1.40 | +23.4% |
| 2 | −1.27 | −31.7% | +0.44 | +5.9% |
| 3 | −0.49 | −34.0% | **−0.82** | **−77.9%** |
| 4 | +0.83 | +12.9% | +0.18 | +6.2% |
| **mean** | **+0.37** (±1.19) | **−8.9%** (±22.7pp) | **+0.49** (±0.89) | **−7.0%** (±40.3pp) |

**Verdict:** FAIL phase-robust on both IS and OOS. Mean OOS αt = +0.49 — **identical to BASE mom+lowvol_combo's +0.49**, so the overlay added no alpha. Pre-reg's primary success gate (|t|≥1.96) fails by wide margin.

**Critical methodological finding — vol-targeting can amplify regime-shift losses on a small-cap weekly grain:**

- Phase 3 OOS: BASE excess net was −43.8%; **overlay −77.9%** (overlay AMPLIFIED the loss).
- Mechanism: vol-targeting is reactive (uses `returns[<t]` only). Going into phase 3's drawdown, the trailing-window vol was low → overlay was at scale ≈ 1.5x. The vol spike happened concurrently with the loss; overlay never had time to de-risk before the position had already been amplified. Textbook "lever into your own crash."
- Phase 1 confirms the asymmetry from the other side: BASE had a spurious αt=+2.00 (single-phase artifact); overlay damped it to +1.40 by de-leveraging the lucky phase. Combined with phase-3 amplification, **the overlay damps gains and amplifies losses on this base**.
- This contradicts the M-M 2017 finding because that mechanism was demonstrated on aggregate-market monthly returns 1926-2015 with strong vol-clustering autocorrelation — does not generalize to long-only small-cap weekly factor portfolios where vol shifts can be event-driven and concurrent with the loss.
- **Time-varying-beta hazard (ADR 0007) confirmed empirically.** Overlay-bearing strategies require Sharpe-improvement vs ungated BASE as the primary metric; here even that fails.

**The architectural-layer attribution is clean.** Failure 10 isolates to Layer 4 (risk overlay). The screener (Layer 1) wasn't re-tested or perturbed. The selection-gate (Layer 2) wasn't involved. We know the overlay added no value on this BASE; we don't have to re-litigate the screener. That's exactly the attribution clarity the layer separation was meant to deliver.

**Documented:** memory `project_vol_target_overlay_failed_2026_04_30.md`, audit JSON `docs/research/vol_target_overlay_multi_phase_audit.json`, driver `scripts/experiment_vol_target_overlay.py`, module `alphalens/overlays/{vol_target,__init__}.py`, tests `tests/test_risk_overlay.py`, ADR `docs/adr/0007-layer-architecture.md`.

## Common patterns from failures 9-10

### Pattern 11: Layer attribution makes failure modes legible

The layer architecture (ADR 0007) cleanly separated *which layer* each failure attributed to:
- Failures 1-7 (Layer 1 screeners): the cross-sectional rank itself fails phase-robust
- Failure 8 (regime-gate Phase 1): the Layer 2 selection-gate's classifiers don't cover the failure window — falsified before any backtest ran, just from coverage diagnostics
- Failure 10 (vol-target overlay): the Layer 4 sizing rule reactive to lagged vol amplifies losses on a base with no phase-robust expectancy

Without the layer separation, failure 10 would have been "another tweak that didn't work." With it, we know vol-targeting per se is on a different abstraction tier from screener selection, and falsifying it doesn't say anything about whether a *different* base (one with phase-robust expectancy) might benefit from the same overlay. That's a cleaner null result.

### Pattern 12: Reactive risk overlays can lever into concurrent regime shifts

Vol-targeting using realized-vol estimates from `returns[<t]` is structurally reactive. When a regime shift produces a loss with a concurrent vol spike (rather than a vol spike that precedes the loss), the overlay never has time to de-risk. The trailing window still shows low vol while the loss is unfolding; the overlay sits at high scale until *after* the loss has already been amplified.

This is well-known in vol-targeting critique literature for daily/weekly grain on individual securities. Moreira-Muir 2017's positive result was monthly aggregate-market with slow-vol-clustering autocorrelation — a structurally different regime. The result does not generalize to small-cap weekly factor portfolios.

**Lesson:** future risk-overlay candidates should be tested against base strategies whose drawdowns lag vol shifts (predictable vol-cluster regimes), not strategies whose drawdowns and vol spikes are concurrent (event-driven small-cap regimes).

## Continuation 2026-05-04 → 2026-05-09

Post-Path-B-refinement search continued under the same pre-registration
discipline. Six days, one further failure (Failure 11), two INCONCLUSIVE
retrospectives (which validated the methodology bundle's strict gating
without admitting a deployable signal), and finally one PASS_MARGINAL —
the project's first phase-robust positive, summarised at the end with
a pointer to its dedicated postmortem.

### Failure 11 — distress_credit v1, 2026-05-04

Distress-credit screener using KMV-style distance-to-default + companyfacts
liabilities feature. Pre-registered as `distress_credit_v1_2026_05_04` (single-class).

Phase A coverage gates (companyfacts liabilities completeness across SP1500
PIT) auto-pivoted to the relaxed-coverage variant per pre-reg
`auto_pivot_triggers`. The relaxed variant then reached the holdout 5-phase
audit and FAIL'd the 4-gate matrix: holdout multi-phase mean αt below the
escalated 3.50 threshold, with at least one phase αt < 0 contributing to
the FAIL classification.

**Layer attribution:** Layer 1 screener (distance-to-default + liabilities
feature). Mechanism not validated on burnt SP1500 holdout. Class 1/1 closed.

**Documented:** memory `project_distress_credit_v1_FAIL_2026_05_04.md`,
design memo `docs/research/distress_credit_v1_design_2026_05_04.md`,
verdict `docs/research/distress_credit/phase_a_verdict_2026_05_04.md`,
ledger entry `distress_credit_v1_2026_05_04`.

### INCONCLUSIVE — v9D retrospective pre-2018, 2026-05-05

Retrospective replication of v9D options-implied-volatility scorer on
2009-2017 (pre-2018 OOS, fresh data unseen by prior options screeners).
Pooled αt = +2.45 across 3 sub-periods × 5 phases. Bonferroni n=27 naive
threshold |t|≥2.86; result fell short. Bounds-Andrews-Manski |t|_lower
= +2.15 (excludes 0).

**Verdict: INCONCLUSIVE.** The options_implied class (v7 / v8 / v9 / v9D)
triangulated to a ~+2.2-2.45 αt ceiling that fails to clear the strict
Bonferroni bar but consistently excludes zero. Saved as a feedback signal
on the upper bound of the class rather than a deployable strategy.

**Documented:** memory `project_v9d_retrospective_pre_2018_INCONCLUSIVE_2026_05_05.md`,
verdict JSON `docs/research/v9d_retrospective_pre_2018_verdict.json`,
postmortem `docs/research/v9d_retrospective_pre_2018_postmortem_2026_05_05.md`.

### INCONCLUSIVE — pc_abnormal_volume retrospective pre-2018, 2026-05-05

Retrospective replication of put/call abnormal-volume scorer on the same
2009-2017 fresh-data window. Pooled αt = +2.65 across 3 sub-periods × 5
phases. Bounds-Andrews-Manski |t|_lower = +1.98.

**Verdict: INCONCLUSIVE.** αt fell in the [1.0, 2.85) ambiguous zone
with a positive bounds-lower CI. Project response: paper-trade
activated for 12-month forward observation rather than capital deploy.
Validated the methodology bundle's "INCONCLUSIVE → forward observation,
not deploy" handling.

**Forward-observation close-out (2026-07-07, issue #769):** the
scorer-level paper-trade observation was retired together with the
paper-trade chain ([ADR 0012](../adr/0012-decommission-paper-trading-and-broker-chain.md));
no scorer-level replay replaces it. The options-implied CLASS is observed
forward instead via the candidate options telemetry (PR #772,
`docs/research/options_telemetry_design_2026_07_07.md`) — a different
universe (catalyst-conditioned mid-caps, not the cross-sectional top
decile) and construction, so it is a class-level partial substitute, not
a continuation of this scorer's observation. The same applies to the v9D
entry above.

**Documented:** memory `project_pc_abnormal_retrospective_INCONCLUSIVE_2026_05_05.md`,
verdict JSON `docs/research/pc_abnormal_retrospective_pre_2018_verdict.json`.

### PASS_MARGINAL — insider_form4_opportunistic v2, 2026-05-09

Cohen-Malloy-Pomorski 2012 opportunistic-insider Form-4 net-buy magnitude
on R2000 PIT 2018-2023. Pooled αt = +2.71 across 5 phase offsets.

The v1 ledger entry (`insider_form4_opportunistic_2026_05_05`) aborted
mid-flight 2026-05-08 — 38 minutes into a 5-phase parallel run a
mid-flight zen+agent code review caught a units mismatch between the
pre-reg `hac_maxlags=126` (intended for daily n~1500 obs per the
rationale text) and the experiment script feeding rebalance-cadence
(~72 obs) returns to `run_regression`. statsmodels HAC silently
inflates t-stats ~3x when `maxlags > n_obs` because the Bartlett kernel
weights `1 - j/(L+1)` stay near 1 for all valid lags `j < n`. v1 was
closed `execution_aborted_units_mismatch`; v2 (`insider_form4_opportunistic_2026_05_08_v2`)
re-spec'd with daily-cadence regression input + synchronous-across-phases
block bootstrap.

**Verdict: PASS_MARGINAL.** Pooled αt = +2.71 missed the Bonferroni n=28
threshold |t|≥3.12 by ~0.4σ, but G2-G5 cleared with extraordinary margin
(per-phase αt range 0.45, excess_net dispersion 1.3pp vs 70pp gate,
block-bootstrap CI [+1.54, +4.20] strongly excludes zero). Capital
deploy stays OFF-TABLE per pre-reg clause; Layer 4 overlay test
eligibility unlocked.

**Final lock 2024-2026 confirmation (also 2026-05-09):** the same scorer
re-evaluated on a fresh independent OOS window (2024-01-01 → 2026-03-31,
per zen-endorsed truncation from pre-reg literal 2026-04-30 due to PIT
universe coverage) returned PASS_MARGINAL with pooled αt=+2.69 (nearly
identical to OOS 2018-2023's +2.71) and STRONGER economic edge: excess_net
+24.4%/y and Sharpe net 1.14-1.38 per phase (vs Phase B's +17.7%/y and
0.82-0.89). Both windows miss G1 strict Bonferroni by similar margin
(~0.4σ); both clear G2-G5 with margin. Cohen-Malloy mechanism appears
genuinely robust on R2000 PIT post-publication, with no decay observed
on fresher data.

**Documented in detail at the dedicated postmortem
[`insider_form4_opportunistic_phase_b_postmortem_2026_05_09.md`](insider_form4_opportunistic_phase_b_postmortem_2026_05_09.md)
(includes final lock 2024-2026 section).**
Memory `project_insider_form4_opportunistic_locked_2026_05_05.md`.
Methodology lessons saved as feedback memories
`feedback_hac_maxlags_units_silent_inflation.md` +
`feedback_synchronous_block_bootstrap_pooled_phases.md`.

### Pattern 13: Strict gating + pre-registration function as designed — aggressively rejecting noise while permitting statistically distinct / orthogonal signals

The 11 failures + 2 INCONCLUSIVES validate the pipeline's noise-rejection
design. Bonferroni n=27→28 naive thresholds (|t|≥2.86→3.12), the every-
phase αt floor (≥1.5), the excess_net dispersion gate (≤70pp), and the
block-bootstrap CI gates were all enforced literally. None was waived
to "let through" a marginal candidate. Failures 1-10 hit the
Bonferroni bar, Failure 8 (regime-gate rescue) was falsified by
coverage diagnostics before any backtest ran, Failure 11 (distress-
credit) FAIL'd the multi-phase escalated threshold, and the two
INCONCLUSIVE retrospectives correctly placed v9D + pc_abnormal in
"forward-observation only" rather than "deploy."

The first phase-robust positive (Cohen-Malloy opportunistic Form-4)
passed 4 of 5 gates with extraordinary margin: per-phase αt range
0.45 across 5 offsets, excess_net dispersion 1.3pp against a 70pp
gate (54x undershoot), and a block-bootstrap CI [+1.54, +4.20] that
firmly excludes zero. Only the strict Bonferroni G1 was missed by
~0.4σ, classifying it as PASS_MARGINAL rather than full PASS. The
pipeline didn't reward endurance — it admitted a signal whose
orthogonality (informed-trader flow class) survives multi-phase
scrutiny under the same gates that killed every prior candidate.

The mechanism behind the positive matters as much as the result:
informed-trader flow is on a different feature axis (insider
transactions, EDGAR-detected) from the technical / regime / overlay
classes that produced the 11 failures. The first positive showing up
in this class is exactly the orthogonality-permits-signal claim that
strict gating is supposed to be selective for.

**Methodology lessons captured live, NOT post-hoc:** the v1 → v2
re-spec on insider_form4_opportunistic was caught by mid-flight zen +
agent code review. The HAC-units bug would have produced a falsely
inflated αt that might have been classified PASS rather than
PASS_MARGINAL — instead it was caught before any results landed in
the canonical record. **Adversarial review pre-compute remains the
load-bearing rule** for protecting headline numbers from silent
statsmodels behavior.

## Continuation 2026-06-24 — PEAD v2 (paradigm #14)

> Note: the per-paradigm narrative paused after 2026-05-09. The canonical
> record for the intervening paradigm-class results (#12 insider_pc_compound
> FAIL, #13 ev_fcff_yield FAIL-below-bar, #15 idiosyncratic_momentum FAIL)
> lives in the `/experiments` ledger (`apps/web/src/lib/data/research-ledger.ts`)
> and `docs/research/preregistration/ledger.json`, each with its own design
> memo. This section closes out #14, the most recent full holdout audit.

### Failure 12 — PEAD v2 (post-earnings drift), 2026-06-24

Canonical post-earnings-announcement-drift on the S&P 500: rank firms
cross-sectionally by price-scaled earnings surprise `PSS = (reportedEPS −
estimatedEPS) / close(t−1)`, go long-only the top quintile, hold 20 trading
days, α2 sub-leveraged weight `1/N_FIXED` (N_FIXED=150 via Little's Law),
invested-days-only Carhart-4F with Newey-West HAC maxlags=20. Pre-registered
as `pead_v5_pss_2026_05_13` (class `event_drift_search_2026_05_03`); design
memo `docs/research/paradigm14_pead_v2_design_2026_05_13.md`.

**The audit machinery was hardened before the run, not after.** Four launch
gates + an in-code doctrine verdict landed first (PRs #660/#661/#664): the
invested-fraction ≥ 0.40 guard, the AV `reportTime` spot-check (5/5 anchors
agree, 0 dangerous against the real cache), the all-days companion αt +
bootstrap-CI diagnostics, and — the dominant fix — `alphalens audit-verdict`,
which enforces the 3.5 / 2.5 / per-phase>0 / net-15bps≥2.0 / AV-PIT bars **in
executable code**. The harness's own `robust_verdict` only checks offset-phase
stability at αt≥1.5; without the doctrine gate, a methodology-inflated marginal
t could have been mistaken for a PASS. The three v3 spec questions were
adjudicated NO-v3 (Bonferroni stays n=3) before launch (memo §18).

**Verdict: FAIL — decisive, every doctrine gate except AV-PIT.** Ran the full
4-window audit (full 2018-2026.04 / IS 2018-2020 / OOS 2021-2023 / FL
2024-2026.04, each × the {0,5,10,15,25}bps cost grid) on a runpod cpu3c pod
(EUR-IS-1, ~$0.08, ~20 min wall) 2026-06-24.

| Gate | Bar | Result |
|---|---|---|
| G1 joint | full-sample net αt ≥ 3.5 | **0.15** — FAIL |
| G2 phase-mean | mean net αt ≥ 2.5 | **0.07** — FAIL |
| G3 per-phase | net αt > 0 each phase | IS +0.00 / OOS +0.44 / **FL −0.23** — FAIL |
| G4 cost-stress | net-15bps αt ≥ 2.0 | <0 every window — FAIL |
| G5 AV-PIT | ≥4/5 anchors | PASS_5_5 — the only PASS |

Excess net was economically positive (+3.6% / +5.0% / +6.4% IS/OOS/FL) but the
factor-adjusted t-stat is indistinguishable from zero — the returns are
beta/size loadings, not alpha. This is exactly what the modern literature
predicts: **large-cap PEAD has been effectively arbitraged away since the
mid-2000s** (perplexity adversarial review pre-compute flagged FAIL as the
expected outcome). Class `event_drift_search_2026_05_03` closes (v3/v4 were
ABANDONED on breadth-collapse; v5 is the first in-class to clear breadth and
run a full holdout → FAIL). Artifacts:
`~/.alphalens/audit/pead_v2_runpod_2026_06_24/{pead_{full,is,oos,fl},pead_doctrine_verdict}.json`;
ledger `pead_v5_pss_2026_05_13.outcome`.

**Mid-run bug, caught and fixed before the verdict landed.** The full/FL
windows initially crashed: an event entered within 20 trading days of the
factor-data tail (FF factors end 2026-02-27) cannot complete its hold, and the
pre-reg `compute_exit_day` raises rather than truncating. The 2018-Q1 preaudit
smoke never reaches the tail, so it slipped through. Fixed with
`_drop_uncompletable_tail_events` in the driver glue (the pinned scorer
untouched) — right-censoring not-yet-observable drift at the data tail (PR
#666, TDD + zen-clean). Same lesson as the HAC-units catch on
insider_form4_opportunistic: **the data tail and the orchestrator boundary are
where un-smoked glue bugs hide.**

### Pattern 14: Economic edge without statistical edge is still a FAIL

PEAD v2 joins #13 (ev_fcff_yield) and #15 (idiosyncratic_momentum) as a
mechanism that produces a positive, plausible *economic* return (+3-6%/y excess
net here) while the factor-adjusted t-stat sits far below the bar. The doctrine
3.5 exists precisely to reject this class: a textbook anomaly whose published
edge has decayed into the factor soup. The in-code doctrine verdict (new this
paradigm) makes the rejection mechanical — the headline number can no longer be
talked up by reporting gross α, a single lucky phase, or the harness's lenient
1.5 stability check. **Build the kill-switch into the code before the run, so
the verdict is computed, not argued.**
