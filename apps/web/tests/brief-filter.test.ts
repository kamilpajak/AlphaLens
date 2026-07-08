import { test, expect, type Page } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const FIXTURES = resolve(__dirname, 'fixtures/api-mock');
const DAYS_ARRAY = JSON.parse(readFileSync(resolve(FIXTURES, 'days.json'), 'utf-8'));
const DAYS_INDEX = JSON.stringify({
	data: DAYS_ARRAY,
	meta: { total: DAYS_ARRAY.length, limit: 200, offset: 0 }
});
const DATE = '2026-05-18';
const DAY_BODY = readFileSync(resolve(FIXTURES, `days/${DATE}.json`), 'utf-8');

// The /brief theme filter is the shared LedgerFilterBar (multi-select): selecting
// two theme chips shows the UNION of their candidates, not a single theme. The
// fixture day has 16 candidates across disjoint themes — promotions (4) and
// retail (3) → 7 together.

function installMock(page: Page) {
	return page.route('**/api/v1/**', (route) => {
		const url = new URL(route.request().url());
		if (url.pathname === '/api/v1/market/status') {
			return route.fulfill({
				json: {
					is_trading_day: false,
					is_half_day: false,
					is_open_now: false,
					next_open_iso: '2099-01-01T13:30:00+00:00',
					next_close_iso: '2099-01-01T20:00:00+00:00',
					exchange: 'XNYS'
				}
			});
		}
		if (url.pathname === '/api/v1/days') {
			return route.fulfill({ contentType: 'application/json', body: DAYS_INDEX });
		}
		if (url.pathname === `/api/v1/days/${DATE}`) {
			return route.fulfill({ contentType: 'application/json', body: DAY_BODY });
		}
		return route.fulfill({ status: 404, json: { detail: `unhandled: ${url.pathname}` } });
	});
}

const cards = (page: Page) => page.locator('article[id]');

test('theme chips multi-select — two themes show their union', async ({ page }) => {
	await installMock(page);
	await page.goto(`/brief/${DATE}`);
	await expect(cards(page)).toHaveCount(16);

	await page.getByRole('button', { name: /^#promotions\b/ }).click();
	await expect(cards(page)).toHaveCount(4);

	// Add a second theme → UNION (multi-select), not replace.
	await page.getByRole('button', { name: /^#retail\b/ }).click();
	await expect(cards(page)).toHaveCount(7);

	// Deselect the first → only the second remains.
	await page.getByRole('button', { name: /^#promotions\b/ }).click();
	await expect(cards(page)).toHaveCount(3);

	// The "all" chip clears the whole selection.
	await page.getByRole('button', { name: /^all\b/ }).click();
	await expect(cards(page)).toHaveCount(16);
});
