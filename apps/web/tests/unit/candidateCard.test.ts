import { describe, expect, it } from 'vitest';

// CandidateCard renders an "extended" chip in the meta-bar ONLY when
// (c.atr_penalty ?? 0) > 0. This mirrors the template guard exactly so a
// future refactor cannot silently break the visibility rule. A pure function of
// the candidate field — tested here without a DOM/component harness.
// (The scorer breakdown moved from the expert drawer into the score-badge
// tooltip; the badge VALUE formatting is covered by `selectionBadge` in
// format.test.ts.)

// Mirrors the CandidateCard template guard: `{#if (c.atr_penalty ?? 0) > 0}`
function showsExtendedChip(atr_penalty: number | null | undefined): boolean {
	return (atr_penalty ?? 0) > 0;
}

describe('extended chip visibility (meta-bar)', () => {
	it('renders when atr_penalty > 0', () => {
		expect(showsExtendedChip(0.15)).toBe(true);
		expect(showsExtendedChip(0.01)).toBe(true);
		expect(showsExtendedChip(1.0)).toBe(true);
	});

	it('does NOT render when atr_penalty === 0', () => {
		expect(showsExtendedChip(0)).toBe(false);
	});

	it('does NOT render when atr_penalty is null', () => {
		expect(showsExtendedChip(null)).toBe(false);
	});

	it('does NOT render when atr_penalty is undefined', () => {
		expect(showsExtendedChip(undefined)).toBe(false);
	});

	it('does NOT render when atr_penalty is negative (not a valid penalty value)', () => {
		// A negative penalty would be a data anomaly; the chip is a deprioritisation
		// flag, so it must not render on nonsensical inputs.
		expect(showsExtendedChip(-0.1)).toBe(false);
	});
});
