import type { DayIndexEntry } from './types';
import { facetMatches } from './faceting';

// Client-side filtering for the /briefs archive table. Pure + framework-free so
// the predicate is unit-testable in isolation. Reuses the shared facet
// membership from `$lib/faceting`; the generic set/URL helpers live in
// `$lib/urlFilters`. The days list is bounded (API caps at 200), so filtering
// the whole set client-side is cheap.

export interface BriefsFilterState {
	/** Free-text query, matched (case-insensitive substring) against date + top theme. */
	query: string;
	/** Selected `top_theme` values; empty = all. */
	themes: Set<string>;
}

export function emptyBriefsFilter(): BriefsFilterState {
	return { query: '', themes: new Set() };
}

/** Any dimension narrowing the list (drives the clear affordance + count line). */
export function isBriefsFilterActive(s: BriefsFilterState): boolean {
	return s.query.trim() !== '' || s.themes.size > 0;
}

/** Apply the text query + the top-theme facet. Query matches date OR top theme;
 *  the theme facet is a union within itself. An empty facet imposes no
 *  constraint. */
export function filterDays(days: DayIndexEntry[], s: BriefsFilterState): DayIndexEntry[] {
	const q = s.query.trim().toLowerCase();
	return days.filter((d) => {
		if (q) {
			const hay = `${d.date} ${d.top_theme ?? ''}`.toLowerCase();
			if (!hay.includes(q)) return false;
		}
		if (!facetMatches(s.themes, d.top_theme)) return false;
		return true;
	});
}
