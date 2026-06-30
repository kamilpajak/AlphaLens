import { describe, expect, it } from 'vitest';
import { finalExitMarkerTime } from '$lib/components/ladderChart';
import type { ChartMarker } from '$lib/types';

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
});
