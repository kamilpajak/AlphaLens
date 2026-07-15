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
