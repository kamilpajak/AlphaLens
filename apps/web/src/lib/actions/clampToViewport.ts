/**
 * clampToViewport — viewport-edge-safe tooltip positioning.
 *
 * Attaches to a tooltip TRIGGER wrapper (the `group relative inline-block`
 * span/div). On open it measures the trigger vs. the viewport and keeps the
 * popover bubble inside the screen by writing two CSS custom properties onto
 * the popover element:
 *
 *   --tt-shift  px : horizontal correction of the bubble away from the edge
 *   --tt-arrow  px : counter-shift so the arrow keeps pointing at the trigger
 *
 * The components read these vars from an inline `transform` on the popover and
 * arrow:
 *   popover: transform: translateX(calc(-50% + var(--tt-shift, 0px)))
 *   arrow:   transform: translateX(calc(-50% + var(--tt-arrow, 0px))) rotate(45deg)
 * Both vars default to 0px, so the prerendered / hydration / JS-off state
 * renders exactly today's centered tooltip — graceful degradation is automatic
 * and there is no SSR hydration mismatch (the first measure only runs on open,
 * client-side).
 *
 * Why a JS action and not CSS anchor positioning: anchor positioning only
 * reached stable Safari/Firefox in early 2026, and a meaningful tail of older
 * mobile browsers still lacks it. The JS clamp works for 100% of users and is
 * the path every floating-UI library uses under the hood.
 *
 * SSR-safe: Svelte `use:` action bodies only run in the browser; we still guard
 * `typeof window` defensively.
 */

import {
	resolveHorizontalShift,
	resolveVerticalPlacement,
	type VerticalPlacement
} from './resolveTooltipPlacement';

// Gap kept between the bubble edge and the viewport edge. Must stay strictly
// below the per-side inset of the CSS width clamp `w-[min(20rem,calc(100vw-2rem))]`
// (2rem total = 16px/side) so the clamp range stays positive even when the
// bubble is at its max width.
const VIEWPORT_MARGIN_PX = 12;

// Keep the arrow this far from the bubble's corners so it never slides off the
// rounded/bordered end when the bubble is pinned hard against a viewport edge.
const ARROW_CORNER_PAD_PX = 12;

const TOOLTIP_SELECTOR = '[role="tooltip"]';

interface ClampOptions {
	/** CSS selector for the popover, relative to the trigger node. */
	tooltipSelector?: string;
}

// The rect of the nearest ancestor that clips overflow (a scroll box like the
// /edge outcomes table's `overflow-auto` container), or the viewport when there
// is none. Drives BOTH the vertical flip (top/bottom) and the horizontal clamp +
// width-cap (left/right) so the scroll box edge never cuts the bubble off.
function nearestClipBox(node: HTMLElement): {
	top: number;
	bottom: number;
	left: number;
	right: number;
} {
	let el: HTMLElement | null = node.parentElement;
	while (el && el !== document.body) {
		const s = getComputedStyle(el);
		// A container that scrolls/clips on EITHER axis clips both (CSS coerces the
		// `visible` axis to `auto` when the other is a scroll value), so it is the
		// clip box for both the vertical flip and the horizontal clamp.
		const clips = (v: string) =>
			v === 'auto' || v === 'scroll' || v === 'hidden' || v === 'clip' || v === 'overlay';
		if (clips(s.overflowY) || clips(s.overflowX)) {
			const r = el.getBoundingClientRect();
			return { top: r.top, bottom: r.bottom, left: r.left, right: r.right };
		}
		el = el.parentElement;
	}
	const d = document.documentElement;
	return { top: 0, bottom: d.clientHeight, left: 0, right: d.clientWidth };
}

// ---------------------------------------------------------------------------
// Shared coordinator: ONE set of geometry listeners for ALL open tooltips, not
// N per-instance listeners (a single page can host 20+ JargonTips). Each open
// instance registers its `measure` fn; resize/scroll re-measures only the
// currently-open ones, coalesced through a single requestAnimationFrame.
// ---------------------------------------------------------------------------
const openMeasures = new Set<() => void>();
let listenersAttached = false;
let rafId = 0;

function flush() {
	rafId = 0;
	for (const measure of openMeasures) measure();
}

function schedule() {
	if (rafId) return;
	rafId = requestAnimationFrame(flush);
}

function ensureListeners() {
	if (listenersAttached || globalThis.window === undefined) return;
	listenersAttached = true;
	// scroll uses capture so it catches scrolling on any ancestor (scroll does
	// not bubble); passive since we never preventDefault.
	window.addEventListener('resize', schedule);
	window.addEventListener('scroll', schedule, { capture: true, passive: true });
	window.visualViewport?.addEventListener('resize', schedule);
}

