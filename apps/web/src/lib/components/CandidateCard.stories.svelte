<script module lang="ts">
	import type { ComponentProps } from 'svelte';
	import { defineMeta } from '@storybook/addon-svelte-csf';
	import { expect, waitFor } from 'storybook/test';
	import type { Candidate } from '$lib/types';
	import CandidateCard from './CandidateCard.svelte';

	type CandidateCardProps = ComponentProps<typeof CandidateCard>;

	// Story 1 — FOUR (rank 1/16):
	//   - has expert_assessments (buffett 62, oneil 55)
	//   - atr_penalty 0.5 → "extended" chip visible
	//   - trade_setup NO_STRUCTURE (no entry ladder)
	//   - source: verbatim from tests/fixtures/api-mock/days/2026-05-18.json candidates[0]
	const candidateFOUR: Candidate = {
		date: '2026-05-18',
		theme: 'retail',
		ticker: 'FOUR',
		company_name: 'Shift4 Payments, Inc.',
		rationale:
			'Shift4 Payments delivers integrated payment processing and point-of-sale software solutions tailored for retail merchants and hospitality venues.',
		llm_confidence: 0.8,
		market_cap: 3403210960.646332,
		gates_passed: ['tenk'],
		n_gates_passed: 1,
		gates_failed: ['press', 'insider'],
		n_gates_failed: 2,
		gates_unknown: ['etf'],
		n_gates_unknown: 1,
		gate_verdict_json: null,
		verified: true,
		source_event_url:
			'https://www.marketwatch.com/story/walmart-and-target-are-about-to-show-just-how-much-shopping-habits-have-changed-due-to-the-iran-war-105b34d9?mod=mw_rss_topstories',
		source_event_title:
			'Walmart and Target are about to show just how much shopping habits have changed due to the Iran war',
		source_event_published_at: '2026-05-17',
		theme_search_keywords: [
			'retail operations',
			'omnichannel commerce',
			'point of sale',
			'e-commerce platform',
			'retail supply chain',
			'inventory management',
			'brick-and-mortar',
			'retail fulfillment',
			'shopping center',
			'merchandising'
		],
		industry_id: 101003.0,
		industry_name: 'Application Software',
		sector_name: 'Technology',
		peer_cohort_level: null,
		insider_score_usd: 0.0,
		insider_score_sector_percentile: 96.92307692307692,
		insider_signal_version: null,
		fcff_yield_pct: 9.121655979731411,
		fcff_yield_sector_percentile: 64.83516483516483,
		valuation_pe: 23.97380244897959,
		valuation_ps: 0.8430978373205742,
		valuation_ev_rev: 1.7000356363636364,
		valuation_ev_ebitda: 11.08603581903276,
		valuation_fcf_margin: 0.15507140228192862,
		valuation_composite_sector_percentile: 58.18603363274127,
		valuation_financials_publish_date: '2026-04-30',
		valuation_financials_age_days: 18,
		roic_pct: 6.986464968152866,
		roe_pct: 10.194174757281553,
		magic_formula_health_pass: false,
		technical_rsi: 46.67170538473019,
		technical_ma50_distance_pct: -4.269044647522551,
		technical_atr_pct: 6.488384191174425,
		technical_volume_zscore: -1.948431348740669,
		technical_pct_off_52w_high: -59.85394434206203,
		technical_pct_off_52w_low: 6.48125299399737,
		technical_ma200_distance_pct: -34.14783255940499,
		technical_ma200_slope_pct_per_day: -0.4227497553645656,
		catalyst_strength: 0.68,
		catalyst_event_type: 'macro',
		catalyst_confidence: 0.9,
		magic_formula_rank: null,
		magic_formula_cohort_n: 3,
		deep_drawdown_reversal: false,
		layer4_weighted_score: 3,
		selection_score: 2.5,
		atr_penalty: 0.5,
		scorer_config_version: 'atr-tilt-v1',
		expert_assessments: {
			buffett: {
				buffett_quality_score: 62
			},
			oneil: {
				oneil_score: 55,
				oneil_pct_off_52w_high: -59.85394434206203,
				oneil_rs_approx_pct: 24,
				oneil_ma200_slope_pct_per_day: -0.4227497553645656,
				oneil_ma200_distance_pct: -34.14783255940499,
				oneil_earnings_growth_yoy_pct: -3.3
			},
			panel: {
				expert_spread: 7,
				panel_config_version: 'panel-v1-absdiff-2x'
			}
		},
		also_in_themes: [],
		rank_in_day: 1,
		cohort_size_in_day: 16,
		next_earnings_date: null,
		brief_model_used: 'gemini-2.5-flash',
		brief_tldr:
			'Shift4 Payments (FOUR) provides integrated payment processing for retail and hospitality, with an upcoming catalyst on May 17, 2026, when Walmart and Target report on changing shopping habits due to the Iran war, potentially impacting payment volumes.',
		brief_supply_chain_md:
			'Shift4 Payments provides integrated payment processing and point-of-sale software, making it a critical component in the retail and hospitality supply chain by facilitating consumer transactions. The upcoming Walmart and Target reports on May 17, 2026, detailing shifts in shopping habits due to the Iran war, directly impact the transaction volumes and types that Shift4 processes. Changes in consumer spending patterns or merchant activity, driven by these broader trends, will directly affect Shift4’s revenue and operational performance, as its business scales with transaction flow.',
		brief_bear_summary_md:
			'Key risks include Shift4’s significant momentum laggard status, with the stock down 34.1% from its MA200 and 59.9% from its 52-week high. The competitive payment processing industry poses ongoing margin pressure, and macroeconomic headwinds impacting consumer spending could reduce transaction volumes across its merchant base.',
		brief_catalyst_failure_exit:
			'If the Walmart and Target reports on May 17, 2026, indicate a significant decline in overall retail transaction volumes or a shift away from Shift4’s core merchant segments, leading to a negative revision of future payment processing expectations, this would trigger an exit.',
		brief_trade_setup: {
			schema_version: '1.0.0',
			status: 'NO_STRUCTURE',
			asof_close: 88.4,
			atr: 5.74,
			disaster_stop: null,
			suggested_size_pct: null,
			order_ttl_days: 10,
			entry_tiers: [],
			tp_tranches: []
		},
		brief_template_id: null,
		brief_template_facts: null,
		brief_generated_at: '2026-05-19T10:53:05.992557+00:00',
		brief_status: 'ok',
		brief_error_kind: null,
		market_state: 'unknown',
		market_state_atr_pct: null,
		market_state_atr_pct_q: null,
		market_state_dist200: null,
		market_state_vix: null,
		market_state_vix_decile: null,
		market_state_squeeze_on: null,
		market_state_config_version: null,
		// Pre-#1066 row: the causal-support assessor did not exist on 2026-05-18,
		// so every status column is NULL and <ChannelRecord> renders nothing. This
		// is the faithful shape for this fixture's date, not a placeholder.
		brief_causal_support: null,
		brief_channel_grounding: null,
		brief_support_guard_status: null,
		channel_type: null,
		channel_text: null,
		channel_evidence: null,
		channel_falsifier: null,
		channel_grounding_quote: null,
		channel_grounding_reason: null
	};

	// Story 2 — PIPR (rank 3/16):
	//   - no expert_assessments (pre-panel snapshot)
	//   - no atr_penalty (no "extended" chip)
	//   - trade_setup OK with full entry ladder + take-profit tranches
	//   - source: verbatim from tests/fixtures/api-mock/days/2026-05-18.json candidates[2]
	const candidatePIPR: Candidate = {
		date: '2026-05-18',
		theme: 'IPO',
		ticker: 'PIPR',
		company_name: 'Piper Sandler Companies',
		rationale:
			'A middle-market investment bank that relies heavily on equity underwriting fees from healthcare and technology IPOs.',
		llm_confidence: 0.8,
		market_cap: 5391327203.128021,
		gates_passed: ['tenk'],
		n_gates_passed: 1,
		gates_failed: ['press', 'insider'],
		n_gates_failed: 2,
		gates_unknown: ['etf'],
		n_gates_unknown: 1,
		gate_verdict_json: null,
		verified: true,
		source_event_url: 'https://www.ft.com/content/846479c8-4ab0-4812-a1d5-08abdd8b952b',
		source_event_title: 'The fate of OpenAI’s $1tn IPO will be decided in an Oakland jury room',
		source_event_published_at: '2026-05-18',
		theme_search_keywords: [
			'initial public offering',
			'IPO',
			'equity capital markets',
			'ECM',
			'underwriting fees',
			'S-1 filing',
			'going public',
			'listing fees',
			'equity underwriting',
			'registration statement'
		],
		industry_id: 104003.0,
		industry_name: 'Brokers, Exchanges & Other',
		sector_name: 'Financial Services',
		peer_cohort_level: null,
		insider_score_usd: 0.0,
		insider_score_sector_percentile: 100.0,
		insider_signal_version: null,
		fcff_yield_pct: 152.1867128139562,
		fcff_yield_sector_percentile: 100.0,
		valuation_pe: 4.750665199904415,
		valuation_ps: 0.7287146966268947,
		valuation_ev_rev: 0.20748697502066946,
		valuation_ev_ebitda: null,
		valuation_fcf_margin: 0.3157676068010713,
		valuation_composite_sector_percentile: 83.80121816168328,
		valuation_financials_publish_date: '2026-04-09',
		valuation_financials_age_days: 39,
		roic_pct: null,
		roe_pct: 20.520026520652337,
		magic_formula_health_pass: false,
		technical_rsi: 39.78772846574773,
		technical_ma50_distance_pct: -1.4370981078075058,
		technical_atr_pct: 3.4214833152621016,
		technical_volume_zscore: -0.13588130693669695,
		technical_pct_off_52w_high: -16.637569248975037,
		technical_pct_off_52w_low: 26.367286036470098,
		technical_ma200_distance_pct: -5.6656424562464744,
		technical_ma200_slope_pct_per_day: 0.044159462080706056,
		catalyst_strength: 0.6200000000000001,
		catalyst_event_type: 'litigation',
		catalyst_confidence: 0.9,
		magic_formula_rank: null,
		magic_formula_cohort_n: 3,
		deep_drawdown_reversal: false,
		layer4_weighted_score: 3,
		selection_score: null,
		atr_penalty: null,
		scorer_config_version: null,
		expert_assessments: null,
		also_in_themes: [],
		rank_in_day: 3,
		cohort_size_in_day: 16,
		next_earnings_date: null,
		brief_model_used: 'gemini-2.5-flash',
		brief_tldr:
			'Piper Sandler, a middle-market investment bank, is positioned to benefit from a potential resurgence in technology IPOs, with the OpenAI IPO trial on 2026-05-18 serving as a key near-term catalyst for equity underwriting fees.',
		brief_supply_chain_md:
			'Piper Sandler Companies operates as a middle-market investment bank, deriving a significant portion of its revenue from equity underwriting fees, particularly from healthcare and technology IPOs. Its business model is directly tied to the health and activity of the primary equity markets, acting as an intermediary in the capital formation process. A robust IPO environment, especially within the technology sector, directly translates to increased deal flow and fee generation for firms like PIPR, positioning it within the financial services supply chain as a critical facilitator of public market access for private companies.',
		brief_bear_summary_md:
			'PIPR faces significant risks from its heavy reliance on the volatile IPO market, especially in healthcare and technology, which can experience prolonged downturns. A general market slowdown or increased competition in investment banking could compress underwriting fees and reduce deal volume. Broader economic conditions also impact capital markets activity.',
		brief_catalyst_failure_exit:
			'If the OpenAI IPO catalyst fails to materialize or is significantly delayed beyond the 2026-05-18 trial, or if the outcome negatively impacts broader technology IPO sentiment, the investment thesis for PIPR would be invalidated, prompting an exit.',
		brief_trade_setup: {
			schema_version: '1.0.0',
			status: 'OK',
			asof_close: 312.5,
			atr: 10.7,
			disaster_stop: 275.05,
			suggested_size_pct: 4.065583485277316,
			order_ttl_days: 10,
			entry_tiers: [
				{ limit: 307.15, alloc_pct: 27.98308726424079, atr_distance: 0.5, tag: 'shallow pullback' },
				{ limit: 299.66, alloc_pct: 34.35419283481206, atr_distance: 1.2, tag: 'momentum reset' },
				{ limit: 291.1, alloc_pct: 37.66271990094714, atr_distance: 2.0, tag: 'deep value add' }
			],
			tp_tranches: [
				{ target: 323.2, tranche_pct: 33.333333333333336, r_multiple: 0.3, tag: 'first scale-out' },
				{ target: 336.04, tranche_pct: 33.333333333333336, r_multiple: 0.6, tag: 'overhead resistance' },
				{ target: 349.95, tranche_pct: 33.333333333333336, r_multiple: 1.0, tag: 'measured-move target' }
			]
		},
		brief_template_id: null,
		brief_template_facts: null,
		brief_generated_at: '2026-05-19T10:53:24.266109+00:00',
		brief_status: 'ok',
		brief_error_kind: null,
		market_state: 'unknown',
		market_state_atr_pct: null,
		market_state_atr_pct_q: null,
		market_state_dist200: null,
		market_state_vix: null,
		market_state_vix_decile: null,
		market_state_squeeze_on: null,
		market_state_config_version: null,
		// Pre-#1066 row: the causal-support assessor did not exist on 2026-05-18,
		// so every status column is NULL and <ChannelRecord> renders nothing. This
		// is the faithful shape for this fixture's date, not a placeholder.
		brief_causal_support: null,
		brief_channel_grounding: null,
		brief_support_guard_status: null,
		channel_type: null,
		channel_text: null,
		channel_evidence: null,
		channel_falsifier: null,
		channel_grounding_quote: null,
		channel_grounding_reason: null
	};

	// Story 3 — empty narrative: FOUR with the three bottom sections blanked to
	// "". Reproduces the MC/Moelis empty-card state (a brief whose LLM narrative
	// came back empty). The card must render "—" placeholders, NOT empty section
	// bodies — the `|| '—'` fallback (empty string is falsy) does this, whereas
	// the old `?? '—'` (nullish) let "" through as a blank body.
	const candidateEmptyNarrative: Candidate = {
		...candidateFOUR,
		brief_supply_chain_md: '',
		brief_bear_summary_md: '',
		brief_catalyst_failure_exit: ''
	};

	// Story 4 — empty DISPLAY fields (the follow-up to #869, which fixed only the
	// three brief_* narrative sections). Same falsy-guard class on the sibling
	// render sites: rationale blockquote fallback (brief_tldr empty -> rationale
	// empty -> '—'), the source-event link (null url/title -> no dead hrefless
	// link), and the sector/industry meta line ('' -> '—'). Django serves these
	// text fields as "" (blank=True), so a nullish `??` would leak the blank.
	const candidateEmptyDisplayFields: Candidate = {
		...candidateFOUR,
		brief_tldr: '',
		rationale: '',
		source_event_title: '',
		source_event_url: '',
		sector_name: '',
		industry_name: ''
	};

	// Story 5 — PAR (rank 16/16), the honest "brief unavailable" state (#921):
	//   - brief_status "unavailable" + brief_error_kind "truncated", brief_tldr null
	//   - the blockquote shows the tone-neutral muted label above the italic
	//     rationale fallback (no verdict words, no authority colours)
	//   - source: verbatim from tests/fixtures/api-mock/days/2026-05-18.json
	//     candidates[15]
	const candidatePARBriefUnavailable: Candidate = {
		date: '2026-05-18',
		theme: 'promotions',
		ticker: 'PAR',
		company_name: 'PAR Technology Corporation',
		rationale:
			"PAR Technology's Punchh division is a leading provider of loyalty and promotional offer management software for enterprise restaurant chains.",
		llm_confidence: 0.75,
		market_cap: 588170807.1805058,
		gates_passed: ['tenk'],
		n_gates_passed: 1,
		gates_failed: ['press', 'insider'],
		n_gates_failed: 2,
		gates_unknown: ['etf'],
		n_gates_unknown: 1,
		gate_verdict_json: null,
		verified: true,
		// The parquet row carries null here; the TS contract types these as
		// plain `string` (Django serves "" for blanks). Both are falsy — the
		// source-event link is gated out either way.
		source_event_url: '',
		source_event_title: '',
		source_event_published_at: '',
		theme_search_keywords: [
			'promotional products',
			'digital coupons',
			'cash-back offers',
			'loyalty program',
			'promotional campaigns',
			'trade promotions',
			'branded merchandise',
			'promotional marketing',
			'discount codes',
			'card-linked offers'
		],
		industry_id: 101003.0,
		industry_name: 'Application Software',
		sector_name: 'Technology',
		peer_cohort_level: null,
		insider_score_usd: 0.0,
		insider_score_sector_percentile: 96.92307692307692,
		insider_signal_version: null,
		fcff_yield_pct: null,
		fcff_yield_sector_percentile: null,
		valuation_pe: null,
		valuation_ps: 1.249388203632117,
		valuation_ev_rev: 1.9396770212513748,
		valuation_ev_ebitda: null,
		valuation_fcf_margin: null,
		valuation_composite_sector_percentile: 60.8,
		valuation_financials_publish_date: '2026-04-16',
		valuation_financials_age_days: 32,
		roic_pct: -6.034613626252513,
		roe_pct: -10.235835908622674,
		magic_formula_health_pass: false,
		technical_rsi: 48.833479835849936,
		technical_ma50_distance_pct: 0.9607205183212039,
		technical_atr_pct: 6.977062309522344,
		technical_volume_zscore: -1.1031444860364434,
		technical_pct_off_52w_high: -79.99438533142664,
		technical_pct_off_52w_low: 20.5583787471656,
		technical_ma200_distance_pct: -54.29660078750816,
		technical_ma200_slope_pct_per_day: -0.7382796701139313,
		catalyst_strength: 0.0,
		catalyst_event_type: null,
		catalyst_confidence: null,
		magic_formula_rank: null,
		magic_formula_cohort_n: 3,
		deep_drawdown_reversal: false,
		layer4_weighted_score: 1,
		selection_score: null,
		atr_penalty: null,
		scorer_config_version: null,
		expert_assessments: null,
		also_in_themes: [],
		rank_in_day: 16,
		cohort_size_in_day: 16,
		next_earnings_date: null,
		// brief_status "unavailable" ⇒ ALL LLM prose columns are None (the
		// orchestrator's `b = brief or {}` path); only the deterministic trade
		// setup + generated_at stamp survive a failed LLM brief.
		brief_model_used: null,
		brief_tldr: null,
		brief_supply_chain_md: null,
		brief_bear_summary_md: null,
		brief_catalyst_failure_exit: null,
		brief_trade_setup: {
			schema_version: '1.0.0',
			status: 'OK',
			asof_close: 54.2,
			atr: 2.71,
			disaster_stop: 44.71,
			suggested_size_pct: 13.0,
			order_ttl_days: 10,
			entry_tiers: [
				{ limit: 52.85, alloc_pct: 50, atr_distance: 0.5, tag: 'shallow pullback' },
				{ limit: 50.95, alloc_pct: 30, atr_distance: 1.2, tag: 'momentum reset' },
				{ limit: 48.78, alloc_pct: 20, atr_distance: 2.0, tag: 'deep value add' }
			],
			tp_tranches: [
				{ target: 56.91, tranche_pct: 40, r_multiple: 0.3, tag: 'first scale-out' },
				{ target: 60.16, tranche_pct: 35, r_multiple: 0.6, tag: 'overhead resistance' },
				{ target: 63.69, tranche_pct: 25, r_multiple: 1.0, tag: 'measured-move target' }
			]
		},
		brief_template_id: null,
		brief_template_facts: null,
		brief_generated_at: '2026-05-19T10:54:59.878202+00:00',
		brief_status: 'unavailable',
		brief_error_kind: 'truncated',
		market_state: 'unknown',
		market_state_atr_pct: null,
		market_state_atr_pct_q: null,
		market_state_dist200: null,
		market_state_vix: null,
		market_state_vix_decile: null,
		market_state_squeeze_on: null,
		market_state_config_version: null,
		// Pre-#1066 row: the causal-support assessor did not exist on 2026-05-18,
		// so every status column is NULL and <ChannelRecord> renders nothing. This
		// is the faithful shape for this fixture's date, not a placeholder.
		brief_causal_support: null,
		brief_channel_grounding: null,
		brief_support_guard_status: null,
		channel_type: null,
		channel_text: null,
		channel_evidence: null,
		channel_falsifier: null,
		channel_grounding_quote: null,
		channel_grounding_reason: null
	};

	// Story 6 — PAR again, the support guard's WITHHELD state:
	//   - same row, but brief_error_kind "unsupported_benefit_claim"
	//   - the label must say the prose was WITHHELD, not that generation failed
	//     (the model returned two schema-valid briefs; the pipeline suppressed
	//     them because the wording asserted a benefit the record cannot carry)
	//   - the italic stage-A rationale fallback is SUPPRESSED here: it is
	//     unguarded mapper prose, and promoting it would undo the withhold
	const candidatePARProseWithheld: Candidate = {
		...candidatePARBriefUnavailable,
		brief_error_kind: 'unsupported_benefit_claim',
		// The guard only ever fires on a row whose record cannot carry a benefit
		// claim, so the withheld state always ships WITH a record — here the
		// lowest support level on an otherwise sound measurement.
		brief_causal_support: 'not_established',
		brief_channel_grounding: 'grounded',
		brief_support_guard_status: 'withheld',
		channel_type: 'none',
		channel_text: '',
		channel_evidence: '',
		channel_falsifier: '',
		channel_grounding_quote: '',
		channel_grounding_reason: ''
	};

	// ------------------------------------------------------------------
	// Stories 7-10 — the causal-support record (#1069).
	//
	// Every value below is VERBATIM from the recorded golden brief day
	// tests/golden/fixtures/brief_day/golden/2026-08-19.parquet — the first
	// production day on the post-#1068 taxonomy. Real assessor output, not
	// plausible-looking prose: a fabricated record here would quietly misrepresent
	// what the instrument actually says, which is the failure this surface exists
	// to prevent.
	// ------------------------------------------------------------------

	// MRVI — suggestive x grounded. The COMMON case and the quiet one: the event
	// implies a supplier chain, the assessor named it, and nothing about the
	// measurement broke. The chain (mechanism / quoted evidence / falsifier) sits
	// in the drawer; the face carries one line.
	const candidateMRVI: Candidate = {
		...candidateFOUR,
		date: '2026-08-19',
		ticker: 'MRVI',
		company_name: 'Maravai LifeSciences Holdings, Inc.',
		theme: 'phase_3_trial',
		catalyst_event_type: 'other',
		catalyst_strength: 0.58,
		source_event_title:
			"Moderna and Merck Just Made History With the First mRNA Cancer Vaccine to Succeed in a Phase 3 Trial. Here's What That Means for Investors.",
		source_event_published_at: '2026-08-19',
		source_event_url:
			'https://www.fool.com/investing/2026/08/19/moderna-and-merck-just-made-history-with-the-first-mrna-cancer-vaccine-to-succeed-in-a-phase-3-trial-heres-what-that-means-for-investors/?source=iedfolrf0000001',
		brief_tldr:
			"A plausible supplier_input channel runs from the Moderna/Merck Phase 3 mRNA cancer vaccine success to increased demand for Maravai's TriLink CleanCap and nucleotides, but the event does not state that any Maravai products were used in the trial or that Maravai will receive associated orders.",
		brief_supply_chain_md:
			"The event validates mRNA oncology, which could boost mRNA manufacturing and demand for TriLink's CleanCap capping analogs and modified nucleotides, creating a plausible supplier_input channel. However, the event does not state that Maravai's products were used in this specific trial or that any new orders for Maravai have been placed. Any revenue benefit is conditional on that link.",
		brief_bear_summary_md:
			"FCFF yield of 1.3% (20th percentile) indicates weak cash-flow generation relative to price. P/S of 5.8 and EV/Rev of 6.2 are elevated. Fundamentals are stale (174 days since last filing). The trial success could benefit competitors or other suppliers, and Maravai's revenue may not increase if mRNA demand does not materialize.",
		brief_catalyst_failure_exit:
			"If Maravai's revenue from mRNA-related products does not increase in the following quarters, the suggested channel is invalidated.",
		brief_causal_support: 'suggestive',
		brief_channel_grounding: 'grounded',
		brief_support_guard_status: 'not_applicable',
		channel_type: 'supplier_input',
		channel_text:
			"The success of Moderna and Merck's mRNA cancer vaccine in Phase 3 trial validates the mRNA oncology platform, leading to increased investment and demand for mRNA-based therapies, which in turn increases demand for mRNA manufacturing inputs such as CleanCap capping analogs and modified nucleotides supplied by Maravai's TriLink, potentially boosting Maravai's revenue in the near future.",
		channel_evidence:
			'First mRNA Cancer Vaccine to Succeed in a Phase 3 Trial; validation of the mRNA oncology platform',
		channel_falsifier:
			"If Maravai's revenue from mRNA-related products does not increase in the following quarters, or if the trial success does not lead to increased mRNA manufacturing demand.",
		channel_grounding_quote: 'validation of the mRNA oncology platform',
		channel_grounding_reason: ''
	};

	// RDN — not_established x THEME_MISROUTE. A pipeline routing defect: a
	// financing story was filed under retail_sales. The card must flag it and say
	// whose fault it is, so the reader discounts this row rather than the tool.
	// No chain and no quoted span, so no drawer is offered — everything the
	// assessor produced fits on the face.
	const candidateRDN: Candidate = {
		...candidateFOUR,
		date: '2026-08-19',
		ticker: 'RDN',
		company_name: 'Radian Group Inc.',
		theme: 'retail_sales',
		catalyst_event_type: 'macro',
		catalyst_strength: 0.62,
		source_event_title: 'Broadcom falls sharply after investors question financing plans',
		source_event_published_at: '2026-08-14',
		brief_tldr:
			"RDN surfaced from Broadcom's sharp decline on financing concerns; no company-specific cash-flow path from this event to Radian was established.",
		brief_supply_chain_md:
			"The event describes Broadcom's stock drop due to investor questions about financing plans. Radian Group provides mortgage insurance, with no direct operational or financial link to Broadcom or semiconductor supply chains. No mechanism connects Broadcom's financing issues to Radian's mortgage insurance volume or premiums.",
		brief_bear_summary_md:
			'Bear risks: low FCFF yield (3.1%, sector 25th percentile) and zero insider buying over 180 days suggest limited internal conviction; technical weakness (RSI 41, price -2.4% below MA50) reflects near-term momentum lag.',
		brief_catalyst_failure_exit:
			"Broadcom's financing concerns are resolved or proven unfounded, removing the event that triggered this screening; no RDN-specific catalyst exit is applicable.",
		brief_causal_support: 'not_established',
		brief_channel_grounding: 'theme_misroute',
		brief_support_guard_status: 'clean',
		channel_type: 'none',
		channel_text: '',
		channel_evidence: '',
		channel_falsifier: '',
		channel_grounding_quote: '',
		channel_grounding_reason: "The event is about Broadcom's financing plans, not retail sales."
	};

	// CRSP — not_established x CANDIDATE_MISFIT. The event is on-theme; this
	// company simply is not in it. A stage-A proposal defect, and a different fix
	// from a misroute, which is why the two never share a word.
	const candidateCRSP: Candidate = {
		...candidateFOUR,
		date: '2026-08-19',
		ticker: 'CRSP',
		company_name: 'CRISPR Therapeutics AG',
		theme: 'melanoma',
		catalyst_event_type: 'other',
		catalyst_strength: 0.54,
		source_event_title:
			'mRNA cancer vaccine succeeded in Phase 3 melanoma trial, Moderna and Merck say',
		source_event_published_at: '2026-08-19',
		brief_tldr:
			'CRISPR Therapeutics surfaced from an mRNA cancer vaccine Phase 3 success for melanoma; no company-specific cash-flow path from that event to this company was established.',
		brief_supply_chain_md:
			"The event describes a competitor's vaccine trial result; CRISPR Therapeutics develops gene-edited cell therapies, not mRNA vaccines. No operational or financial link is stated in the event or grounding. The pairing rests on the theme tag 'melanoma' alone.",
		brief_bear_summary_md:
			'P/S 1647.6 and EV/Rev 1548.6 are extreme valuation multiples with no FCF margin reported. Fundamentals are 188 days stale; RSI at 70 signals overbought while MA200 slope is negative. No insider buys in 180 days.',
		brief_catalyst_failure_exit:
			'No company-specific mechanism exists; the event-level condition is whether mRNA cancer vaccines broadly lift immuno-oncology sentiment, but no CRISPR product or cash-flow link is named.',
		brief_causal_support: 'not_established',
		brief_channel_grounding: 'candidate_misfit',
		brief_support_guard_status: 'clean',
		channel_type: 'none',
		channel_text: '',
		channel_evidence: '',
		channel_falsifier: '',
		channel_grounding_quote: '',
		channel_grounding_reason:
			'Event is about an mRNA cancer vaccine trial for melanoma; CRISPR Therapeutics develops gene-edited cell therapies, not mRNA vaccines, and is not mentioned or implied.'
	};

	// Assessor OUTAGE. No such row exists in the recorded golden day, so this is
	// the ONE derived state here: the real CRSP row with only the two status
	// tokens swapped for the literals the orchestrator substitutes when no draw
	// survives. Its prose is therefore still CRSP's real prose and describes a
	// verdict that WAS reached — read this story for the record block alone.
	// Kept distinct from not_established on purpose: "nothing answered" and "the
	// answer was weak" must never render the same way.
	const candidateNoRecord: Candidate = {
		...candidateCRSP,
		brief_causal_support: 'no_record',
		brief_channel_grounding: 'unknown',
		brief_support_guard_status: 'clean',
		channel_type: '',
		channel_grounding_reason: ''
	};

	const { Story } = defineMeta({
		title: 'Composites/CandidateCard',
		component: CandidateCard,
		tags: ['autodocs'],
		parameters: { layout: 'padded' }
	});
