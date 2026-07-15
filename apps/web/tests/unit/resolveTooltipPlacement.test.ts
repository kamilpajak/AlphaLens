import { describe, expect, it } from 'vitest';
import { resolveVerticalPlacement } from '../../src/lib/actions/resolveTooltipPlacement';

// Pins the auto-flip that keeps tooltips inside a scroll/overflow box (the /edge
// outcomes table's `overflow-auto` container clips upward-opening tooltips on
// the top rows). Coordinates are client-space; `clip` is the scroll box.

const M = 12;
const H = 66; // a 2-line classification bubble

describe('resolveVerticalPlacement', () => {
	const clip = { top: 100, bottom: 660 }; // a 560px scroll viewport

	it('keeps the authored side when it fits', () => {
		const trigger = { top: 400, bottom: 420 }; // mid-box: room both ways
		expect(resolveVerticalPlacement('above', trigger, H, clip, M)).toBe('above');
		expect(resolveVerticalPlacement('below', trigger, H, clip, M)).toBe('below');
	});

	it('flips above→below for a row near the top of the scroll box', () => {
		const trigger = { top: 120, bottom: 140 }; // only 20px above → above clips
		expect(resolveVerticalPlacement('above', trigger, H, clip, M)).toBe('below');
	});

	it('flips below→above for a row near the bottom of the scroll box', () => {
		const trigger = { top: 630, bottom: 650 }; // only 10px below → below clips
		expect(resolveVerticalPlacement('below', trigger, H, clip, M)).toBe('above');
	});

	it('does not flip when the authored side fits even if the other is tight', () => {
		const trigger = { top: 600, bottom: 620 }; // above has 500px room, fits
		expect(resolveVerticalPlacement('above', trigger, H, clip, M)).toBe('above');
	});

	it('picks the roomier side when both clip (short clip box)', () => {
		const tightClip = { top: 300, bottom: 380 }; // 80px box, bubble 66px
		const trigger = { top: 330, bottom: 350 };
		// roomAbove=30, roomBelow=30 → tie resolves to above; shift trigger down
		const lower = { top: 345, bottom: 365 }; // roomAbove=45, roomBelow=15
		expect(resolveVerticalPlacement('below', lower, H, tightClip, M)).toBe('above');
	});
});
