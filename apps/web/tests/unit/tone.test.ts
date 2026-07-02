import { describe, expect, it } from 'vitest';
import { toneClass, TONE_KEYS, type SemanticTone } from '../../src/lib/tone';
import { alphaBand, ALPHA_T_MARGINAL, ALPHA_T_DOCTRINE } from '../../src/lib/data/research-ledger';

// Pins the unified tone vocabulary. `toneClass` is the single source for the
// `text-X` / `border-X` / `bg-X` class strings that `statusTone` (paradigm),
// `toolStatusTone`, and the /edge `toneClasses` all used to hand-roll in
// parallel. `alphaBand` is the single source for the αt doctrine ladder shared
// by `alphaValueTone` (text) and `tBarTone` (bar fill).

describe('toneClass', () => {
	it('defaults to text + border, in that order', () => {
		expect(toneClass('red')).toBe('text-red border-red');
		expect(toneClass('green')).toBe('text-green border-green');
		expect(toneClass('magenta')).toBe('text-magenta border-magenta');
	});
	it('honours the requested variant order', () => {
		// /edge toneClasses uses border-first.
		expect(toneClass('green', ['border', 'text'])).toBe('border-green text-green');
		expect(toneClass('violet', ['border', 'text'])).toBe('border-violet text-violet');
	});
	it('supports a single variant (e.g. bar fill / text only)', () => {
		expect(toneClass('green', ['bg'])).toBe('bg-green');
		expect(toneClass('amber', ['text'])).toBe('text-amber');
	});
	it('muted maps to the fg-muted / grid-strong pair', () => {
		expect(toneClass('muted', ['border', 'text'])).toBe('border-grid-strong text-fg-muted');
		expect(toneClass('muted')).toBe('text-fg-muted border-grid-strong');
	});
	it('every declared tone resolves for every variant', () => {
		for (const tone of TONE_KEYS) {
			for (const v of ['text', 'border', 'bg'] as const) {
				expect(toneClass(tone as SemanticTone, [v])).toMatch(new RegExp(`^${v}-`));
			}
		}
	});
});

describe('alphaBand', () => {
	it('null / non-finite → null', () => {
		expect(alphaBand(null)).toBeNull();
		expect(alphaBand(NaN)).toBeNull();
		expect(alphaBand(Infinity)).toBeNull();
	});
	it('negative → "negative"', () => {
		expect(alphaBand(-0.01)).toBe('negative');
		expect(alphaBand(-3)).toBe('negative');
	});
	it('[0, marginal) → "noise"', () => {
		expect(alphaBand(0)).toBe('noise');
		expect(alphaBand(ALPHA_T_MARGINAL - 0.01)).toBe('noise');
	});
	it('[marginal, doctrine) → "marginal"', () => {
		expect(alphaBand(ALPHA_T_MARGINAL)).toBe('marginal');
		expect(alphaBand(ALPHA_T_DOCTRINE - 0.01)).toBe('marginal');
	});
	it('>= doctrine → "deploy"', () => {
		expect(alphaBand(ALPHA_T_DOCTRINE)).toBe('deploy');
		expect(alphaBand(10)).toBe('deploy');
	});
});
