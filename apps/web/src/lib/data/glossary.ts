// Glossary terms used inline on the /experiments research ledger.
//
// Single source of truth for:
//   - JargonTip tooltip body content (lookup via tipProps in +page.svelte)
//   - Inline [term] / [term|label] markup parsing in data text fields
//   - Auto-generated Playwright discovery tests (tests/smoke.test.ts)
//   - The pnpm run audit:tooltips check (scripts/audit-tooltips.mjs)
//
// Adding a new glossary entry:
//   1. Append entry here with { term, full, body }.
//   2. Add at least one inline reference in +page.svelte (via [term] markup OR
//      <JargonTip {...tipProps('term')}>). The discovery test enforces ≥1
//      inline reference per glossary entry.
//   3. The audit script will surface any drift or orphan markup automatically.

export interface GlossaryEntry {
	term: string;
	full: string;
	body: string;
	// Hybrid policy enforcement (audit-tooltips.mjs reads this):
	//   - 'always'   = short acronym; every occurrence in non-story data text
	//                  must be wrapped. Unwrapped occurrence = audit fail.
	//   - 'first-per-section' = multi-word / longer term; wrap first occurrence
	//                  per section only. Over-wrap (>1 in one section) = fail.
	category: 'always' | 'first-per-section';
	// Routes where this term is expected to appear. Drives the per-page
	// auto-discovery smoke test (tests/smoke.test.ts loops the glossary and
	// asserts ≥1 inline JargonTip with matching data-term on each page in
	// this list). Default ['experiments']; brief-detail-only acronyms set
	// ['briefs']; shared concepts set both.
	pages?: ('experiments' | 'briefs')[];
}

// Brief-detail-page metrics share the same shape (first-per-section /
// pages: ['briefs']). Constructor avoids the literal-object repetition that
// SonarCloud CPD flags as new-line duplication when entries are added.
function briefMetric(term: string, full: string, body: string): GlossaryEntry {
	return { term, full, body, category: 'first-per-section', pages: ['briefs'] };
}

