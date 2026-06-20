import { svelte } from '@sveltejs/vite-plugin-svelte';
import { defineConfig } from 'vitest/config';

// Vitest owns the *unit* suite under `tests/unit/`. Playwright owns the
// browser smoke suite under `tests/*.test.ts` (config: playwright.config.ts).
// The two never overlap: vitest's `include` is scoped to `tests/unit/**` so
// `pnpm run test` (Playwright) and `pnpm run test:unit` (vitest) stay
// independent and can both run in CI without stepping on each other.
//
// The `svelte` plugin is loaded so `.svelte.ts` rune modules (e.g.
// `$lib/session.svelte.ts`, `$lib/marketStatus.svelte.ts`) are compiled —
// `$state`/`$derived` are macros that the Svelte compiler rewrites, and
// without the plugin they throw `ReferenceError: $state is not defined` when
// imported from a unit test.
export default defineConfig({
	plugins: [svelte()],
	test: {
		include: ['tests/unit/**/*.test.ts'],
		environment: 'node'
	}
});
