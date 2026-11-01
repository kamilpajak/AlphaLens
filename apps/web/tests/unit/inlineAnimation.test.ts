/**
 * No component writes the `animation` shorthand as an inline style.
 *
 * An inline declaration beats every author stylesheet rule that is not
 * `!important`, so one inline `animation: …` keeps running under
 * `prefers-reduced-motion: reduce` while the media query in `app.css` appears
 * to work everywhere else. The suppression is invisible at the element that
 * breaks it, which is why this is a source scan rather than a rendered check:
 * a browser test only sees the routes its fixtures happen to render, and the
 * one offender this gate was written for sat on a section that the home-page
 * fixture does not populate.
 *
 * Staggering is still fine — `animation-delay` as an inline style carries no
 * `animation-name`, so the media query overrides it normally. That is the
 * pattern every other entrance here uses: the class supplies the animation,
 * the inline style supplies only the delay.
 */

import { describe, expect, it } from 'vitest';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { dirname, join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const SRC = resolve(dirname(fileURLToPath(import.meta.url)), '../../src');

/** The `animation` shorthand, and never the longhands. `animation-delay`,
 *  `animation-duration` and friends are allowed, so the property name must end
 *  at the colon. */
const INLINE_SHORTHAND = /(^|[;"'{\s])animation\s*:/;

function sourceFiles(dir: string): string[] {
	return readdirSync(dir).flatMap((entry) => {
		const full = join(dir, entry);
		if (statSync(full).isDirectory()) return sourceFiles(full);
		return /\.(svelte|ts)$/.test(entry) && !entry.endsWith('.gen.ts') ? [full] : [];
	});
}

/** Every `style="…"` / `style={…}` attribute value in one source file. */
function styleAttributes(text: string): string[] {
	return Array.from(text.matchAll(/style\s*=\s*(?:"([^"]*)"|'([^']*)'|\{([^}]*)\})/g)).map(
		(m) => m[1] ?? m[2] ?? m[3] ?? ''
	);
}

describe('inline animation shorthand', () => {
	it('appears nowhere under src/', () => {
		const offenders = sourceFiles(SRC).flatMap((file) =>
			styleAttributes(readFileSync(file, 'utf-8'))
				.filter((value) => INLINE_SHORTHAND.test(value))
				.map((value) => `${relative(SRC, file)}: ${value}`)
		);
		expect(offenders).toEqual([]);
	});

	it('is what the matcher actually looks for', () => {
		// A positive control, so an over-narrow regex cannot make the gate above
		// pass by matching nothing.
		expect(INLINE_SHORTHAND.test('width: 10%; animation: fade-up 0.5s ease-out 0s backwards')).toBe(
			true
		);
		expect(INLINE_SHORTHAND.test('animation: blink 1.2s infinite')).toBe(true);
		// The staggering pattern must NOT be caught.
		expect(INLINE_SHORTHAND.test('animation-delay: 0.1s')).toBe(false);
		expect(INLINE_SHORTHAND.test('width: 10%; animation-delay: 0.05s')).toBe(false);
	});
});
