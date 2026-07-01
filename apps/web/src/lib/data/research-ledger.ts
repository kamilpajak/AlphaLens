// Research ledger static data — paradigm experiments, live infrastructure,
// methodology artifacts, durable failure patterns, and the status legend.
//
// Extracted from `src/routes/experiments/+page.svelte` (the route component now
// imports these). Keeping the data here keeps the route a thin view layer and
// lets the audit script + future tests import the same source of truth.
//
// Source for paradigm content: docs/research/paradigm_failures_postmortem.md
// (mirrored under /docs/research/ for the Evidence drawer). Stories are
// hand-written plain-English narratives; structured fields preserve quant
// precision.
//
// WHEN CLOSING A NEW PARADIGM: append a row to `paradigms`, populate ALL
// fields including `story` (plain English) and `is_t`/`oos_t` (numeric,
// nullable), and add the evidence filename to
// scripts/sync-research-docs.mjs::REFERENCED. New acronyms should also get
// a row in the `GLOSSARY` array in `$lib/data/glossary`.

export type ParadigmStatus = 'FAIL' | 'SLIPPAGE-FAIL' | 'IN-FLIGHT' | 'INCONCLUSIVE' | 'PASS_MARGINAL';
export type LiveStatus = 'LIVE' | 'SHIPPED' | 'DONE';

// Layer + two-axis taxonomy. Each paradigm header carries a
// "<layer> · <axis_a> / <axis_b>" tag with inline JargonTips; the tooltips
// (sourced from $lib/data/glossary) explain the taxonomy on hover, replacing
// the upfront architecture block.
// `axis_a` = structural (how the rule is built); `axis_b` = data sources
// the rule reads. Compound paradigms have ≥2 entries in axis_b.
// Layer-4 paradigms (overlay) use axis_a='overlay' and axis_b=null.
//
// KEEP IN SYNC with the AxisA / AxisB / layer_id type unions above and the
// glossary.ts entries. If a value is retired from the data, remove it from
// this list — otherwise the audit script's "unreferenced terms" check
// silently passes the dead entry (the Playwright auto-discovery DOM test
// would still catch a missing inline render, but the audit script alone
// would not).
// audit-tooltips:dynamic-terms L2 L4 screener combo compound gate overlay price fundamental insider options event-drift macro
// (Paradigm headers render <JargonTip {...tipProps(VAR)}> where VAR is the
// paradigm's layer_id / axis_a / axis_b. The audit script's literal-only
// regex can't see these dynamic uses, so the line above credits each term
// with one inline reference for the "unreferenced terms" check.)
export type AxisA = 'screener' | 'combo' | 'compound' | 'gate' | 'overlay';
export type AxisB = 'price' | 'fundamental' | 'insider' | 'options' | 'event-drift' | 'macro';

export type Paradigm = {
	id: string;
	display: string;
	name: string;
	layer_id: 'L2' | 'L4';
	axis_a: AxisA;
	axis_b: AxisB[] | null;
	status: ParadigmStatus;
	date: string;
	story: string;
	hypothesis: string;
	mechanism: string;
	metric: string;
	lesson: string;
	evidence: string | null;
	is_t: number | null;
	oos_t: number | null;
};

export type Live = {
	id: string;
	name: string;
	what: string;
	status: LiveStatus;
	deploy: string;
	date: string;
};

export type Artifact = {
	id: string;
	name: string;
	description: string;
	link: string;
	status: 'OSS' | 'INTERNAL';
};

export type Pattern = {
	n: string;
	name: string;
	body: string;
};

export type StatusDef = {
	status: ParadigmStatus;
	definition: string;
};

// `Glossary` type + GLOSSARY array extracted to `$lib/data/glossary` —
// shared with discovery tests and the audit script as single source of truth.

