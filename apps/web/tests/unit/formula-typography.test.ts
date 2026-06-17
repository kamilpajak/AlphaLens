import { describe, expect, it } from 'vitest';
import formulas from '../../src/lib/formulas.json';

// Enforce the math-typography convention (ISO 80000-2 / NIST) on every
// formulas.json entry, so a regression like a bare `EV` (which renders as the
// italic product E·V) is caught in CI instead of shipping to a tooltip.
//
// The rules:
//   - Multi-letter names / acronyms (EV, EPS, FCF, MA200, price, revenue, …)
//     MUST be upright — wrapped in \text{} / \mathrm{}. A bare run of >= 2
//     letters is a HARD failure (it would typeset as a product of italics).
//   - Single-letter math variables are italic by convention, but THIS project
//     spells out named quantities and uses only the ones in ALLOWED_BARE below
//     (R = return). Any other bare single letter is a failure — adding one is a
//     deliberate decision, so extend the set on purpose (e.g. index variables
//     i / n if a \sum_{i} ever appears).

const ALLOWED_BARE = new Set(['R']);

/**
 * Reduce a LaTeX formula to the bare letters Temml would render as italic
 * <mi>: drop \text{}/\mathrm{}/\operatorname{} blocks (upright content), every
 * `\command` word, any other backslash escape, then all non-letter glyphs —
 * leaving space-separated runs of bare letters.
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

describe('formula typography (ISO 80000-2)', () => {
	for (const [name, tex] of Object.entries(formulas as Record<string, string>)) {
		it(`"${name}" — names upright, only allowed single letters bare`, () => {
			for (const run of bareLetterRuns(tex)) {
				if (run.length > 1) {
					throw new Error(
						`bare multi-letter "${run}" in formula "${name}" renders as italic ` +
							`${run.split('').join('·')} — wrap the name/acronym in \\text{}`
					);
				}
				expect(
					ALLOWED_BARE.has(run),
					`bare single letter "${run}" in formula "${name}" is not an allowed bare ` +
						`variable (spell it with \\text{} or add it to ALLOWED_BARE on purpose)`
				).toBe(true);
			}
		});
	}

	// Positive controls — prove the validator actually rejects violations so the
	// regex can never silently rot into accepting everything.
	it('rejects a bare multi-letter acronym', () => {
		const runs = bareLetterRuns('\\dfrac{EV}{\\text{revenue}}');
		expect(runs).toContain('EV');
		expect(runs.some((r) => r.length > 1)).toBe(true);
	});

	it('rejects a disallowed bare single letter', () => {
		const runs = bareLetterRuns('Q + \\text{x}');
		expect(runs).toEqual(['Q']);
		expect(ALLOWED_BARE.has('Q')).toBe(false);
	});

	it('accepts an allowed bare variable with an upright subscript', () => {
		const runs = bareLetterRuns('R_{\\text{cand}}');
		expect(runs).toEqual(['R']);
		expect(ALLOWED_BARE.has('R')).toBe(true);
	});
});
