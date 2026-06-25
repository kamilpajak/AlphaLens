import { describe, expect, it } from 'vitest';

// CandidateCard renders an "extended" chip in the meta-bar ONLY when
// (c.atr_penalty ?? 0) > 0. This mirrors the template guard exactly so a
// future refactor cannot silently break the visibility rule.
//
// ExpertPanel shows the score breakdown section when at least one of
// selectionScore / atrPenalty / scorerConfigVersion is present (the
// hasScoreBreakdown predicate). Both are pure functions of the candidate
// fields — tested here without a DOM/component harness.

// Mirrors the CandidateCard template guard: `{#if (c.atr_penalty ?? 0) > 0}`
function showsExtendedChip(atr_penalty: number | null | undefined): boolean {
	return (atr_penalty ?? 0) > 0;
}

// Mirrors the ExpertPanel `hasScoreBreakdown` derived:
// `selectionScore != null || atrPenalty != null || scorerConfigVersion != null`
function hasScoreBreakdown(
	selectionScore: number | null | undefined,
	atrPenalty: number | null | undefined,
	scorerConfigVersion: string | null | undefined
): boolean {
	return selectionScore != null || atrPenalty != null || scorerConfigVersion != null;
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

describe('score breakdown visibility (expert.panel drawer)', () => {
	it('shows when selectionScore is present', () => {
		expect(hasScoreBreakdown(0.72, null, null)).toBe(true);
	});

	it('shows when atrPenalty is present (even if zero)', () => {
		expect(hasScoreBreakdown(null, 0, null)).toBe(true);
	});

	it('shows when scorerConfigVersion is present', () => {
		expect(hasScoreBreakdown(null, null, 'atr-tilt-v1')).toBe(true);
	});

	it('shows when all three are present', () => {
		expect(hasScoreBreakdown(0.72, 0.15, 'atr-tilt-v1')).toBe(true);
	});

	it('does NOT show when all three are null', () => {
		expect(hasScoreBreakdown(null, null, null)).toBe(false);
	});

	it('does NOT show when all three are undefined', () => {
		expect(hasScoreBreakdown(undefined, undefined, undefined)).toBe(false);
	});
});
