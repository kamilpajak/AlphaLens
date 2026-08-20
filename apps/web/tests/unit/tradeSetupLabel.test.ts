import { describe, it, expect } from 'vitest';
import { entryTierLabel } from '$lib/tradeSetupRisk';
import type { EntryTier } from '$lib/types';

const bare = (i: number): EntryTier => ({
	limit: 100 - i,
	alloc_pct: 30,
	atr_distance: 0.5 * (i + 1),
	tag: 'x'
});

describe('entryTierLabel', () => {
	it('derives E1/E2/E3 from position when the tier has no label', () => {
		expect([0, 1, 2].map((i) => entryTierLabel(bare(i), i))).toEqual(['E1', 'E2', 'E3']);
	});

	it('prefers a canonical data label when present', () => {
		expect(entryTierLabel({ ...bare(0), label: 'E7' }, 0)).toBe('E7');
	});

	it('falls back to position when label is an empty string', () => {
		expect(entryTierLabel({ ...bare(1), label: '' }, 1)).toBe('E2');
	});
});