export function clampToViewport(node: HTMLElement, options: ClampOptions = {}) {
	const tooltipSelector = options.tooltipSelector ?? TOOLTIP_SELECTOR;

	// Actions only run client-side, but guard defensively for any reuse.
	if (globalThis.window === undefined) {
		return { destroy() {} };
	}

	function measure() {
		// Re-query every time: the popover may be conditionally rendered
		// (`{#if info}` / `{#if tooltip}`) and `:scope >` selects only the
		// DIRECT-child popover so nested JargonTips (αt wrapping Carhart-4F)
		// clamp their own bubble, not a descendant's.
		const tooltip = node.querySelector<HTMLElement>(`:scope > ${tooltipSelector}`);
		if (!tooltip) return;

		// The nearest scroll/overflow box (or the viewport) — it clips the bubble on
		// every edge, so it bounds both the width-cap and the horizontal clamp.
		const clip = nearestClipBox(node);

		// Cap the bubble's width to the clip box so a box narrower than the default
		// 20rem never cuts it off sideways. `--tt-maxw` folds into the width `min()`
		// in TooltipBubble; unset (viewport fallback) leaves the CSS width unchanged.
		const maxW = Math.max(0, clip.right - clip.left - 2 * VIEWPORT_MARGIN_PX);
		tooltip.style.setProperty('--tt-maxw', `${maxW}px`);

		// Measure AFTER the cap so width + any re-wrapped height are current.
		const bubbleRect = tooltip.getBoundingClientRect();
		const bubbleWidth = bubbleRect.width;
		if (bubbleWidth <= 0) return; // not laid out yet

		const triggerRect = node.getBoundingClientRect();
		// Use the layout-viewport width so it shares the coordinate space of
		// getBoundingClientRect() (visualViewport.width would mix spaces under
		// pinch-zoom). The layout width is unaffected by the iOS URL bar, which
		// only changes height, so horizontal clamping stays stable.
		const vw = document.documentElement.clientWidth;
		const triggerCenterX = triggerRect.left + triggerRect.width / 2;

		// Horizontal clamp: keep the bubble inside BOTH the viewport and the clip
		// box, and counter-shift the arrow to keep it pointing at the trigger.
		const { shiftX, arrowX } = resolveHorizontalShift(
			triggerCenterX,
			bubbleWidth,
			vw,
			clip,
			VIEWPORT_MARGIN_PX,
			ARROW_CORNER_PAD_PX
		);
		// Set both on the popover; the arrow inherits --tt-arrow via inheritance.
		tooltip.style.setProperty('--tt-shift', `${shiftX}px`);
		tooltip.style.setProperty('--tt-arrow', `${arrowX}px`);

		// Vertical auto-flip: open toward the side with room inside the clip box so
		// the bubble is never clipped by it. `data-tt-flip` drives the bubble+arrow
		// position classes (see TooltipBubble).
		const authored = (tooltip.dataset.ttPlacement as VerticalPlacement) ?? 'above';
		const flip = resolveVerticalPlacement(
			authored,
			triggerRect,
			bubbleRect.height,
			clip,
			VIEWPORT_MARGIN_PX
		);
		if (tooltip.dataset.ttFlip !== flip) tooltip.dataset.ttFlip = flip;
	}

	function open() {
		ensureListeners();
		openMeasures.add(measure);
		schedule();
	}

	function close(e: FocusEvent | PointerEvent) {
		// focusout/pointerleave also fire when moving between the trigger and its
		// own children; only close when focus/pointer actually left the wrapper.
		const related = e.relatedTarget as Node | null;
		if (related && node.contains(related)) return;
		openMeasures.delete(measure);
	}

	// Open on hover, focus, and the touch pointerdown→focus path the components
	// already wire; close when leaving the wrapper.
	node.addEventListener('pointerenter', open);
	node.addEventListener('pointerdown', open);
	node.addEventListener('focusin', open);
	node.addEventListener('pointerleave', close);
	node.addEventListener('focusout', close);

	return {
		destroy() {
			openMeasures.delete(measure);
			node.removeEventListener('pointerenter', open);
			node.removeEventListener('pointerdown', open);
			node.removeEventListener('focusin', open);
			node.removeEventListener('pointerleave', close);
			node.removeEventListener('focusout', close);
		}
	};
}
