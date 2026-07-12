import { describe, expect, it } from 'vitest';
import { oneilTone } from '../../src/lib/format';

// The expert-panel disagreement scalar is surfaced RAW (gap number + caveat), NOT
// bucketed into a consensus/mixed/split verdict word or authority colour — the
// consensusBand/consensusTone helpers were removed. The transition shim is the
// single predicate Number.isFinite(spread) — null / NaN / undefined degrade to the
// no-scale / coverage state deterministically (the deploy-window guarantee).

describe('oneilTone', () => {
	it('three-state by score, muted when absent', () => {
		expect(oneilTone(80)).toBe('green');
		expect(oneilTone(70)).toBe('green');
		expect(oneilTone(55)).toBe('amber');
		expect(oneilTone(40)).toBe('amber');
		expect(oneilTone(20)).toBe('muted');
		expect(oneilTone(null)).toBe('muted');
		expect(oneilTone(undefined)).toBe('muted');
		expect(oneilTone(NaN)).toBe('muted');
	});
});

describe('disagreement-scale render shim (pure-function level)', () => {
	// The shim's single predicate: a finite spread => the two-lens scale + gap
	// render; anything else (null/NaN/Infinity/undefined) => no scale, deterministic.
	const renders = (spread: number | null | undefined) => Number.isFinite(spread as number);
	it('finite spread renders the scale; non-finite never does', () => {
		expect(renders(47)).toBe(true);
		expect(renders(0)).toBe(true); // 0 = a real (zero) gap, not "absent"
		expect(renders(null)).toBe(false);
		expect(renders(undefined)).toBe(false);
		expect(renders(NaN)).toBe(false);
		expect(renders(Infinity)).toBe(false);
	});
});

// Mirrors the ExpertPanel `sections` Buffett-arm rule: the Buffett card shows
// when it has qualitative data OR a numeric score (symmetry with O'Neil).
function showsBuffettCard(hasBuffQual: boolean, buffScore: number | null): boolean {
	return hasBuffQual || buffScore !== null;
}

describe('Buffett card inclusion (symmetry)', () => {
	it('shows on qual only', () => {
		expect(showsBuffettCard(true, null)).toBe(true);
	});
	it('shows on numeric score only', () => {
		expect(showsBuffettCard(false, 62)).toBe(true);
	});
	it('shows on both', () => {
		expect(showsBuffettCard(true, 62)).toBe(true);
	});
	it('hidden when neither', () => {
		expect(showsBuffettCard(false, null)).toBe(false);
	});
});
