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
		market_state: 'unknown',
		market_state_atr_pct: null,
		market_state_atr_pct_q: null,
		market_state_dist200: null,
		market_state_vix: null,
		market_state_vix_decile: null,
		market_state_squeeze_on: null,
		market_state_config_version: null
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
		market_state: 'unknown',
		market_state_atr_pct: null,
		market_state_atr_pct_q: null,
		market_state_dist200: null,
		market_state_vix: null,
		market_state_vix_decile: null,
		market_state_squeeze_on: null,
		market_state_config_version: null
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
