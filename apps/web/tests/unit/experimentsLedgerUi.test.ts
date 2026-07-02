import { describe, expect, it } from 'vitest';
import { alphaValueTone, stripLedgerMarkup } from '../../src/lib/data/research-ledger';

// Pins the pure UI helpers behind the /experiments scannability pass:
// - alphaValueTone colours the IS/OOS bar VALUE by the doctrine thresholds
//   (<0 red · <2 muted · <3.5 amber · ≥3.5 green) — text colour only, no border.
// (The status-coloured left rail was removed — verdict colour now reads off the
// status pill alone, so the row carries no coloured edge and statusRail is gone.)

describe('alphaValueTone', () => {
	it('null / non-finite → muted', () => {
		expect(alphaValueTone(null)).toBe('text-fg-muted');
		expect(alphaValueTone(NaN)).toBe('text-fg-muted');
		expect(alphaValueTone(Infinity)).toBe('text-fg-muted');
		expect(alphaValueTone(-Infinity)).toBe('text-fg-muted');
	});
	it('negative → red', () => {
		expect(alphaValueTone(-1.2)).toBe('text-red');
	});
	it('below the marginal bar (2.0) → muted', () => {
		expect(alphaValueTone(0.15)).toBe('text-fg-muted');
		expect(alphaValueTone(1.99)).toBe('text-fg-muted');
	});
	it('marginal band [2.0, 3.5) → amber', () => {
		expect(alphaValueTone(2.0)).toBe('text-amber');
		expect(alphaValueTone(3.49)).toBe('text-amber');
	});
	it('doctrine bar (≥3.5) → green', () => {
		expect(alphaValueTone(3.5)).toBe('text-green');
		expect(alphaValueTone(4.2)).toBe('text-green');
	});
});

// Strips the [term] / [term|label] tooltip markup down to plain text — the
// status-chip tooltips render the definition as a plain body (the JargonTip
// popover is pointer-events-none, so nested inline tips can't live inside it).
describe('stripLedgerMarkup', () => {
	it('leaves plain text unchanged', () => {
		expect(stripLedgerMarkup('tested and rejected')).toBe('tested and rejected');
	});
	it('[term] → term', () => {
		expect(stripLedgerMarkup('rejected on [multi-phase audit] gates')).toBe(
			'rejected on multi-phase audit gates'
		);
	});
	it('[term|label] → label', () => {
		expect(stripLedgerMarkup('the [R² vs benchmark|R²] rule')).toBe('the R² rule');
	});
	it('handles multiple + mixed tokens', () => {
		expect(stripLedgerMarkup('[multi-phase audit] gates ([αt] below)')).toBe(
			'multi-phase audit gates (αt below)'
		);
	});
});
