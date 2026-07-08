import { describe, it, expect } from 'vitest';
import {
	facetValue,
	deriveFacet,
	facetMatches,
	buildFilterChips,
	type FacetOption
} from '$lib/faceting';

// The shared faceted-filter core used by the /edge toolbar and the /experiments
// ledgers: facet-count derivation, single-facet membership, and the
// LedgerFilterBar chip builder.

describe('facetValue', () => {
	it('maps null/undefined to the empty bucket, passes strings through', () => {
		expect(facetValue(null)).toBe('');
		expect(facetValue(undefined)).toBe('');
		expect(facetValue('SL_HIT')).toBe('SL_HIT');
	});
});

describe('deriveFacet', () => {
	const rows = [
		{ s: 'a' },
		{ s: 'b' },
		{ s: 'a' },
		{ s: 'a' },
		{ s: 'b' },
		{ s: null }
	];

	it('counts distinct values in descending-count then key order (generic over T)', () => {
		expect(deriveFacet(rows, (r) => r.s)).toEqual([
			{ key: 'a', count: 3 },
			{ key: 'b', count: 2 }
		]);
	});

	it('drops the null/empty bucket so a missing value contributes no chip', () => {
		expect(deriveFacet([{ s: null }, { s: '' }, { s: 'x' }], (r) => r.s)).toEqual([
			{ key: 'x', count: 1 }
		]);
	});

	it('breaks count ties by key (deterministic)', () => {
		expect(deriveFacet([{ s: 'z' }, { s: 'a' }], (r) => r.s)).toEqual([
			{ key: 'a', count: 1 },
			{ key: 'z', count: 1 }
		]);
	});
});

describe('facetMatches', () => {
	it('empty selection imposes no constraint (all pass)', () => {
		expect(facetMatches(new Set(), 'anything')).toBe(true);
		expect(facetMatches(new Set(), null)).toBe(true);
	});
	it('non-empty selection requires membership (null → empty bucket)', () => {
		expect(facetMatches(new Set(['a', 'b']), 'a')).toBe(true);
		expect(facetMatches(new Set(['a', 'b']), 'c')).toBe(false);
		expect(facetMatches(new Set(['a']), null)).toBe(false);
	});
});

describe('buildFilterChips', () => {
	const facet: FacetOption[] = [
		{ key: 'SL_HIT', count: 3 },
		{ key: 'TP_FULL', count: 2 }
	];
	const cfg = {
		all: { count: 5 },
		label: (k: string) => k.toLowerCase(),
		tone: (k: string) => `tone-${k}`,
		def: (k: string) => `def ${k}`
	};

	it('prepends an ALL chip and maps each option in the given order', () => {
		expect(buildFilterChips(facet, cfg)).toEqual([
			{ key: 'ALL', label: 'all', count: 5, tone: 'text-fg border-fg-muted', def: 'Show all.' },
			{ key: 'SL_HIT', label: 'sl_hit', count: 3, tone: 'tone-SL_HIT', def: 'def SL_HIT' },
			{ key: 'TP_FULL', label: 'tp_full', count: 2, tone: 'tone-TP_FULL', def: 'def TP_FULL' }
		]);
	});

	it('honors ALL-chip overrides (key/label/def/tone)', () => {
		const chips = buildFilterChips([], {
			...cfg,
			all: { count: 9, key: 'ALL', label: 'all', def: 'every row', tone: 'text-cyan border-cyan' }
		});
		expect(chips).toEqual([
			{ key: 'ALL', label: 'all', count: 9, tone: 'text-cyan border-cyan', def: 'every row' }
		]);
	});

	it('preserves the facet order it is given (does not re-sort)', () => {
		const ordered: FacetOption[] = [
			{ key: 'FAIL', count: 1 },
			{ key: 'INCONCLUSIVE', count: 9 }
		];
		expect(buildFilterChips(ordered, cfg).map((c) => c.key)).toEqual([
			'ALL',
			'FAIL',
			'INCONCLUSIVE'
		]);
	});
});
