import { defineConfig } from '@playwright/test';

export default defineConfig({
	testDir: 'tests',
	// `tests/unit/**` holds the vitest unit suite (run via `pnpm run test:unit`).
	// Those files import from 'vitest', so Playwright must not collect them —
	// otherwise `pnpm test` tries to run them and vitest's expect() blows up
	// with "Vitest failed to access its internal state".
	testIgnore: '**/tests/unit/**',
	timeout: 30_000,
	fullyParallel: false,
	reporter: process.env.CI ? 'github' : 'list',
	use: {
		baseURL: 'http://127.0.0.1:4173',
		trace: 'retain-on-failure'
	},
	webServer: {
		command: 'pnpm run build && pnpm run preview --host 127.0.0.1 --port 4173',
		port: 4173,
		reuseExistingServer: !process.env.CI,
		timeout: 120_000
	},
	projects: [
		{
			name: 'chromium',
			use: { browserName: 'chromium' }
		}
	]
});
