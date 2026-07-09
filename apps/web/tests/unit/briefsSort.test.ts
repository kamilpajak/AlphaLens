import { describe, it, expect } from 'vitest';
import { sortDays, defaultDir } from '$lib/briefsSort';
import type { DayIndexEntry } from '$lib/types';

// Pure client-side sort for the /briefs archive table: per-column comparators,
// nulls-last, stable date-desc tiebreak.

function day(over: Partial<DayIndexEntry>): DayIndexEntry {
	return { date: '2026-05-18', n_candidates: 5, n_themes: 3, top_theme: 'ai-infra', ...over };
}

const DAYS: DayIndexEntry[] = [
	day({ date: '2026-05-18', n_candidates: 5, n_themes: 3, top_theme: 'retail' }),
	day({ date: '2026-06-01', n_candidates: 9, n_themes: 1, top_theme: 'ai-infra' }),
	day({ date: '2026-05-20', n_candidates: 2, n_themes: 5, top_theme: null })
];

const dates = (rows: DayIndexEntry[]) => rows.map((d) => d.date);

describe('defaultDir', () => {
	it('dates + counts default descending, text ascending', () => {
		expect(defaultDir('date')).toBe('desc');
		expect(defaultDir('cand')).toBe('desc');
		expect(defaultDir('themes')).toBe('desc');
		expect(defaultDir('top')).toBe('asc');
	});
});

describe('sortDays', () => {
	it('does not mutate the input array', () => {
		const copy = [...DAYS];
		sortDays(DAYS, 'cand', 'desc');
		expect(DAYS).toEqual(copy);
	});

	it('sorts by date (chronological via lexicographic)', () => {
		expect(dates(sortDays(DAYS, 'date', 'asc'))).toEqual(['2026-05-18', '2026-05-20', '2026-06-01']);
		expect(dates(sortDays(DAYS, 'date', 'desc'))).toEqual(['2026-06-01', '2026-05-20', '2026-05-18']);
	});

	it('sorts by candidate count', () => {
		expect(sortDays(DAYS, 'cand', 'desc').map((d) => d.n_candidates)).toEqual([9, 5, 2]);
		expect(sortDays(DAYS, 'cand', 'asc').map((d) => d.n_candidates)).toEqual([2, 5, 9]);
	});

	it('sorts by theme count', () => {
		expect(sortDays(DAYS, 'themes', 'desc').map((d) => d.n_themes)).toEqual([5, 3, 1]);
	});

	it('sorts by top theme with a null top_theme forced last in BOTH directions', () => {
		expect(dates(sortDays(DAYS, 'top', 'asc'))).toEqual(['2026-06-01', '2026-05-18', '2026-05-20']);
		// desc reverses the non-null order but the null day stays last.
		expect(dates(sortDays(DAYS, 'top', 'desc'))).toEqual(['2026-05-18', '2026-06-01', '2026-05-20']);
	});

	it('breaks ties by date desc (deterministic)', () => {
		const tied = [
			day({ date: '2026-05-18', n_candidates: 4 }),
			day({ date: '2026-06-09', n_candidates: 4 }),
			day({ date: '2026-05-30', n_candidates: 4 })
		];
		expect(dates(sortDays(tied, 'cand', 'desc'))).toEqual(['2026-06-09', '2026-05-30', '2026-05-18']);
	});
});
