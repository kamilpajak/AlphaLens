import { describe, it, expect } from 'vitest';
import {
	fmtPct,
	fmtPctile,
	fcffYieldRawDisplay,
	tenkAvailable,
	selectionBadge,
	catalystLabel,
	fmtSigned,
	fmtUsdCompact
} from '../../src/lib/format';

describe('fmtSigned — a signed fixed-decimal number (+1.20 / -1.20 / —)', () => {
	it('prefixes non-negative values with +', () => {
		expect(fmtSigned(1.2, 2)).toBe('+1.20');
		expect(fmtSigned(0, 2)).toBe('+0.00');
	});
	it('leaves the built-in minus for negatives (no double sign)', () => {
		expect(fmtSigned(-1.2, 2)).toBe('-1.20');
	});
	it('rounds to the requested digits (default 2)', () => {
		expect(fmtSigned(1.23, 1)).toBe('+1.2');
		expect(fmtSigned(3.456)).toBe('+3.46');
	});
	it('null / undefined / non-finite → em dash', () => {
		expect(fmtSigned(null)).toBe('—');
		expect(fmtSigned(undefined)).toBe('—');
		expect(fmtSigned(NaN)).toBe('—');
		expect(fmtSigned(Infinity)).toBe('—');
	});
});

describe('fmtUsdCompact — the single compact USD formatter (market caps AND typed facts)', () => {
	// TemplateFacts *_usd values are structurally >= $1M (the extractor's
	// _normalize_amount_usd returns None below that), so only the B and M
	// bands ever render for fact data. Pin the fact magnitudes here since no
	// unit test covered any fmtUsdCompact currency string before the fold.
	it('renders billions with two decimals ($5.00B / $9.00B) — matches market-cap style', () => {
		expect(fmtUsdCompact(5_000_000_000)).toBe('$5.00B');
		expect(fmtUsdCompact(9_000_000_000)).toBe('$9.00B');
		expect(fmtUsdCompact(1_230_000_000)).toBe('$1.23B');
	});
	it('renders millions with one decimal ($450.0M)', () => {
		expect(fmtUsdCompact(450_000_000)).toBe('$450.0M');
	});
	it('null / undefined / non-finite → em dash', () => {
		expect(fmtUsdCompact(null)).toBe('—');
		expect(fmtUsdCompact(undefined)).toBe('—');
		expect(fmtUsdCompact(NaN)).toBe('—');
	});
});

describe('catalystLabel — humanise the raw event-type enum', () => {
	it('maps acronyms', () => {
		expect(catalystLabel('m_and_a')).toBe('M&A');
		expect(catalystLabel('ipo')).toBe('IPO');
	});
	it('replaces underscores with spaces otherwise', () => {
		expect(catalystLabel('product_launch')).toBe('product launch');
		expect(catalystLabel('exec_change')).toBe('exec change');
		expect(catalystLabel('macro')).toBe('macro');
	});
	it('null/empty → null (caller drops the suffix)', () => {
		expect(catalystLabel(null)).toBe(null);
		expect(catalystLabel(undefined)).toBe(null);
		expect(catalystLabel('')).toBe(null);
	});
});

describe('selectionBadge — meta-bar headline (operative ranking score)', () => {
	it('shows selection_score, no decimals when integer-valued (penalty=0)', () => {
		expect(selectionBadge(3, 3)).toBe('3');
	});
	it('shows 2 decimals when an ATR tilt makes it fractional', () => {
		expect(selectionBadge(1.4933603274784968, 2)).toBe('1.49');
		expect(selectionBadge(0.4879744561060064, 1)).toBe('0.49');
	});
	it('falls back to layer4 when selection_score is absent', () => {
		expect(selectionBadge(null, 2)).toBe('2');
		expect(selectionBadge(undefined, 3)).toBe('3');
	});
	it('em-dash when neither is finite', () => {
		expect(selectionBadge(null, null)).toBe('—');
		expect(selectionBadge(NaN, undefined)).toBe('—');
	});
});

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
