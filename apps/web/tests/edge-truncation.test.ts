import { test, expect } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));

// The /v1/edge/outcomes listing is capped server-side (`_OUTCOMES_LIMIT`). When
// the window exceeds the cap the API returns the newest N rows plus the TRUE
// match count + a `truncated` flag, and the table must surface an honest
// "showing N of M" notice instead of silently hiding the oldest rows (whose
// absence would also quietly skew the client-side terminal/ongoing chip counts).

const FIXTURES = resolve(__dirname, 'fixtures/api-mock');
const SUMMARY = JSON.parse(readFileSync(resolve(FIXTURES, 'edge-summary.json'), 'utf-8'));
const OUTCOMES = JSON.parse(readFileSync(resolve(FIXTURES, 'edge-outcomes.json'), 'utf-8'));

async function stubLayout(page: import('@playwright/test').Page) {
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
	await page.route('**/v1/edge/summary**', (route) => route.fulfill({ json: SUMMARY }));
}

test('outcomes table shows an honest truncation notice when the server caps the list', async ({
	page
}) => {
	await stubLayout(page);
	await page.route('**/v1/edge/outcomes**', (route) =>
		route.fulfill({
			json: { ...OUTCOMES, total: 5000, returned: OUTCOMES.data.length, truncated: true }
		})
	);

	await page.goto('/edge');
	await expect(page.locator('header a[href="/"]').first()).toBeVisible();

	const notice = page.getByTestId('outcomes-truncation-notice');
	await expect(notice).toBeVisible();
	// The TRUE total (M), not just the returned slice, must be shown.
	await expect(notice).toContainText('5000');
});

test('no truncation notice when the list is not capped', async ({ page }) => {
	await stubLayout(page);
	await page.route('**/v1/edge/outcomes**', (route) =>
		route.fulfill({
			json: { ...OUTCOMES, total: OUTCOMES.data.length, returned: OUTCOMES.data.length, truncated: false }
		})
	);

	await page.goto('/edge');
	await expect(page.locator('header a[href="/"]').first()).toBeVisible();
	await expect(page.getByTestId('outcomes-table')).toBeVisible();
	await expect(page.getByTestId('outcomes-truncation-notice')).toHaveCount(0);
});