export const GLOSSARY: GlossaryEntry[] = [
	{
		term: 'αt',
		full: 't-statistic on Carhart-4F α',
		body: 'A measure of how strongly a strategy beats its risk-adjusted benchmark, normalised so that values can be compared across strategies. Conventionally 2.0 = "publishable" / marginal evidence; this project uses 3.5 as the deploy-eligibility bar.',
		category: 'always'
	},
	{
		term: 'Carhart 4F',
		full: 'Carhart 4-factor model',
		body: 'A regression that explains stock returns using four factors: the overall market, size (small minus large), value (book/price), and momentum. Any return not explained by these four is the strategy\'s "α" — the part you can claim is real edge.'
,
		category: 'first-per-section'
	},
	{
		term: 'Bonferroni correction',
		full: 'multiple-testing correction',
		body: 'The more strategies you test, the more likely one looks good by chance. Bonferroni divides your significance threshold by the number of tests, so a strategy must clear a higher bar the more variants you try. This project ratchets the bar up after each failed test.'
,
		category: 'first-per-section'
	},
	{
		term: 'OOS',
		full: 'out-of-sample',
		body: 'Data the model has never seen. A strategy is only trustworthy if it works on OOS data — performance on IS (in-sample / training) data is mostly a measure of how well you fit, not how well you predict.'
,
		category: 'always'
	},
	{
		term: 'IS',
		full: 'in-sample',
		body: 'The training data used to build and tune a strategy. IS performance is always optimistic; OOS is the honest test.'
,
		category: 'always'
	},
	{
		term: 'FL',
		full: 'final-lock window',
		body: 'A second, independent fresh window evaluated AFTER OOS — typically the most recent available data (e.g. 2024-2026). Used to confirm an OOS result on a window that didn\'t exist when the strategy was designed; both OOS and FL must clear gates independently.'
,
		category: 'always'
	},
	{
		term: 'phase-aliasing',
		full: 'stride-offset artifact',
		body: 'A weekly-rebalance backtest can be started on Monday, Tuesday, Wednesday, Thursday or Friday — five different "phase offsets". Each can give wildly different results on the same signal (we saw 167.8 percentage points spread in P09). Reporting only one phase is a sample-of-one artifact. The methodology bundle runs all five and reports the mean ± std (this is the "multi-phase audit").'
,
		category: 'first-per-section'
	},
	{
		term: 'multi-phase audit',
		full: '(project protocol)',
		body: 'The protocol that runs a backtest at ALL stride-offset phases (typically 5 for weekly rebalances), then reports αt and other metrics as mean ± std across phases. Replaces single-phase reporting, which is statistically equivalent to a sample of one.'
,
		category: 'first-per-section'
	},
	{
		term: 'Phase A/B/C/D/E',
		full: 'pipeline phases (implementation milestones)',
		body: 'Larger paradigms (e.g. PEAD v2 paradigm #14) are split into sequential implementation milestones — Phase A = data backfill, B = scorer + cost-model lock, C = attribution regression infrastructure, D = experiment scaffold, E = audit launch. NOT the same as stride-offset "phases" — these are project milestones, not statistical replicates.'
,
		category: 'first-per-section'
	},
	{
		term: 'Form-4',
		full: 'SEC Form 4',
		body: 'Mandatory filing whenever a company insider (officer, director, ≥10% owner) buys or sells stock. Public within 2 business days of the trade; the full timeline is parsed in the form4_parquet cache.'
,
		category: 'first-per-section'
	},
	{
		term: 'Q5',
		full: 'top quintile',
		body: 'The top 20% of stocks ranked by some signal. "Q5 alpha" means the spread between the top quintile and the bottom (or against the index).'
,
		category: 'always'
	},
	{
		term: 'BAB',
		full: 'betting against beta',
		body: 'A well-known factor: low-beta stocks (less volatile than the market) tend to outperform on a risk-adjusted basis. When a strategy\'s alpha is partially explained by accidental low-beta exposure, it\'s called a "BAB confound".'
,
		category: 'always'
	},
	{
		term: 'FCFF',
		full: 'free cash flow to the firm',
		body: 'Cash the company generates from operations, minus capital spending, before interest payments. "FCFF yield" is FCFF / enterprise value — a value-investing favourite.'
,
		category: 'first-per-section'
	},
	{
		term: 'PEAD',
		full: 'post-earnings announcement drift',
		body: 'The textbook anomaly that stocks beating earnings keep drifting up for weeks, and stocks missing keep drifting down. One of the most-studied "edge" patterns in academic finance.'
,
		category: 'first-per-section'
	},
	{
		term: 'CAR',
		full: 'cumulative abnormal return',
		body: 'Sum of a stock\'s returns above what was expected over an event window (e.g. +1 to +20 days post-filing). Used to test whether an event (earnings, 8-K, M&A) predicts price movement.'
,
		category: 'always'
	},
	{
		term: 'R2000-PIT',
		full: 'Russell 2000 point-in-time universe',
		body: 'Russell 2000 = an index of ~2000 US small-cap stocks. "Point-in-time" means we use the actual roster that existed on each historical date, not today\'s roster (avoiding survivorship bias).'
,
		category: 'first-per-section'
	},
	{
		term: 'NW HAC',
		full: 'Newey-West heteroscedasticity- and autocorrelation-consistent SE',
		body: 'A way to compute standard errors in regressions that accounts for noise being correlated over time and not constant in magnitude. Important for daily-frequency strategy regressions.'
,
		category: 'first-per-section'
	},
	{
		term: 'ADR',
		full: 'Architectural Decision Record',
		body: 'A short markdown file documenting a major architectural choice and the reasoning behind it. Lives in docs/adr/; sequentially numbered.'
,
		category: 'first-per-section'
	},
	{
		term: 'R² vs benchmark',
		full: 'coefficient of determination vs benchmark',
		body: 'How much of an active strategy\'s daily return variance is explained by a passive benchmark (e.g. SPY). R² close to 1.0 means the strategy moves almost identically to the benchmark — no real differentiation. Project rule: R² > 0.95 vs passive = signal mathematically dominated by passive exposure.'
,
		category: 'first-per-section'
	},
	{
		term: 'single-phase',
		full: 'single-offset backtest',
		body: 'Running a strided backtest with ONE rebalance start-day offset only — e.g. Monday-start, ignoring Tuesday/Wednesday/Thursday/Friday alternatives. The αt and other metrics from a single phase are sample-of-one artifacts — the same signal can produce up to 167.8pp spread across the 5 offsets. Replaced in this project by the multi-phase audit protocol.'
,
		category: 'first-per-section'
	},
	{
		term: 'PASS_MARGINAL',
		full: 'marginal pass verdict',
		body: 'Gross-of-cost passes 4 of 5 audit gates but misses the strict Bonferroni threshold by ~0.4σ. Mechanism vindicated economically (every phase positive, low dispersion, bootstrap CI excludes zero) but the doctrine 3.5 αt bar not cleared. Slippage diagnostic is mandatory before any deploy / Layer-4 overlay decision. The Cohen-Malloy opportunistic Form-4 strategy reached PASS_MARGINAL on gross then SLIPPAGE-FAIL on net.'
,
		category: 'first-per-section'
	},
	{
		term: 'burnt holdout',
		full: 'previously-burned holdout window',
		body: 'A test window that has already been used by other paradigm tests in the same project. Reusing it raises the multiple-testing cost (Bonferroni) because every test "burns" the data\'s information for future tests. The αt threshold escalates after each burnt-holdout test — paradigm #11 distress_credit had to clear 3.50.'
,
		category: 'first-per-section'
	},
	{
		term: 'phase offset',
		full: 'rebalance start-day offset',
		body: 'In a weekly-rebalance backtest, which day-of-week the strategy starts trading. With stride=5 there are 5 possible offsets (Monday … Friday); each produces a slightly different time series of holdings and returns. The multi-phase audit runs all 5 and reports mean ± std rather than a single offset\'s sample-of-one result.'
,
		category: 'first-per-section'
	},
	{
		term: 'PIT',
		full: 'point-in-time',
		body: 'Historical data that reflects what was actually known at each past date — uses the actual roster, ratings, fundamentals available at time t, not today\'s revised version. Avoids look-ahead and survivorship bias. "R2000 PIT" = Russell 2000 with PIT roster; "S&P 1500 PIT" similar. Critical for honest backtests.'
,
		category: 'first-per-section'
	},
	{
		term: 'pre-registration ledger',
		full: 'pre-commit strategy spec',
		body: 'A locked-in spec file (in the kamilpajak/phase-robust-backtesting OSS bundle) that records hypothesis, gates, thresholds, and config fingerprint BEFORE the backtest runs. Once committed, any deviation costs Bonferroni budget. Stops "ex-post rationalisation" where you adjust the test after seeing results — this is the methodology bundle\'s load-bearing safeguard.'
,
		category: 'first-per-section'
	},
	{
		term: 'coverage diagnostic',
		full: 'gate-classifier coverage check',
		body: 'Before running a multi-classifier regime gate over a Bonferroni budget, measure each classifier\'s ON/OFF coverage of the actual failure window. If a "target" classifier covers <5% of the failure window, it cannot logically be the cause of failure — no backtest needed. Paradigm #08 used this to falsify Perplexity\'s recommended classifiers in 30 minutes vs 3–4 hours of theatrical backtests.'
,
		category: 'first-per-section'
	},
	{
		term: 'winsorize',
		full: 'tail clipping (cap outliers)',
		body: 'Clip the top and bottom N% of a distribution to the N-th percentile values, then compute statistics. Reveals the median/middle behaviour of heavy-tailed distributions (events, M&A, momentum) where the raw mean is dominated by a few large outliers. 8-K Item 5.03 had +606 bps raw mean but +602 bps winsorized with std 5783 across n=36 — meaning most positive contribution came from rare large spikes, not consistent drift.'
,
		category: 'first-per-section'
	},
	// Layer architecture tags (ADR 0007). Each paradigm row's header carries a
	// "<layer> · <axis_a> / <axis_b>" tag — the tooltip explains the tag inline
	// so first-time readers don't need an upfront architecture primer.
	{
		term: 'L2',
		full: 'Layer 2 — stock-selection',
		body: 'Picks WHICH tickers to trade. 17 of 18 paradigms here are L2 rules. The axis_a (screener/combo/compound/gate) says how the rule is built; axis_b (price/fundamental/insider/options/event-drift/macro) says what data it reads.',
		category: 'first-per-section'
	},
	{
		term: 'L4',
		full: 'Layer 4 — risk overlay',
		body: 'Time-series sizing on portfolio realised vol. Changes HOW MUCH exposure, not which tickers. P10 vol-target overlay is the only L4 paradigm tested.',
		category: 'first-per-section'
	},
	{
		term: 'screener',
		full: 'single-signal ranker',
		body: 'L2 structural type — one signal ranks the universe (e.g. opportunistic-buy magnitude, FCFF yield). The simplest L2 form.',
		category: 'first-per-section'
	},
	{
		term: 'combo',
		full: 'multi-signal composite, same data class',
		body: 'L2 structural type — two or more signals from the SAME data class (e.g. momentum × value × quality on price/fundamentals). Cheaper Bonferroni cost than compound because no new data classes.',
		category: 'first-per-section'
	},
	{
		term: 'compound',
		full: 'cross-class composite',
		body: 'L2 structural type — signals from DIFFERENT data classes (e.g. insider × options). Adds a Bonferroni budget cost vs single-class because crossing classes is a fresh hypothesis space.',
		category: 'first-per-section'
	},
	{
		term: 'gate',
		full: 'conditional filter on an existing rule',
		body: 'L2 structural type — a filter ON TOP of another rule (e.g. "deploy mom+lowvol only when VIX > 20"). Doesn\'t make new picks; just turns an existing rule on/off.',
		category: 'first-per-section'
	},
	{
		term: 'overlay',
		full: 'time-series exposure modulation',
		body: 'L4 structural type — modulates total portfolio exposure based on realised vol or drawdown. Doesn\'t change which tickers are held; changes the dollar size of the book.',
		category: 'first-per-section'
	},
	{
		term: 'price',
		full: 'price-return data class',
		body: 'Data-source axis — signal computed from price returns alone (momentum, mean-reversion, idiosyncratic momentum, BAB).',
		category: 'first-per-section'
	},
	{
		term: 'fundamental',
		full: 'company-financials data class',
		body: 'Data-source axis — signal computed from financial statements (FCFF, ROE, book/price, distance-to-default).',
		category: 'first-per-section'
	},
	{
		term: 'insider',
		full: 'SEC Form-4 insider-trade data class',
		body: 'Data-source axis — signal computed from insider purchases/sales (cluster-buys, opportunistic Cohen-Malloy magnitude).',
		category: 'first-per-section'
	},
	{
		term: 'options',
		full: 'options-market data class',
		body: 'Data-source axis — signal computed from options data (implied vol, put/call ratios, abnormal volume).',
		category: 'first-per-section'
	},
	{
		term: 'event-drift',
		full: 'scheduled-event data class',
		body: 'Data-source axis — signal triggered by scheduled events (earnings surprises = PEAD, 8-K filings). Pre-announcement timing matters.',
		category: 'first-per-section'
	},
	{
		term: 'macro',
		full: 'macroeconomic data class',
		body: 'Data-source axis — signal computed from macro indicators (yield curve, VIX, credit spreads, NFCI). Used by P03/P08 gate paradigms.',
		category: 'first-per-section'
	},
	// ---- Brief-detail-page valuation + technical jargon (P2) ----
	// These terms appear in /brief/[date] candidate cards (CandidateCard.svelte)
	// as dt labels in the fundamentals + technicals grids. Per-card scoping
	// means each card naturally wraps each acronym once → first-per-section.
	{
		term: 'PE',
		full: 'price-to-earnings ratio',
		body: 'Market price per share divided by trailing twelve-month earnings per share. Lower = "cheaper" relative to current profits; very low can also mean the market expects earnings to fall.',
		category: 'first-per-section',
		pages: ['briefs']
	},
	{
		term: 'PS',
		full: 'price-to-sales ratio',
		body: 'Market cap divided by trailing twelve-month revenue. Useful for unprofitable / cyclical companies where PE is undefined or volatile. Lower = pay less per dollar of revenue.',
		category: 'first-per-section',
		pages: ['briefs']
	},
	{
		term: 'EV/REV',
		full: 'enterprise value to revenue',
		body: 'Enterprise value (market cap + debt − cash) divided by revenue. Like PS but adjusts for capital structure — a debt-heavy company looks "cheap" on PS but expensive on EV/REV.',
		category: 'first-per-section',
		pages: ['briefs']
	},
	{
		term: 'EV/EBITDA',
		full: 'enterprise value to EBITDA',
		body: 'Enterprise value divided by earnings before interest, taxes, depreciation, and amortization. A profitability-aware valuation multiple — comparable across companies regardless of leverage or D&A policy.',
		category: 'first-per-section',
		pages: ['briefs']
	},
	{
		term: 'ROE',
		full: 'return on equity',
		body: 'Net income divided by shareholder equity, expressed as %. How efficiently the company turns its equity base into profit. >15% is generally considered strong for non-leveraged businesses.',
		category: 'first-per-section',
		pages: ['briefs']
	},
	{
		term: 'FCF margin',
		full: 'free cash flow margin',
		body: 'Free cash flow (operating cash flow minus capex) divided by revenue. Measures how much of each dollar of sales the business converts to discretionary cash — a "real" profitability signal less manipulable than reported earnings.',
		category: 'first-per-section',
		pages: ['briefs']
	},
	{
		term: 'ATR',
		full: 'average true range',
		body: 'A volatility measure: average daily price range (high − low, adjusted for gaps) over a lookback window (here %). Used for position sizing (smaller positions on higher-ATR names) and stop placement.',
		category: 'first-per-section',
		pages: ['briefs']
	},
	{
		term: 'MA50',
		full: '50-day moving average',
		body: 'Average closing price over the last 50 trading days. Distance from the MA50 is a short-term trend gauge: well above = trending strongly, well below = falling. Crosses with MA200 ("golden cross" / "death cross") are momentum signals.',
		category: 'first-per-section',
		pages: ['briefs']
	},
	{
		term: 'MA200',
		full: '200-day moving average',
		body: 'Average closing price over the last 200 trading days — the canonical long-term trend filter. Price above + MA200 slope > 0 = secular uptrend; price below + slope < 0 = downtrend; positive slope under price = deep-drawdown-reversal candidate setup.',
		category: 'first-per-section',
		pages: ['briefs']
	},
	{
		term: 'magic formula',
		full: 'Greenblatt magic-formula rank',
		body: 'Joel Greenblatt\'s ranking that combines earnings yield (cheap) with return on capital (high quality). Each candidate gets ranked within its sector cohort — lower magic-formula rank # = better combined score. Failed health gates (no PE / negative equity) leave the cell blank.',
		category: 'first-per-section',
		pages: ['briefs']
	},
	briefMetric('financials age', 'days since last filing', 'Calendar days between the candidate brief-date and the publish date of the latest 10-K / 10-Q used to derive fundamentals (PE, PS, ROE, FCFF yield, …). Higher = staler fundamentals = larger blind-spot risk if the business has changed since the filing. Typical fresh quarter is 30–90d; >180d means the next earnings could materially repaint the picture.'),
	briefMetric('next earnings', 'next scheduled earnings date', 'Next confirmed quarterly earnings release for the company. Holding a position through earnings adds a binary event-risk that the trade setup (ATR-based stops / tiers) does not price — the post-print gap can blow through the disaster stop intraday. Blank = no confirmed date available; treat as "unknown, could be soon" if the last filing is >75d old.'),
	briefMetric('MA200 slope', '200-day moving average slope', 'Day-over-day change in the MA200, expressed as % per day. Positive slope = the long-term trend is still rising (price drawdowns happen against an up-trending base — classic deep-drawdown-reversal setup). Negative slope = secular downtrend; "buy the dip" is fighting the trend. Magnitude is small by construction (typical band ±0.1–0.5%/d).')
];

export const GLOSSARY_BY_TERM: Map<string, GlossaryEntry> = new Map(
	GLOSSARY.map((g) => [g.term, g])
);