export const paradigms: Paradigm[] = [
	{
		id: 'P01', display: '#1', name: 'Layer 2b — small-cap themed momentum', layer_id: 'L2', axis_a: 'screener', axis_b: ['price'],
		status: 'FAIL', date: '2026-04-22',
		story: 'We picked stocks from a hand-curated list of 113 theme names (quantum / AI / biotech) by a 7-metric momentum score and bought the top 15 each day. The result looked statistically strong on 2017-2022 training data, but the signal collapsed on fresh 2023-2026 data. Two reasons: some stocks had been added to the universe after the fact (which inflated training results), and we did not correct for testing many strategy variants.',
		hypothesis: '7-metric momentum scorer on a 113-ticker curated theme universe (quantum / AI / biotech) produces phase-robust [αt] > 2 [OOS].',
		mechanism: 'top-15 daily rebalance, 60-day hold; [IS] 2017-2022 vs true [OOS] 2023-2026; linear weighting.',
		metric: '[IS] [Carhart 4F|Carhart] [αt] = 2.62 → [OOS] t = 0.82 (−69%).',
		lesson: 'universe survivorship bias (18 retrospectively-added tickers contributed ~0.3 [OOS] [αt]) + multiple-testing under-correction (true n≈26 vs [Bonferroni correction|Bonferroni] n=2 applied).',
		evidence: 'layer2b_audit_final.md',
		is_t: 2.62, oos_t: 0.82
	},
	{
		id: 'P02', display: '#2', name: 'Layer 2d — insider Form-4 cluster-buy', layer_id: 'L2', axis_a: 'screener', axis_b: ['insider'],
		status: 'FAIL', date: '2026-04-24',
		story: 'We bought small-cap stocks whenever at least 3 company insiders bought ≥5% notional of their holdings in the same window — a classic "cluster-buy" signal. Looked statistically borderline on training data; collapsed on fresh data. Most likely cause: the same Form-4 filings are visible to HFT and quant funds about 10 seconds after submission, so by the time a retail investor sees them, the alpha is already traded out.',
		hypothesis: '≥3 insiders × ≥5% notional cluster-buys on R2000 produce [αt] > 2 [OOS].',
		mechanism: 'weekly rebalance, top-15 by cluster-buy score, 60-day hold.',
		metric: '[IS] [Carhart 4F|Carhart] [αt] = 2.14 → [OOS] t = 0.68 (−68%).',
		lesson: 'Form-4 is publicly available at ~10s latency post-filing; strategy crowded by HFT/quant funds — alpha bled by the time retail can execute.',
		evidence: 'paradigm_failures_postmortem.md',
		is_t: 2.14, oos_t: 0.68
	},
	{
		id: 'P03', display: '#3', name: 'Layer 2e — tactical sector rotation', layer_id: 'L2', axis_a: 'gate', axis_b: ['macro'],
		status: 'FAIL', date: '2026-04-24',
		story: 'We tilted a passive 60/30/10 SPY/QQQ/IWM portfolio by up to ±10% based on 4 macro signals (yield curve, VIX, momentum). The training-period result looked decent until we checked correlation — the tilted portfolio was 99.9% identical to the passive benchmark. A ±25 basis-point tilt on a portfolio with ~100 bps daily moves is mathematically invisible. We should have spotted this at design time.',
		hypothesis: '4-rule macro overlay (yield curve × VIX × QQQ/IWM spread) on SPY/QQQ/IWM 60/30/10 core beats passive by [αt] > 1.5 [OOS].',
		mechanism: '±10% max tilt per ticker, quarterly rebalance; [IS] 2009-2020 vs [OOS] 2021-2026.',
		metric: '[OOS] α = 7.9 bps t = 0.33; passive correlation 1.000; min-252d rolling Sharpe −0.94.',
		lesson: 'R² ≈ 1.0 vs benchmark = signal mathematically dominated. Should have caught at design (±25 bps tilt vs ~100 bps daily std).',
		evidence: 'paradigm_failures_postmortem.md',
		is_t: 1.96, oos_t: 0.33
	},
	{
		id: 'P04', display: '#4', name: 'Layer 2f — 8-K event-driven go/no-go', layer_id: 'L2', axis_a: 'screener', axis_b: ['event-drift'],
		status: 'FAIL', date: '2026-04-25',
		story: 'Companies file an 8-K with the SEC whenever something material happens (executive changes, material agreements, big losses). We hoped specific 8-K Item types would predict short-term outperformance. A simple 1-day pilot killed the idea: almost every Item type produces negative average returns after filing. Why? Most material 8-K events are bad news — good news goes through earnings calls and press releases instead.',
		hypothesis: 'specific 8-K Item types (1.01, 5.02, 8.01) produce positive [CAR] at +1/+5/+20/+60d post-filing.',
		mechanism: '150 random S&P 500 tickers × 2022-2024; aggregate [CAR] by Item type.',
		metric: 'All Items [winsorize|winsorized] [CAR] < 50 bps (or negative); Items 1.01/5.02/8.01/9.01 median [CAR] −100 to −250 bps.',
		lesson: 'most 8-K filings are bad news. Asymmetry: positive announcements go through earnings / press releases. Always winsorize on heavy-tailed event distributions.',
		evidence: 'paradigm_failures_postmortem.md',
		is_t: null, oos_t: null
	},
	{
		id: 'P05', display: '#5', name: 'Layer 2g — GuruAgent Buffett-style LLM', layer_id: 'L2', axis_a: 'screener', axis_b: ['fundamental'],
		status: 'FAIL', date: '2026-04-25',
		story: 'We asked Gemini Pro to pick 10 stocks per year using Warren Buffett\'s value-investing criteria, and tested across 4 different market regimes (2018, 2020, 2022, 2024). The portfolio beat the S&P by less than 1% on average, with very high correlation (97%). The reason: value-style screening systematically avoids speculative growth stocks, but mega-cap tech (NVDA, MSFT, GOOG, META, AMZN) is now > 25% of the index. Avoiding speculative growth in the 2020s ≈ missing the index return.',
		hypothesis: 'Gemini 3.1 Pro with Polygon-backed Buffett-style prompt picks 10 stocks per year that beat SPY by >2pp mean and >−5pp min-year across 4 regimes.',
		mechanism: 'S&P 500 random 30 tickers/year × {2018, 2020, 2022, 2024}; 1-year equal-weight hold; pre-committed kill thresholds.',
		metric: 'mean +82 bps, min-year −5.43%, correlation +0.97 to SPY — KILL on all 3 gates.',
		lesson: 'value-style structurally fails in 2020s+ regime — avoiding speculative growth = missing the index when mega-cap tech > 25% of cap.',
		evidence: 'paradigm_failures_postmortem.md',
		is_t: null, oos_t: null
	},
	{
		id: 'P06', display: '#6', name: 'tri-factor (momentum × value × quality)', layer_id: 'L2', axis_a: 'combo', axis_b: ['price', 'fundamental'],
		status: 'FAIL', date: '2026-04-29',
		story: 'We combined three classical factors (12-month momentum × price-to-book × return-on-equity) into one composite score. One particular slice of the training data showed a statistically borderline result. But running the same backtest with the rebalance schedule shifted by 1-4 days each time produced wildly different results — the "winning" slice was just one of five phase offsets. Average across all five was noise. This was the first empirical proof of "phase-aliasing" and triggered building the multi-phase audit framework.',
		hypothesis: 'top-decile composite of 12-1 momentum × P/B × ROE on R2000 [PIT] produces [αt] > 2.5 phase-robust.',
		mechanism: 'weekly rebalance, 5-15 bp cost, [IS] 2019-2022; [multi-phase audit] at stride=5.',
		metric: '[single-phase] [IS] [αt] = 2.24 (Phase 4) → multi-phase mean [αt] = +0.34, dispersion 28pp.',
		lesson: '[phase-aliasing]: strided multi-rebalance backtests have 5 [phase offset|phase offsets] — single-phase [αt] is sample-of-one. Phase-robust mean ± std is the honest summary.',
		evidence: 'tri_factor_multi_phase_verdict.md',
		is_t: 2.24, oos_t: 0.34
	},
	{
		id: 'P07', display: '#7', name: 'momentum × low-volatility combo', layer_id: 'L2', axis_a: 'combo', axis_b: ['price'],
		status: 'FAIL', date: '2026-04-29',
		story: 'A two-factor combo (momentum × inverse-volatility) was the first hypothesis tested under the newly-built pre-registration framework. Single-phase IS would have called it a survivor (one phase showed positive signal); the multi-phase audit killed it with a mean of +0.49 and 44.5pp dispersion across phases. The pre-registration framework worked exactly as designed — it caught a false positive that single-phase reporting would have missed.',
		hypothesis: 'top-decile composite of 12-1 momentum × inverse-vol on R2000 [PIT] produces [αt] > 2.5 phase-robust.',
		mechanism: 'weekly rebalance, same cost model as P06; FIRST real test of [pre-registration ledger].',
		metric: '[multi-phase audit|multi-phase] mean [OOS] [αt] = +0.49, dispersion 44.5pp.',
		lesson: 'class `price_factor_search_2026_04_29` ends 3/3 FAIL. Scorer reused later as Layer 4 vol-target base.',
		evidence: 'strategy_validation_playbook.md',
		is_t: null, oos_t: 0.49
	},
	{
		id: 'P08', display: '#8', name: 'regime-gate rescue (mom+lowvol)', layer_id: 'L2', axis_a: 'gate', axis_b: ['macro'],
		status: 'FAIL', date: '2026-04-29',
		story: 'After mom+lowvol failed, we tried to rescue it by switching the strategy off during "bad regimes" identified by 5 macro classifiers (yield curve, VIX, credit spreads, etc.). Before running any backtest, we measured how much of the actual failure window each classifier covered. The flagship classifier (Perplexity\'s top recommendation, cross-sectional dispersion) covered only 4.5% of the failure window — incoherent with the failure mechanism. 30 minutes of coverage diagnostics saved 3-4 hours of theatrical backtesting.',
		hypothesis: '5 macro classifiers (yield-curve / VIX>20 / NFCI>+1 / HY-OAS>400bp / cross-sectional dispersion) gate the failure window of P07.',
		mechanism: 'Phase 1 [coverage diagnostic] on 1499 SPY days ([IS] 2017-2022) BEFORE backtesting any phase 2.',
		metric: 'C3 NFCI: 0.0% coverage. C6 (Perplexity primary): 4.5% with 2.5-day mean run (noise). Phase 2 cancelled.',
		lesson: 'coverage diagnostics falsify regime-gating hypotheses without burning Bonferroni budget. Measure OFF-coverage of the failure window FIRST.',
		evidence: 'regime_gate_phase1_diagnostic.md',
		is_t: null, oos_t: null
	},
	{
		id: 'P09', display: '#9', name: 'quality × momentum', layer_id: 'L2', axis_a: 'combo', axis_b: ['price', 'fundamental'],
		status: 'FAIL', date: '2026-04-30',
		story: 'We added a return-on-equity (quality) factor to momentum, hoping ROE would rescue the strategy through the 2017-2022 underperformance window. Result: dispersion across 5 rebalance phases was 167.8 percentage points — same signal, range from +112% to −56% depending only on which day we started rebalancing. One phase looked great (would have passed the publication threshold in single-phase reporting); average across all five was noise. The strongest empirical evidence yet for the phase-aliasing pattern.',
		hypothesis: 'z(mom_12_1) + z(roe_ttm) top-15 on R2000-PIT + EDGAR fundamentals produces [αt] > 2.5 phase-robust.',
		mechanism: 'weekly stride=5, ADV ≥ $5M, 5 bp cost; 4th hypothesis in class `price_factor_search`.',
		metric: '[multi-phase audit|multi-phase] mean [IS] [αt] = +0.58 (±0.69), [OOS] [αt] = +0.38 (±1.02), [OOS] dispersion 167.8pp.',
		lesson: 'a Phase-1 outlier [αt] = +2.00 is a sample-of-one artifact, not a survivor. Quality didn\'t rescue mom-and-vol — added 2017-2022 hole + post-2022 underperformance.',
		evidence: 'quality_momentum_multi_phase_audit.json',
		is_t: 0.58, oos_t: 0.38
	},
	{
		id: 'P10', display: '#10', name: 'vol-target overlay (Layer 4)', layer_id: 'L4', axis_a: 'overlay', axis_b: null,
		status: 'FAIL', date: '2026-04-30',
		story: 'Moreira & Muir 2017 published a famous result: scaling portfolio exposure inversely to recent volatility improves Sharpe on the aggregate market. We applied it to our mom+lowvol base. Result: zero alpha added (identical to the ungated base), AND the overlay AMPLIFIED the worst-phase loss from −44% to −78%. The mechanism is reactive — it uses past volatility to decide leverage, so when a regime shift hits with loss + volatility spike at the same time (common in event-driven small-cap regimes), the overlay levers UP before de-risking. M-M\'s positive result was on monthly aggregate-market data — it does not generalise to weekly small-cap factors.',
		hypothesis: 'Moreira-Muir vol-targeting (target_vol=0.10 ann, lookback=5w, max_lev=1.5) wrapping mom+lowvol base recovers Sharpe.',
		mechanism: 'dynamic per-rebalance cost (turnover × scale + |Δ scale|); 5-phase audit.',
		metric: 'mean [OOS] [αt] = +0.49 — identical to BASE. Phase 3: BASE excess_net −43.8% → overlay −77.9% (AMPLIFIED).',
		lesson: 'reactive vol-targeting using trailing window levers INTO concurrent regime shifts on small-cap weekly grain. M-M 2017 was monthly aggregate-market — does not generalise.',
		evidence: 'vol_target_overlay_multi_phase_audit.json',
		is_t: 0.37, oos_t: 0.49
	},
	{
		id: 'P11', display: '#11', name: 'distress_credit v1', layer_id: 'L2', axis_a: 'screener', axis_b: ['fundamental'],
		status: 'FAIL', date: '2026-05-04',
		story: 'We built a distress-credit screener using a KMV-style "distance to default" metric (how close a company\'s assets are to its liabilities, in standard deviations). The training data coverage was thin so we auto-pivoted to a relaxed variant (per the pre-registration trigger). The relaxed variant still failed the burnt-holdout audit on a SP1500 universe — at least one phase had negative αt and the multi-phase mean sat below the escalated threshold.',
		hypothesis: 'KMV distance-to-default + companyfacts liabilities on SP1500 [PIT] produces phase-robust [αt] > 2 on [burnt holdout].',
		mechanism: 'Phase A coverage gate auto-pivoted to relaxed variant; 5-phase holdout audit; 4-gate matrix.',
		metric: 'holdout [multi-phase audit|multi-phase] mean [αt] below escalated 3.50 threshold; ≥1 phase [αt]<0.',
		lesson: 'class 1/1 closed. Mechanism not validated on burnt SP1500. Auto-pivot trigger in pre-reg worked.',
		evidence: 'distress_credit/phase_a_verdict_2026_05_04.md',
		is_t: null, oos_t: null
	},
	{
		id: 'P12', display: '#12', name: 'insider_pc_compound', layer_id: 'L2', axis_a: 'compound', axis_b: ['insider', 'options'],
		status: 'FAIL', date: '2026-05-11',
		story: 'Our first attempt at combining two different data sources: opportunistic insider buys (Form-4) × abnormal put/call ratios (options flow). The hope was that two independently-positive signals would compound into a stronger combined signal that clears the strict 3.5 threshold. Reality: on fresh out-of-sample data the compound result was αt = −0.03 — worse than either component alone. Compounding two weak signals does not create a strong signal; it just adds Bonferroni cost. Six launch attempts on a 64GB pod to land the final audit (precheck data gap, stride-5 conflation, framework bugs, artifact collision, OOM at 16GB, success at 64GB).',
		hypothesis: 'cross-data compound: opportunistic insider buys × abnormal P/C ratio produces phase-robust [αt] > 3.5.',
		mechanism: 'R2000 [PIT] 2007-2026 with iVolatility SMD + Form-4 parquet intersection; 5-phase audit on 64GB pod (6 launch attempts).',
		metric: '[OOS] [αt] = −0.03 (G1 FAIL); [FL] [αt] = +0.67 + excess_net −0.28% (G3 FAIL). Both windows reject independently.',
		lesson: 'compound mechanism does not survive fresh [OOS] even when both component classes show positive evidence individually.',
		evidence: 'insider_pc_compound_oos_2026-05-11.json',
		is_t: null, oos_t: -0.03
	},
	{
		id: 'P13', display: '#13', name: 'ev_fcff_yield', layer_id: 'L2', axis_a: 'screener', axis_b: ['fundamental'],
		status: 'FAIL', date: '2026-05-13',
		story: 'Buying small-caps with the highest free-cash-flow yield (an academic-textbook value signal) produced positive returns on EVERY single phase across all three test windows (IS, OOS, final-lock) — 15 out of 15 phases positive, with an economic excess of 1-12% per year. But the statistical strength sat at αt ≈ 1.2 — well below the 3.5 threshold the project doctrine requires before deploying capital. The signal is real but small; deploying it is off-table by self-imposed discipline. As a side finding, we discovered an orchestrator bug where one of the cost-stress gates was a no-op duplicate of another.',
		hypothesis: '[FCFF] yield rank on R2000 [PIT] 2007-2026 produces phase-robust [αt] > 3.5.',
		mechanism: 'cpu3g-8-32 pod EU-RO-1, $0.33 spend, 10.7 min wall.',
		metric: '[αt] mean 1.25 / 1.34 / 0.96 ([IS]/[OOS]/[FL]), every-phase positive 15/15. Excess net +1.2% / +12.4% / +4.0%.',
		lesson: 'FCF-yield mechanism vindicated (every phase positive) but below Bonferroni 3.5. Class `fundamental_value_dcf_2026_05_12` remains OPEN. G4 cost-stress no-op duplicate.',
		evidence: 'ev_fcff_yield_audit_verdict_2026_05_12.md',
		is_t: 1.25, oos_t: 1.34
	},
	{
		id: 'P14', display: '#14', name: 'PEAD v2 (post-earnings drift)', layer_id: 'L2', axis_a: 'screener', axis_b: ['event-drift'],
		status: 'FAIL', date: '2026-06-24',
		story: 'Post-earnings announcement drift (PEAD) is a textbook anomaly — stocks that beat or miss earnings tend to keep drifting in that direction for weeks. We built the full version-2 machine (Alpha Vantage earnings feed, Little\'s Law position-size lock, daily-rebalance adapter, invested-days Carhart regression, plus a doctrine-verdict gate that enforces the 3.5 bar in code) and ran the real audit on a cloud pod across all four windows. Result: a clean, decisive FAIL — full-sample alpha t-stat 0.15 against a 3.5 bar, with the final-lock window actually negative. This is exactly what the literature predicts: large-cap PEAD has been effectively arbitraged away since the mid-2000s. Paradigm #14 is closed.',
		hypothesis: 'canonical post-announcement [PEAD] with PSS + [NW HAC] + invested-days regression on S&P 500 [PIT] clears [Bonferroni correction|Bonferroni] 3.5.',
		mechanism: 'runpod cpu3c pod EUR-IS-1, ~$0.08 spend, ~20 min wall; 4-window doctrine audit (full / [IS] / [OOS] / [FL] x 5-cost grid), `audit-verdict` applies the 3.5 / 2.5 / per-phase>0 / net-15bps / AV-PIT bars in code.',
		metric: 'doctrine FAIL — full-sample net [αt] 0.15 (G1<3.5); phase-mean 0.07 (G2<2.5); per-window net [αt] 0.00 / 0.44 / -0.23 ([IS]/[OOS]/[FL], G3 fails on negative [FL]); net-15bps <0 every window (G4). AV-PIT only PASS. Excess net +3.6% / +5.0% / +6.4%.',
		lesson: 'large-cap PEAD effectively dead since ~2006 — vindicates the literature, not the signal. All four launch gates + the in-code doctrine bar landed first, so a methodology-inflated marginal t could not be mistaken for a PASS. Class `event_drift_search_2026_05_03` closed.',
		evidence: 'paradigm14_pead_v2_design_2026_05_13.md',
		is_t: 0, oos_t: 0.44
	},
	{
		id: 'P15', display: '#15', name: 'idiosyncratic_momentum', layer_id: 'L2', axis_a: 'screener', axis_b: ['price'],
		status: 'FAIL', date: '2026-05-14',
		story: 'Standard momentum (last 12 months minus last 1) on stock returns; but we first stripped out the part of returns explained by the three Fama-French factors (market, size, value), leaving only the stock-specific "idiosyncratic" part. Result: statistically strengthens monotonically across the 3 test windows (IS 0.02 → OOS 0.71 → final-lock 1.58), opposite to the anticipated momentum-crisis penalty for 2023-2024. But the entire trajectory still sits below the 3.5 doctrine bar. The mechanism is partially vindicated; the whole price-factor research class is now dead at 5/5 FAILs.',
		hypothesis: 'idiosyncratic-momentum (residual-of-FF3 12-1) on S&P 1500 [PIT] produces phase-robust [αt] > 3.5.',
		mechanism: 'community CPU pod EU-RO-1, 30.4 min wall, $0.04 spend, n=5 [multi-phase audit|phases] per window.',
		metric: '[αt] mean 0.02 / 0.71 / 1.58 ([IS]/[OOS]/[FL]); β_market 0.97-1.17 (no [BAB] confound); turnover 19-36%/mo.',
		lesson: 'JOINT FAIL all three windows. Whole price_factor_search class dead. Pattern matches paradigm #13 — vindicated mechanism below the bar.',
		evidence: 'idiosyncratic_momentum_audit_verdict_2026_05_14.md',
		is_t: 0.02, oos_t: 0.71
	},
	{
		id: 'R01', display: 'R1', name: 'v9D options-implied retrospective', layer_id: 'L2', axis_a: 'screener', axis_b: ['options'],
		status: 'INCONCLUSIVE', date: '2026-05-05',
		story: 'A retrospective re-run of an old options-implied-volatility scorer on fresh 2009-2017 data that no version of the scorer had ever seen. The pooled t-statistic across 3 sub-periods × 5 phases landed at +2.45 — short of the strict multiple-testing threshold (2.86), but with a lower confidence-interval bound (+2.15) that excludes zero. Not strong enough to deploy capital; strong enough to start a 12-month paper-trade observation.',
		hypothesis: 'v9D options-implied-vol scorer replicated on 2009-2017 (pre-2018 [OOS], unseen) produces [αt] > 2.86 ([Bonferroni correction|Bonferroni] n=27 naive).',
		mechanism: '3 sub-periods × 5 phases pooled bootstrap.',
		metric: 'pooled [αt] = +2.45; Bounds-Andrews-Manski |t|_lower = +2.15 (excludes 0).',
		lesson: 'options_implied class triangulates to ~+2.2-2.45 [αt] ceiling. INCONCLUSIVE → 12mo paper-trade observation, not deploy.',
		evidence: 'v9d_retrospective_pre_2018_postmortem_2026_05_05.md',
		is_t: null, oos_t: 2.45
	},
	{
		id: 'R02', display: 'R2', name: 'P/C abnormal-volume retrospective', layer_id: 'L2', axis_a: 'screener', axis_b: ['options'],
		status: 'INCONCLUSIVE', date: '2026-05-05',
		story: 'Same setup as R01 but a different scorer — abnormal put/call option volume. Pooled t-statistic +2.65, again in the ambiguous band between zero and the strict threshold. The methodology bundle\'s "inconclusive → paper-trade for 12 months, no capital deploy" handling worked as designed.',
		hypothesis: 'put/call abnormal-volume scorer replicated on 2009-2017 produces [αt] > 2.86.',
		mechanism: 'same 3 sub-periods × 5 phases pooled bootstrap.',
		metric: 'pooled [αt] = +2.65; Bounds-Andrews-Manski |t|_lower = +1.98.',
		lesson: 'INCONCLUSIVE band. Methodology bundle\'s INCONCLUSIVE → forward-observation handling validated.',
		evidence: 'pc_abnormal_retrospective_pre_2018_verdict.json',
		is_t: null, oos_t: 2.65
	},
	{
		id: 'S01', display: 'S1', name: 'insider_form4_opportunistic (post-PASS_MARGINAL)', layer_id: 'L2', axis_a: 'screener', axis_b: ['insider'],
		status: 'SLIPPAGE-FAIL', date: '2026-05-12',
		story: 'The project\'s first phase-robust positive: a Cohen-Malloy "opportunistic" Form-4 insider scorer reached gross αt = +2.71 (PASS_MARGINAL) on both an OOS window and a fresh final-lock window. Then we modelled realistic trading friction — bid-ask spreads of about 50 basis points on the small-cap names involved — and the net signal collapsed to αt = +1.27 (OOS) and +1.95 (final-lock), both below the threshold. The strong-looking signal sat exactly in the high-spread corner of the small-cap universe, so it was a cost-mirage. Paper-trade suspended; capital deploy stays off-table.',
		hypothesis: 'Cohen-Malloy-Pomorski opportunistic [Form-4] on [R2000-PIT|R2000 PIT] 2018-2023 produces phase-robust [αt] > 2.0 net of realistic costs.',
		mechanism: 'pooled [αt] across 5 [phase offset|phase offsets]; final-lock confirmation on independent 2024-2026 window; slippage diagnostic at H=50bps median half-spread.',
		metric: 'gross [αt] = +2.71 ([PASS_MARGINAL]) → net [αt] = +1.27 [OOS] / +1.95 [FL] — G1 violated both windows.',
		lesson: 'cyclicality reversal MATERIAL FINDING: pre-cost counter-cyclical mechanism was cost-mirage. R2000 long-only counter-cyclical [Q5] alpha MUST run slippage diagnostic BEFORE Layer 4 design.',
		evidence: 'insider_form4_opportunistic_slippage_stress_postmortem_2026_05_12.md',
		is_t: 2.71, oos_t: 1.27
	}
];

