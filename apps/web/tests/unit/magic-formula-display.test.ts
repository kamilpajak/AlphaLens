import { describe, it, expect } from 'vitest';
import { magicFormulaDisplay } from '../../src/lib/format';

// The magic-formula cell sits in the FUNDAMENTALS list where every sibling row
// (PE, PS, EV/REV, FCF margin, ...) collapses a missing value to a muted "—".
// When a candidate fails the health gate (no PE / negative equity) it never
// gets ranked, and the old card rendered the verbose phrase "health-gate fail"
// in that one cell — visually inconsistent with the column of numbers/dashes,
// and contradicting the glossary which documents the cell as blank. The helper
// returns a muted "—" for the unranked case (the reason lives in the tooltip),
// and a rank/cohort pair otherwise.
describe('magicFormulaDisplay', () => {
	it('no rank (null/undefined/NaN) -> muted dash', () => {
		expect(magicFormulaDisplay(null, 42)).toEqual({ mode: 'muted', label: '—' });
		expect(magicFormulaDisplay(undefined, 42)).toEqual({ mode: 'muted', label: '—' });
		expect(magicFormulaDisplay(Number.NaN, 42)).toEqual({ mode: 'muted', label: '—' });
	});

	it('finite rank -> rank mode, rounded, with cohort', () => {
		expect(magicFormulaDisplay(3, 40)).toEqual({ mode: 'rank', rank: 3, cohortN: 40 });
		expect(magicFormulaDisplay(3.6, 40)).toEqual({ mode: 'rank', rank: 4, cohortN: 40 });
	});

	it('rank present but cohort missing -> rank mode with null cohort', () => {
		expect(magicFormulaDisplay(5, null)).toEqual({ mode: 'rank', rank: 5, cohortN: null });
		expect(magicFormulaDisplay(5, Number.NaN)).toEqual({ mode: 'rank', rank: 5, cohortN: null });
	});
});
