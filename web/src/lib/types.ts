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
	gemini_confidence: number;
	market_cap: number | null;
	gates_passed: string[];
	gates_passed_str: string;
	n_gates_passed: number;
	gates_failed: string[];
	gates_failed_str: string;
	n_gates_failed: number;
	gates_unknown: string[];
	gates_unknown_str: string;
	n_gates_unknown: number;
	verified: boolean;
	source_event_url: string;
	source_event_title: string;
	source_event_published_at: string;
	theme_search_keywords: string[];
	industry_id: number | null;
	industry_name: string | null;
	sector_name: string | null;
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
	technicals_summary_str: string | null;
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
	brief_entry_price_note: string | null;
	brief_position_pct: number | null;
	brief_time_exit_weeks: number | null;
	brief_time_exit_on_catalyst_failure_weeks: number | null;
	brief_disaster_stop_pct: number | null;
	brief_full_md: string | null;
	brief_generated_at: string | null;
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