export const live: Live[] = [
	{ id: 'L1', name: 'EDGAR watchdog', what: 'detects S&P 100 EDGAR filings → classifies → writes candidates.db. Worker archived per ADR 0008.', status: 'LIVE', deploy: 'systemd `detect` every 15 min', date: 'continuous' },
	{ id: 'L2', name: 'Literature review', what: 'weekly RSS scan (arXiv quant-fin + selected blogs) + monthly Perplexity deep scan with reasoning effort = high.', status: 'LIVE', deploy: 'systemd weekly + monthly timers', date: 'continuous' },
	{ id: 'L3', name: 'Thematic tool MVP (Phase A–E)', what: 'news ingest → Flash extraction → Pro theme→beneficiary mapping → 4 verification gates → Layer 4 quant screen → Layer 5 brief generator. NVDA→QUBT replay end-to-end green.', status: 'SHIPPED', deploy: 'PRs #128–#152 merged', date: '2026-05-17' },
	{ id: 'L4', name: 'VPS backfills', what: 'Form-4 cross-shard merge DONE (37 MB tar.zst, 2.66M rows). AV EARNINGS daily systemd timer LIVE — ~21d backfill window at free-tier 25/day quota.', status: 'LIVE', deploy: 'jacoren@ VPS systemd-user units', date: '2026-05-08 / running' }
];

