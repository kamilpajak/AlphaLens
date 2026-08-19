import { describe, it, expect } from 'vitest';
import {
	classFacetFromServer,
	emptyFilterState,
	isFilterActive,
	filterOutcomes,
	filterToParams,
	filterFromParams,
	windowDenominator,
	type EdgeFilterState
} from '$lib/edgeFilter';
import type { EdgeOutcome, EdgeOutcomesFacets } from '$lib/types';

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

describe('classFacetFromServer', () => {
	// The server splits facets.classification per view by the ACTUAL per-row
	// `terminal` flag (GROUP BY terminal, ladder_classification) — the SPA does
	// no view-slicing of its own anymore, it just reads the requested view's map.
	// The same class can legitimately appear in both maps with disjoint counts
	// (a NO_FILL whose 7-day entry window is still open is an ongoing row).
	const CLASSIFICATION: EdgeOutcomesFacets['classification'] = {
		terminal: { TP_FULL: 2, SL_HIT: 1, TIME_STOP: 1, BAD_GEOMETRY: 1, NO_FILL: 3 },
		ongoing: { OPEN: 2, PARTIAL_TP_OPEN: 1, NO_FILL: 1, NO_DATA: 1 }
	};

	it('terminal view is a pure passthrough of the terminal map (no cross-view leakage)', () => {
		const facet = classFacetFromServer(CLASSIFICATION, 'terminal');
		expect(facet.map((f) => f.key).sort()).toEqual([
			'BAD_GEOMETRY',
			'NO_FILL',
			'SL_HIT',
			'TIME_STOP',
			'TP_FULL'
		]);
		expect(facet.find((f) => f.key === 'TP_FULL')?.count).toBe(2);
		expect(facet.some((f) => f.key === 'OPEN')).toBe(false);
	});

	it('ongoing view is a pure passthrough of the ongoing map (no cross-view leakage)', () => {
		const facet = classFacetFromServer(CLASSIFICATION, 'ongoing');
		expect(facet.map((f) => f.key).sort()).toEqual([
			'NO_DATA',
			'NO_FILL',
			'OPEN',
			'PARTIAL_TP_OPEN'
		]);
		expect(facet.find((f) => f.key === 'OPEN')?.count).toBe(2);
		expect(facet.some((f) => f.key === 'TP_FULL')).toBe(false);
	});

	it('a class present in BOTH views carries its own disjoint per-view count', () => {
		const terminal = classFacetFromServer(CLASSIFICATION, 'terminal');
		const ongoing = classFacetFromServer(CLASSIFICATION, 'ongoing');
		expect(terminal.find((f) => f.key === 'NO_FILL')?.count).toBe(3);
		expect(ongoing.find((f) => f.key === 'NO_FILL')?.count).toBe(1);
	});

	it('orders count-desc then key, matching deriveFacet', () => {
		const facet = classFacetFromServer(
			{ terminal: { SL_HIT: 1, TP_FULL: 2, TIME_STOP: 2 }, ongoing: {} },
			'terminal'
		);
		expect(facet.map((f) => f.key)).toEqual(['TIME_STOP', 'TP_FULL', 'SL_HIT']);
	});

	it('never emits the empty-string bucket (defensive — the server already drops it)', () => {
		const facet = classFacetFromServer({ terminal: { '': 4, TP_FULL: 1 }, ongoing: {} }, 'terminal');
		expect(facet.map((f) => f.key)).toEqual(['TP_FULL']);
	});
});

describe('windowDenominator', () => {
	// The "N shown of M in window" counter's M: server-truth window population
	// (the same numbers the chips show), never the fetched/capped row count.
	const FACETS: EdgeOutcomesFacets = {
		status: { terminal: 505, ongoing: 40 },
		classification: {
			terminal: { TP_FULL: 163, SL_HIT: 139, TIME_STOP: 118, NO_FILL: 85 },
			ongoing: { OPEN: 25, NO_FILL: 11 }
		}
	};

	it('is null when facets are absent (caller falls back to rows.length)', () => {
		expect(windowDenominator(null, 'terminal', new Set())).toBeNull();
	});

	it('with no class selected it is the view status count', () => {
		expect(windowDenominator(FACETS, 'terminal', new Set())).toBe(505);
		expect(windowDenominator(FACETS, 'ongoing', new Set())).toBe(40);
	});

	it('with classes selected it sums the selected classes in THIS view', () => {
		expect(windowDenominator(FACETS, 'terminal', new Set(['TP_FULL', 'SL_HIT']))).toBe(302);
		expect(windowDenominator(FACETS, 'ongoing', new Set(['NO_FILL']))).toBe(11);
	});

	it('a stale cross-view selection contributes 0, not NaN (selection persists across the view toggle)', () => {
		// OPEN exists only in the ongoing map; toggling to terminal with it still
		// selected must not poison the sum.
		expect(windowDenominator(FACETS, 'terminal', new Set(['OPEN']))).toBe(0);
		expect(windowDenominator(FACETS, 'terminal', new Set(['OPEN', 'TP_FULL']))).toBe(163);
	});

	it('an unknown selected class contributes 0', () => {
		expect(windowDenominator(FACETS, 'terminal', new Set(['BRAND_NEW']))).toBe(0);
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
