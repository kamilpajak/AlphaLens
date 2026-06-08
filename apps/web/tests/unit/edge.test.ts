import { describe, expect, it } from 'vitest';
import {
	classificationTone,
	EXCESS_RETURN_BAR_DOMAIN,
	excessBarGeometry,
	fmtFracPct,
	fmtR,
	statsUnlocked,
	statusLabel,
	toneClasses
} from '../../src/lib/edge';

// Pins the pure derivation/formatting the /edge dashboard relies on: the
// classification → colour map (memo §5), the CENTERED excess-R bar geometry
// (zero in the middle, magnitude scaled + clamped to the half-track), and
// R formatting (signed, "R" suffix). No client-side aggregation is exercised —
// the N-gate is server-side; statsUnlocked is only a presentation switch.

describe('classificationTone', () => {
	it('maps the known ladder classifications to memo §5 colours', () => {
		expect(classificationTone('TP_FULL')).toBe('green');
		expect(classificationTone('SL_HIT')).toBe('red');
		expect(classificationTone('TIME_STOP')).toBe('amber');
		expect(classificationTone('OPEN')).toBe('cyan');
	});

	it('maps both partial-take-profit states to violet', () => {
		expect(classificationTone('PARTIAL_TP_OPEN')).toBe('violet');
		expect(classificationTone('PARTIAL_TP_THEN_SL')).toBe('violet');
	});

	it('is case- and whitespace-insensitive', () => {
		expect(classificationTone(' tp_full ')).toBe('green');
		expect(classificationTone('Sl_Hit')).toBe('red');
		expect(classificationTone('partial_tp_open')).toBe('violet');
	});

	it('falls back to muted for unknown / NO_FILL / null', () => {
		expect(classificationTone('NO_FILL')).toBe('muted');
		expect(classificationTone('SOMETHING_ELSE')).toBe('muted');
		expect(classificationTone(null)).toBe('muted');
		expect(classificationTone(undefined)).toBe('muted');
		expect(classificationTone('')).toBe('muted');
	});

	it('maps each tone to its border + text classes', () => {
		expect(toneClasses('green')).toBe('border-green text-green');
		expect(toneClasses('violet')).toBe('border-violet text-violet');
		expect(toneClasses('muted')).toBe('border-grid-strong text-fg-muted');
	});
});

describe('excessBarGeometry', () => {
	it('centers a positive value rightward from the midpoint', () => {
		// +0.5R at domain 1.0 → 50% of the half-track (50% of 50% = 25%).
		const g = excessBarGeometry(0.5, 1.0);
		expect(g.left).toBe(50);
		expect(g.width).toBeCloseTo(25, 6);
		expect(g.positive).toBe(true);
	});

	it('centers a negative value leftward, ending at the midpoint', () => {
		// -0.5R at domain 1.0 → width 25%, left = 50 - 25 = 25.
		const g = excessBarGeometry(-0.5, 1.0);
		expect(g.left).toBeCloseTo(25, 6);
		expect(g.width).toBeCloseTo(25, 6);
		expect(g.positive).toBe(false);
	});

	it('clamps a value beyond the domain to the half-track edge', () => {
		const pos = excessBarGeometry(3.0, 1.0);
		expect(pos.width).toBeCloseTo(50, 6);
		expect(pos.left).toBe(50);
		const neg = excessBarGeometry(-3.0, 1.0);
		expect(neg.width).toBeCloseTo(50, 6);
		expect(neg.left).toBeCloseTo(0, 6);
	});

	it('returns a zero-width segment centered at 50 for null / non-finite / bad domain', () => {
		for (const v of [null, undefined, Number.NaN, Number.POSITIVE_INFINITY]) {
			const g = excessBarGeometry(v as number | null | undefined, 1.0);
			expect(g).toEqual({ left: 50, width: 0, positive: false });
		}
		expect(excessBarGeometry(0.5, 0)).toEqual({ left: 50, width: 0, positive: false });
	});

	it('treats exactly zero as a positive (right-anchored) zero-width segment', () => {
		const g = excessBarGeometry(0, 1.0);
		expect(g.left).toBe(50);
		expect(g.width).toBe(0);
		expect(g.positive).toBe(true);
	});

	it('respects a custom domain', () => {
		// +1.0R at domain 2.0 → frac 0.5 → half the half-track → 25% width.
		const g = excessBarGeometry(1.0, 2.0);
		expect(g.width).toBeCloseTo(25, 6);
	});
});

