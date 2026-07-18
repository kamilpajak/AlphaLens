import { describe, expect, it } from 'vitest';
import { briefLineTime, finalExitMarkerTime } from '$lib/components/ladderChart';
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
