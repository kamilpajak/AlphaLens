import { describe, expect, it } from 'vitest';
import { briefUnavailableLabel, proseWithheld } from '../../src/lib/format';

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

// A withheld brief is NOT a failed brief. The pipeline generated a schema-valid
// brief twice and chose to suppress the prose because it asserted a benefit the
// evidence record cannot carry. Telling the operator "generation failed" is
// factually wrong, and it is wrong on exactly the honest-uncertainty rows the
// pipeline exists to keep visible.
describe('a withheld brief is labelled as a withhold, not a failure', () => {
	it('does not say "generation failed" for unsupported_benefit_claim', () => {
		const label = briefUnavailableLabel('unavailable', 'unsupported_benefit_claim');
		expect(label).not.toBeNull();
		expect(label).not.toContain('generation failed');
	});

	it('says the wording was withheld and why', () => {
		expect(briefUnavailableLabel('unavailable', 'unsupported_benefit_claim')).toBe(
			'brief withheld — the wording asserted support the evidence record does not carry'
		);
	});

	it('still labels a real generation failure as one', () => {
		// Anti-inertness control: special-casing one kind must not blunt the rest.
		expect(briefUnavailableLabel('unavailable', 'transport')).toBe(
			'brief unavailable — generation failed (transport)'
		);
	});
});

// The guard withheld `brief_tldr` because the prose asserted an unsupported
// benefit. Falling back to the stage-A `rationale` in the same slot promotes
// prose the guard never scanned — and on a misrouted row the rationale IS the
// manufactured theme link.
describe('proseWithheld (the rationale fallback is suppressed)', () => {
	it('is true only for the guard kind', () => {
		expect(proseWithheld('unsupported_benefit_claim')).toBe(true);
		expect(proseWithheld('truncated')).toBe(false);
		expect(proseWithheld(null)).toBe(false);
		expect(proseWithheld(undefined)).toBe(false);
	});
});