describe('fmtR', () => {
	it('prepends + on non-negative and renders the R suffix', () => {
		expect(fmtR(0.41)).toBe('+0.41R');
		expect(fmtR(0)).toBe('+0.00R');
		expect(fmtR(1.85)).toBe('+1.85R');
	});

	it('keeps the minus sign on negatives', () => {
		expect(fmtR(-0.88)).toBe('-0.88R');
	});

	it('honours a custom digit count', () => {
		expect(fmtR(0.4, 1)).toBe('+0.4R');
	});

	it('renders an em dash for null / non-finite', () => {
		expect(fmtR(null)).toBe('—');
		expect(fmtR(undefined)).toBe('—');
		expect(fmtR(Number.NaN)).toBe('—');
	});
});

describe('fmtFracPct', () => {
	it('scales a fraction-of-book to a percent string (the % BOOK / risk% bug)', () => {
		// The population-monitor size columns store FRACTIONS (0.0021 = 0.21%).
		// fmtPct alone (no ×100) would render "+0.00%" — the regression this fixes.
		expect(fmtFracPct(0.0021, 2)).toBe('+0.21%');
		expect(fmtFracPct(0.05, 2)).toBe('+5.00%');
		expect(fmtFracPct(0.0008, 2)).toBe('+0.08%');
	});

	it('drops the leading sign when withSign=false (risk%, fill-rate)', () => {
		expect(fmtFracPct(0.0033, 2, false)).toBe('0.33%');
		expect(fmtFracPct(0.5314, 1, false)).toBe('53.1%');
	});

	it('keeps the minus sign on negative contributions', () => {
		expect(fmtFracPct(-0.0033, 2)).toBe('-0.33%');
	});

	it('renders +0.00% for an exact zero', () => {
		expect(fmtFracPct(0, 2)).toBe('+0.00%');
	});

	it('renders an em dash for null / undefined / non-finite', () => {
		expect(fmtFracPct(null)).toBe('—');
		expect(fmtFracPct(undefined)).toBe('—');
		expect(fmtFracPct(Number.NaN)).toBe('—');
		expect(fmtFracPct(Number.POSITIVE_INFINITY)).toBe('—');
	});
});

describe('EXCESS_RETURN_BAR_DOMAIN', () => {
	it('maps a benchmark-excess RETURN (fraction) onto the bar, ±25% → edge', () => {
		// A terminal row's bar value is an excess RETURN (e.g. 0.2085 = +20.85%
		// over SPY), not an R-multiple — so the domain is in return units.
		const g = excessBarGeometry(EXCESS_RETURN_BAR_DOMAIN, EXCESS_RETURN_BAR_DOMAIN);
		expect(g.width).toBeCloseTo(50, 6); // a +25% excess fills the half-track
		expect(g.positive).toBe(true);
		// AMPL's real +20.85% excess sits most of the way out, not a sliver.
		const ampl = excessBarGeometry(0.2085, EXCESS_RETURN_BAR_DOMAIN);
		expect(ampl.width).toBeGreaterThan(35);
	});
});

describe('statsUnlocked / statusLabel', () => {
	it('hides stats only for the insufficient gate', () => {
		expect(statsUnlocked('insufficient')).toBe(false);
		expect(statsUnlocked('early')).toBe(true);
		expect(statsUnlocked('ok')).toBe(true);
	});

	it('labels each gate state', () => {
		expect(statusLabel('insufficient')).toBe('insufficient data');
		expect(statusLabel('early')).toContain('early');
		expect(statusLabel('ok')).toBe('unlocked');
	});
});
