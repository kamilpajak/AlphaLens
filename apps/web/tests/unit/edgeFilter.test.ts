import { describe, it, expect } from 'vitest';
import {
	emptyFilterState,
	isFilterActive,
	filterOutcomes,
	filterToParams,
	filterFromParams,
	type EdgeFilterState
} from '$lib/edgeFilter';
import type { EdgeOutcome } from '$lib/types';

// Pure client-side filtering behind the /edge toolbar: the text+facet predicate
// and the deep-linkable URL round-trip. (Generic facet derivation moved to
// `$lib/faceting` — see tests/unit/faceting.test.ts.)

function row(over: Partial<EdgeOutcome>): EdgeOutcome {
	return {
		ticker: 'AAA',
		brief_date: '2026-05-18',
		matured_at: '2026-05-29',
		theme: 'high-gas',
		ladder_classification: 'TP_FULL',
		terminal: true,
		realized_r: 1,
		open_r: null,
		market_excess_return: 0.1,
		forward_return: 0.05,
		benchmark_window_return: 0.02,
		holding_days_elapsed: 10,
		realized_return_pct_of_book: 0.15,
		scorer_config_version: 'v1',
		...over
	} as EdgeOutcome;
}

const ROWS: EdgeOutcome[] = [
	row({ ticker: 'AMPL', theme: 'high-gas', ladder_classification: 'TP_FULL', scorer_config_version: 'v1' }),
	row({ ticker: 'SNAP', theme: 'ai-infra', ladder_classification: 'SL_HIT', scorer_config_version: 'v1' }),
	row({ ticker: 'NVDA', theme: 'ai-infra', ladder_classification: 'TIME_STOP', scorer_config_version: 'v2' }),
	row({ ticker: 'PLUG', theme: 'high-gas', ladder_classification: 'SL_HIT', scorer_config_version: 'v2' })
];

describe('isFilterActive', () => {
	it('is false for the empty state and true once any dimension is set', () => {
		expect(isFilterActive(emptyFilterState())).toBe(false);
		expect(isFilterActive({ ...emptyFilterState(), query: 'nv' })).toBe(true);
		expect(isFilterActive({ ...emptyFilterState(), classes: new Set(['SL_HIT']) })).toBe(true);
		expect(isFilterActive({ ...emptyFilterState(), cohorts: new Set(['v2']) })).toBe(true);
		// whitespace-only query does not count as active
		expect(isFilterActive({ ...emptyFilterState(), query: '   ' })).toBe(false);
	});
});

describe('filterOutcomes', () => {
	it('empty state returns every row', () => {
		expect(filterOutcomes(ROWS, emptyFilterState())).toHaveLength(4);
	});

	it('query matches ticker OR theme, case-insensitively', () => {
		expect(filterOutcomes(ROWS, { ...emptyFilterState(), query: 'nv' }).map((r) => r.ticker)).toEqual([
			'NVDA'
		]);
		// theme substring hits both high-gas rows
		expect(
			filterOutcomes(ROWS, { ...emptyFilterState(), query: 'HIGH-GAS' }).map((r) => r.ticker).sort()
		).toEqual(['AMPL', 'PLUG']);
	});

	it('classes facet is a union within itself', () => {
		const s: EdgeFilterState = { ...emptyFilterState(), classes: new Set(['SL_HIT', 'TIME_STOP']) };
		expect(filterOutcomes(ROWS, s).map((r) => r.ticker).sort()).toEqual(['NVDA', 'PLUG', 'SNAP']);
	});

	it('facets intersect across dimensions (class AND cohort AND query)', () => {
		const s: EdgeFilterState = {
			query: 'ai-infra',
			classes: new Set(['SL_HIT']),
			cohorts: new Set(['v1'])
		};
		// ai-infra → SNAP, NVDA; SL_HIT → SNAP, PLUG; v1 → SNAP, AMPL → intersection SNAP
		expect(filterOutcomes(ROWS, s).map((r) => r.ticker)).toEqual(['SNAP']);
	});

	it('treats an empty (pending) classification as the empty bucket (never matches a real code)', () => {
		const withPending = [...ROWS, row({ ticker: 'ZZZ', ladder_classification: '' })];
		expect(
			filterOutcomes(withPending, { ...emptyFilterState(), classes: new Set(['TP_FULL']) }).map(
				(r) => r.ticker
			)
		).toEqual(['AMPL']);
	});
});

describe('URL round-trip', () => {
	it('serializes active dimensions and omits empty ones', () => {
		const p = filterToParams({ query: 'nv', classes: new Set(['b', 'a']), cohorts: new Set() });
		expect(p.get('q')).toBe('nv');
		expect(p.get('class')).toBe('a,b'); // sorted regardless of insertion order
		expect(p.has('cohort')).toBe(false);
	});

	it('preserves unrelated params on the passed-in URLSearchParams', () => {
		const into = new URLSearchParams('page=2');
		const p = filterToParams({ ...emptyFilterState(), query: 'x' }, into);
		expect(p.get('page')).toBe('2');
		expect(p.get('q')).toBe('x');
	});

	it('round-trips through params', () => {
		const s: EdgeFilterState = { query: 'ai', classes: new Set(['SL_HIT']), cohorts: new Set(['v2']) };
		const back = filterFromParams(filterToParams(s));
		expect(back.query).toBe('ai');
		expect([...back.classes]).toEqual(['SL_HIT']);
		expect([...back.cohorts]).toEqual(['v2']);
	});

	it('deletes a stale param when the dimension is cleared', () => {
		const into = new URLSearchParams('q=old&class=SL_HIT');
		const p = filterToParams(emptyFilterState(), into);
		expect(p.has('q')).toBe(false);
		expect(p.has('class')).toBe(false);
	});
});
