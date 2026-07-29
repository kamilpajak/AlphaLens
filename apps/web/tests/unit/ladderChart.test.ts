import { describe, expect, it } from 'vitest';
import {
	briefLineTime,
	collapseEntryMarkers,
	deeperEntryTierLines,
	finalExitMarkerTime
} from '$lib/components/ladderChart';
import type { ChartBar, ChartMarker } from '$lib/types';

// Pins the in-trade shading band's END selection. The band runs from the first
// ENTRY to the position's FINAL exit. Markers are chronological (built from the
// replay sequence), so a multi-tranche scale-out (TP1 → TP2 → TP3) closes only
// at the LAST take-profit — the band must end there, not at the first partial
// TP. Returns null when nothing exited (an open/plan-preview ladder).

function marker(kind: ChartMarker['kind'], time: string, label: string = kind): ChartMarker {
	return { time, kind, level_id: label.toLowerCase(), price: 0, label, ambiguous: false };
}

describe('finalExitMarkerTime', () => {
	it('returns the LAST take-profit of a multi-tranche scale-out, not the first', () => {
		const markers = [
			marker('ENTRY', '2026-06-13', 'E1'),
			marker('TP', '2026-06-15', 'TP1'),
			marker('TP', '2026-06-19', 'TP2'),
			marker('TP', '2026-06-23', 'TP3')
		];
		expect(finalExitMarkerTime(markers)).toBe('2026-06-23');
	});

	it('returns the SL of a partial-then-stop exit', () => {
		const markers = [
			marker('ENTRY', '2026-06-13', 'E1'),
			marker('TP', '2026-06-15', 'TP1'),
			marker('SL', '2026-06-18', 'SL')
		];
		expect(finalExitMarkerTime(markers)).toBe('2026-06-18');
	});

	it('returns the single take-profit of a one-tranche ladder', () => {
		const markers = [marker('ENTRY', '2026-06-13', 'E1'), marker('TP', '2026-06-15', 'TP1')];
		expect(finalExitMarkerTime(markers)).toBe('2026-06-15');
	});

	it('returns the TIME_STOP time when the position was time-stopped', () => {
		const markers = [marker('ENTRY', '2026-06-13', 'E1'), marker('TIME_STOP', '2026-07-20')];
		expect(finalExitMarkerTime(markers)).toBe('2026-07-20');
	});

	it('returns null when only an ENTRY exists (still open, never exited)', () => {
		expect(finalExitMarkerTime([marker('ENTRY', '2026-06-13', 'E1')])).toBeNull();
	});

	it('returns null for an empty marker list', () => {
		expect(finalExitMarkerTime([])).toBeNull();
	});

	it('never selects an ENTRY marker as the exit', () => {
		// An ENTRY appearing after an exit (defensive) must not be chosen.
		const markers = [
			marker('ENTRY', '2026-06-13', 'E1'),
			marker('TP', '2026-06-15', 'TP1'),
			marker('ENTRY', '2026-06-16', 'E2')
		];
		expect(finalExitMarkerTime(markers)).toBe('2026-06-15');
	});

	it('ends the band at the last TOUCHED TP even when it sold nothing', () => {
		// DFIN case: E1 fills, TP1 sells the whole held position, TP2/TP3 are
		// TOUCHED but sell nothing. The replay marks exit_reached at the all-TPs
		// bar, so the band must still run to the last (touched) TP — a TP_TOUCHED
		// crossing closes the band exactly as a sold TP does.
		const markers = [
			marker('ENTRY', '2026-06-13', 'E1'),
			marker('TP', '2026-06-15', 'TP1'),
			marker('TP_TOUCHED', '2026-06-19', 'TP2'),
			marker('TP_TOUCHED', '2026-06-23', 'TP3')
		];
		expect(finalExitMarkerTime(markers)).toBe('2026-06-23');
	});
});

// Pins the "brief" vertical-line anchor: the session the candidate appeared in
// its brief. Bars are chronological daily sessions; brief_date may fall on a
// non-trading day (weekend brief), so the anchor snaps FORWARD to the first
// bar at/after it — by construction the arrival session (session_on_or_after).
// Null means "draw nothing" (no bars, or the brief postdates every bar).

