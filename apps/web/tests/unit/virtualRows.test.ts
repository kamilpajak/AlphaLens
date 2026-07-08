import { describe, it, expect } from 'vitest';
import { prefixOffsets, heightsFromKeys, windowRange } from '$lib/virtualRows.svelte';

// The pure windowing math behind the /edge table's row virtualization. Kept as
// plain functions (no DOM, no runes) so the tricky parts — cumulative offsets,
// the visible-window slice, overscan clamping, and the padTop+visible+padBottom
// invariant — are unit-testable in isolation from the ResizeObserver engine.

describe('prefixOffsets', () => {
	it('builds cumulative offsets with a leading 0 and a trailing total', () => {
		expect(prefixOffsets([10, 20, 30])).toEqual([0, 10, 30, 60]);
	});
	it('empty heights → [0]', () => {
		expect(prefixOffsets([])).toEqual([0]);
	});
});

describe('heightsFromKeys', () => {
	it('uses the measured height per key, else the estimate', () => {
		const measured = new Map([['b', 50]]);
		expect(heightsFromKeys(['a', 'b', 'c'], measured, 44)).toEqual([44, 50, 44]);
	});
	it('is keyed — a re-sort keeps each row its measured height (heights follow the key, not the index)', () => {
		const measured = new Map([
			['a', 100],
			['b', 20]
		]);
		expect(heightsFromKeys(['a', 'b'], measured, 44)).toEqual([100, 20]);
		expect(heightsFromKeys(['b', 'a'], measured, 44)).toEqual([20, 100]);
	});
});

describe('windowRange', () => {
	// 5 rows of height 100 → offsets [0,100,200,300,400,500], total 500.
	const offsets = prefixOffsets([100, 100, 100, 100, 100]);

	it('slices to the visible window [start, end) with correct padding', () => {
		// scrollTop 150, viewport 200 → visible band [150, 350).
		const r = windowRange(offsets, 150, 200, 0);
		expect(r.start).toBe(1); // row 0 (bottom 100) is above the fold
		expect(r.end).toBe(4); // exclusive: row 3 (top 300) visible, row 4 (top 400) not
		expect(r.padTop).toBe(100);
		expect(r.padBottom).toBe(100);
		expect(r.total).toBe(500);
	});

	it('overscan expands the window and clamps at the edges', () => {
		const r = windowRange(offsets, 150, 200, 2);
		expect(r.start).toBe(0);
		expect(r.end).toBe(5);
		expect(r.padTop).toBe(0);
		expect(r.padBottom).toBe(0);
	});

	it('padTop + windowed height + padBottom === total (no drift)', () => {
		const r = windowRange(offsets, 250, 120, 1);
		const windowed = offsets[r.end] - offsets[r.start];
		expect(r.padTop + windowed + r.padBottom).toBe(r.total);
	});

	it('empty list → fully zeroed range', () => {
		expect(windowRange(prefixOffsets([]), 0, 100, 3)).toEqual({
			start: 0,
			end: 0,
			padTop: 0,
			padBottom: 0,
			total: 0
		});
	});

	it('clamps a negative scrollTop to the top of the list', () => {
		const r = windowRange(offsets, -50, 100, 0);
		expect(r.start).toBe(0);
		expect(r.padTop).toBe(0);
	});
});
