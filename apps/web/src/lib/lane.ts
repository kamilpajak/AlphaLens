// Candidate source lanes (epic #1293, #1298) — pure, framework-free. A brief
// row comes from the thematic pipeline, from the insider-cluster event lane,
// or from BOTH (a thematic row whose ticker also completed a cluster that day:
// one card, thematic catalyst primary, `event_overlap` set — merge.py keeps it
// in both cohorts downstream). The day-list `source` filter and the card chip
// are lane MEMBERSHIP, not the raw `source` string, so an overlap row stays
// reachable from the insider-cluster chip. Shared with the /edge outcomes
// toolbar for the lane label so the two surfaces cannot drift.
//
// Display-only: nothing here feeds ordering or selection.

import type { FacetOption } from './faceting';
import { fmtUsdCompact } from './format';

export const LANE_THEMATIC = 'thematic';
export const LANE_INSIDER_CLUSTER = 'insider_cluster';

/** The two fields lane membership reads. Both optional so the helper is safe
 *  against a row served by an API that predates the lane (deploy window). */
export interface LaneFields {
	source?: string | null;
	event_overlap?: boolean | null;
}

/** The lanes a row belongs to, thematic first. An empty / missing `source` is
 *  thematic — the same allow-list the Django edge aggregates apply
 *  (`THEMATIC_SOURCES = ("", "thematic")`). */
export function candidateLanes(c: LaneFields): string[] {
	const source = c.source || LANE_THEMATIC;
	if (source === LANE_THEMATIC) {
		return c.event_overlap ? [LANE_THEMATIC, LANE_INSIDER_CLUSTER] : [LANE_THEMATIC];
	}
	return [source];
}

/** Per-lane row counts over a day, count-desc then key (the `deriveFacet`
 *  ordering). An overlap row is counted in EVERY lane it belongs to, so on an
 *  overlap day the counts sum to more than the number of rows — by design. */
export function laneFacet(rows: LaneFields[]): FacetOption[] {
	const counts = new Map<string, number>();
	for (const c of rows) {
		for (const lane of candidateLanes(c)) counts.set(lane, (counts.get(lane) ?? 0) + 1);
	}
	return [...counts.entries()]
		.map(([key, count]) => ({ key, count }))
		.sort((a, b) => b.count - a.count || a.key.localeCompare(b.key));
}

/** Union within the facet: an empty selection passes everything; otherwise the
 *  row passes when ANY of its lanes is selected. */
export function matchesLanes(selected: Set<string>, c: LaneFields): boolean {
	if (selected.size === 0) return true;
	return candidateLanes(c).some((lane) => selected.has(lane));
}

/** Human label for a lane key (chip face). */
export function laneLabel(key: string): string {
	return key === LANE_INSIDER_CLUSTER ? 'insider cluster' : key;
}

/** The insider-cluster facts the card chip reads. */
export interface InsiderClusterFields {
	event_n_insiders?: number | null;
	event_cluster_usd?: number | null;
}

/** Chip face text: `insiders · 2 buyers · $610k`. Null when the row carries no
 *  cluster (every plain thematic row, and any row from the pre-lane API). */
export function insiderChipLabel(c: InsiderClusterFields): string | null {
	const n = c.event_n_insiders;
	if (n === null || n === undefined) return null;
	const buyers = n === 1 ? 'buyer' : 'buyers';
	return `insiders · ${n} ${buyers} · ${fmtUsdCompact(c.event_cluster_usd)}`;
}
