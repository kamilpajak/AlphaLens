// Pure vertical-placement decision for tooltips, shared by clampToViewport.
//
// A tooltip opens toward its authored side by default. If that side would be
// clipped by the nearest scroll/overflow ancestor (or the viewport) and the
// opposite side has room, it flips. When BOTH sides clip (a very short clip
// box), it picks the side with more room so the most content stays visible.
//
// This is what keeps the /edge outcomes table's classification tooltips from
// being cut off by the `overflow-auto` scroll box: top-of-viewport rows flip to
// `below`, bottom-of-viewport rows flip to `above`, no matter the authored side.

export type VerticalPlacement = 'above' | 'below';

interface Span {
	top: number;
	bottom: number;
}

/** True when a bubble of `bubbleHeight` on `side` of `trigger` overflows `clip`. */
function clipsOn(
	side: VerticalPlacement,
	trigger: Span,
	bubbleHeight: number,
	clip: Span,
	margin: number
): boolean {
	return side === 'above'
		? trigger.top - bubbleHeight - margin < clip.top
		: trigger.bottom + bubbleHeight + margin > clip.bottom;
}

export function resolveVerticalPlacement(
	authored: VerticalPlacement,
	trigger: Span,
	bubbleHeight: number,
	clip: Span,
	margin: number
): VerticalPlacement {
	if (!clipsOn(authored, trigger, bubbleHeight, clip, margin)) return authored;

	const other: VerticalPlacement = authored === 'above' ? 'below' : 'above';
	if (!clipsOn(other, trigger, bubbleHeight, clip, margin)) return other;

	// Both sides clip — keep whichever has more room so more of the bubble shows.
	const roomAbove = trigger.top - clip.top;
	const roomBelow = clip.bottom - trigger.bottom;
	return roomBelow > roomAbove ? 'below' : 'above';
}

// --- Horizontal placement -------------------------------------------------

interface HSpan {
	left: number;
	right: number;
}

/**
 * Horizontal shift + arrow counter-shift that keeps a centered bubble inside
 * BOTH the viewport and its nearest clip box. The bubble is centered on the
 * trigger by default; this returns the correction (in px) to keep its edges off
 * the tighter of the two boundaries. When the space is narrower than the bubble
 * (`hi < lo`) it prefers the left edge — width-capping (done by the caller) keeps
 * that from happening for a fitting box.
 */
export function resolveHorizontalShift(
	triggerCenterX: number,
	bubbleWidth: number,
	viewportWidth: number,
	clip: HSpan,
	margin: number,
	arrowCornerPad: number
): { shiftX: number; arrowX: number } {
	const idealLeft = triggerCenterX - bubbleWidth / 2;
	const lo = Math.max(margin, clip.left + margin);
	const hi = Math.min(viewportWidth - margin - bubbleWidth, clip.right - margin - bubbleWidth);
	const clampedLeft = hi < lo ? lo : Math.min(Math.max(idealLeft, lo), hi);
	const shiftX = clampedLeft - idealLeft;

	// The bubble carries shiftX, so the arrow (a child) moves with it; counter-
	// shift by -shiftX to keep it on the trigger, clamped inside the bubble corners.
	const arrowLimit = Math.max(0, bubbleWidth / 2 - arrowCornerPad);
	const arrowX = Math.min(Math.max(-shiftX, -arrowLimit), arrowLimit);
	// `+ 0` normalizes a `-0` (from rounding negative-zero) to `0`.
	return { shiftX: Math.round(shiftX) + 0, arrowX: Math.round(arrowX) + 0 };
}
