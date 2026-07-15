import { describe, expect, it } from 'vitest';
import { resolveHorizontalShift } from '../../src/lib/actions/resolveTooltipPlacement';

// Pins the horizontal clamp that keeps a tooltip bubble inside BOTH the viewport
// and its nearest clip box. The /edge outcomes table is width-capped/centered, so
// the scroll box's right edge sits inside the viewport — clamping to the viewport
// alone let the bubble overflow the box's right edge and get clipped.

const M = 12; // viewport margin
const PAD = 12; // arrow corner pad
const VIEWPORT = { left: 0, right: 1000 };

describe('resolveHorizontalShift', () => {
	it('leaves a centered bubble that fits both bounds unshifted', () => {
		const { shiftX, arrowX } = resolveHorizontalShift(500, 320, 1000, VIEWPORT, M, PAD);
		expect(shiftX).toBe(0);
		expect(arrowX).toBe(0);
	});

	it('pulls the bubble left so it stays inside a clip box narrower than the viewport', () => {
		// Trigger at 500, bubble 320 → ideal right 660, but the clip box ends at 600.
		const clip = { left: 0, right: 600 };
		const { shiftX, arrowX } = resolveHorizontalShift(500, 320, 1000, clip, M, PAD);
		// Right edge must land at clip.right - margin = 588 → clampedLeft 268, ideal 340.
		expect(shiftX).toBe(-72);
		// Arrow counter-shifts to keep pointing at the trigger (clamped to corners).
		expect(arrowX).toBe(72);
		// Sanity: resulting right edge is inside the clip box.
		const idealLeft = 500 - 160;
		expect(idealLeft + shiftX + 320).toBeLessThanOrEqual(600 - M);
	});

	it('pushes right off a clip box that starts inside the viewport', () => {
		const clip = { left: 200, right: 1000 };
		const { shiftX } = resolveHorizontalShift(100, 320, 1000, clip, M, PAD);
		// idealLeft = -60, lo = max(12, 212) = 212 → shift +272.
		expect(shiftX).toBe(272);
	});

	it('still respects the viewport when it is the tighter bound', () => {
		const clip = { left: 0, right: 2000 }; // clip wider than viewport
		const { shiftX } = resolveHorizontalShift(950, 320, 1000, clip, M, PAD);
		// hi = min(1000-12-320=668, ...) = 668, idealLeft 790 → shift -122.
		expect(shiftX).toBe(-122);
	});

	it('prefers the left edge when the bubble cannot fit the box (hi < lo)', () => {
		const clip = { left: 0, right: 300 }; // 300px box, 320px bubble
		const { shiftX } = resolveHorizontalShift(150, 320, 1000, clip, M, PAD);
		// lo = 12, hi = min(668, 300-12-320=-32) = -32 < lo → clampedLeft = 12.
		const idealLeft = 150 - 160; // -10
		expect(shiftX).toBe(12 - idealLeft);
	});
});
