import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import yaml from 'js-yaml';
import { describe, expect, it } from 'vitest';
import type { Candidate } from '../../src/lib/types';

// Contract test: the hand-maintained `Candidate` interface in $lib/types is the
// single biggest unvalidated surface in the SPA (~65 fields, consumed via an
// `as DayBrief` cast). The backend's drf-spectacular schema is the source of
// truth. We pin the expected key set so a backend rename/drop surfaces here as
// a test failure instead of an `undefined` at runtime in the UI.
//
// `openapi/schema.yaml` is regenerated from the Django app via
// `manage.py spectacular` (see apps/web/README contract-codegen note). When a
// field legitimately changes, regenerate the schema + the TS type
// (`pnpm run gen:api-types`) and update KNOWN_TS_ONLY / KNOWN_SCHEMA_ONLY below
// with a one-line reason.

const __dirname = dirname(fileURLToPath(import.meta.url));
const SCHEMA_PATH = resolve(__dirname, '../../openapi/schema.yaml');

interface OpenApiSchema {
	components: { schemas: Record<string, { properties?: Record<string, unknown> }> };
}

function schemaCandidateKeys(): Set<string> {
	const doc = yaml.load(readFileSync(SCHEMA_PATH, 'utf-8')) as OpenApiSchema;
	const cand = doc.components.schemas.Candidate;
	expect(cand, 'schema.yaml must declare a Candidate component').toBeDefined();
	return new Set(Object.keys(cand.properties ?? {}));
}

// The TS `Candidate` interface keys, kept in sync manually by listing them.
// A drift between this list and the actual interface is caught by the
// `satisfies` type assertion below — adding/removing a key on the interface
// without updating this array is a compile error under `svelte-check`.
const TS_CANDIDATE_KEYS = [
	'date',
	'theme',
	'ticker',
	'company_name',
	'rationale',
	'llm_confidence',
	'market_cap',
	'gates_passed',
	'n_gates_passed',
	'gates_failed',
	'n_gates_failed',
	'gates_unknown',
	'n_gates_unknown',
	'verified',
	'source_event_url',
	'source_event_title',
	'source_event_published_at',
	'theme_search_keywords',
	'industry_id',
	'industry_name',
	'sector_name',
	'peer_cohort_level',
	'insider_score_usd',
	'insider_score_sector_percentile',
	'fcff_yield_pct',
	'fcff_yield_sector_percentile',
	'valuation_pe',
	'valuation_ps',
	'valuation_ev_rev',
	'valuation_ev_ebitda',
	'valuation_fcf_margin',
	'valuation_composite_sector_percentile',
	'valuation_financials_publish_date',
	'valuation_financials_age_days',
	'roic_pct',
	'roe_pct',
	'magic_formula_health_pass',
	'technical_rsi',
	'technical_ma50_distance_pct',
	'technical_atr_pct',
	'technical_volume_zscore',
	'technical_pct_off_52w_high',
	'technical_pct_off_52w_low',
	'technical_ma200_distance_pct',
	'technical_ma200_slope_pct_per_day',
	'catalyst_strength',
	'catalyst_event_type',
	'catalyst_confidence',
	'magic_formula_rank',
	'magic_formula_cohort_n',
	'deep_drawdown_reversal',
	'layer4_weighted_score',
	'buffett_owner_earnings_yield_pct',
	'buffett_roic_latest',
	'buffett_roic_3y_avg',
	'buffett_margin_of_safety_pct',
	'buffett_data_coverage',
	'buffett_quality_score',
	'buffett_moat_type',
	'buffett_moat_trend',
	'buffett_management_candor',
	'buffett_understandable',
	'buffett_qualitative_rationale',
	'buffett_used_scuttlebutt',
	'buffett_qual_computed_at',
	'also_in_themes',
	'rank_in_day',
	'cohort_size_in_day',
	'next_earnings_date',
	'brief_model_used',
	'brief_tldr',
	'brief_supply_chain_md',
	'brief_bear_summary_md',
	'brief_catalyst_failure_exit',
	'brief_trade_setup',
	'brief_template_id',
	'brief_template_facts',
	'brief_generated_at'
] as const satisfies readonly (keyof Candidate)[];

// Exhaustiveness guard: if the interface gains a key not listed above, this
// mapped-type assignment fails to compile (every Candidate key must appear in
// the union). Keeps the array honest without a runtime reflection trick.
type _MissingFromList = Exclude<keyof Candidate, (typeof TS_CANDIDATE_KEYS)[number]>;
const _exhaustive: _MissingFromList extends never ? true : never = true;
void _exhaustive;

describe('Candidate contract vs OpenAPI schema', () => {
	it('TS interface and schema declare the identical key set', () => {
		const schemaKeys = schemaCandidateKeys();
		const tsKeys = new Set<string>(TS_CANDIDATE_KEYS);

		const tsOnly = [...tsKeys].filter((k) => !schemaKeys.has(k)).sort();
		const schemaOnly = [...schemaKeys].filter((k) => !tsKeys.has(k)).sort();

		expect(tsOnly, 'keys on the TS Candidate but not in the backend schema').toEqual([]);
		expect(schemaOnly, 'keys in the backend schema but missing from the TS Candidate').toEqual(
			[]
		);
	});
});
