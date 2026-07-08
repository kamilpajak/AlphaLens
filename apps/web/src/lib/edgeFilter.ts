import type { EdgeOutcome } from './types';

// Client-side filtering for the /edge outcomes table. Pure + framework-free so
// the predicate, facet derivation, and URL (de)serialization are unit-testable
// in isolation from the toolbar component and the virtualization engine. The
// table pipeline is: outcomes → terminal/ongoing → filterOutcomes → sort →
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

/** Normalize a nullable code to the empty-string bucket so null/missing values
 *  are a single, matchable facet key rather than scattered nullish holes. */
function codeOf(value: string | null | undefined): string {
	return value ?? '';
}

/** Apply the text query + the classification/cohort facet selections. Each facet
 *  is a UNION within itself (any selected class matches) and an INTERSECTION
 *  across facets (class AND cohort AND query) — the standard faceted-search
 *  semantics. An empty facet imposes no constraint. */
export function filterOutcomes(rows: EdgeOutcome[], s: EdgeFilterState): EdgeOutcome[] {
	const q = s.query.trim().toLowerCase();
	return rows.filter((o) => {
		if (q) {
			const hay = `${o.ticker} ${o.theme ?? ''}`.toLowerCase();
			if (!hay.includes(q)) return false;
		}
		if (s.classes.size > 0 && !s.classes.has(codeOf(o.ladder_classification))) return false;
		if (s.cohorts.size > 0 && !s.cohorts.has(codeOf(o.scorer_config_version))) return false;
		return true;
	});
}

export interface FacetOption {
	/** The raw value (empty string for null/missing). */
	key: string;
	count: number;
}

/** Distinct values of `pick` over `rows` with their counts, in descending-count
 *  then key order (stable, deterministic). The empty-string bucket is dropped so
 *  a facet with only missing values contributes no chip — a consequence is that
 *  rows with a missing value (e.g. a PENDING/blank classification) get no chip
 *  and so can only be excluded by a facet selection, never isolated. */
export function deriveFacet(
	rows: EdgeOutcome[],
	pick: (o: EdgeOutcome) => string | null | undefined
): FacetOption[] {
	const counts = new Map<string, number>();
	for (const o of rows) {
		const k = codeOf(pick(o));
		if (k === '') continue;
		counts.set(k, (counts.get(k) ?? 0) + 1);
	}
	return [...counts.entries()]
		.map(([key, count]) => ({ key, count }))
		.sort((a, b) => b.count - a.count || a.key.localeCompare(b.key));
}

// ── URL (de)serialization ────────────────────────────────────────────────────
// Deep-linkable filter state: `?q=`, `?class=A,B`, `?cohort=X,Y`. Sets are
// serialized sorted so the URL is stable regardless of click order.

const PARAM_QUERY = 'q';
const PARAM_CLASSES = 'class';
const PARAM_COHORTS = 'cohort';

function setToParam(set: Set<string>): string {
	return [...set].sort().join(',');
}

function paramToSet(value: string | null): Set<string> {
	if (!value) return new Set();
	return new Set(value.split(',').filter(Boolean));
}

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
