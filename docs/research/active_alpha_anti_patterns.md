# Active Alpha Anti-Patterns — Retail Quant Catalog

**Author:** Solo retail quant, Polish tax resident on XTB
**Last updated:** 2026-04-25
**Status:** Living document; append new patterns as future research uncovers them
**Companion to:** `docs/research/paradigm_failures_postmortem.md`

## Why this exists

The postmortem documents *what failed* in five specific strategies. This catalog extracts the *generalizable warnings* — patterns that recur across strategies and would have caught failures earlier if they had been on a checklist before each pilot.

**Use case:** Before committing infrastructure work to a new strategy idea, screen it against every anti-pattern below. Each "yes, my strategy might be vulnerable to this" is a precommit task: define the test that would falsify the strategy under that pattern, and the kill threshold. If you can't define a test, that vulnerability is unbounded — fix the design or drop the idea.

**This is a *retail* anti-pattern catalog.** Some entries (cost model unrealism, alt-data crowding) are far more punishing for retail than for institutional players with infrastructure to mitigate them.

---

## AP-1: IS Carhart t-stat degrades 50-70% out of sample

**Symptom:** In-sample Carhart-4F α t-stat between 1.5 and 3.0 looks "promising". Strategy passes paper-trade backtest with reasonable Sharpe.

**Why deceiving:** Carhart attribution corrects for known factor exposures (mkt, SMB, HML, UMD), so a positive α t-stat in IS *feels* like real edge. But IS includes whatever idiosyncratic noise the scorer was inadvertently fit to. OOS removes that fit.

**Detection:** Run true OOS window (data the scorer/parameters never touched, including hyperparameter tuning). Compute Carhart α t-stat on the OOS window separately. Compare ratio.

**Mitigation:**
- Target IS Carhart α t > 3.0 if you want OOS t > 1.5 after expected 50-70% degradation.
- Pre-commit OOS window before any IS work — write `oos_start_date` to a config file, fingerprint it, never edit it.
- Walk-forward validation as default, not single split.

**AlphaLens evidence:**
- Layer 2b: IS t=2.62 → OOS t=0.82 (-69%)
- Layer 2d: IS t=2.14 → OOS t=0.68 (-68%)
- Layer 2e: IS t=1.96 → OOS t=0.33 (-83%)

---

## AP-2: R² approaching 1.0 vs benchmark = signal dead, not alpha

**Symptom:** Strategy daily returns correlate >0.95 with a passive benchmark (SPY, 60/40, target sector ETF). Backtest still shows positive α relative to that benchmark.

**Why deceiving:** Standard regression-based α decomposition can produce statistically positive intercept even when the strategy is mathematically dominated by passive exposure. The t-stat looks legitimate; the signal is noise around a passive trajectory.

**Detection:** Compute R² (or correlation²) of strategy daily returns vs passive benchmark. If R² > 0.95, the "active" component is too small to matter after costs. This is a *design-stage* check — runs on synthetic returns, doesn't need production data.

**Mitigation:**
- Sanity check before OOS: `passive_correlation < 0.95`. If failing, redesign the strategy (larger tilts, distinct universe, different rebalance frequency) — don't run the OOS test until this passes.
- For overlay/tilt strategies: `tilt_magnitude / total_volatility` ratio. If <0.10, you're moving on noise.

**AlphaLens evidence:**
- Layer 2e: R²=0.999 vs passive 60/30/10. Tilt magnitude ±25 bps vs daily ETF std ~100 bps = mathematically dominated.
- Layer 2g: correlation +0.97 vs SPY (LLM-researcher GuruAgent).

**Pattern note:** Catch this at *design* stage by computing expected tilt magnitude vs benchmark daily volatility. Layer 2e was caught only after IS run — that's late.

---

## AP-3: Universe concentration ratio P/N > 10% inflates overfit

**Symptom:** Universe size N modest (~30-150), top-N picks P relatively large (10-20). Ratio P/N > 0.10. Scorer scores the universe; backtest picks top P.

**Why deceiving:** With small N, sampling variance of a few "lucky" tickers dominates the signal mean. The scorer can be perfectly random and still produce attractive Sharpe purely from variance.

**Detection:**
- Compute P/N. Above 0.10 = vulnerable. Above 0.30 = catastrophic.
- Bootstrap-CI on Sharpe — if 5th percentile is below zero with random scorer baseline, your "edge" is sampling variance.

