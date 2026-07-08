import { describe, it, expect } from 'vitest';
import {
	emptyBriefsFilter,
	isBriefsFilterActive,
	filterDays,
	type BriefsFilterState
} from '$lib/briefsFilter';
import type { DayIndexEntry } from '$lib/types';

// Pure client-side filtering behind the /briefs archive toolbar: the text query
// (date + top theme) and the top-theme facet.

function day(over: Partial<DayIndexEntry>): DayIndexEntry {
	return { date: '2026-05-18', n_candidates: 5, n_themes: 3, top_theme: 'ai-infra', ...over };
}

const DAYS: DayIndexEntry[] = [
	day({ date: '2026-05-18', top_theme: 'ai-infra' }),
	day({ date: '2026-05-19', top_theme: 'high-gas' }),
	day({ date: '2026-06-01', top_theme: 'ai-infra' }),
	day({ date: '2026-06-02', top_theme: null })
];

describe('isBriefsFilterActive', () => {
	it('false for empty, true once any dimension is set', () => {
		expect(isBriefsFilterActive(emptyBriefsFilter())).toBe(false);
		expect(isBriefsFilterActive({ ...emptyBriefsFilter(), query: 'ai' })).toBe(true);
		expect(isBriefsFilterActive({ ...emptyBriefsFilter(), themes: new Set(['x']) })).toBe(true);
		expect(isBriefsFilterActive({ ...emptyBriefsFilter(), query: '   ' })).toBe(false);
	});
});

describe('filterDays', () => {
	it('empty state returns every day', () => {
		expect(filterDays(DAYS, emptyBriefsFilter())).toHaveLength(4);
	});

	it('query matches the date (e.g. a month prefix)', () => {
		expect(filterDays(DAYS, { ...emptyBriefsFilter(), query: '2026-06' }).map((d) => d.date)).toEqual(
			['2026-06-01', '2026-06-02']
		);
	});

	it('query matches the top theme, case-insensitively', () => {
		expect(
			filterDays(DAYS, { ...emptyBriefsFilter(), query: 'AI-INFRA' }).map((d) => d.date).sort()
		).toEqual(['2026-05-18', '2026-06-01']);
	});

	it('theme facet is a union within itself', () => {
		const s: BriefsFilterState = { query: '', themes: new Set(['ai-infra', 'high-gas']) };
		expect(filterDays(DAYS, s).map((d) => d.date).sort()).toEqual([
			'2026-05-18',
			'2026-05-19',
			'2026-06-01'
		]);
	});

	it('query AND facet intersect', () => {
		const s: BriefsFilterState = { query: '2026-06', themes: new Set(['ai-infra']) };
		expect(filterDays(DAYS, s).map((d) => d.date)).toEqual(['2026-06-01']);
	});

	it('a null top_theme is excluded when a theme is selected (empty bucket)', () => {
		expect(
			filterDays(DAYS, { ...emptyBriefsFilter(), themes: new Set(['ai-infra']) }).some(
				(d) => d.top_theme === null
			)
		).toBe(false);
	});
});
