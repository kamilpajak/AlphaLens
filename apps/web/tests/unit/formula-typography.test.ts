import { describe, expect, it } from 'vitest';
import formulas from '../../src/lib/formulas.json';
import lexicon from '../../src/lib/formula-lexicon.json';

// CI validator for the math-typography convention (ISO 80000-2 / NIST) on every
// src/lib/formulas.json entry, so a regression like a bare `EV` (which typesets
// as the italic product E·V) is caught here instead of shipping to a tooltip.
//
// Rules enforced (the machine-checkable subset of ISO 80000-2; the rest, e.g.
// "is bare `t` an index or a label", is semantic and cannot be read off the
// LaTeX source):
//   1. Every token inside \text{}/\mathrm{}/\operatorname{} must be registered
//      in formula-lexicon.json (catches typos like EBTIDA, forces a deliberate
//      decision for each new symbol, pins spelling + capitalisation).
//   2. A bare run of >= 2 letters is a hard failure — multi-letter names and
//      acronyms must be upright (\text{}), never bare italic.
//   3. A bare single letter must be a registered `variables` entry (R). Other
//      bare letters fail: spell them with \text{} or register them on purpose.
//   4. A registered `constants` letter (e, i) must be upright — a bare one is a
//      failure (NIST: mathematical constants are roman).

const UPRIGHT = new Set<string>([
	...lexicon.names,
	...lexicon.operators,
	...lexicon.labels,
	...lexicon.units,
	...lexicon.constants
]);
const VARIABLES = new Set<string>(lexicon.variables);
const CONSTANTS = new Set<string>(lexicon.constants);

/** Contents of every \text{}/\mathrm{}/\operatorname{} block (the upright tokens). */
function uprightTokens(tex: string): string[] {
	return [...tex.matchAll(/\\(?:text|mathrm|operatorname)\s*\{([^{}]*)\}/g)].map((m) => m[1].trim());
}

/**
 * The bare letter runs Temml renders as italic <mi>: drop upright blocks, every
 * `\command` word and other backslash escape, then all non-letter glyphs.
 */
function bareLetterRuns(tex: string): string[] {
	return tex
		.replace(/\\(?:text|mathrm|operatorname)\s*\{[^{}]*\}/g, ' ')
		.replace(/\\[a-zA-Z]+/g, ' ')
		.replace(/\\./g, ' ')
		.replace(/[^a-zA-Z]+/g, ' ')
		.trim()
		.split(/\s+/)
		.filter(Boolean);
}

describe('formula typography (ISO 80000-2 / NIST)', () => {
	for (const [name, tex] of Object.entries(formulas as Record<string, string>)) {
		it(`"${name}" — upright tokens registered, only known variables bare`, () => {
			for (const token of uprightTokens(tex)) {
				expect(
					UPRIGHT.has(token),
					`unknown upright token "${token}" in formula "${name}" — fix a typo or ` +
						`register it in src/lib/formula-lexicon.json`
				).toBe(true);
			}
			for (const run of bareLetterRuns(tex)) {
				if (run.length > 1) {
					throw new Error(
						`bare multi-letter "${run}" in formula "${name}" renders as italic ` +
							`${run.split('').join('·')} — wrap the name/acronym in \\text{}`
					);
				}
				if (CONSTANTS.has(run)) {
					throw new Error(
						`constant "${run}" in formula "${name}" must be upright ` +
							`(\\mathrm{${run}}), not a bare italic letter`
					);
				}
				expect(
					VARIABLES.has(run),
					`bare single letter "${run}" in formula "${name}" is not a registered ` +
						`variable — spell it with \\text{} or add it to lexicon.variables on purpose`
				).toBe(true);
			}
		});
	}

	// Positive controls — prove each rule actually rejects a violation so the
	// detector can never silently rot into accepting everything.
	it('rejects an unregistered upright token (typo / new name)', () => {
		expect(uprightTokens('\\text{EBTIDA}')).toEqual(['EBTIDA']);
		expect(UPRIGHT.has('EBTIDA')).toBe(false);
	});

	it('rejects a bare multi-letter acronym', () => {
		expect(bareLetterRuns('\\dfrac{EV}{\\text{revenue}}')).toContain('EV');
	});

	it('flags a bare constant as needing upright', () => {
		expect(bareLetterRuns('e + 1')).toEqual(['e']);
		expect(CONSTANTS.has('e')).toBe(true);
	});

	it('rejects an unknown bare single letter', () => {
		expect(bareLetterRuns('Q')).toEqual(['Q']);
		expect(VARIABLES.has('Q')).toBe(false);
	});

	it('accepts a registered variable with an upright descriptive subscript', () => {
		expect(bareLetterRuns('R_{\\text{cand}}')).toEqual(['R']);
		expect(VARIABLES.has('R')).toBe(true);
		expect(UPRIGHT.has('cand')).toBe(true);
	});
});
