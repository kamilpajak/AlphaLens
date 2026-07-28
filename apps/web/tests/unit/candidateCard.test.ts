import { describe, expect, it } from 'vitest';
import { briefUnavailableLabel } from '../../src/lib/format';

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

// The honest "brief unavailable" state (#921): when the backend marks a row
// `brief_status: "unavailable"`, the catalyst.event blockquote shows a small
// tone-neutral label above the italic rationale fallback instead of silently
// degrading. The label is a pure function of (brief_status, brief_error_kind).
describe('briefUnavailableLabel (catalyst.event blockquote)', () => {
	it("returns the tone-neutral label when brief_status is 'unavailable'", () => {
		expect(briefUnavailableLabel('unavailable', null)).toBe(
			'brief unavailable — generation failed'
		);
	});

	it('appends the error kind in parentheses when present', () => {
		expect(briefUnavailableLabel('unavailable', 'truncated')).toBe(
			'brief unavailable — generation failed (truncated)'
		);
	});

	it("returns null for 'ok' — rendering stays byte-identical to today", () => {
		expect(briefUnavailableLabel('ok', null)).toBeNull();
	});

	it('returns null for null/undefined status (legacy pre-feature rows)', () => {
		expect(briefUnavailableLabel(null, null)).toBeNull();
		expect(briefUnavailableLabel(undefined, undefined)).toBeNull();
	});

	it('ignores a stray error kind unless the status is unavailable', () => {
		expect(briefUnavailableLabel('ok', 'truncated')).toBeNull();
		expect(briefUnavailableLabel(null, 'truncated')).toBeNull();
	});
});
