import { describe, expect, it } from 'vitest';
import { marketLabel } from '../../src/lib/marketLabel';

// The footer session chip shows a human "market" label, not the raw ISO 10383
// MIC. The candidate universe spans XNYS + XNAS (Nasdaq) + XASE (NYSE American),
// all sharing one US-equity session calendar, so a single MIC (XNYS) would
// under-sell the scope and mislead. `marketLabel` collapses every US venue to
// "US MARKET". Unknown / not-yet-mapped venues fall back to the raw MIC so a
// newly wired exchange still renders something sensible instead of blank.

describe('marketLabel', () => {
	it('maps every US venue to "US MARKET"', () => {
		expect(marketLabel('XNYS')).toBe('US MARKET');
		expect(marketLabel('XNAS')).toBe('US MARKET');
		expect(marketLabel('XASE')).toBe('US MARKET');
	});

	it('falls back to the raw MIC for a not-yet-mapped venue', () => {
		// XWAR (Warsaw) is not wired server-side yet; until a label is added it
		// shows the raw MIC rather than a wrong "US MARKET".
		expect(marketLabel('XWAR')).toBe('XWAR');
	});

	it('normalises casing / whitespace before matching', () => {
		expect(marketLabel(' xnys ')).toBe('US MARKET');
	});
});
