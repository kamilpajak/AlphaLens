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