export const artifacts: Artifact[] = [
	{ id: 'A1', name: 'phase-robust-backtesting', description: 'methodology bundle: preregistration ledger + multi-phase audit + Bonferroni + audit_multi_phase driver. Extracted per ADR 0006 after the methodology proved more durable than any individual paradigm.', link: 'kamilpajak/phase-robust-backtesting v0.2.3+', status: 'OSS' },
	{ id: 'A2', name: 'ADRs 0001–0008', description: '8 architectural decisions: pivot to research infrastructure, queue contract, screener-agnostic backtest, vendored upstream (superseded), closed-layers anti-pattern policy, OSS extraction, 5-layer architecture, sunset TradingAgents.', link: 'docs/adr/', status: 'INTERNAL' },
	{ id: 'A3', name: 'Layer architecture (ADR 0007)', description: '5-layer separation: screener → selection-gate → engine → risk-overlay → attribution. Paradigm #10 vol-target overlay isolated to Layer 4 without re-litigating screener.', link: 'docs/adr/0007-layer-architecture.md', status: 'INTERNAL' },
	{ id: 'A4', name: 'Closed-layers anti-pattern catalog (ADR 0005)', description: '`alphalens/archive/` namespace keeps failed paradigms as reusable framework + anti-pattern reference. Each closed `__init__.py` carries `__closed_date__`, `__closed_reason__`, `__closed_evidence__`.', link: 'docs/adr/0005-closed-layers-as-anti-pattern-catalog.md', status: 'INTERNAL' }
];

