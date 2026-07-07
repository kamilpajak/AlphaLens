// apps/web/tests/unit/excessScatter.test.ts
import { describe, it, expect } from 'vitest';
import {
	dateToMs,
	makeLinScale,
	buildScales,
	pointCircles,
	trendPolyline,
	bandPath
} from '$lib/excessScatter';
import type { EdgeExcessPoint, EdgeExcessTrend } from '$lib/types';

const BOX = { width: 400, height: 200, padLeft: 40, padRight: 10, padTop: 10, padBottom: 20 };

const POINTS: EdgeExcessPoint[] = [
	{ date: '2026-06-01', excess: -0.02, ticker: 'A', holding_days: 5, episode_repeat: false },
	{ date: '2026-06-03', excess: 0.04, ticker: 'B', holding_days: 9, episode_repeat: true }
];
const TREND: EdgeExcessTrend[] = [
	{ date: '2026-06-01', mean: -0.01, lo: -0.03, hi: 0.01 },
	{ date: '2026-06-03', mean: 0.02, lo: 0.0, hi: 0.05 }
];

describe('excessScatter geometry', () => {
	it('makeLinScale maps domain ends to range ends', () => {
		const s = makeLinScale(0, 10, 100, 200);
		expect(s(0)).toBe(100);
		expect(s(10)).toBe(200);
		expect(s(5)).toBe(150);
	});

	it('xScale is monotonic and equal dates map to equal x', () => {
		const { x } = buildScales(POINTS, TREND, BOX);
		expect(x(dateToMs('2026-06-01'))).toBeLessThan(x(dateToMs('2026-06-03')));
		expect(x(dateToMs('2026-06-01'))).toBe(x(dateToMs('2026-06-01')));
	});

	it('zeroY equals yScale(0) and sits inside the plot box', () => {
		const { y, zeroY } = buildScales(POINTS, TREND, BOX);
		expect(zeroY).toBe(y(0));
		expect(zeroY).toBeGreaterThanOrEqual(BOX.padTop);
		expect(zeroY).toBeLessThanOrEqual(BOX.height - BOX.padBottom);
	});

	it('pointCircles returns one entry per point carrying the repeat flag', () => {
		const { x, y } = buildScales(POINTS, TREND, BOX);
		const circles = pointCircles(POINTS, x, y);
		expect(circles).toHaveLength(2);
		expect(circles[1].repeat).toBe(true);
		expect(Number.isFinite(circles[0].cx)).toBe(true);
	});

	it('trendPolyline yields an M/L path with one vertex per trend point', () => {
		const { x, y } = buildScales(POINTS, TREND, BOX);
		const d = trendPolyline(TREND, x, y);
		expect(d.startsWith('M')).toBe(true);
		expect((d.match(/L/g) ?? []).length).toBe(1); // 2 points -> 1 L
	});

	it('bandPath is a closed polygon (starts M, ends Z)', () => {
		const { x, y } = buildScales(POINTS, TREND, BOX);
		const d = bandPath(TREND, x, y);
		expect(d.startsWith('M')).toBe(true);
		expect(d.trim().endsWith('Z')).toBe(true);
	});
});
