/**
 * Row virtualization for the /edge outcomes table.
 *
 * The DOM engine (`createRowWindow`, added with the table wiring) is a thin runes
 * wrapper around these pure functions: a measured-height Map keyed by the
 * canonical row key, a scroll listener, and a `measure` action driving a
 * ResizeObserver. Keeping the math here — no runes, no DOM — makes the
 * cumulative-offset / visible-window / overscan logic unit-testable and
 * impossible to break silently.
 */

/** Cumulative top offsets for `heights`: length n+1, `offsets[0] === 0`,
 *  `offsets[n]` is the full scroll height. */
export function prefixOffsets(heights: number[]): number[] {
	const offsets = new Array<number>(heights.length + 1);
	offsets[0] = 0;
	for (let i = 0; i < heights.length; i++) offsets[i + 1] = offsets[i] + heights[i];
	return offsets;
}

/** Height per row: the measured value for its key, else `estimate` (for rows not
 *  yet rendered/measured). Keyed — never index-keyed — so a re-sort or re-filter
 *  carries each row's real measured height instead of corrupting it. */
export function heightsFromKeys(
	keys: string[],
	measured: Map<string, number>,
	estimate: number
): number[] {
	return keys.map((k) => measured.get(k) ?? estimate);
}

export interface WindowRange {
	/** First row to render (inclusive). */
	start: number;
	/** One past the last row to render (exclusive — slice as `rows.slice(start, end)`). */
	end: number;
	/** Spacer height above the window (px). */
	padTop: number;
	/** Spacer height below the window (px). */
	padBottom: number;
	/** Full scroll height of all rows (px). */
	total: number;
}

/** The rows to paint for a given scroll position, plus the top/bottom spacer
 *  heights that keep the scrollbar honest. `overscan` renders a few extra rows on
 *  each side so fast scrolling never flashes blank. `offsets` comes from
 *  `prefixOffsets`. */
export function windowRange(
	offsets: number[],
	scrollTop: number,
	viewportHeight: number,
	overscan: number
): WindowRange {
	const n = offsets.length - 1;
	const total = offsets[n];
	if (n === 0) return { start: 0, end: 0, padTop: 0, padBottom: 0, total: 0 };

	const top = Math.max(0, scrollTop);
	const bottom = top + Math.max(0, viewportHeight);

	// First row whose bottom edge (offsets[i+1]) is past the top of the viewport.
	let start = 0;
	while (start < n && offsets[start + 1] <= top) start++;
	// First row whose top edge (offsets[i]) reaches the bottom of the viewport
	// (exclusive end).
	let end = start;
	while (end < n && offsets[end] < bottom) end++;

	start = Math.max(0, start - overscan);
	end = Math.min(n, end + overscan);

	return { start, end, padTop: offsets[start], padBottom: total - offsets[end], total };
}
