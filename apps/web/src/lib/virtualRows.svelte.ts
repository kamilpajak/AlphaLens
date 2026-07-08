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
 *  carries each row's real measured height instead of corrupting it.
 *
 *  NOTE: `createRowWindow` inlines this two-map (row + detail) variant rather than
 *  calling this helper, so it stays as the unit-tested reference for the pure
 *  single-map case. */
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

// ─────────────────────────────────────────────────────────────────────────────
// Runes DOM engine
//
// A thin reactive wrapper around the pure functions above. It owns the scroll
// position + viewport height (`$state`, driven by the scroll container action)
// and two sticky per-key height maps, and derives the visible `WindowRange`.
//
// Why two maps: a logical row in the /edge table is a data `<tr>` plus an
// OPTIONAL detail `<tr>` (the ladder chart, when the row is expanded). `rowH`
// holds the always-present data-row height; `detailH` holds the chart-row height
// and is only ADDED to a key's total when that key is currently open (per the
// `isOpen` predicate). Both maps are sticky — never pruned on unmount — so an
// open row scrolled out of the window keeps its tall height in the offsets, and
// a closed row simply ignores its stale `detailH`. This is what lets a row stay
// correctly sized whether or not it is in the rendered slice.
//
// The estimate for not-yet-measured rows is the running AVERAGE of measured
// data rows (falling back to a constant before anything is measured). Because
// closed rows are near-identical in height, the average converges to the true
// height almost immediately, so predicted (unmeasured) rows match their eventual
// measured height — no cumulative scroll jump. Residual drift from expanded rows
// is absorbed by the browser's default CSS scroll-anchoring (we never set
// `overflow-anchor: none`).
// ─────────────────────────────────────────────────────────────────────────────

const INITIAL_ROW_ESTIMATE = 44;
const INITIAL_DETAIL_ESTIMATE = 360;

/** Which sub-element of a logical row a `measure` action is reporting. */
export type MeasureSlot = 'row' | 'detail';

export interface RowWindowOptions {
	/** Ordered row keys, in the SAME order as the array the caller will slice with
	 *  `range.start`/`range.end`. Read reactively (e.g. `() => rows.map(rowKey)`). */
	keys: () => string[];
	/** Whether a key's detail (chart) row is currently expanded. Read reactively. */
	isOpen: (key: string) => boolean;
	/** Extra rows rendered on each side of the visible band (default 6). */
	overscan?: number;
}

export interface RowWindow {
	/** The current visible window + spacer heights. Reactive. */
	readonly range: WindowRange;
	/** Svelte action for the scrolling container `<div>`: tracks scrollTop and
	 *  viewport height. */
	scrollContainer: (node: HTMLElement) => { destroy(): void };
	/** Svelte action for a rendered `<tr>`: measures its height into the keyed
	 *  map for its slot. */
	measure: (
		node: HTMLElement,
		params: { key: string; slot: MeasureSlot }
	) => { update(params: { key: string; slot: MeasureSlot }): void; destroy(): void };
	/** Scroll the window back to the top. Call when the row set changes wholesale
	 *  (filter / sort switch) so the viewport is not left stranded over the old
	 *  scroll offset of a now-different list. */
	resetScroll: () => void;
}

export function createRowWindow(options: RowWindowOptions): RowWindow {
	const overscan = options.overscan ?? 6;

	// Non-reactive height maps; reactivity is signalled by bumping `measureVersion`
	// (avoids copying a Map on every ResizeObserver callback).
	const rowH = new Map<string, number>();
	const detailH = new Map<string, number>();

	let scrollTop = $state(0);
	// Seed a plausible viewport so the first (pre-measure / SSR) render paints a
	// reasonable window instead of a single row; corrected on mount.
	let viewportHeight = $state(800);
	let measureVersion = $state(0);
	// The mounted scroll container, captured by the action so `resetScroll` can
	// move the real DOM offset (not just the tracked `$state`).
	let scrollNode: HTMLElement | null = null;

	function rowEstimate(): number {
		if (rowH.size === 0) return INITIAL_ROW_ESTIMATE;
		let sum = 0;
		for (const h of rowH.values()) sum += h;
		return sum / rowH.size;
	}

	function detailEstimate(): number {
		if (detailH.size === 0) return INITIAL_DETAIL_ESTIMATE;
		let sum = 0;
		for (const h of detailH.values()) sum += h;
		return sum / detailH.size;
	}

	const heights = $derived.by(() => {
		measureVersion; // establish dependency on measurements
		const ks = options.keys();
		const rowEst = rowEstimate();
		const detailEst = detailEstimate();
		return ks.map((k) => {
			let h = rowH.get(k) ?? rowEst;
			if (options.isOpen(k)) h += detailH.get(k) ?? detailEst;
			return h;
		});
	});

	const offsets = $derived(prefixOffsets(heights));
	const range = $derived(windowRange(offsets, scrollTop, viewportHeight, overscan));

	function scrollContainer(node: HTMLElement) {
		scrollNode = node;
		const onScroll = () => {
			scrollTop = node.scrollTop;
		};
		const ro = new ResizeObserver(() => {
			viewportHeight = node.clientHeight;
		});
		node.addEventListener('scroll', onScroll, { passive: true });
		ro.observe(node);
		// Prime from the mounted node immediately.
		viewportHeight = node.clientHeight;
		scrollTop = node.scrollTop;
		return {
			destroy() {
				node.removeEventListener('scroll', onScroll);
				ro.disconnect();
				if (scrollNode === node) scrollNode = null;
			}
		};
	}

	function resetScroll() {
		scrollTop = 0;
		if (scrollNode) scrollNode.scrollTop = 0;
	}

	function measure(node: HTMLElement, params: { key: string; slot: MeasureSlot }) {
		let { key, slot } = params;
		const report = () => {
			const h = node.offsetHeight;
			const map = slot === 'row' ? rowH : detailH;
			if (map.get(key) !== h) {
				map.set(key, h);
				measureVersion++;
			}
		};
		const ro = new ResizeObserver(report);
		ro.observe(node);
		report();
		return {
			update(next: { key: string; slot: MeasureSlot }) {
				if (next.key !== key || next.slot !== slot) {
					key = next.key;
					slot = next.slot;
					report();
				}
			},
			destroy() {
				ro.disconnect();
			}
		};
	}

	return {
		get range() {
			return range;
		},
		scrollContainer,
		measure,
		resetScroll
	};
}
