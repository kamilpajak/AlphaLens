import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, it, expect } from 'vitest';
import {
	fmtPct,
	fmtPctile,
	fcffYieldRawDisplay,
	tenkAvailable,
	selectionBadge,
	catalystLabel,
	fmtSigned,
	fmtUsdCompact,
	fmtFracSignedPct,
	marketStateTone,
	marketStateLabel,
	MARKET_STATE_ATR_HIGH_PCTILE,
	MARKET_STATE_VIX_HIGH,
	MARKET_STATE_TREND_FLIP_PCT
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

describe('marketStateTone — display-only tone for the market-context banner', () => {
	it('maps each of the 4 regime states to its memo tone', () => {
		expect(marketStateTone('bull_quiet')).toBe('green');
		expect(marketStateTone('bull_volatile')).toBe('amber');
		expect(marketStateTone('bear_volatile')).toBe('red');
		expect(marketStateTone('bear_quiet')).toBe('red-dim');
	});
	it('unknown / absent / unrecognised → muted (never a false regime colour)', () => {
		expect(marketStateTone('unknown')).toBe('muted');
		expect(marketStateTone('')).toBe('muted');
		expect(marketStateTone(null)).toBe('muted');
		expect(marketStateTone(undefined)).toBe('muted');
		expect(marketStateTone('bananas')).toBe('muted');
	});
});

describe('marketStateLabel — hyphenated regime label for the chip', () => {
	it('renders each known state as trend-vol hyphenated', () => {
		expect(marketStateLabel('bull_quiet')).toBe('bull-quiet');
		expect(marketStateLabel('bull_volatile')).toBe('bull-volatile');
		expect(marketStateLabel('bear_volatile')).toBe('bear-volatile');
		expect(marketStateLabel('bear_quiet')).toBe('bear-quiet');
	});
	it('null / undefined / empty / unrecognised → "unknown"', () => {
		expect(marketStateLabel('unknown')).toBe('unknown');
		expect(marketStateLabel(null)).toBe('unknown');
		expect(marketStateLabel(undefined)).toBe('unknown');
		expect(marketStateLabel('')).toBe('unknown');
		expect(marketStateLabel('bananas')).toBe('unknown');
	});
});

describe('fmtFracSignedPct — a FRACTION rendered as a signed percentage', () => {
	it('scales by 100 and keeps the sign', () => {
		expect(fmtFracSignedPct(-0.038, 2)).toBe('-3.80%');
		expect(fmtFracSignedPct(0.016, 2)).toBe('+1.60%');
		expect(fmtFracSignedPct(0, 2)).toBe('+0.00%');
	});
	it('null / undefined / non-finite → em dash', () => {
		expect(fmtFracSignedPct(null)).toBe('—');
		expect(fmtFracSignedPct(undefined)).toBe('—');
		expect(fmtFracSignedPct(Number.NaN)).toBe('—');
		expect(fmtFracSignedPct(Number.POSITIVE_INFINITY)).toBe('—');
	});
	it('does NOT render a fraction as if it were already a percent', () => {
		// The #446/#447 defect in one assertion: a 3.8% move must not read as
		// -0.04%. The producing code stores the FRACTION.
		expect(fmtFracSignedPct(-0.038, 2)).not.toBe('-0.04%');
	});
});

describe('market-context flip points agree with the Python classifier', () => {
	// The banner annotates each driver with the value at which the label flips.
	// Reading the producing module here turns a drift into a red test rather
	// than a quietly wrong caption (the toolchainPin.test.ts precedent).
	const classifier = readFileSync(
		join(
			__dirname,
			'..',
			'..',
			'..',
			'alphalens-pipeline',
			'alphalens_pipeline',
			'market',
			'market_state.py'
		),
		'utf8'
	);

	function constant(name: string): number {
		const match = new RegExp(`^${name} = ([0-9.]+)$`, 'm').exec(classifier);
		if (!match) throw new Error(`${name} not found in market_state.py`);
		return Number(match[1]);
	}

	it('the ATR flip point is the classifier ATR_HIGH_Q as a percentile', () => {
		expect(MARKET_STATE_ATR_HIGH_PCTILE).toBe(Math.round(constant('ATR_HIGH_Q') * 100));
	});

	it('the VIX flip point is the classifier VIX_HIGH', () => {
		expect(MARKET_STATE_VIX_HIGH).toBe(constant('VIX_HIGH'));
	});

	it('the trend flip point is ZERO', () => {
		// The classifier's trend word is exactly `dist200 >= 0`, so zero is the
		// flip point by construction rather than by measurement. It used to be
		// a measured claim: a ±2% `DIST_FLAT_BAND` deadband and an SMA50 slope
		// sat in the Python rule but could only route the answer to a `neutral`
		// that folded back by sign(dist200), making all of it inert. Those
		// values were deleted on 2026-10-30.
		expect(MARKET_STATE_TREND_FLIP_PCT).toBe(0);
	});

	it('no deadband constant has been reintroduced on the Python side', () => {
		// The anti-reintroduction pin. If a future change puts a deadband or a
		// slope window back into the classifier, this face annotation stops
		// being correct by construction and silently becomes a claim again —
		// so the names must stay absent, not merely unused.
		for (const name of ['DIST_FLAT_BAND', 'SLOPE_EPS', 'SLOPE_WIN', 'SMA_FAST']) {
			expect(
				new RegExp(`^${name} = `, 'm').test(classifier),
				`${name} is back in market_state.py; MARKET_STATE_TREND_FLIP_PCT may no longer be 0`
			).toBe(false);
		}
		// Positive control: the regex DOES find a constant that is still there,
		// so the loop above cannot pass by failing to read the file.
		expect(new RegExp('^SMA_SLOW = ', 'm').test(classifier)).toBe(true);
	});
});
