import { describe, expect, it } from 'vitest';
import {
	LANE_INSIDER_CLUSTER,
	LANE_THEMATIC,
	candidateLanes,
	insiderChipLabel,
	laneFacet,
	laneLabel,
	matchesLanes
} from '$lib/lane';

// Candidate source lanes (epic #1293, #1298). A card belongs to the thematic
// lane, the insider-cluster lane, or BOTH (a thematic row whose ticker also
// completed a cluster that day: `event_overlap`). The day-list `source` filter
// is lane MEMBERSHIP, not the raw `source` string, so an overlap row is
// reachable from the insider-cluster chip. Pre-#1293 rows (empty / missing
// `source`) are thematic — the same allow-list Django's edge aggregates use.

const thematic = { source: 'thematic', event_overlap: false };
const legacy = { source: '', event_overlap: false };
const insider = { source: 'insider_cluster', event_overlap: false };
const overlap = { source: 'thematic', event_overlap: true };

describe('candidateLanes', () => {
	it('maps the pipeline source to one lane', () => {
		expect(candidateLanes(thematic)).toEqual([LANE_THEMATIC]);
		expect(candidateLanes(insider)).toEqual([LANE_INSIDER_CLUSTER]);
	});

	it('treats an empty or missing source as thematic (pre-#1293 rows, old API)', () => {
		expect(candidateLanes(legacy)).toEqual([LANE_THEMATIC]);
		expect(candidateLanes({ source: undefined, event_overlap: undefined })).toEqual([LANE_THEMATIC]);
	});

	it('puts an overlap row in BOTH lanes, thematic first', () => {
		expect(candidateLanes(overlap)).toEqual([LANE_THEMATIC, LANE_INSIDER_CLUSTER]);
	});
});

describe('laneFacet', () => {
	it('counts an overlap row in every lane it belongs to, count-desc then key', () => {
		expect(laneFacet([thematic, thematic, insider, overlap])).toEqual([
			{ key: LANE_THEMATIC, count: 3 },
			{ key: LANE_INSIDER_CLUSTER, count: 2 }
		]);
	});

	it('yields a single option on a thematic-only day (the bar stays hidden)', () => {
		expect(laneFacet([thematic, legacy])).toEqual([{ key: LANE_THEMATIC, count: 2 }]);
	});

	it('is empty for an empty day', () => {
		expect(laneFacet([])).toEqual([]);
	});
});

describe('matchesLanes', () => {
	it('passes everything when nothing is selected', () => {
		expect(matchesLanes(new Set(), insider)).toBe(true);
		expect(matchesLanes(new Set(), legacy)).toBe(true);
	});

	it('matches when ANY of the row lanes is selected (union within the facet)', () => {
		const insiderOnly = new Set([LANE_INSIDER_CLUSTER]);
		expect(matchesLanes(insiderOnly, insider)).toBe(true);
		expect(matchesLanes(insiderOnly, overlap)).toBe(true);
		expect(matchesLanes(insiderOnly, thematic)).toBe(false);
		expect(matchesLanes(new Set([LANE_THEMATIC]), overlap)).toBe(true);
	});
});

describe('laneLabel', () => {
	it('humanises the insider lane and passes other keys through', () => {
		expect(laneLabel(LANE_INSIDER_CLUSTER)).toBe('insider cluster');
		expect(laneLabel(LANE_THEMATIC)).toBe('thematic');
		expect(laneLabel('future_lane')).toBe('future_lane');
	});
});

describe('insiderChipLabel', () => {
	it('formats buyers + compact USD from the real EQPT facts', () => {
		expect(insiderChipLabel({ event_n_insiders: 2, event_cluster_usd: 610250 })).toBe(
			'insiders · 2 buyers · $610k'
		);
	});

	it('uses the singular for one buyer (cannot occur by gate, formatted anyway)', () => {
		expect(insiderChipLabel({ event_n_insiders: 1, event_cluster_usd: 250000 })).toBe(
			'insiders · 1 buyer · $250k'
		);
	});

	it('is null when the row carries no cluster (thematic rows, old API)', () => {
		expect(insiderChipLabel({ event_n_insiders: null, event_cluster_usd: null })).toBeNull();
		expect(insiderChipLabel({ event_n_insiders: undefined, event_cluster_usd: undefined })).toBeNull();
	});

	it('still renders the buyer count when the USD is missing', () => {
		expect(insiderChipLabel({ event_n_insiders: 2, event_cluster_usd: null })).toBe(
			'insiders · 2 buyers · —'
		);
	});
});
