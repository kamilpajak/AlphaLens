import { test, expect } from '@playwright/test';

const OK_PAYLOAD = {
	benchmark: 'SPY',
	status: 'ok',
	gate_threshold: 30,
	n_total: 42,
	n_effective: 17,
	median_holding_days: 8,
	smoother_window: 20,
	metric_note: 'telemetry only — not confirmatory.',
	benchmark_note: 'SPY is a broad-market proxy.',
	points: [
		{ date: '2026-06-01', excess: -0.02, ticker: 'AAA', holding_days: 5, episode_repeat: false },
		{ date: '2026-06-03', excess: 0.04, ticker: 'BBB', holding_days: 9, episode_repeat: true }
	],
	trend: [
		{ date: '2026-06-01', mean: -0.01, lo: -0.03, hi: 0.01 },
		{ date: '2026-06-03', mean: 0.02, lo: 0.0, hi: 0.05 }
	]
};

test('excess-telemetry panel expands and renders the trend', async ({ page }) => {
	// Stub the layout-level endpoints (market status + days index) so the SPA
	// shell hydrates without network errors on the footer chip / session tiles.
	await page.route('**/api/v1/market/status**', (route) =>
		route.fulfill({
			json: {
				is_trading_day: false,
				is_half_day: false,
				is_open_now: false,
				next_open_iso: '2099-01-01T13:30:00+00:00',
				next_close_iso: '2099-01-01T20:00:00+00:00',
				exchange: 'XNYS'
			}
		})
	);
	await page.route('**/api/v1/days**', (route) =>
		route.fulfill({ json: { data: [], meta: { total: 0, limit: 200, offset: 0 } } })
	);
	// Let the page's own loader endpoints degrade to their empty states; only the
	// telemetry endpoint needs a body for this smoke.
	await page.route('**/v1/edge/excess-telemetry**', (route) =>
		route.fulfill({ json: OK_PAYLOAD })
	);
	// Stub the loader endpoints so the page degrades cleanly to the "no edge
	// data" state without crashing (a partial {} EdgeSummary would throw when
	// the template accesses summary.edge.status).
	await page.route('**/v1/edge/summary**', (route) =>
		route.fulfill({ status: 404, json: { detail: 'not found' } })
	);
	await page.route('**/v1/edge/outcomes**', (route) =>
		route.fulfill({ status: 404, json: { detail: 'not found' } })
	);

	await page.goto('/edge');
	// Wait for the SPA to hydrate: the header nav is the first element painted by
	// the layout shell; once it is visible the Svelte router has run and the /edge
	// route component is mounted.
	await expect(page.locator('header a[href="/"]').first()).toBeVisible();
	await page.getByRole('button', { name: /spy-relative|trend vs spy|effectiveness/i }).click();
	await expect(page.getByTestId('excess-scatter')).toBeVisible();
	await expect(page.getByTestId('excess-scatter-trend')).toBeVisible();
	// The legend explains every mark; with a trend present it shows all four
	// entries (dot, trailing mean, CI band, SPY parity).
	await expect(page.getByTestId('excess-scatter-legend')).toBeVisible();
	await expect(page.getByText(/one closed trade/)).toBeVisible();
	await expect(page.getByText(/trailing mean/)).toBeVisible();
	await expect(page.getByText(/95% confidence band/)).toBeVisible();
});