**Mitigation:**
- Larger universe (R2000+, full S&P 500, full Polygon listed) wherever the strategy permits.
- Lower top-N relative to universe.
- Always include a *random scorer* benchmark in the backtest harness as null hypothesis. If your scorer's α t-stat doesn't dominate the random scorer's bootstrap CI, you have no edge.

**AlphaLens evidence:**
- Layer 2b: 113 tickers / 15 picks = 13% concentration.
- Layer 2g: 30 tickers / 10 picks = 33% concentration. Even more vulnerable; 4-year mean +82 bps within bootstrap CI of random scorer.

---

## AP-4: Outlier-dominated raw mean deceives on heavy-tailed signals

**Symptom:** Raw mean of a CAR/return distribution looks attractive. Median is much smaller (or negative). Std dev is large. Distribution has a long right tail (M&A spikes, breakout winners, takeout premia).

**Why deceiving:** A handful of outsized positive events drag the mean above the kill threshold. The *typical* outcome is neutral or negative. You'd need to reliably catch the outliers — but the strategy doesn't have an edge in identifying them ex-ante.

**Detection:**
- Always report median + winsorized mean (5/95th percentile) + t-stat alongside raw mean.
- Inspect histogram or quantile-quantile plot before trusting any single summary statistic.
- For events: report distribution by bucket (Item 1.01 vs 5.02 vs 8.01) — pooled means hide structural negativity.

**Mitigation:**
- For event-driven strategies: kill threshold on median CAR, not mean.
- For factor strategies: report Sharpe with autocorrelation adjustment (Lo 2002) and bootstrap CI.

**AlphaLens evidence:**
- Layer 2f 8-K: Item 5.03 raw mean +606 bps → winsorized still +602 bps but with std 5783 across n=36 — single M&A spike contaminates. Items 1.01, 5.02, 8.01, 9.01 all had median CAR -100 to -250 bps despite occasionally positive raw means.

---

## AP-5: Multiple-testing correction counts only final variants, not config-changing commits

**Symptom:** You ran 5 hyperparameter sweeps and 3 universe variants and 4 weighting schemes, ended up with one configuration with t=2.5, applied Bonferroni assuming n=2 (the two most-recent variants you remember).

**Why deceiving:** Every commit that changed scorer logic, threshold, universe, weighting, or rebalance cadence was a separate hypothesis test. Forgetting them doesn't reduce the multiple-testing burden.

**Detection:**
- Count `git log --oneline | grep -iE "(scorer|threshold|universe|weight|rebalance)"` on the strategy branch.
- Count YAML config fingerprint changes (use `alphalens/archive/rotation/precommit.py` `ConfigFingerprint` pattern).
- True `n` is closer to 15-30 than to 2-3 for any meaningful research program.

**Mitigation:**
- Pre-commit YAML fingerprint + git SHA before any backtest. `n_tests++` on every config change.
- BH-FDR (Benjamini-Hochberg) instead of Bonferroni when `n` is large — less conservative but still principled.
- Lock `n_tests` budget BEFORE starting research. "I will run at most 10 distinct configurations; if none pass at FDR 5%, kill."

**AlphaLens evidence:**
- Layer 2b: ~26 hypothesis tests across the research program; Bonferroni applied n=2; α t=2.62 IS doesn't survive FDR-25 correction. (`docs/research/multiple_testing_audit_2026-04.md`)

---

## AP-6: Value-style screens structurally underperform in 2020s+ growth-bull regimes

**Symptom:** A Buffett/Graham-style screen (low P/E, high ROIC, conservative balance sheet, dividend stability) avoids speculative growth names by design.

**Why deceiving:** Strategy looks defensively-positioned. In bear/flat years it preserves capital. Aggregate "looks fine".

**Why it fails in current regime:** Mega-cap tech (NVDA + MSFT + GOOG + META + AMZN + AAPL) is >25% of SPY market cap. "Avoiding speculative growth" = "missing the index return" in growth-bull years. The drag is structural, not cyclical.

**Detection:**
- Test across distinct regime years, not just aggregate. Specifically include 2020 (COVID growth bull) and 2024 (AI growth bull).
- Compute per-year vs SPY outperformance. If two of last five years are <-3pp underperformance, the structural drag is binding.
- Decomposition: how much of SPY return came from top-10 names? If >50%, your value screen needs a non-trivial top-10 allocation to compete.

**Mitigation:**
- Don't pursue value-only screens for retail in current regime without explicit *style-rotation* logic that admits growth in growth-favorable conditions.
- If you must, require min-year underperformance tolerance ≥ -5pp AND mean outperformance ≥ +200 bps as joint kill conditions.

