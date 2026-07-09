/** Pure client-side sort for the /briefs archive table.
 *
 * The days list is fully loaded (API caps at 200), so sorting is client-side.
 * Rules mirror the /edge sort: NULLs always sort LAST (an em-dash top-theme must
 * never jump to the top, regardless of direction), and a stable secondary
 * tiebreaker (date desc) keeps the order deterministic. This is deliberately
 * SEPARATE from `edgeSort.ts` — that table's sort (8 keys, terminal-only column
 * visibility, per-column comparators) is genuinely different, not shared.
 */
import type { DayIndexEntry } from './types';

export type BriefsSortKey = 'date' | 'cand' | 'themes' | 'top';
export type SortDir = 'asc' | 'desc';

// Columns whose natural first-click direction is descending (dates read
// newest-first, counts read high-to-low); the text column defaults to ascending.
const DESC_FIRST: ReadonlySet<BriefsSortKey> = new Set(['date', 'cand', 'themes']);

export function defaultDir(key: BriefsSortKey): SortDir {
	return DESC_FIRST.has(key) ? 'desc' : 'asc';
}

function keyValue(day: DayIndexEntry, key: BriefsSortKey): string | number | null {
	switch (key) {
		case 'date':
			// 'YYYY-MM-DD' sorts lexicographically = chronologically.
			return day.date;
		case 'cand':
			return day.n_candidates;
		case 'themes':
			return day.n_themes;
		case 'top':
			return day.top_theme;
	}
}

/** 3-way comparator: -1 / 0 / +1 for a < b / a === b / a > b. */
function cmp(a: string | number, b: string | number): number {
	if (a < b) return -1;
	if (a > b) return 1;
	return 0;
}

/** Return a NEW array sorted by `key`/`dir`; nulls last; stable secondary order
 *  (date desc). */
export function sortDays(days: DayIndexEntry[], key: BriefsSortKey, dir: SortDir): DayIndexEntry[] {
	const sign = dir === 'asc' ? 1 : -1;
	return [...days].sort((x, y) => {
		const a = keyValue(x, key);
		const b = keyValue(y, key);
		// `== null` (not `===`) so a missing field (undefined) is also forced last.
		const aNull = a == null;
		const bNull = b == null;
		if (aNull && bNull) return secondary(x, y);
		if (aNull) return 1; // nulls last, independent of dir
		if (bNull) return -1;
		const primary = cmp(a, b) * sign;
		return primary === 0 ? secondary(x, y) : primary;
	});
}

// Stable tiebreaker: most-recent date first (dates are unique per day, so this
// fully determines ties).
function secondary(x: DayIndexEntry, y: DayIndexEntry): number {
	if (x.date === y.date) return 0;
	return x.date < y.date ? 1 : -1; // desc
}
