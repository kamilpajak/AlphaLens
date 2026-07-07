// Pure SVG geometry for the SPY-excess telemetry scatter. No DOM, no chart lib
// (Lightweight Charts needs unique ascending timestamps and cannot draw multiple
// points on one date). All statistics are computed server-side; this module only
// maps prepared numbers to pixels + path strings, so it is fully unit-testable.
import type { EdgeExcessPoint, EdgeExcessTrend } from './types';

export interface Box {
	width: number;
	height: number;
	padLeft: number;
	padRight: number;
	padTop: number;
	padBottom: number;
}

export type Scale = (v: number) => number;

export function dateToMs(iso: string): number {
	return Date.parse(iso);
}

export function makeLinScale(d0: number, d1: number, r0: number, r1: number): Scale {
	if (d1 === d0) return () => (r0 + r1) / 2; // degenerate domain -> mid-range
	const m = (r1 - r0) / (d1 - d0);
	return (v: number) => r0 + (v - d0) * m;
}

export function buildScales(
	points: EdgeExcessPoint[],
	trend: EdgeExcessTrend[],
	box: Box
): { x: Scale; y: Scale; zeroY: number } {
	const xs = points.map((p) => dateToMs(p.date));
	const trendXs = trend.map((t) => dateToMs(t.date));
	const allX = [...xs, ...trendXs];
	const ys = [
		...points.map((p) => p.excess),
		...trend.flatMap((t) => [t.lo, t.hi]),
		0 // always include the parity line in the y-domain
	];
	const xMin = allX.length ? Math.min(...allX) : 0;
	const xMax = allX.length ? Math.max(...allX) : 1;
	const yMin = ys.length ? Math.min(...ys) : -0.01;
	const yMax = ys.length ? Math.max(...ys) : 0.01;
	const x = makeLinScale(xMin, xMax, box.padLeft, box.width - box.padRight);
	// y is inverted: larger excess -> smaller pixel (top of the box).
	const y = makeLinScale(yMin, yMax, box.height - box.padBottom, box.padTop);
	return { x, y, zeroY: y(0) };
}

export function pointCircles(
	points: EdgeExcessPoint[],
	x: Scale,
	y: Scale
): { cx: number; cy: number; repeat: boolean }[] {
	return points.map((p) => ({ cx: x(dateToMs(p.date)), cy: y(p.excess), repeat: p.episode_repeat }));
}

export function trendPolyline(trend: EdgeExcessTrend[], x: Scale, y: Scale): string {
	return trend
		.map((t, i) => `${i === 0 ? 'M' : 'L'} ${x(dateToMs(t.date))} ${y(t.mean)}`)
		.join(' ');
}

export function bandPath(trend: EdgeExcessTrend[], x: Scale, y: Scale): string {
	if (trend.length === 0) return '';
	const upper = trend.map((t) => `${x(dateToMs(t.date))} ${y(t.hi)}`);
	const lower = [...trend].reverse().map((t) => `${x(dateToMs(t.date))} ${y(t.lo)}`);
	return `M ${upper.join(' L ')} L ${lower.join(' L ')} Z`;
}