**AlphaLens evidence:**
- Layer 2g GuruAgent Buffett: 2/4 years underperform (2020 -3.56pp, 2024 -5.43pp). Both growth bulls. Mean +82 bps. Verdict: KILL on min-year tolerance + mean threshold + correlation gate.

---

## AP-7: Flat bps cost model is ~100× optimistic for retail microcap daily-rebalance

**Symptom:** Backtest uses constant `transaction_cost_bps=10` or `50` regardless of ticker, position size, or rebalance frequency. Strategy looks profitable.

**Why deceiving:** Flat bps abstracts away the three multiplicative cost drivers — spread, turnover, and frequency — which interact non-linearly for small caps and high-frequency rebalance.

**Realistic cost decomposition:**
- Spread: 50-200 bps for typical microcap (<$500M cap), wider on illiquid days.
- Turnover: 100% per rebalance for top-N strategies (everything sold/replaced each cycle).
- Frequency: daily = 252× annual, weekly = 52×, monthly = 12×.
- Realistic round-trip cost = spread × 2 (in+out) × turnover × frequency.

For daily-rebalance microcap: 100 bps spread × 2 × 1.0 turnover × 252 = ~50,400 bps = ~500% annual drag. Even at 50 bps spread: ~250% annual drag. **No retail microcap daily-rebalance strategy survives realistic costs.**

**Detection:**
- Compute realistic cost as spread × 2 × turnover × frequency. Compare to backtest assumption.
- Real per-ticker spread from Polygon last-trade vs NBBO snapshot, OR estimate via Roll's effective spread on OHLCV.

**Mitigation:**
- Per-ticker cost model. `alphalens/per_ticker_cost.py` infrastructure exists (RESEARCH_ONLY) but proves Δ Sharpe -3.33 over 5y for Layer 2b — the strategy doesn't survive its own cost model.
- Lower-frequency rebalance (monthly, quarterly) for any small-cap exposure.
- Larger-cap universe (S&P 500) where spreads are 1-5 bps not 50-200.

**AlphaLens evidence:**
- Layer 2b: `cost_model.py` flat 100 bps "moderate" = ~100× optimistic. Real daily-rebalance microcap drag erases all signal. Per-ticker cost validation showed Δ Sharpe -3.33 (5y).

---

## AP-8: Universe survivorship bias from retrospectively-curated tickers

**Symptom:** YAML universe (or basket file) edited to add tickers that "would have been included if we'd known". E.g., adding NVDA to a 2018 AI-theme list because it's an AI play *now*.

**Why deceiving:** The retrospective additions are precisely the names that survived to be remembered. Their backtest contribution is positive by selection. PIT-correct universes show much weaker α.

**Detection:**
- Check git log of universe.yaml. Tickers added after `IS_END_DATE` for "completeness" are red flags.
- PIT-95 sub-universe test: drop tickers added in the last 5% of date range. If α t-stat drops materially (e.g., 1.70 → 1.36 in Layer 2b), you have selection contamination.

**Mitigation:**
- Universe construction code references only data available at `as_of_date`. Use Polygon's `?date=YYYY-MM-DD&active=*` listing endpoints.
- Include delisted tickers (`active=false`) — bankruptcies and M&A targets must appear in the universe with their actual fates, not be silently dropped.
- Universe YAML for thematic strategies: each ticker tagged with `added_date` and `theme`. Backtest filters by `added_date <= as_of_date`.

**AlphaLens evidence:**
- Layer 2b: 18 tickers added 2026-04-19 retrospectively contributed ~0.3 OOS α t. PIT-95 sub-universe drop OOS α t from 1.70 to 1.36 (`docs/research/pit_universe_backtest.md`).
- MVP2 survivorship probe: backfill delisted from `?active=false` flipped Sharpe 1.49 → 1.75 (bias direction was *opposite* of expected — selection toward winners, not survivors).

---

## AP-9: Crowded alt-data signal at public latency

**Symptom:** Strategy uses publicly-disclosed alt data (SEC Form 4 insider trades, 13F holdings, FAA aircraft registrations, ship AIS, satellite parking lot counts). The data is "alternative" but the disclosure is public.

**Why deceiving:** "Alt data" sounds proprietary. Often the disclosure is publicly accessible at low latency (Form 4 = SEC EDGAR within 2 days, 13F = quarterly within 45 days, satellite imagery via commercial provider). Hedge funds with $1B+ AUM run identical signals at HFT latency, eating the alpha before retail can act.

