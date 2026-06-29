import { describe, expect, it } from 'vitest';

// SignalBar renders an optional `subValue` annotation immediately left of the
// main (tone-coloured) value on the value line — used by the FCFF YIELD bar to
// show the raw yield (e.g. "+8.36%") beside its sector-%ile. The annotation
// must render ONLY when a non-empty string is supplied, so an absent raw value
// (`fcffYieldRawDisplay` returns null → subValue undefined) leaves the value
// line clean with no empty span. Mirrors the `{#if subValue}` template guard as
// a pure predicate — tested here without a DOM/component harness, matching the
// house style for template-visibility contracts.

// Mirrors the SignalBar template guard: `{#if subValue}`
function showsSubValue(subValue: string | null | undefined): boolean {
	return Boolean(subValue);
}

describe('subValue annotation visibility (value line)', () => {
	it('renders when a non-empty string is supplied', () => {
		expect(showsSubValue('+8.36%')).toBe(true);
		expect(showsSubValue('-2.50%')).toBe(true);
	});

	it('does NOT render for an empty string', () => {
		expect(showsSubValue('')).toBe(false);
	});

	it('does NOT render when absent (null / undefined)', () => {
		expect(showsSubValue(null)).toBe(false);
		expect(showsSubValue(undefined)).toBe(false);
	});
});
