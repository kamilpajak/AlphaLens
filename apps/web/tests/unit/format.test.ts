import { describe, it, expect } from 'vitest';
import { fmtPct, fmtPctile, fcffYieldRawDisplay, tenkAvailable } from '../../src/lib/format';

describe('fmtPctile — percentile RANK (O\'Neil rel-strength uses this, not fmtPct)', () => {
	it('rounds to an integer with no sign and no % suffix', () => {
		// The rel-strength value is a 0-100 percentile, e.g. TTD 4.84 -> "5%ile" once
		// suffixed by the caller. fmtPctile itself never emits a sign or a "%".
		expect(fmtPctile(4.84)).toBe('5');
		expect(fmtPctile(89.3)).toBe('89');
		expect(fmtPctile(0)).toBe('0');
		expect(fmtPctile(100)).toBe('100');
	});

	it('never prefixes a "+" sign (a percentile is unsigned, unlike a % change)', () => {
		expect(fmtPctile(4.84).startsWith('+')).toBe(false);
		expect(fmtPctile(50).includes('%')).toBe(false);
	});

	it('renders missing / non-finite as the em-dash placeholder', () => {
		expect(fmtPctile(null)).toBe('—');
		expect(fmtPctile(undefined)).toBe('—');
		expect(fmtPctile(NaN)).toBe('—');
	});
});

describe('fmtPct — signed % change (the WRONG formatter for a percentile rank)', () => {
	it('emits a leading + and a trailing % — why rel-strength must NOT use it', () => {
		// This is exactly the misleading "+4.8%" the rel-strength readout used to show.
		expect(fmtPct(4.84)).toBe('+4.8%');
		expect(fmtPct(-78.5)).toBe('-78.5%');
	});
});

describe('fcffYieldRawDisplay (merged valuation fcff raw annotation)', () => {
	it('finite → signed 2-decimal %', () => {
		expect(fcffYieldRawDisplay(5.09)).toBe('+5.09%');
	});
	it('negative keeps its sign', () => {
		expect(fcffYieldRawDisplay(-2.5)).toBe('-2.50%');
	});
	it('null / undefined / NaN → null', () => {
		expect(fcffYieldRawDisplay(null)).toBe(null);
		expect(fcffYieldRawDisplay(undefined)).toBe(null);
		expect(fcffYieldRawDisplay(NaN)).toBe(null);
	});
});

describe('tenkAvailable (10-K presence from gate arrays)', () => {
	it('true when tenk passed (keywords matched)', () => {
		expect(tenkAvailable(['tenk'], ['press'])).toBe(true);
	});
	it('true when tenk failed (10-K exists, no keyword match)', () => {
		expect(tenkAvailable(['press'], ['tenk'])).toBe(true);
	});
	it('false when tenk only unknown / absent from both arrays', () => {
		expect(tenkAvailable(['press'], ['insider'])).toBe(false);
	});
	it('false / safe on null / undefined inputs', () => {
		expect(tenkAvailable(null, undefined)).toBe(false);
		expect(tenkAvailable(undefined, null)).toBe(false);
	});
});
