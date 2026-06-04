export interface DayIndexEntry {
	date: string;
	n_candidates: number;
	n_themes: number;
	top_theme: string | null;
}

export interface Candidate {
	// ``date`` is the second half of the (date, ticker) primary key. The api
	// surfaces it on every candidate so cross-day endpoints (theme history,
	// ticker history) are self-describing without an envelope per row.
	date: string;
	theme: string;
	ticker: string;
	company_name: string;
	rationale: string;
	llm_confidence: number;
	market_cap: number | null;
	gates_passed: string[];
	n_gates_passed: number;
	gates_failed: string[];
	n_gates_failed: number;
	gates_unknown: string[];
	n_gates_unknown: number;
	verified: boolean;
	source_event_url: string;
	source_event_title: string;
	source_event_published_at: string;
	theme_search_keywords: string[];
	industry_id: number | null;
	industry_name: string | null;
	sector_name: string | null;
	/**
	 * Issue #197: how the SIC peer cohort was resolved.
	 *   - `sic4` — exact 4-digit cohort
	 *   - `sic3` — fell back to 3-digit prefix (wider cohort)
	 *   - `thin` — neither cohort hit min size; percentiles are null
	 * Empty string when the candidate's industry could not be resolved.
	 */
	peer_cohort_level: string | null;
	insider_score_usd: number | null;
	insider_score_sector_percentile: number | null;
	fcff_yield_pct: number | null;
	fcff_yield_sector_percentile: number | null;
	valuation_pe: number | null;
	valuation_ps: number | null;
	valuation_ev_rev: number | null;
	valuation_ev_ebitda: number | null;
	valuation_fcf_margin: number | null;
	valuation_composite_sector_percentile: number | null;
	valuation_financials_publish_date: string | null;
	valuation_financials_age_days: number | null;
	roic_pct: number | null;
	roe_pct: number | null;
	magic_formula_health_pass: boolean | null;
	technical_rsi: number | null;
	technical_ma50_distance_pct: number | null;
	technical_atr_pct: number | null;
	technical_volume_zscore: number | null;
	technical_pct_off_52w_high: number | null;
	technical_pct_off_52w_low: number | null;
	technical_ma200_distance_pct: number | null;
	technical_ma200_slope_pct_per_day: number | null;
	catalyst_strength: number | null;
	catalyst_event_type: string | null;
	catalyst_confidence: number | null;
	magic_formula_rank: number | null;
	magic_formula_cohort_n: number | null;
	deep_drawdown_reversal: boolean | null;
	layer4_weighted_score: number | null;
	also_in_themes: string[];
	rank_in_day: number | null;
	cohort_size_in_day: number | null;
	next_earnings_date: string | null;
	brief_model_used: string | null;
	brief_tldr: string | null;
	brief_supply_chain_md: string | null;
	brief_bear_summary_md: string | null;
	brief_catalyst_failure_exit: string | null;
	brief_trade_setup: TradeSetup | null;
	/**
	 * PR-3 of epic #321 — typed facts from the template engine.
	 * `brief_template_id` is the source template (`m_and_a_press_release`,
	 * `earnings_surprise`, …) or empty string for flash-extracted catalysts.
	 * `brief_template_facts` is the deserialised key/value dict the SPA
	 * evidence panel renders inline. Both are null/empty together; a
	 * present id with null facts is the corrupt-JSON degradation case.
	 */
	brief_template_id: string | null;
	brief_template_facts: Record<string, unknown> | null;
	brief_generated_at: string | null;
}

/**
 * One limit-buy rung of the entry ladder. `atr_distance` is how far below the
 * reference close the limit sits, expressed in ATR units (always positive;
 * render as "−0.5 ATR").
 */
export interface EntryTier {
	limit: number;
	alloc_pct: number;
	atr_distance: number;
	tag: string;
}

/** One take-profit tranche. `r_multiple` is the reward-to-risk multiple. */
export interface TpTranche {
	target: number;
	tranche_pct: number;
	r_multiple: number;
	tag: string;
}

/**
 * Structured trade-setup block, mirrors the Python `TradeSetup.to_dict()`.
 * Reference levels are anchored to the last close — coordination points, not a
 * forecast. When `status === 'NO_STRUCTURE'` the price-derived fields
 * (`disaster_stop`, `suggested_size_pct`) are null and the ladders are empty.
 */
export interface TradeSetup {
	schema_version: string;
	status: 'OK' | 'NO_STRUCTURE';
	asof_close: number;
	atr: number;
	disaster_stop: number | null;
	suggested_size_pct: number | null;
	order_ttl_days: number;
	entry_tiers: EntryTier[];
	tp_tranches: TpTranche[];
}

export interface DayBrief {
	date: string;
	n_candidates: number;
	n_themes: number;
	top_theme: string | null;
	theme_counts: Record<string, number>;
	candidates: Candidate[];
}

// Pagination envelope returned by every list endpoint on the api
// (`/api/v1/days`, `/api/v1/themes`, `/api/v1/tickers/{ticker}/history`, …).
// Mirrors `alphalens.api.models.Paginated[T]`.
export interface Paginated<T> {
	data: T[];
	meta: {
		total: number;
		limit: number;
		offset: number;
	};
}