// Pattern body/name uses markup syntax: [term] or [term|label] wraps a
// glossary term inline. The term must match an entry in `glossary` above
// (lookup via `tipProps`); label optionally overrides the visible text.
export const patterns: Pattern[] = [
	{ n: '01', name: '[IS]→[OOS] [Carhart 4F|Carhart] degradation 30-70%', body: 'When [IS] [αt] sits in 1.5-3.0, expect 50-70% degradation [OOS]. Plan strategy economics around [OOS] expectations, not [IS] observations.' },
	{ n: '02', name: '[R² vs benchmark|R²] ≈ 1.0 vs benchmark = signal dead', body: 'When active strategy daily returns correlate >0.95 with passive benchmark, the active alpha is mathematically dominated. Differentiator too small after costs.' },
	{ n: '03', name: 'Outlier-dominated raw means deceive', body: 'Heavy-tailed distributions need median + [winsorize|winsorized] mean + t-stat alongside raw mean. Raw mean alone misleads.' },
	{ n: '04', name: 'Value-style structural drag in 2020s+', body: 'Buffett-screening avoids speculative growth = misses the index. Mega-cap tech > 25% of SPY cap means value style underperforms structurally in growth bulls.' },
	{ n: '05', name: 'Universe concentration → overfit coefficient', body: 'Top-N / universe-N ratio matters. 113/15 = 13% concentration over-fits; 30/10 = 33% catastrophically. Larger universe + smaller relative concentration = lower overfit risk.' },
	{ n: '06', name: 'Multiple-testing correction ([Bonferroni correction|Bonferroni]) = n of config commits', body: 'True n counts every config-changing commit, every parameter sweep, every gate variant. [pre-registration ledger|Pre-registration ledger] enforces this honestly.' },
	{ n: '07', name: '[phase-aliasing|Phase-aliasing] in strided backtests', body: 'Stride-5 weekly rebalance has 5 [phase offset|phase offsets] — [single-phase] [αt] is sample-of-one. Quality+momentum showed 167.8pp [OOS] dispersion across phases on the same signal.' },
	{ n: '08', name: 'Pre-registration bites the hand that built it', body: 'Mom+lowvol was first real pre-reg test — it killed the strategy [single-phase] [IS] would have called a survivor. Working as designed.' },
	{ n: '09', name: '[coverage diagnostic|Coverage diagnostics] falsify regime gates pre-backtest', body: 'Measure each classifier\'s OFF-coverage of the failure window BEFORE running. <5% coverage = incoherent with failure mechanism. 30 min diagnostics > 3-4 h theatrical backtests.' },
	{ n: '10', name: 'Structural drift ≠ daily-classification regime', body: 'Mega-cap-vs-small-cap dominance 2017-2022 was slow cumulative drift, never point-in-time dispersion strong enough to trip a sensible threshold. Macro snapshots don\'t gate it.' },
	{ n: '11', name: 'Layer attribution makes failure modes legible', body: '[ADR] 0007 separation lets paradigm #10 vol-target fail isolate cleanly to Layer 4 without re-litigating screener.' },
	{ n: '12', name: 'Reactive risk overlays lever INTO concurrent regime shifts', body: 'Vol-targeting using trailing window levers UP into low-vol windows. When loss + vol-spike are concurrent (event-driven small-cap), overlay amplifies the loss. M-M 2017 was monthly aggregate-market — doesn\'t generalise.' },
	{ n: '13', name: 'Counter-cyclical alpha can be cost-mirage', body: '[R2000-PIT|R2000] long-only [PASS_MARGINAL] with EXTREME counter-cyclical [Q5] alpha MUST run slippage diagnostic BEFORE Layer 4 design. insider_form4: gross [αt]=+2.71 → net +1.27.' }
];