</script>

<!-- Story 1: FOUR — rank 1, expert panel present, atr_penalty chip, NO_STRUCTURE trade setup -->
<Story
	name="FOUR With Expert Panel and ATR Penalty"
	play={async ({ canvas }) => {
		await waitFor(() => expect(canvas.getByText('FOUR')).toBeVisible());
		await waitFor(() => expect(canvas.getByText('Shift4 Payments, Inc.')).toBeVisible());
	}}
>
	{#snippet template()}
		<div style="width: 56rem; padding: 2rem;">
			<CandidateCard candidate={candidateFOUR} index={0} />
		</div>
	{/snippet}
</Story>

<!-- Story 2: PIPR — rank 3, no expert panel, no atr_penalty, trade setup OK with full entry ladder -->
<Story
	name="PIPR With Trade Setup OK"
	play={async ({ canvas }) => {
		await waitFor(() => expect(canvas.getByText('PIPR')).toBeVisible());
		await waitFor(() => expect(canvas.getByText('Piper Sandler Companies')).toBeVisible());
	}}
>
	{#snippet template()}
		<div style="width: 56rem; padding: 2rem;">
			<CandidateCard candidate={candidatePIPR} index={2} />
		</div>
	{/snippet}
</Story>

<!-- Story 3: empty narrative — the three bottom sections must show "—", not blank bodies -->
<Story
	name="Empty Narrative Placeholders"
	play={async ({ canvas }) => {
		await waitFor(() => expect(canvas.getByText('supply.chain')).toBeVisible());
		// All three narrative bodies fall back to the em-dash placeholder.
		await waitFor(() => expect(canvas.getAllByText('—').length).toBeGreaterThanOrEqual(3));
	}}
>
	{#snippet template()}
		<div style="width: 56rem; padding: 2rem;">
			<CandidateCard candidate={candidateEmptyNarrative} index={0} />
		</div>
	{/snippet}
</Story>

<!-- Story 4: empty display fields — rationale/source-event/sector fallbacks (follow-up to #869) -->
<Story
	name="Empty Display Field Fallbacks"
	play={async ({ canvas }) => {
		await waitFor(() => expect(canvas.getByText('FOUR')).toBeVisible());
		// rationale + sector/industry both fall back to the em-dash.
		await waitFor(() => expect(canvas.getAllByText('—').length).toBeGreaterThanOrEqual(2));
		// With null source_event_url the catalyst link is gated out entirely —
		// no dead hrefless "source event" link renders.
		expect(canvas.queryByRole('link', { name: /source event/i })).toBeNull();
	}}
>
	{#snippet template()}
		<div style="width: 56rem; padding: 2rem;">
			<CandidateCard candidate={candidateEmptyDisplayFields} index={0} />
		</div>
	{/snippet}
</Story>

<!-- Story 5: PAR — brief_status "unavailable": the muted tone-neutral label renders above the italic rationale fallback -->
<Story
	name="Brief Unavailable"
	play={async ({ canvas }) => {
		await waitFor(() =>
			expect(canvas.getByText('brief unavailable — generation failed (truncated)')).toBeVisible()
		);
		// The italic screener-rationale fallback stays rendered below the label.
		await waitFor(() =>
			expect(canvas.getByText(/Punchh division is a leading provider/)).toBeVisible()
		);
	}}
>
	{#snippet template()}
		<div style="width: 56rem; padding: 2rem;">
			<CandidateCard candidate={candidatePARBriefUnavailable} index={15} />
		</div>
	{/snippet}
</Story>

<!-- Story 6: PAR — the support guard withheld the prose. Distinct label, and NO rationale fallback. -->
<Story
	name="Prose Withheld"
	play={async ({ canvas }) => {
		await waitFor(() =>
			expect(
				canvas.getByText(
					'brief withheld — the wording asserted support the evidence record does not carry'
				)
			).toBeVisible()
		);
		// The unguarded stage-A rationale must NOT take the vacated slot.
		expect(canvas.queryByText(/Punchh division is a leading provider/)).toBeNull();
	}}
>
	{#snippet template()}
		<div style="width: 56rem; padding: 2rem;">
			<CandidateCard candidate={candidatePARProseWithheld} index={15} />
		</div>
	{/snippet}
</Story>

<!-- Story 7: MRVI — suggestive x grounded. The COMMON case: present but quiet.
     No amber, no alarm — the measurement worked and reported a conditional link. -->
<Story
	name="Channel Record Grounded"
	play={async ({ canvas }) => {
		await waitFor(() => expect(canvas.getByText('evidence')).toBeVisible());
		// The verb is what separates this level from the one below it — a reader
		// must not have to decode a colour to tell them apart.
		await waitFor(() =>
			expect(canvas.getByText('the event implies a link to this company')).toBeVisible()
		);
		// Nothing about a sound measurement is flagged.
		expect(canvas.queryByText(/does not concern this theme/)).toBeNull();
		expect(canvas.queryByText(/withheld/)).toBeNull();
	}}
>
	{#snippet template()}
		<div style="width: 56rem; padding: 2rem;">
			<CandidateCard candidate={candidateMRVI} index={0} />
		</div>
	{/snippet}
</Story>

<!-- Story 8: RDN — theme_misroute. Flagged, and named as an upstream routing
     defect so the reader discounts the row rather than the whole tool. -->
<Story
	name="Channel Record Theme Misroute"
	play={async ({ canvas }) => {
		await waitFor(() =>
			expect(canvas.getByText('the event does not state a link to this company')).toBeVisible()
		);
		await waitFor(() =>
			expect(
				canvas.getByText('the event does not concern this theme — a routing defect upstream')
			).toBeVisible()
		);
	}}
>
	{#snippet template()}
		<div style="width: 56rem; padding: 2rem;">
			<CandidateCard candidate={candidateRDN} index={0} />
		</div>
	{/snippet}
</Story>

<!-- Story 9: CRSP — candidate_misfit. On-theme event, wrong company: a stage-A
     proposal defect, worded differently from a misroute because the fix differs. -->
<Story
	name="Channel Record Candidate Misfit"
	play={async ({ canvas }) => {
		await waitFor(() =>
			expect(canvas.getByText('the event does not involve this company')).toBeVisible()
		);
	}}
>
	{#snippet template()}
		<div style="width: 56rem; padding: 2rem;">
			<CandidateCard candidate={candidateCRSP} index={0} />
		</div>
	{/snippet}
</Story>

<!-- Story 10: assessor outage. "Not assessed" must never read as "assessed and
     found weak" — the whole point of keeping no_record out of the level scale. -->
<Story
	name="Channel Record Not Assessed"
	play={async ({ canvas }) => {
		await waitFor(() => expect(canvas.getByText('the link was not assessed')).toBeVisible());
		await waitFor(() => expect(canvas.getByText('grounding was not assessed')).toBeVisible());
		// It must NOT borrow the wording of a real verdict.
		expect(canvas.queryByText('the event does not state a link to this company')).toBeNull();
	}}
>
	{#snippet template()}
		<div style="width: 56rem; padding: 2rem;">
			<CandidateCard candidate={candidateNoRecord} index={0} />
		</div>
	{/snippet}
</Story>
