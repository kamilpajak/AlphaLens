import { describe, expect, it } from 'vitest';
import {
	fullLadderBlendedEntry,
	stopDistanceFracFull,
	impliedRiskPctOfBook
} from '../../src/lib/tradeSetupRisk';
import type { EntryTier } from '../../src/lib/types';

// Pins the forward-looking risk arithmetic the SETUP panel surfaces, mirroring
// the canonical Python (`population_ladder_monitor._full_ladder_blended_entry`
// + `_size_fields`). The hand-computed case is the live BLBD 2026-05-27 ladder.
const tier = (limit: number, alloc_pct: number): EntryTier => ({
	limit,
	alloc_pct,
	atr_distance: 0,
	tag: ''
});

describe('fullLadderBlendedEntry', () => {
	it('alloc-weights over all three tiers (BLBD 28/34/38)', () => {
		const tiers = [tier(69.22, 28), tier(64.28, 34), tier(62.56, 38)];
		// (69.22*28 + 64.28*34 + 62.56*38) / 100 = 65.0096
		expect(fullLadderBlendedEntry(tiers)).toBeCloseTo(65.0096, 4);
	});

	it('falls back to equal-weight when allocs are absent/zero', () => {
		const tiers = [tier(60, 0), tier(70, 0)];
		expect(fullLadderBlendedEntry(tiers)).toBeCloseTo(65, 6);
	});

	it('returns null for empty / missing tiers', () => {
		expect(fullLadderBlendedEntry([])).toBeNull();
		expect(fullLadderBlendedEntry(null)).toBeNull();
		expect(fullLadderBlendedEntry(undefined)).toBeNull();
	});

	it('skips non-finite limit prices', () => {
		const tiers = [tier(Number.NaN, 50), tier(70, 50)];
		expect(fullLadderBlendedEntry(tiers)).toBeCloseTo(70, 6);
	});
});

describe('stopDistanceFracFull', () => {
	it('computes (blended - stop) / blended as a fraction (BLBD)', () => {
		// (65.0096 - 48.94) / 65.0096 = 0.24718...
		expect(stopDistanceFracFull(65.0096, 48.94)).toBeCloseTo(0.2472, 4);
	});

	it('returns null on missing / non-finite / zero blended', () => {
		expect(stopDistanceFracFull(null, 48.94)).toBeNull();
		expect(stopDistanceFracFull(65, null)).toBeNull();
		expect(stopDistanceFracFull(0, 48.94)).toBeNull();
		expect(stopDistanceFracFull(Number.NaN, 48.94)).toBeNull();
	});
});

describe('impliedRiskPctOfBook', () => {
	it('multiplies suggested-size percent by stop-distance fraction → percent of book (BLBD ≈ 1.0%)', () => {
		// 4.07 (percent) * 0.2472 (fraction) = 1.006 (percent of book)
		expect(impliedRiskPctOfBook(4.07, 0.2472)).toBeCloseTo(1.006, 3);
	});

	it('returns null when suggested size or stop distance is missing', () => {
		expect(impliedRiskPctOfBook(null, 0.25)).toBeNull();
		expect(impliedRiskPctOfBook(4.07, null)).toBeNull();
		expect(impliedRiskPctOfBook(Number.NaN, 0.25)).toBeNull();
	});

	it('end-to-end on the BLBD ladder composes to ≈ 1.0% of book', () => {
		const tiers = [tier(69.22, 28), tier(64.28, 34), tier(62.56, 38)];
		const blended = fullLadderBlendedEntry(tiers);
		const frac = stopDistanceFracFull(blended, 48.94);
		const risk = impliedRiskPctOfBook(4.07, frac);
		expect(risk).toBeCloseTo(1.006, 2);
	});
});
