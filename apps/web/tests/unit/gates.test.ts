import { describe, it, expect } from 'vitest';
import { orderedGates, GATE_ORDER } from '$lib/gates';

const c = (
	gates_passed: string[] = [],
	gates_failed: string[] = [],
	gates_unknown: string[] = []
) => ({ gates_passed, gates_failed, gates_unknown });

describe('orderedGates — fixed per-gate slot order', () => {
	it('returns known gates in the canonical GATE_ORDER, not by status', () => {
		// tenk FAILED, press PASSED, insider UNKNOWN. If this were sorted by status
		// (passed → failed → unknown) the order would be press, tenk, insider.
		// Fixed-position means it stays tenk, press, insider regardless.
		const out = orderedGates(c(['press'], ['tenk'], ['insider']));
		expect(out.map((g) => g.name)).toEqual(['tenk', 'press', 'insider']);
		expect(out).toEqual([
			{ name: 'tenk', status: 'failed' },
			{ name: 'press', status: 'passed' },
			{ name: 'insider', status: 'unknown' }
		]);
	});

	it('keeps the same order when statuses flip', () => {
		const allPass = orderedGates(c(['tenk', 'press', 'insider'])).map((g) => g.name);
		const allFail = orderedGates(c([], ['tenk', 'press', 'insider'])).map((g) => g.name);
		const mixed = orderedGates(c(['insider'], ['tenk'], ['press'])).map((g) => g.name);
		expect(allPass).toEqual(['tenk', 'press', 'insider']);
		expect(allFail).toEqual(['tenk', 'press', 'insider']);
		expect(mixed).toEqual(['tenk', 'press', 'insider']);
	});

	it('places the etf gate after insider when present', () => {
		const out = orderedGates(c(['tenk', 'etf'], ['press'], ['insider']));
		expect(out.map((g) => g.name)).toEqual(['tenk', 'press', 'insider', 'etf']);
	});

	it('omits gates that were not evaluated (absent from all arrays)', () => {
		const out = orderedGates(c(['tenk'], ['insider']));
		expect(out.map((g) => g.name)).toEqual(['tenk', 'insider']);
	});

	it('appends unknown/future gate names (not in GATE_ORDER) at the end, never dropping them', () => {
		const out = orderedGates(c(['tenk', 'newgate'], ['press'], ['insider']));
		expect(out.map((g) => g.name)).toEqual(['tenk', 'press', 'insider', 'newgate']);
		expect(out.at(-1)).toEqual({ name: 'newgate', status: 'passed' });
	});

	it('appends multiple unknown gate names in passed → failed → unknown then array order', () => {
		const out = orderedGates(c(['zeta'], ['alpha'], ['omega', 'beta']));
		// Known gates absent here → the leftovers pin the fallback order exactly.
		expect(out).toEqual([
			{ name: 'zeta', status: 'passed' },
			{ name: 'alpha', status: 'failed' },
			{ name: 'omega', status: 'unknown' },
			{ name: 'beta', status: 'unknown' }
		]);
	});

	it('returns an empty list when there are no gates', () => {
		expect(orderedGates(c())).toEqual([]);
	});

	it('GATE_ORDER is the pipeline sequence plus the unwired etf gate', () => {
		expect(GATE_ORDER).toEqual(['tenk', 'press', 'insider', 'etf']);
	});
});
