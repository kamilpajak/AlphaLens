import { fileURLToPath } from 'node:url';
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
	resolve: {
		// SvelteKit's `$lib` alias is normally injected by the `svelte-kit sync`
		// tsconfig + the SvelteKit Vite plugin, neither of which runs under the
		// bare vitest config. Map it explicitly so unit tests can import rune
		// modules that resolve sibling modules via `$lib/...` internally (e.g.
		// `marketStatus.svelte.ts` → `$lib/api`).
		alias: {
			$lib: fileURLToPath(new URL('./src/lib', import.meta.url))
		}
	},
	test: {
		include: ['tests/unit/**/*.test.ts'],
		environment: 'node'
	}
});
