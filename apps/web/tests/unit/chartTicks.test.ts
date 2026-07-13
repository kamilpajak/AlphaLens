import { describe, expect, it } from 'vitest';
import { evenTimeTicks } from '../../src/lib/chartTicks';

// Pins the x-axis tick generator for the SPY-relative telemetry chart. The old
// approach subsampled the distinct exit-dates BY LIST INDEX and handed them to
// a time scale, so ticks landed at their true (uneven) calendar positions —
// producing visibly uneven label gaps (6/2 · 6/9 · 6/15 · 6/23 · 6/26 · …).
// The fix places ticks at even whole-day intervals across the domain instead.

const MS_PER_DAY = 86_400_000;
const diffsMs = (ticks: Date[]) =>
	ticks.slice(1).map((d, i) => d.getTime() - ticks[i].getTime());

describe('evenTimeTicks', () => {
	it('spaces ticks evenly in time (constant tick-to-tick gap)', () => {
		// 38-day span with irregular real exit-dates mixed in.
		const dates = ['2026-06-02', '2026-06-03', '2026-06-09', '2026-06-15', '2026-06-23',
			'2026-06-26', '2026-07-01', '2026-07-07', '2026-07-10'];
		const ticks = evenTimeTicks(dates, 8);
		const gaps = diffsMs(ticks);
		expect(gaps.length).toBeGreaterThan(0);
		// every consecutive gap is identical → evenly spaced pixels on a time scale.
		expect(new Set(gaps).size).toBe(1);
		// and the step is a whole number of days.
		expect(gaps[0] % MS_PER_DAY).toBe(0);
	});

	it('never emits sub-day steps (no duplicate M/D labels)', () => {
		// A short, dense span used to make d3 auto-ticks collapse to duplicate M/D.
		const dates = ['2026-06-01', '2026-06-02', '2026-06-03', '2026-06-04', '2026-06-05'];
		const ticks = evenTimeTicks(dates, 8);
		for (const g of diffsMs(ticks)) expect(g).toBeGreaterThanOrEqual(MS_PER_DAY);
	});

	it('anchors the last tick on the max date (right edge is labelled)', () => {
		const dates = ['2026-06-02', '2026-06-09', '2026-06-15', '2026-07-10'];
		const ticks = evenTimeTicks(dates, 8);
		expect(ticks.at(-1)!.getTime()).toBe(Date.UTC(2026, 6, 10));
	});

	it('keeps all ticks within the data domain, at UTC midnight', () => {
		const dates = ['2026-06-02', '2026-06-09', '2026-06-15', '2026-06-23', '2026-07-10'];
		const min = Date.UTC(2026, 5, 2);
		const max = Date.UTC(2026, 6, 10);
		for (const t of evenTimeTicks(dates, 8)) {
			const ms = t.getTime();
			expect(ms).toBeGreaterThanOrEqual(min);
			expect(ms).toBeLessThanOrEqual(max);
			expect(ms % MS_PER_DAY).toBe(0); // UTC-midnight aligned
		}
	});

	it('tolerates unsorted input with duplicates', () => {
		const dates = ['2026-07-10', '2026-06-02', '2026-06-02', '2026-06-15', '2026-06-09'];
		const ticks = evenTimeTicks(dates, 8);
		expect(ticks.at(0)!.getTime()).toBeLessThan(ticks.at(-1)!.getTime());
		expect(ticks.at(-1)!.getTime()).toBe(Date.UTC(2026, 6, 10));
	});

	it('returns the raw dates when the span is degenerate (<=2 distinct)', () => {
		expect(evenTimeTicks(['2026-06-02'], 8)).toEqual([new Date(Date.UTC(2026, 5, 2))]);
		expect(evenTimeTicks(['2026-06-02', '2026-06-09'], 8)).toEqual([
			new Date(Date.UTC(2026, 5, 2)),
			new Date(Date.UTC(2026, 5, 9))
		]);
	});

	it('returns an empty array for no input', () => {
		expect(evenTimeTicks([], 8)).toEqual([]);
	});
});