export const statusLegend: StatusDef[] = [
	{ status: 'FAIL', definition: 'hypothesis rejected on [multi-phase audit] gates ([αt] below the required threshold OR negative on ≥1 phase OR cost-stress fail).' },
	{ status: 'INCONCLUSIVE', definition: '[αt] landed in the ambiguous 1.0-2.85 band — paper-trade activated for 12-month forward observation, no capital deploy.' },
	{ status: 'PASS_MARGINAL', definition: 'gross-of-cost passes 4 of 5 gates; slippage diagnostic mandatory before any Layer 4 overlay or deploy decision.' },
	{ status: 'SLIPPAGE-FAIL', definition: '[PASS_MARGINAL] knocked back when realistic spread (50 bps half-spread) brought net [αt] below threshold in both windows.' },
	{ status: 'IN-FLIGHT', definition: 'hypothesis pre-registered, audit infrastructure built, awaiting data backfill or compute window.' }
];

// ---------------------------------------------------------------------------
// tool.experiments — the OTHER track's ledger. Where the paradigm ledger above
// falsifies standalone alpha hypotheses (measured in Carhart-4F alpha t-stat),
// this section logs experiments that tune the LIVE thematic tool's SELECTION
// and EXIT behaviour. Different metric axis (realized_r / market-excess return /
// live sample size N, not alpha-t), a different status vocabulary, and forward
// validation instead of terminal audits. Honesty rule: FORWARD-LOG / in-sample
// numbers are what-if replays that never touched the real trade record and have
// NOT passed a fresh forward test.
//
// WHEN ADDING A ROW: append to `toolExperiments`, use a status defined in
// `toolStatusLegend`, and if the row cites an evidence memo add that filename
// to scripts/sync-research-docs.mjs::REFERENCED (the Playwright smoke test
// asserts every rendered evidence file resolves 200).
export type ToolStatus = 'SHIPPED' | 'FORWARD-LOG' | 'AWAITING-N' | 'NO-GO' | 'FINDING';

