import { describe, it, expect } from 'vitest';
import { insiderDisplay } from '../../src/lib/format';

// The insider sector-percentile is a `<=`-rank: a ticker with ZERO net
// opportunistic buying ranks ~100th whenever its sector peers are net
// sellers (0 <= 0 counts). Showing that as a green "100%ile" chip reads as
// "strong insider buying" when it really means "not selling, in a selling
// sector". insiderDisplay gates the bullish percentile bar on actual net
// buying (insider_score_usd > 0); everything else renders a muted, honest
// state — never a high percentile on a zero/negative dollar signal.
describe('insiderDisplay', () => {
	it('no Form-4 data (null) -> muted dash', () => {
		expect(insiderDisplay(null, null)).toEqual({ mode: 'muted', label: '—' });
		expect(insiderDisplay(undefined, 100)).toEqual({ mode: 'muted', label: '—' });
		expect(insiderDisplay(Number.NaN, 100)).toEqual({ mode: 'muted', label: '—' });
	});

	it('zero net opportunistic USD -> muted "no buys" (NOT a percentile bar)', () => {
		expect(insiderDisplay(0, 100)).toEqual({ mode: 'muted', label: 'no buys' });
		// The pathological live case: 0 dollars but 100th percentile must NOT
		// surface as a bullish bar.
		expect(insiderDisplay(0, 100).mode).toBe('muted');
	});

	it('net selling (<0) -> muted "net selling"', () => {
		expect(insiderDisplay(-500_000, 88)).toEqual({ mode: 'muted', label: 'net selling' });
	});

	it('real net buying (>0) -> percentile bar', () => {
		expect(insiderDisplay(120_000, 80)).toEqual({
			mode: 'bar',
			percentile: 80,
			netUsd: 120_000
		});
	});

	it('net buying but thin cohort (percentile null) -> bar with null percentile', () => {
		expect(insiderDisplay(120_000, null)).toEqual({
			mode: 'bar',
			percentile: null,
			netUsd: 120_000
		});
	});
});
