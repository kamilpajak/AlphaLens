import { describe, expect, it } from 'vitest';
import {
	paradigms,
	PARADIGM_GROUPS,
	groupedParadigms,
	paradigmScatter,
	ALPHA_T_DOCTRINE
} from '../../src/lib/data/research-ledger';

// Pins the two pure helpers behind the greenfield /experiments redesign:
// - groupedParadigms buckets the paradigm ledger into ordered research-class
//   chapters (PRICE-FACTOR, EVENT-DRIVEN, …), each with a tested/cleared tally.
// - paradigmScatter reduces the ledger to the hero αt-distribution strip: one
//   representative (out-of-sample-preferred) t per paradigm that produced one,
//   plus the honest "N of 18 produced a t-stat" split.

describe('PARADIGM_GROUPS ↔ paradigm.group integrity', () => {
	it('every paradigm carries a group key that exists in PARADIGM_GROUPS', () => {
		const keys = new Set(PARADIGM_GROUPS.map((g) => g.key));
		for (const p of paradigms) {
			expect(keys.has(p.group), `paradigm ${p.id} group "${p.group}" is defined`).toBe(true);
		}
	});
	it('every declared group is actually used by ≥1 paradigm (no dead groups)', () => {
		for (const g of PARADIGM_GROUPS) {
			const n = paradigms.filter((p) => p.group === g.key).length;
			expect(n, `group ${g.key} has members`).toBeGreaterThan(0);
		}
	});
	it('groups carry a label and a plain-english gloss', () => {
		for (const g of PARADIGM_GROUPS) {
			expect(g.label.trim().length).toBeGreaterThan(0);
			expect(g.gloss.trim().length).toBeGreaterThan(0);
		}
	});
});

describe('groupedParadigms', () => {
	const grouped = groupedParadigms(paradigms, PARADIGM_GROUPS);

	it('preserves PARADIGM_GROUPS order and drops empty groups', () => {
		const order = grouped.map((g) => g.key);
		const expected = PARADIGM_GROUPS.filter((g) =>
			paradigms.some((p) => p.group === g.key)
		).map((g) => g.key);
		expect(order).toEqual(expected);
	});
	it('partitions all paradigms exactly once (no loss, no dupes)', () => {
		const flat = grouped.flatMap((g) => g.items.map((p) => p.id));
		expect(flat.length).toBe(paradigms.length);
		expect(new Set(flat).size).toBe(paradigms.length);
	});
	it('tested = item count; cleared = paradigms reaching the doctrine bar (0 by thesis)', () => {
		for (const g of grouped) {
			expect(g.tested).toBe(g.items.length);
			const cleared = g.items.filter(
				(p) => (p.oos_t ?? p.is_t ?? 0) >= ALPHA_T_DOCTRINE
			).length;
			expect(g.cleared).toBe(cleared);
		}
		// The whole point of the page: nothing ever cleared the 3.5 bar.
		expect(grouped.reduce((s, g) => s + g.cleared, 0)).toBe(0);
	});
	it('every group shares its key across items', () => {
		for (const g of grouped) {
			for (const p of g.items) expect(p.group).toBe(g.key);
		}
	});
});

describe('paradigmScatter', () => {
	const s = paradigmScatter(paradigms);

	it('counts the honest split: all 18 total, 14 produced a t-stat', () => {
		expect(s.nTotal).toBe(paradigms.length);
		expect(s.nTotal).toBe(18);
		const withT = paradigms.filter(
			(p) => Number.isFinite(p.oos_t ?? p.is_t ?? NaN)
		).length;
		expect(s.nWithT).toBe(withT);
		expect(s.nWithT).toBe(14);
	});
	it('one tick per paradigm that produced a t-stat, using OOS-preferred t', () => {
		expect(s.ticks.length).toBe(s.nWithT);
		for (const t of s.ticks) {
			expect(Number.isFinite(t.t)).toBe(true);
			expect(typeof t.id).toBe('string');
			expect(typeof t.display).toBe('string');
		}
	});
	it('ticks are sorted ascending by t', () => {
		const ts = s.ticks.map((t) => t.t);
		expect(ts).toEqual([...ts].sort((a, b) => a - b));
	});
	it('maxT is the largest representative t and NONE reach the 3.5 bar', () => {
		expect(s.maxT).not.toBeNull();
		expect(s.maxT!).toBeLessThan(ALPHA_T_DOCTRINE);
		expect(s.maxT!).toBeCloseTo(2.65, 2); // R02 OOS — the closest call, still short
		expect(Math.max(...s.ticks.map((t) => t.t))).toBe(s.maxT);
		expect(s.ticks.some((t) => t.isMax)).toBe(true);
	});
});
