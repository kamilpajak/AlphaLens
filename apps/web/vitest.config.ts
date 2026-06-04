import { defineConfig } from 'vitest/config';

// Vitest owns the *unit* suite under `tests/unit/`. Playwright owns the
// browser smoke suite under `tests/*.test.ts` (config: playwright.config.ts).
// The two never overlap: vitest's `include` is scoped to `tests/unit/**` so
// `pnpm run test` (Playwright) and `pnpm run test:unit` (vitest) stay
// independent and can both run in CI without stepping on each other.
export default defineConfig({
	test: {
		include: ['tests/unit/**/*.test.ts'],
		environment: 'node'
	}
});