function bar(time: string): ChartBar {
	return { time, open: 1, high: 2, low: 0.5, close: 1.5, volume: 100 };
}

describe('briefLineTime', () => {
	it('returns null for an empty bar list (NO_DATA payloads)', () => {
		expect(briefLineTime([], '2026-06-13')).toBeNull();
	});

	it('returns the bar time when brief_date lands exactly on a session', () => {
		const bars = [bar('2026-06-12'), bar('2026-06-15'), bar('2026-06-16')];
		expect(briefLineTime(bars, '2026-06-15')).toBe('2026-06-15');
	});

	it('snaps a weekend brief forward to the next session', () => {
		// 2026-06-14 is a Sunday; the next session bar is Monday 06-15.
		const bars = [bar('2026-06-12'), bar('2026-06-15'), bar('2026-06-16')];
		expect(briefLineTime(bars, '2026-06-14')).toBe('2026-06-15');
	});

	it('returns null when brief_date postdates every bar', () => {
		const bars = [bar('2026-06-12'), bar('2026-06-15')];
		expect(briefLineTime(bars, '2026-06-16')).toBeNull();
	});

	it('returns the first bar when brief_date predates all bars', () => {
		// No lead-in history available (sparse listing) — the first bar IS the
		// arrival session, so anchoring at the left edge is correct, not clamped.
		const bars = [bar('2026-06-15'), bar('2026-06-16')];
		expect(briefLineTime(bars, '2026-06-10')).toBe('2026-06-15');
	});

	it('returns null for a missing brief date', () => {
		const bars = [bar('2026-06-12')];
		expect(briefLineTime(bars, null)).toBeNull();
		expect(briefLineTime(bars, undefined)).toBeNull();
		expect(briefLineTime(bars, '')).toBeNull();
	});
});

// Pins collapse of coincident entry-tier markers. When a fast move (e.g. a
// gap-down open) fills several entry rungs in the SAME daily session, all their
// ENTRY markers share one bar time and one `belowBar` slot, so three arrows +
// labels overlap into an illegible single mark. collapseEntryMarkers folds a
// same-time ENTRY group into ONE marker whose label joins the tier ids (E1·E2·E3),
// leaving every other marker (and entries on distinct days) untouched.

function entryMarker(time: string, label: string, price = 0): ChartMarker {
	return { time, kind: 'ENTRY', level_id: label.toLowerCase(), price, label, ambiguous: false };
}

