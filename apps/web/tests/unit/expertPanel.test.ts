import { describe, expect, it } from 'vitest';
import {
	oneilTone,
	consensusTone,
	consensusBand,
	panelMagnitudeFormula
} from '../../src/lib/format';

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

describe('panelMagnitudeFormula (decode config slug → human magnitude, drawer footer)', () => {
	it('decodes the abs-difference family to the plain gap (the trailing Nx is ARITY, not a multiplier)', () => {
		// disagreement.py compute_spread() = max(present) − min(present) = abs(buffett − oneil).
		// The `2x` in `absdiff-2x` is the 2-expert ARITY (the range over two lens scores),
		// NOT a ×2 scale — so it must decode to the plain gap, never "× 2". Matches both the
		// v1 and the O'Neil-R (v1r) slug; a term-set bump alone does not change the magnitude.
		expect(panelMagnitudeFormula('panel-v1r-absdiff-2x')).toBe("|Buffett − O'Neil|");
		expect(panelMagnitudeFormula('panel-v1-absdiff-2x')).toBe("|Buffett − O'Neil|");
		expect(panelMagnitudeFormula('panel-v2-absdiff')).toBe("|Buffett − O'Neil|");
	});
	it('falls back to a generic, never-wrong phrase for unknown / absent / non-absdiff slugs', () => {
		// The reserved `pstdev-3x` family is a DIFFERENT dispersion measure (population
		// std-dev over 3 scores) — decoding it as |Buffett − O'Neil| would be wrong, so it
		// degrades to the generic phrase, as does any future formula we have not taught.
		expect(panelMagnitudeFormula('panel-v2-pstdev-3x')).toBe('gap between lens scores');
		expect(panelMagnitudeFormula('panel-v9-zscore')).toBe('gap between lens scores');
		expect(panelMagnitudeFormula('')).toBe('gap between lens scores');
		expect(panelMagnitudeFormula(null)).toBe('gap between lens scores');
		expect(panelMagnitudeFormula(undefined)).toBe('gap between lens scores');
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
