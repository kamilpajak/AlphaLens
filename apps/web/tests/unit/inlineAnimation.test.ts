/**
 * Two source gates that keep the reduced-motion suppression honest.
 *
 * 1. No component writes the `animation` shorthand as an inline style. An inline
 *    declaration beats every author stylesheet rule that is not `!important`, so
 *    one inline `animation: …` keeps running under `prefers-reduced-motion:
 *    reduce` while the media query appears to work everywhere else. The
 *    suppression is invisible at the element that breaks it, which is why this is
 *    a source scan rather than a rendered check: a browser test only sees the
 *    routes its fixtures happen to render, and the one offender this gate was
 *    written for sat on a section the home-page fixture does not populate.
 *
 * 2. Every selector that declares an animation in `app.css` also appears inside
 *    the `prefers-reduced-motion: reduce` block. Without this, a third animation
 *    added later runs under the preference with every other test still green —
 *    the per-class browser assertions in `tests/reduced-motion.test.ts` name
 *    `.fade-up` and `.blink` and cannot see a new one.
 *
 * Staggering stays allowed: `animation-delay` as an inline style carries no
 * `animation-name`, so the media query overrides it normally. That is the pattern
 * every entrance here uses — the class supplies the animation, the inline style
 * supplies only the delay. Both matchers below therefore stop the property name
 * at the colon, and the positive-control cases pin that they do.
 */