**Detection:**
- What is the latency between event and broker order at retail vs institutional? If institutional latency is <10s and retail is hours-to-days, the signal is crowded.
- Search literature for academic papers using the same signal. If 5+ papers exist (especially in *JF*, *RFS*, *JFE* post-2018), the signal is over-mined.

**Mitigation:**
- Don't pursue retail alpha on publicly-disclosed alt data without a structural retail advantage (longer holding period offsets HFT speed disadvantage; tax efficiency on losses; specific niche too small for institutional).
- Only viable if: (a) signal is hard to access mechanically, (b) holding period is long enough that latency doesn't matter, OR (c) the signal interpretation requires meaningful human/LLM judgment that hasn't been productized.

**AlphaLens evidence:**
- Layer 2d Form 4 cluster-buy: IS Carhart t=2.14 → OOS t=0.68. Crowded by HFT/quant funds — Form 4 is public on SEC EDGAR within 2 business days; institutional players ingest it in seconds.
- Quiver Congress L/S validation: α t=−2.14 HAC, robust to drop-top-50 filter. Signal so crowded it inverted.

---

## AP-10: 8-K event asymmetry — most filings are bad news

**Symptom:** Event-study backtest pools all 8-K filings, computes mean CAR, finds positive number, declares signal.

**Why deceiving:** 8-K is the SEC catch-all for material events. Positive announcements (earnings beats, dividend hikes, large customer wins) typically go through earnings calls or press releases or scheduled 10-Q. Negative material events (loss disclosure, default, agreement termination, exec departure under pressure, restatement) often show up *only* in 8-K. Pooled mean over Item types confuses M&A surprise spikes with structural negativity.

**Detection:**
- Decompose by Item type. Items 1.01 (Material Definitive Agreement), 5.02 (Executive Departure), 8.01 (Other Events), 9.01 (Financial Statements) carry distinct distributions.
- Report median + winsorized mean per Item, not pooled mean.

**Mitigation:**
- If pursuing 8-K: SHORT-side strategy (most are negative), or restrict to specific Items with empirical positive median CAR (none observed in 150-ticker S&P 500 sample).
- For *any* event-driven strategy: pre-commit median CAR threshold per event-type bucket, not pooled mean.

**AlphaLens evidence:**
- Layer 2f 8-K screen: All Items had winsorized mean CAR < 50 bps or negative. Items 1.01/5.02/8.01/9.01 median CAR -100 to -250 bps. KILL verdict in 1 day of analysis vs weeks of building infrastructure.

---

## AP-11: External LLM consultation as evidence (Perplexity, ChatGPT, Gemini chat)

**Symptom:** "Perplexity says retail can do X" or "Gemini ranked these strategies" treated as validation input to go/no-go decision.

**Why deceiving:** LLM consultations summarize structural priors from training data dressed as "current consensus". They reflect what's been published frequently — which is a noisy indicator of what works (publication bias toward strategies that worked once, in some specific window).

**Concrete observed pattern:** Perplexity successively retracted evidence base across two consultations during AlphaLens journey when pushed for OOS validation specifics. Initial recommendation strength weakened on follow-up.

**Detection:**
- Treat LLM as *directional opinion*, not *Bayesian update*. Don't tighten kill thresholds based on it.
- Run own pilot with own pre-committed thresholds before believing any external assessment.

**Mitigation:**
- LLM consultations OK for: idea generation, methodology critique, finding academic citations.
- LLM consultations NOT OK for: setting kill thresholds, validating OOS results, making capital-allocation decisions.

**AlphaLens evidence:**
- Layer 2e: Perplexity initially endorsed sector rotation paradigm; on follow-up critique introduced sanity-check requirements that exposed R²=0.999 trap.
- Layer 2g: Perplexity's CXO Advisory citation (46.9% accuracy ceiling, 2006-2025) ultimately the *most useful* output — but came as critique, not endorsement.

---

## AP-12: Build infrastructure before validating thesis

**Symptom:** Spending 2-3 weeks writing scorers, backtest harness, data pipelines, monitoring dashboards before testing whether the underlying signal has any edge.

**Why deceiving:** Building feels productive. Infrastructure that "would be needed if the strategy works" piles up. Sunk cost biases toward continuing even when pilot shows kill verdict.

**Mitigation: 1-day go/no-go screen FIRST.**
- 30-150 ticker sample, simplest possible signal computation, pooled metric per Item/bucket.
- Pre-committed kill thresholds in YAML.
- Run, evaluate, accept verdict. If KILL: drop the idea before any infrastructure.
- If go: THEN build infrastructure with confidence.

