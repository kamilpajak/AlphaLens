import type { EdgeOutcome, EdgeOutcomesFacets } from './types';
import { facetMatches, type FacetOption } from './faceting';
import { setToParam, paramToSet } from './urlFilters';

// Client-side filtering for the /edge outcomes table. Pure + framework-free so
// the predicate and URL (de)serialization are unit-testable in isolation from
// the toolbar component and the virtualization engine. The generic facet
// primitives (deriveFacet / facetMatches / buildFilterChips) live in
// `$lib/faceting`; this module is the /edge-specific predicate + URL round-trip.
// The table pipeline is: outcomes → terminal/ongoing → filterOutcomes → sort →
// virtual window.

export interface EdgeFilterState {
	/** Free-text query, matched (case-insensitive substring) against ticker + theme. */
	query: string;
	/** Selected `ladder_classification` codes; empty = all. */
	classes: Set<string>;
	/** Selected `scorer_config_version` cohorts; empty = all. */
	cohorts: Set<string>;
}

export function emptyFilterState(): EdgeFilterState {
	return { query: '', classes: new Set(), cohorts: new Set() };
}

/** Any dimension narrowing the list (drives the "clear" affordance + count line). */
export function isFilterActive(s: EdgeFilterState): boolean {
	return s.query.trim() !== '' || s.classes.size > 0 || s.cohorts.size > 0;
}

/** Apply the text query + the classification/cohort facet selections. Each facet
 *  is a UNION within itself (any selected class matches, via `facetMatches`) and
 *  an INTERSECTION across facets (class AND cohort AND query) — the standard
 *  faceted-search semantics. An empty facet imposes no constraint. */
export function filterOutcomes(rows: EdgeOutcome[], s: EdgeFilterState): EdgeOutcome[] {
	const q = s.query.trim().toLowerCase();
	return rows.filter((o) => {
		if (q) {
			const hay = `${o.ticker} ${o.theme ?? ''}`.toLowerCase();
			if (!hay.includes(q)) return false;
		}
		if (!facetMatches(s.classes, o.ladder_classification)) return false;
		if (!facetMatches(s.cohorts, o.scorer_config_version)) return false;
		return true;
	});
}

/** Chip options for ONE view from the server's per-view window facets. The
 *  server groups `facets.classification` by the ACTUAL per-row `terminal` flag
 *  (the same flag the client-side view filter applies), so this is a pure
 *  passthrough of the requested view's map — no client-side guessing of a
 *  code's terminal semantics, and a new pipeline class lands in exactly the
 *  view its rows are in. The empty-string bucket is dropped defensively (the
 *  server already omits it). Ordering is count-desc then key — byte-for-byte
 *  the `deriveFacet` ordering. */
export function classFacetFromServer(
	classification: EdgeOutcomesFacets['classification'],
	view: 'terminal' | 'ongoing'
): FacetOption[] {
	return Object.entries(classification[view])
		.filter(([key]) => key !== '')
		.map(([key, count]) => ({ key, count }))
		.sort((a, b) => b.count - a.count || a.key.localeCompare(b.key));
}

/** Server-truth denominator M for the toolbar's "N shown of M in window"
 *  counter: the selected classes' window counts in THIS view, or the whole
 *  view population when no class is selected — the same numbers the chips
 *  show, honest under the listing cap. A selected class absent from this
 *  view's map contributes 0 (the selection persists across the view toggle, so
 *  a stale cross-view selection must not yield NaN). Null when the server sent
 *  no facets (older API build / degraded fetch) — the caller falls back to the
 *  fetched row count. */
export function windowDenominator(
	facets: EdgeOutcomesFacets | null,
	view: 'terminal' | 'ongoing',
	classes: ReadonlySet<string>
): number | null {
	if (!facets) return null;
	if (classes.size === 0) return facets.status[view];
	let sum = 0;
	for (const k of classes) sum += facets.classification[view][k] ?? 0;
	return sum;
}

// ── URL (de)serialization ────────────────────────────────────────────────────
// Deep-linkable filter state: `?q=`, `?class=A,B`, `?cohort=X,Y`. Sets are
// serialized sorted so the URL is stable regardless of click order.

const PARAM_QUERY = 'q';
const PARAM_CLASSES = 'class';
const PARAM_COHORTS = 'cohort';

/** Write the active dimensions into a URLSearchParams (omitting empty ones so a
 *  cleared filter yields a clean URL). Mutates + returns `into` when given, so
 *  callers can preserve unrelated params. */
export function filterToParams(s: EdgeFilterState, into?: URLSearchParams): URLSearchParams {
	const p = into ?? new URLSearchParams();
	const q = s.query.trim();
	if (q) p.set(PARAM_QUERY, q);
	else p.delete(PARAM_QUERY);
	if (s.classes.size > 0) p.set(PARAM_CLASSES, setToParam(s.classes));
	else p.delete(PARAM_CLASSES);
	if (s.cohorts.size > 0) p.set(PARAM_COHORTS, setToParam(s.cohorts));
	else p.delete(PARAM_COHORTS);
	return p;
}

export function filterFromParams(p: URLSearchParams): EdgeFilterState {
	return {
		query: p.get(PARAM_QUERY) ?? '',
		classes: paramToSet(p.get(PARAM_CLASSES)),
		cohorts: paramToSet(p.get(PARAM_COHORTS))
	};
}
