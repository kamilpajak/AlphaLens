import { describe, expect, it } from 'vitest';
import {
	candorTone,
	moatTone,
	moatTrendTone,
	understoodLabel,
	understoodTone
} from '../../src/lib/format';

// The Buffett deep-read drawer (card PR-4) renders four qualitative pillars as
// tone-coloured badges. These pure mappings turn the LLM enum / bool into a
// good / mixed / bad / muted tone. Absent values (`""` enums from the no-10-K
// path, or null `understandable`) must read as `muted`, never a false verdict.

describe('moatTone', () => {
	it('a real moat type is good', () => {
		expect(moatTone('brand')).toBe('good');
		expect(moatTone('switching_cost')).toBe('good');
	});
	it('"none" is bad', () => {
		expect(moatTone('none')).toBe('bad');
	});
	it('absent ("" / null) is muted', () => {
		expect(moatTone('')).toBe('muted');
		expect(moatTone(null)).toBe('muted');
	});
});

describe('moatTrendTone', () => {
	it('maps the trend vocabulary', () => {
		expect(moatTrendTone('widening')).toBe('good');
		expect(moatTrendTone('stable')).toBe('mixed');
		expect(moatTrendTone('narrowing')).toBe('bad');
	});
	it('unclear / absent is muted', () => {
		expect(moatTrendTone('unclear')).toBe('muted');
		expect(moatTrendTone('')).toBe('muted');
		expect(moatTrendTone(null)).toBe('muted');
	});
});

describe('candorTone', () => {
	it('maps the candor vocabulary', () => {
		expect(candorTone('candid')).toBe('good');
		expect(candorTone('mixed')).toBe('mixed');
		expect(candorTone('promotional')).toBe('bad');
	});
	it('unclear / absent is muted', () => {
		expect(candorTone('unclear')).toBe('muted');
		expect(candorTone(null)).toBe('muted');
	});
});

describe('understood', () => {
	it('tone: true good, false bad, null muted', () => {
		expect(understoodTone(true)).toBe('good');
		expect(understoodTone(false)).toBe('bad');
		expect(understoodTone(null)).toBe('muted');
	});
	it('label: yes / no / dash', () => {
		expect(understoodLabel(true)).toBe('yes');
		expect(understoodLabel(false)).toBe('no');
		expect(understoodLabel(null)).toBe('—');
	});
});
