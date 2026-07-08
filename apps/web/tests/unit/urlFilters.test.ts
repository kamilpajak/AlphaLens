import { describe, it, expect } from 'vitest';
import { setToParam, paramToSet, nextUrlTarget } from '$lib/urlFilters';

// Shared URL (de)serialization for the deep-linkable filters on /edge,
// /experiments and /brief.

describe('setToParam / paramToSet', () => {
	it('serializes a Set sorted (stable regardless of insertion order)', () => {
		expect(setToParam(new Set(['b', 'a', 'c']))).toBe('a,b,c');
		expect(setToParam(new Set())).toBe('');
	});
	it('parses a param value back to a Set, dropping blanks', () => {
		expect([...paramToSet('a,b')].sort()).toEqual(['a', 'b']);
		expect(paramToSet(null).size).toBe(0);
		expect(paramToSet('').size).toBe(0);
		expect([...paramToSet('a,,b,')]).toEqual(['a', 'b']);
	});
	it('round-trips', () => {
		expect([...paramToSet(setToParam(new Set(['x', 'y'])))].sort()).toEqual(['x', 'y']);
	});
});

describe('nextUrlTarget', () => {
	it('returns null when the query already matches (no history churn)', () => {
		expect(nextUrlTarget('?q=a', 'q=a', '/edge', '')).toBeNull();
		expect(nextUrlTarget('', '', '/edge', '')).toBeNull();
	});
	it('builds a ?query target when the params changed', () => {
		expect(nextUrlTarget('', 'q=a', '/edge', '')).toBe('?q=a');
		expect(nextUrlTarget('?q=old', 'q=new', '/edge', '')).toBe('?q=new');
	});
	it('drops the ? entirely when the query is cleared (falls back to pathname)', () => {
		expect(nextUrlTarget('?q=a', '', '/edge', '')).toBe('/edge');
	});
	it('preserves the hash on both branches', () => {
		expect(nextUrlTarget('', 'p=FAIL', '/experiments', '#P14')).toBe('?p=FAIL#P14');
		expect(nextUrlTarget('?p=FAIL', '', '/experiments', '#P14')).toBe('/experiments#P14');
	});
	it('treats a leading ? on currentSearch as equivalent to none', () => {
		expect(nextUrlTarget('?a=1', 'a=1', '/x', '')).toBeNull();
	});
});