export type ToolExperiment = {
	display: string;
	id: string;
	name: string;
	status: ToolStatus;
	metric: string;
	date: string;
	hypothesis: string;
	mechanism: string;
	outcome: string;
	lesson: string;
	prs: string[];
	evidence: string | null;
};

export type ToolStatusDef = {
	status: ToolStatus;
	definition: string;
};

export const toolStatusLegend: ToolStatusDef[] = [
	{ status: 'SHIPPED', definition: 'wired into the live tool (changes the order names are shown in); forward results still being collected.' },
	{ status: 'FORWARD-LOG', definition: 'display-only what-if; logs what would have happened on live trades without touching the real record. In-sample, not yet validated.' },
	{ status: 'AWAITING-N', definition: 'built and logging, but too few finished trades to judge; the verdict waits for at least 30 matured outcomes (~Sept 2026).' },
	{ status: 'NO-GO', definition: 'tested and rejected; the idea did not beat what the tool already does.' },
	{ status: 'FINDING', definition: 'a diagnostic result that taught us something but shipped no change to the tool.' }
];

// Numbers below are one dated snapshot: VPS `~/.alphalens` stores as of
// 2026-07-01 (372 plannable / 89 terminal outcomes over 43 brief-days).
export const toolExperiments: ToolExperiment[] = [
	{
		display: 'T1',
		id: 'exit_stop_lenses',
		name: 'Exit-stop what-if lenses (break-even + fill-anchored)',
		status: 'FORWARD-LOG',
		metric: 'break-even lens +0.075R vs −0.258R realized (N~55 live); fill-anchored lens: no matured outcomes yet',
		date: '2026-06-30',
		hypothesis: 'Our exit stop is set for a deep entry ladder, but trades often fill shallow — so a smarter stop might turn small losers into small wins.',
		mechanism: 'Two display-only lenses replay past trades with different stops (move to break-even after +0.5R; or stop 0.5×ATR below the tier that actually filled) and record the result without touching the real ledger.',
		outcome: 'On replay the break-even stop flips the average from −0.371R to positive with zero real winners hurt (+0.075R vs −0.258R on the live sample). The fill-anchored lens has no matured data yet.',
		lesson: 'This is in-sample and not validated. Moving a live price level links which tiers fill to when the trade stops, so it must pass a fresh forward test before any real stop change.',
		prs: ['#722', '#723', '#724', '#727'],
		evidence: 'exit_geometry_reward_risk_2026_06_30.md'
	},
	{
		display: 'T2',
		id: 'entry_not_the_lever',
		name: 'Is entry timing the lever? (dip-buy vs market / VWAP)',
		status: 'NO-GO',
		metric: 'dip-buy baseline ~flat (−0.0%, CI includes 0) beats market-at-arrival −1.9% and VWAP −1.9%; N=250 (refreshed 2026-07-01)',
		date: '2026-06-23',
		hypothesis: 'Buying at the market price on arrival, at VWAP, or fitting a best-possible ladder might beat our usual wait-for-a-dip entry.',
		mechanism: 'We replayed 5 entry styles over the same prices with the same exit rule and equal position size, compared returns to the S&P 500, and checked whether there was enough data to fit a custom ladder.',
		outcome: 'The dip-buy entry was the best of the five and came out roughly break-even; buying at market or VWAP lost clearly. There are too few finished trades to fit a custom ladder without overfitting.',
		lesson: 'Entry timing is not the lever — the real question is which names get picked. An earlier "dip-buy is an anti-signal" read was a measurement trap and is now corrected.',
		prs: ['#652', '#654'],
		evidence: 'entry_model_redesign_design_2026_06_23.md'
	},
	{
		display: 'T3',
		id: 'selection_is_the_lever',
		name: 'Where the weakness is: selection, and the first real signal',
		status: 'FINDING',
		metric: 'all picks lag SPY, deepening: −1.9% / −3.0% / −5.6% at 5 / 10 / 20 days (N=372, 43 brief-days). Confirmed separator: entry-time volatility (ATR) ρ −0.39, clears the strict bar.',
		date: '2026-06-25',
		hypothesis: 'The name-picker may have no real edge; and if it has any signal, it may just be chasing stocks that already jumped.',
		mechanism: 'We compared every pick to the S&P 500 over 1–20 days and tested ~50 numeric signals against a strict multiple-testing bar to see which ones separate winners from losers.',
		outcome: 'Picks lose to the market on average and the loss grows with time. On a fresh 43-day sample exactly one signal holds up: high-volatility / already-stretched names (ATR) fade hardest (ρ −0.39). Two related "already-popped" measures point the same way but are not yet robustness-checked, and our own composite score is a weak positive.',
		lesson: 'The lever is which names get picked, not how we enter. Even the confirmed signal only makes picks "less bad" versus the market — the list as a whole still lags, so nothing here is a proven money-maker yet. (An earlier raw 5-day run-up signal did not replicate.)',
		prs: ['#643', '#644', '#674'],
		evidence: 'edge_signal_attribution_2026_06_25.md'
	},
	{
		display: 'T4',
		id: 'atr_soft_tilt',
		name: 'ATR-soft-tilt: show volatile / popped names lower',
		status: 'SHIPPED',
		metric: 'live since 2026-06-25; the ATR signal it rests on replicated on fresh data (ρ −0.39); 0 matured outcomes under the new cohort yet; forward verdict pre-registered ~early-Aug 2026',
		date: '2026-06-25',
		hypothesis: 'Very volatile or already run-up names tend to do worse afterward (finding T3), so the tool should show them lower in the daily list.',
		mechanism: 'A new sort score lowers a candidate rank based on recent price choppiness (ATR). It only changes the order shown, never which names get tracked, so it can be measured fairly later.',
		outcome: 'Wired into live ordering and logging under a new cohort version. The signal it rests on held up when re-checked on a fresh 43-day sample, but no matured results exist yet to confirm the tilt itself helped.',
		lesson: 'Shipped as a conservative re-ranking only; the tracked list is unchanged. The forward verdict is pre-registered for early August 2026 once enough finished outcomes exist — until then it is unproven.',
		prs: ['#673', '#675', '#676', '#677'],
		evidence: 'edge_signal_attribution_2026_06_25.md'
	},
	{
		display: 'T5',
		id: 'expert_edge_calibration',
		name: 'Do the expert lenses (Buffett / O’Neil) predict winners?',
		status: 'AWAITING-N',
		metric: 'no fresh-data verdict yet; expert scores are underpowered so far (none clear the multiple-testing bar); needs N≥30 matured outcomes (~Sept 2026)',
		date: '2026-06-11',
		hypothesis: 'Names the Buffett / O’Neil lenses rate as higher quality should go on to beat the market more often than names they rate poorly.',
		mechanism: 'A job tags each finished trade with the expert score the pick had on its arrival day, then checks whether that score lines up with the actual market-beating return.',
		outcome: 'Not answered yet. The expert panel is built and logging scores live but stays display-only, and there are too few finished trades to measure any link.',
		lesson: 'Wait for real finished outcomes before scoring a lens. An early hint (lowest-quality names did worst) is suggestive but not proof, so the check is parked until about September 2026.',
		prs: ['#514'],
		evidence: null
	}
];
