import { describe, expect, it } from 'vitest';
import {
	classificationTone,
	EXCESS_RETURN_BAR_DOMAIN,
	excessBarGeometry,
	fmtFracPct,
	fmtR,
	SIZING_MODEL_RISK_LABEL,
	statsUnlocked,
	statusLabel,
	toneClasses,
	tpCaptureLabel,
	excessCellState
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

	it('renders a whole-percent rate unsigned (hit-rate: 0 → 0%, not — / +0%)', () => {
		// The EDGE-panel hit-rate cell uses fmtFracPct(rate, 0, false): a 0% hit
		// rate is a real, meaningful value and must render as "0%", never hidden.
		expect(fmtFracPct(0, 0, false)).toBe('0%');
		expect(fmtFracPct(0.6944, 0, false)).toBe('69%');
		expect(fmtFracPct(1, 0, false)).toBe('100%');
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

describe('per-name sizing panel copy', () => {
	// The shared-book aggregates (size-weighted R, book contribution) were removed
	// — they assumed one shared capital book that does NOT exist for this
	// decision-support tool (a vestige of the decommissioned paper-trade chain,
	// ADR 0012). Only the per-name suggested risk remains.
	it('relabels mean-risk as a per-name suggestion against a 1% budget', () => {
		expect(SIZING_MODEL_RISK_LABEL).toBe('suggested risk per name (1% budget)');
	});
});

// Mirrors the /edge outcomes table guard:
// `{#if row.scorer_config_version}` — truthy-only, so empty string and null both hide.
function showsScorerVersionChip(scorer_config_version: string | null | undefined): boolean {
	return !!scorer_config_version;
}

describe('scorer_config_version chip visibility (/edge outcomes row)', () => {
	it('shows when scorer_config_version is a non-empty string', () => {
		expect(showsScorerVersionChip('atr-tilt-v1')).toBe(true);
		expect(showsScorerVersionChip('v2')).toBe(true);
	});

	it('does NOT show when scorer_config_version is null', () => {
		expect(showsScorerVersionChip(null)).toBe(false);
	});

	it('does NOT show when scorer_config_version is undefined', () => {
		expect(showsScorerVersionChip(undefined)).toBe(false);
	});

	it('does NOT show when scorer_config_version is an empty string', () => {
		expect(showsScorerVersionChip('')).toBe(false);
	});
});

describe('tpCaptureLabel (/edge outcomes CLASS chip honesty)', () => {
	it('labels a partial capture when fewer TPs sold than touched', () => {
		// DFIN case: all three TP levels touched, only one sold.
		expect(tpCaptureLabel({ captured_tp_count: 1, touched_tp_count: 3 })).toBe('1/3 sold');
	});

	it('returns null when every touched TP was also sold (honest TP_FULL)', () => {
		expect(tpCaptureLabel({ captured_tp_count: 3, touched_tp_count: 3 })).toBeNull();
	});

	it('returns null when no TP was touched', () => {
		expect(tpCaptureLabel({ captured_tp_count: 0, touched_tp_count: 0 })).toBeNull();
	});

	it('returns null when the counts are unknown (older rows)', () => {
		expect(tpCaptureLabel({ captured_tp_count: null, touched_tp_count: null })).toBeNull();
		expect(tpCaptureLabel({ captured_tp_count: null, touched_tp_count: 3 })).toBeNull();
	});
});

describe('excessCellState (/edge EXCESS RETURN cell)', () => {
	it('is "value" when the excess is present', () => {
		expect(
			excessCellState({
				market_excess_return: 0.04,
				forward_return: 0.06,
				benchmark_window_return: 0.02
			})
		).toBe('value');
	});

	it('is "value" for a real zero excess (not treated as missing)', () => {
		expect(
			excessCellState({
				market_excess_return: 0,
				forward_return: 0.02,
				benchmark_window_return: 0.02
			})
		).toBe('value');
	});

	it('is "pending" when the stock return is known but the SPY benchmark leg is missing', () => {
		// DFIN case: forward_return present, benchmark NULL → a retriable data gap
		// that recomputes nightly, NOT a genuine n/a.
		expect(
			excessCellState({
				market_excess_return: null,
				forward_return: 0.107,
				benchmark_window_return: null
			})
		).toBe('pending');
	});

	it('is "na" when neither the excess nor the stock return is available', () => {
		expect(
			excessCellState({
				market_excess_return: null,
				forward_return: null,
				benchmark_window_return: null
			})
		).toBe('na');
	});
});
