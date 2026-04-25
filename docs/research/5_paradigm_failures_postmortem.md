# Five Paradigm Failures — AlphaLens Active Alpha Postmortem

**Author:** Solo retail quant, Polish tax resident on XTB
**Period covered:** 2026-04-18 → 2026-04-25 (8 days, 5 strategies)
**Status:** Project pivoted from "active alpha generation" to "research/learning infrastructure" 2026-04-25
**Audience:** Future-self when re-evaluating active strategies; potentially other retail quants who stumble into similar territory

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
- `alphalens/rotation/overlay_engine.py` — overlay/tilt strategies
- `alphalens/backtest/factor_analysis.py` — Carhart-4F, FF5+UMD, Q4 attribution with Newey-West HAC
- `alphalens/backtest/multiple_testing.py` — Bonferroni + BH-FDR + t→p conversion
- `alphalens/backtest/sharpe.py` — autocorr-adjusted Sharpe (Lo 2002)

### Sanity check framework (4 gates pattern)
- `alphalens/rotation/sanity_checks.py` — passive_correlation, rolling_sharpe_stability, per_regime_vs_passive, overlay_alpha
- Reusable for ANY future strategy before commitment to OOS

### Pre-commit discipline
- `alphalens/rotation/precommit.py` — config fingerprinting + git SHA tracking
- `alphalens/rotation/config.py` — `ConfigFingerprint` pattern (file SHA-256 + git SHA)
- `alphalens/guru/prompt.py` — prompt fingerprinting (same pattern, applied to LLM prompt files)

### Data clients (production-grade, rate-limit-aware)
- `alphalens/alt_data/sec_edgar_client.py` — SEC EDGAR submissions, Form 4, fundamentals
- `alphalens/macro/fred_client.py` — FRED time series with disk cache
- `alphalens/guru/polygon_fundamentals.py` — Polygon Stocks Starter PIT-correct financials (income/balance/cashflow)
- `alphalens/screeners/lean/polygon_client.py` — Polygon market data (OHLCV, ticker reference)

### LLM infrastructure
- `alphalens/guru/llm_scorer.py` — disk-cached GuruScorer with structured JSON output, cost tracking
- `alphalens/runner.py` — TradingAgentsRunner (per-stock multi-agent analysis)
- `alphalens/config_gemini.py` — Gemini config builder
- TradingAgents framework (vendored) — multi-agent graph for deep per-stock analysis

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
