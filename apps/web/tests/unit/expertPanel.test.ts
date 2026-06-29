import { describe, expect, it } from 'vitest';
import { oneilTone, consensusTone, consensusBand } from '../../src/lib/format';

// The expert-panel disagreement helpers (PR-8b). The resting chip is coverage-only
// (tone-neutral); the band word + colour are drawer-only. The transition shim is the
// single predicate Number.isFinite(spread) — null / NaN / undefined degrade to the
// no-band / coverage state deterministically (the deploy-window guarantee).

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

describe('consensusTone (disagreement bands, drawer-only)', () => {
	it('green < 20, amber 20-49, red >= 50', () => {
		expect(consensusTone(0)).toBe('green');
		expect(consensusTone(19)).toBe('green');
		expect(consensusTone(20)).toBe('amber');
		expect(consensusTone(49)).toBe('amber');
		expect(consensusTone(50)).toBe('red');
		expect(consensusTone(100)).toBe('red');
	});
	it('absent / non-finite is muted (no band)', () => {
		expect(consensusTone(null)).toBe('muted');
		expect(consensusTone(undefined)).toBe('muted');
		expect(consensusTone(NaN)).toBe('muted');
		expect(consensusTone(Infinity)).toBe('muted');
	});
});

describe('consensusBand', () => {
	it('descriptive words, dash when absent', () => {
		expect(consensusBand(5)).toBe('consensus');
		expect(consensusBand(35)).toBe('mixed');
		expect(consensusBand(70)).toBe('split');
		expect(consensusBand(null)).toBe('—');
		expect(consensusBand(NaN)).toBe('—');
	});
});

describe('transition-shim precedence (pure-function level)', () => {
	// The shim's single predicate: a finite spread => band+headline render path;
	// anything else (null/NaN/Infinity/undefined) => no band, deterministic.
	const renders = (spread: number | null | undefined) => Number.isFinite(spread as number);
	it('finite spread renders the band; non-finite never does', () => {
		expect(renders(47)).toBe(true);
		expect(renders(0)).toBe(true); // 0 = agreement is a REAL band (consensus), not "absent"
		expect(consensusBand(0)).toBe('consensus');
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