import { describe, expect, it } from 'vitest';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { dirname, join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const SRC = resolve(dirname(fileURLToPath(import.meta.url)), '../../src');
const APP_CSS = join(SRC, 'app.css');
const REDUCED_MOTION_AT_RULE = '@media (prefers-reduced-motion: reduce)';

/** The `animation` shorthand and never a longhand: the property name must end at
 *  the colon. The leading class includes a backtick so a template-literal style
 *  whose first declaration is the shorthand is still caught. */
const INLINE_SHORTHAND = /(^|[;"'`{\s])animation\s*:/;

/** Svelte's `style:animation={…}` directive, which compiles to an inline style
 *  and so defeats the media query exactly as the shorthand does. Matched over raw
 *  file text, because it is a directive and not a `style=` attribute. The
 *  lookahead keeps `style:animation-delay` out. */
const STYLE_DIRECTIVE = /style:animation(?=[\s=])/;

function sourceFiles(dir: string): string[] {
	return readdirSync(dir).flatMap((entry) => {
		const full = join(dir, entry);
		if (statSync(full).isDirectory()) return sourceFiles(full);
		return /\.(svelte|ts|js|html)$/.test(entry) && !entry.endsWith('.gen.ts') ? [full] : [];
	});
}

/** Every `style="…"` / `style='…'` / `style={…}` attribute value in one file.
 *  The braced form allows one level of nesting so a Svelte template literal
 *  (`style={`…${x}…`}`) is read whole instead of being cut at the first `}`. */
function styleAttributes(text: string): string[] {
	const pattern = /style\s*=\s*(?:"([^"]*)"|'([^']*)'|\{((?:[^{}]|\{[^{}]*\})*)\})/g;
	return Array.from(text.matchAll(pattern)).map((m) => m[1] ?? m[2] ?? m[3] ?? '');
}

/** The body of the `prefers-reduced-motion: reduce` block, and the stylesheet
 *  with that block removed. Brace-matched rather than regexed, because the block
 *  contains nested rules. */
function splitOnReducedMotion(css: string): { inside: string; outside: string } {
	const at = css.indexOf(REDUCED_MOTION_AT_RULE);
	if (at === -1) return { inside: '', outside: css };
	const open = css.indexOf('{', at);
	let depth = 0;
	let close = open;
	for (let i = open; i < css.length; i += 1) {
		if (css[i] === '{') depth += 1;
		else if (css[i] === '}') {
			depth -= 1;
			if (depth === 0) {
				close = i;
				break;
			}
		}
	}
	return {
		inside: css.slice(open + 1, close),
		outside: css.slice(0, at) + css.slice(close + 1)
	};
}

/** Each individual selector of every rule whose body declares the `animation`
 *  shorthand. A comma-separated selector list contributes one entry per selector,
 *  so `.a, .b { animation: none }` covers both `.a` and `.b`. */
function selectorsDeclaringAnimation(css: string): Set<string> {
	const found = new Set<string>();
	for (const match of css.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
		if (!INLINE_SHORTHAND.test(match[2])) continue;
		const selectorList = match[1].split(/[\n]/).filter((line) => !line.trim().startsWith('/*'));
		for (const selector of selectorList.join(' ').split(',')) {
			const trimmed = selector.trim();
			if (trimmed !== '') found.add(trimmed);
		}
	}
	return found;
}

describe('inline animation shorthand', () => {
	it('appears in no style attribute under src/', () => {
		const offenders = sourceFiles(SRC).flatMap((file) =>
			styleAttributes(readFileSync(file, 'utf-8'))
				.filter((value) => INLINE_SHORTHAND.test(value))
				.map((value) => `${relative(SRC, file)}: ${value}`)
		);
		expect(offenders).toEqual([]);
	});

	it('appears in no style: directive under src/', () => {
		const offenders = sourceFiles(SRC)
			.filter((file) => STYLE_DIRECTIVE.test(readFileSync(file, 'utf-8')))
			.map((file) => relative(SRC, file));
		expect(offenders).toEqual([]);
	});

	it('is what the matchers actually look for', () => {
		// Positive controls, so an over-narrow matcher cannot make the two gates
		// above pass by matching nothing. Every shape that would defeat the media
		// query must be CAUGHT; the staggering pattern must not be.
		const caught = [
			'animation: blink 1.2s infinite',
			'width: 10%; animation: fade-up 0.5s ease-out 0s backwards',
			'animation:fade-up 1s',
			'`animation: fade-up ${d}s`'
		];
		const allowed = [
			'animation-delay: 0.1s',
			'width: 10%; animation-delay: 0.05s',
			'animation-duration: 0.2s',
			'display: contents'
		];
		expect(caught.filter((v) => !INLINE_SHORTHAND.test(v))).toEqual([]);
		expect(allowed.filter((v) => INLINE_SHORTHAND.test(v))).toEqual([]);

		expect(STYLE_DIRECTIVE.test('style:animation={x}')).toBe(true);
		expect(STYLE_DIRECTIVE.test('style:animation = "x"')).toBe(true);
		expect(STYLE_DIRECTIVE.test('style:animation-delay={x}')).toBe(false);

		// The braced branch must survive one level of nesting, which is what a
		// Svelte template-literal style produces.
		expect(styleAttributes('<div style={`width: ${p}%; animation: fade-up 1s`}></div>')).toEqual([
			'`width: ${p}%; animation: fade-up 1s`'
		]);
		// ...and the quoted branch must keep reading past a Svelte interpolation.
		expect(styleAttributes('<div style="width: {p}%; animation-delay: {d}s"></div>')).toEqual([
			'width: {p}%; animation-delay: {d}s'
		]);
	});
});

describe('every animation is suppressed under reduced motion', () => {
	it('each selector that animates in app.css also appears in the reduced-motion block', () => {
		const { inside, outside } = splitOnReducedMotion(readFileSync(APP_CSS, 'utf-8'));
		const suppressed = selectorsDeclaringAnimation(inside);
		const animating = selectorsDeclaringAnimation(outside);
		expect([...animating].filter((selector) => !suppressed.has(selector))).toEqual([]);
	});

	it('finds the animations it claims to be checking', () => {
		// Without this control the gate above would pass on a stylesheet that had
		// lost its animations, or on a parser that found none.
		const { inside, outside } = splitOnReducedMotion(readFileSync(APP_CSS, 'utf-8'));
		expect([...selectorsDeclaringAnimation(outside)].sort()).toEqual(['.blink', '.fade-up']);
		expect([...selectorsDeclaringAnimation(inside)].sort()).toEqual(['.blink', '.fade-up']);
	});
});