**AlphaLens evidence:**
- Layer 2f 8-K screen: 1 day to write the screen script. KILL verdict in 1 hour of analysis. Saved 2-3 weeks of building event-driven trading infrastructure on a thesis that didn't survive a basic CAR check.
- Inverse: Layers 2b/2c/2d each spent ~5 days building before discovering the OOS failure. Layer 2g pilot v2 partially used this principle (4-year regime test before infrastructure) — saved further weeks.

---

## AP-13: Bankruptcy/delisting sign flip in augmented universes

**Symptom:** Adding delisted tickers (M&A targets, bankruptcies) to backtest universe inverts the strategy's direction. Train α t-stat goes from +0.80 to -0.86 or similar.

**Why deceiving:** You'd expect including failures to reduce returns slightly (small downward shift). A *sign flip* means the scorer was systematically picking failures masked as winners by survivorship — biotech that "looked promising" by momentum/fundamentals features but went bankrupt.

**Detection:**
- Run two backtests: clean universe, augmented universe with `?active=false` historical listings backfilled.
- If α t-stat differs by >0.5 in magnitude, your scorer is sensitive to selection — investigate which sub-population dominates.

**Mitigation:**
- Always backtest on PIT-correct, delisting-inclusive universe.
- For thematic strategies: explicitly handle delisting (bankruptcy = -100% from delisting date, M&A target = absorbed at announcement price).

**AlphaLens evidence:**
- Layer 2b augmented universe: train α t flipped +0.80 → -0.86. Scorer picked biotech failures (`docs/research/pipeline_bias_repair_plan.md`).

---

## AP-14: Forecasting accuracy ceiling ~47% (CXO Advisory benchmark)

**Symptom:** Strategy banks on outperforming "average forecaster" through better tools (LLM, more data, faster signal). Implicit assumption: accuracy ceiling has room to grow with technology.

**Why deceiving:** CXO Advisory tracked 6,582 graded market forecasts from 2006-2020 across 68 forecasters. Average accuracy 46.9% — barely above coin flip. Has not improved in ~20 years despite massive advances in data, ML, compute. The ceiling is *market predictability*, not *analysis sophistication*.

**Detection:**
- Estimate the strategy's required accuracy to be profitable (after costs, after factor exposure adjustment). If >55%, you're betting against the structural ceiling.
- Compare to known benchmarks: Buffett's actual SPY-relative outperformance has been ~0% over last decade.

**Mitigation:**
- Don't pursue strategies whose theoretical edge depends on >55% directional accuracy on liquid US equities at retail scale.
- Tax/structural edges (TFSA/IKE/IKZE optimization, loss harvesting, qualified dividend rate gaming) have higher ceilings than directional accuracy.

**AlphaLens evidence:**
- Layer 2g GuruAgent: hit -82 bps mean over 4 years — within the structural ceiling band. LLMs can replicate Buffett-style analysis but cannot break the predictability ceiling.

---

## How to use this catalog for a new idea

Before committing infrastructure work to a strategy idea:

1. **Read every anti-pattern title above.** Mark each as: vulnerable / not vulnerable / unclear.

2. **For each "vulnerable" or "unclear":**
   - Define the test that would falsify the strategy under that pattern.
   - Define the kill threshold for that test (pre-committed in YAML, fingerprinted).
   - Estimate effort to run the test.

3. **Run the cheapest tests first.** Most anti-patterns can be checked with a minimal go/no-go screen (AP-12) before any infrastructure.

4. **If any test fires KILL: accept the verdict.** Don't iterate the design to "fix" it without resetting `n_tests` budget.

5. **Document outcomes regardless.** Even (especially) failures generate reusable evidence — that's the entire purpose of this catalog.

## Cross-reference

- **Postmortem narrative:** `docs/research/paradigm_failures_postmortem.md`
- **Per-layer detailed audits:** `docs/research/layer2{b,d}_*.md`, `docs/research/multiple_testing_audit_2026-04.md`, `docs/research/pipeline_bias_repair_plan.md`, `docs/research/pit_universe_backtest.md`
- **Reusable infrastructure:** `alphalens/backtest/{factor_analysis,multiple_testing,sharpe}.py`, `alphalens/archive/rotation/{sanity_checks,precommit,config}.py`, `alphalens/per_ticker_cost.py`
- **Methodology principles:** Postmortem §"Methodology principles" — applies to next idea

## Future additions

When next research run uncovers new failure modes, append as AP-N entries. Keep the format consistent: Symptom, Why deceiving, Detection, Mitigation, AlphaLens evidence (or external evidence if the future run is on a different system).
