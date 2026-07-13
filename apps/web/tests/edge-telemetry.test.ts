import { test, expect } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const FIXTURES = resolve(__dirname, 'fixtures/api-mock');
const SUMMARY = JSON.parse(readFileSync(resolve(FIXTURES, 'edge-summary.json'), 'utf-8'));
const OUTCOMES = JSON.parse(readFileSync(resolve(FIXTURES, 'edge-outcomes.json'), 'utf-8'));

const OK_TELEMETRY = {
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

async function stub(page: import('@playwright/test').Page) {
	await page.route('**/api/v1/market/status**', (r) =>
		r.fulfill({
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
	await page.route('**/api/v1/days**', (r) =>
		r.fulfill({ json: { data: [], meta: { total: 0, limit: 200, offset: 0 } } })
	);
	await page.route('**/v1/edge/summary**', (r) => r.fulfill({ json: SUMMARY }));
	await page.route('**/v1/edge/outcomes**', (r) => r.fulfill({ json: OUTCOMES }));
	await page.route('**/v1/edge/excess-telemetry**', (r) => r.fulfill({ json: OK_TELEMETRY }));
}

test('excess-telemetry renders expanded by default and above the outcomes table', async ({
	page
}) => {
	await stub(page);
	await page.goto('/edge');
	await expect(page.locator('header a[href="/"]').first()).toBeVisible();

	// Expanded by default — the scatter + trend are visible WITHOUT any click.
	const scatter = page.getByTestId('excess-scatter');
	await expect(scatter).toBeVisible();
	await expect(page.getByTestId('excess-scatter-trend')).toBeVisible();
	// The legend explains every mark; with a trend present it shows all four entries.
	await expect(page.getByTestId('excess-scatter-legend')).toBeVisible();
	await expect(page.getByText(/one closed trade/)).toBeVisible();
	await expect(page.getByText(/trailing mean/)).toBeVisible();
	await expect(page.getByText(/95% confidence band/)).toBeVisible();
	await expect(page.getByText(/SPY parity/)).toBeVisible();

	// The telemetry panel sits ABOVE the per-candidate outcomes table.
	const outcomes = page.getByTestId('outcomes-table');
	await expect(outcomes).toBeVisible();
	const scatterBox = await scatter.boundingBox();
	const outcomesBox = await outcomes.boundingBox();
	expect(scatterBox).not.toBeNull();
	expect(outcomesBox).not.toBeNull();
	expect(scatterBox!.y).toBeLessThan(outcomesBox!.y);
});

test('telemetry panel can be collapsed via its toggle', async ({ page }) => {
	await stub(page);
	await page.goto('/edge');
	await expect(page.getByTestId('excess-scatter')).toBeVisible();
	await page.getByRole('button', { name: /spy-relative signal telemetry/i }).click();
	await expect(page.getByTestId('excess-scatter')).toBeHidden();
});
