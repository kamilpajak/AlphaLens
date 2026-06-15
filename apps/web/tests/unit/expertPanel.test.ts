import { describe, expect, it } from 'vitest';
import {
	oneilTone,
	consensusTone,
	consensusBand,
	panelCoverageLabel,
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

describe('panelCoverageLabel (resting +1 token, tone-neutral)', () => {
	it('counts finite scores: 2 lenses / 1 lens / dash', () => {
		expect(panelCoverageLabel(31, 78)).toBe('2 lenses');
		expect(panelCoverageLabel(31, null)).toBe('1 lens');
		expect(panelCoverageLabel(null, 78)).toBe('1 lens');
		expect(panelCoverageLabel(null, null)).toBe('—');
	});
	it('non-finite scores do not count as present', () => {
		expect(panelCoverageLabel(NaN, 78)).toBe('1 lens');
		expect(panelCoverageLabel(undefined, undefined)).toBe('—');
		expect(panelCoverageLabel(Infinity, NaN)).toBe('—');
	});
});

describe('panelMagnitudeFormula (decode config slug → human magnitude, drawer footer)', () => {
	it('decodes the known abs-difference formulae from the version slug', () => {
		// `absdiff-2x` => |Buffett − O'Neil| scaled ×2; matches both the v1 and
		// the O'Neil-R (v1r) slug so a term-set bump alone does not re-jargon it.
		expect(panelMagnitudeFormula('panel-v1r-absdiff-2x')).toBe("|Buffett − O'Neil| × 2");
		expect(panelMagnitudeFormula('panel-v1-absdiff-2x')).toBe("|Buffett − O'Neil| × 2");
		// abs-difference without the ×2 scale tag => the unscaled gap.
		expect(panelMagnitudeFormula('panel-v2-absdiff')).toBe("|Buffett − O'Neil|");
	});
	it('falls back to a generic, never-wrong phrase for unknown / absent slugs', () => {
		// A future formula we have not taught the decoder must NOT be mis-described.
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