// Feedback ledger — mirrors apps/alphalens-pipeline/.../feedback/store.py
// dataclass. Action + dismiss enums match the locked 2026-05-29 design
// memo; the SPA fetches the taxonomy from /v1/feedback/taxonomy at boot
// instead of hard-coding the literals so a backend update is picked up
// without a frontend redeploy.
export type FeedbackAction =
	| 'interested'
	| 'watching'
	| 'dismissed'
	| 'paper_traded'
	| 'live_traded';

export type DismissCategory = 'thesis_setup' | 'risk_quality' | 'portfolio_style' | 'other';

export interface Decision {
	id: string;
	brief_date: string;
	ticker: string;
	theme: string;
	surfaced_at: string;
	action: FeedbackAction;
	action_at: string;
	dismiss_category: DismissCategory | null;
	dismiss_reason: string | null;
	dismiss_note: string | null;
	confidence_subjective: number | null;
	paper_trade_plan_id: string | null;
	position_size_usd: number | null;
	entry_price: number | null;
	market_regime_at_entry: string | null;
}

export interface FeedbackTaxonomy {
	actions: FeedbackAction[];
	categories: Record<DismissCategory, string[]>;
}

// ── Edge dashboard (market-behavior ledger) ─────────────────────────────
// Mirrors GET /v1/edge/summary + /v1/edge/outcomes (the Phase-1 backend on
// branch feature/feedback-edge-dashboard). The dashboard is EXPLORATORY only
// (hypothesis-gen, never confirmatory) per
// docs/research/feedback_edge_dashboard_2026_06_04.md §0/§3.
//
// The N-gate is enforced SERVER-SIDE: when n_matured < 30 the backend nulls
// the stat fields (means/medians/quantiles) but never drops the keys, so the
// frontend branches on `status` and renders whatever the API returns — it
// never computes an aggregate client-side.

/** Tri-state N-gate verdict. `insufficient` (<30) hides stats; `early`
 *  (30..99) shows them with a high-variance warning; `ok` (>=100) is the
 *  unlocked state. */
export type EdgeStatus = 'insufficient' | 'early' | 'ok';

/** Excess-R quantile triple (raw R-units). All null under the N-gate. */
export interface EdgeQuantiles {
	p10: number | null;
	p50: number | null;
	p90: number | null;
}

/** The benchmark-relative EDGE panel — the §3.1 PRIMARY metric. All the
 *  mean/median/quantile fields are null when `status === 'insufficient'`. */
export interface EdgePanel {
	status: EdgeStatus;
	n_matured: number;
	threshold: number;
	market_excess_mean: number | null;
	market_excess_median: number | null;
	market_excess_quantiles: EdgeQuantiles;
	gross_realized_r_mean: number | null;
	gross_realized_r_median: number | null;
	gross_realized_r_n: number;
	holding_days_n: number;
	holding_days_p50: number | null;
	holding_days_p95: number | null;
	gross_of_cost: boolean;
	regime_stratified: boolean;
}

/** The size-weighted PORTFOLIO panel (§ additive size layer). N-gated. */
export interface PortfolioPanel {
	status: EdgeStatus;
	n_matured: number;
	threshold: number;
	total_realized_contribution_pct_of_book: number | null;
	size_weighted_realized_r: number | null;
	mean_realized_risk_pct: number | null;
	mean_tiers_filled_count: number | null;
	gross_of_cost: boolean;
}

/** Deployment metrics — N-INDEPENDENT, always populated (live from day one). */
export interface DeploymentPanel {
	n_terminal: number;
	n_filled: number;
	n_no_fill: number;
	fill_rate: number | null;
	no_fill_rate: number | null;
	mean_tiers_filled_count: number | null;
}

/** Open-positions block — DESCRIPTIVE ONLY (§3.3). Never a mean open_R. */
export interface OpenPositionsPanel {
	n_open: number;
	near_tp: number;
	near_sl: number;
	note: string;
}

export interface EdgeSummary {
	n_brief: number;
	n_plannable: number;
	n_terminal: number;
	n_matured: number;
	n_gate_threshold: number;
	benchmark: string;
	metric_note: string;
	edge: EdgePanel;
	portfolio: PortfolioPanel;
	deployment: DeploymentPanel;
	open_positions: OpenPositionsPanel;
}

/** One per-candidate outcome row (GET /v1/edge/outcomes). `theme` is joined
 *  from the brief cache (null when uncached). `realized_r`/`open_r` are
 *  mutually exclusive by `terminal`. */
export interface EdgeOutcome {
	ticker: string;
	brief_date: string;
	theme: string | null;
	ladder_classification: string;
	terminal: boolean;
	realized_r: number | null;
	open_r: number | null;
	market_excess_return: number | null;
	forward_return: number | null;
	benchmark_window_return: number | null;
	holding_days_elapsed: number | null;
	realized_return_pct_of_book: number | null;
}