describe('collapseEntryMarkers', () => {
	it('folds three coincident entry tiers into one joined-label marker', () => {
		const raw = [
			entryMarker('2026-07-22', 'E1', 29.9),
			entryMarker('2026-07-22', 'E2', 28.66),
			entryMarker('2026-07-22', 'E3', 27.57)
		];
		const out = collapseEntryMarkers(raw);
		expect(out).toHaveLength(1);
		expect(out[0].kind).toBe('ENTRY');
		expect(out[0].label).toBe('E1·E2·E3');
		expect(out[0].time).toBe('2026-07-22');
	});

	it('keeps a same-time non-ENTRY marker (SL) separate from the folded entries', () => {
		const raw = [
			entryMarker('2026-07-22', 'E1'),
			entryMarker('2026-07-22', 'E2'),
			entryMarker('2026-07-22', 'E3'),
			marker('SL', '2026-07-22', 'SL')
		];
		const out = collapseEntryMarkers(raw);
		expect(out).toHaveLength(2);
		expect(out[0].label).toBe('E1·E2·E3');
		expect(out[1].kind).toBe('SL');
		expect(out[1].label).toBe('SL');
	});

	it('does NOT fold entries that fill on different sessions', () => {
		const raw = [
			entryMarker('2026-07-13', 'E1'),
			entryMarker('2026-07-16', 'E2'),
			entryMarker('2026-07-20', 'E3')
		];
		const out = collapseEntryMarkers(raw);
		expect(out).toHaveLength(3);
		expect(out.map((m) => m.label)).toEqual(['E1', 'E2', 'E3']);
	});

	it('leaves a single entry marker unchanged', () => {
		const raw = [entryMarker('2026-07-13', 'E1', 44.13)];
		const out = collapseEntryMarkers(raw);
		expect(out).toHaveLength(1);
		expect(out[0].label).toBe('E1');
		expect(out[0].price).toBe(44.13);
	});

	it('preserves chronological order and passes exit markers through', () => {
		// DFIN-like: E1 fills one day, then a multi-tranche scale-out.
		const raw = [
			entryMarker('2026-07-13', 'E1'),
			marker('TP', '2026-07-14', 'TP1'),
			marker('TP_TOUCHED', '2026-07-16', 'TP2')
		];
		const out = collapseEntryMarkers(raw);
		expect(out.map((m) => m.label)).toEqual(['E1', 'TP1', 'TP2']);
	});

	it('returns an empty list for no markers', () => {
		expect(collapseEntryMarkers([])).toEqual([]);
	});

	it('mutates ONLY the label — every other field stays the first tier’s', () => {
		const raw = [entryMarker('2026-07-22', 'E1', 29.9), entryMarker('2026-07-22', 'E2', 28.66)];
		const out = collapseEntryMarkers(raw);
		expect(out).toEqual([{ ...raw[0], label: 'E1·E2' }]);
	});
});

// Pins the deeper-tier price lines. The horizontal `entry` price line only ever
// draws E1 (the blended-entry anchor); when E2/E3 also fill, their limit prices
// are invisible. deeperEntryTierLines surfaces every filled tier BELOW E1 as a
// {price, title} pair so each rung gets its own dashed line at its real level.

describe('deeperEntryTierLines', () => {
	it('returns E2/E3 lines at their limit prices, excluding E1', () => {
		const raw = [
			entryMarker('2026-07-22', 'E1', 29.9),
			entryMarker('2026-07-22', 'E2', 28.66),
			entryMarker('2026-07-22', 'E3', 27.57)
		];
		expect(deeperEntryTierLines(raw)).toEqual([
			{ price: 28.66, title: 'e2' },
			{ price: 27.57, title: 'e3' }
		]);
	});

	it('returns an empty list when only E1 filled', () => {
		expect(deeperEntryTierLines([entryMarker('2026-07-13', 'E1', 44.13)])).toEqual([]);
	});

	it('returns an empty list when no entry filled (plan preview / exits only)', () => {
		const raw = [marker('SL', '2026-07-22', 'SL')];
		expect(deeperEntryTierLines(raw)).toEqual([]);
	});

	it('ignores exit markers, keeping only deeper ENTRY tiers', () => {
		const raw = [
			entryMarker('2026-07-22', 'E1', 29.9),
			entryMarker('2026-07-22', 'E2', 28.66),
			marker('SL', '2026-07-22', 'SL')
		];
		expect(deeperEntryTierLines(raw)).toEqual([{ price: 28.66, title: 'e2' }]);
	});

	it('emits one line per tier even if a tier marker is duplicated', () => {
		// Backend contract says a tier fills once; if that is ever violated, two
		// identical overlapping price lines would draw. Dedupe keeps it cosmetic-proof.
		const raw = [
			entryMarker('2026-07-22', 'E1', 29.9),
			entryMarker('2026-07-22', 'E2', 28.66),
			entryMarker('2026-07-23', 'E2', 28.66)
		];
		expect(deeperEntryTierLines(raw)).toEqual([{ price: 28.66, title: 'e2' }]);
	});

	it('skips an ENTRY marker with no level_id (tier identity unknown)', () => {
		const raw: ChartMarker[] = [
			entryMarker('2026-07-22', 'E1', 29.9),
			{ time: '2026-07-22', kind: 'ENTRY', level_id: null, price: 28.66, label: '', ambiguous: false }
		];
		expect(deeperEntryTierLines(raw)).toEqual([]);
	});
});
