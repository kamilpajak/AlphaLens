import { test, expect } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const FIXTURES = resolve(__dirname, 'fixtures/api-mock');
const SUMMARY = JSON.parse(readFileSync(resolve(FIXTURES, 'edge-summary.json'), 'utf-8'));

// The /edge outcomes toolbar: free-text search over ticker+theme, faceted
// multi-select on ladder-classification + scorer cohort, an "N of M" count, a
// clear-all, and URL round-trip so a filtered view is deep-linkable.

// 6 terminal rows: 3 classes, 2 cohorts, 2 themes — enough for every facet.
const OUTCOMES = {
	data: [
		mk('NVDA', 'ai-infra', 'TP_FULL', 'v1'),
		mk('AMD', 'ai-infra', 'SL_HIT', 'v1'),
		mk('SNAP', 'ai-infra', 'SL_HIT', 'v2'),
		mk('PLUG', 'high-gas', 'TIME_STOP', 'v1'),
		mk('AMPL', 'high-gas', 'TP_FULL', 'v2'),
		mk('BE', 'high-gas', 'SL_HIT', 'v2')
	],
	total: 6,
	returned: 6,
	truncated: false
};

function mk(ticker: string, theme: string, cls: string, cohort: string) {
	return {
		ticker,
		brief_date: '2026-05-18',
		matured_at: '2026-05-29',
		theme,
		ladder_classification: cls,
		terminal: true,
		realized_r: 1.1,
		open_r: null,
		market_excess_return: 0.1,
		forward_return: 0.05,
		benchmark_window_return: 0.02,
		holding_days_elapsed: 10,
		realized_return_pct_of_book: 0.15,
		scorer_config_version: cohort
	};
}

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
}

const rowLinks = (page: import('@playwright/test').Page) => page.locator('tbody a[href^="/brief/"]');

test('free-text search narrows the table and updates the count + URL', async ({ page }) => {
	await stub(page);
	await page.goto('/edge');
	await expect(page.getByTestId('outcomes-filter')).toBeVisible();
	await expect(rowLinks(page)).toHaveCount(6);

	await page.getByTestId('outcomes-search').fill('nvda');
	await expect(rowLinks(page)).toHaveCount(1);
	await expect(rowLinks(page).first()).toHaveText('NVDA');
	await expect(page.getByTestId('outcomes-match-count')).toContainText('1 of 6');
	await expect.poll(() => new URL(page.url()).searchParams.get('q')).toBe('nvda');

	// Theme substring hits every high-gas row.
	await page.getByTestId('outcomes-search').fill('high-gas');
	await expect(rowLinks(page)).toHaveCount(3);
});

test('a classification facet chip filters, and clear-all resets', async ({ page }) => {
	await stub(page);
	await page.goto('/edge');
	await expect(rowLinks(page)).toHaveCount(6);

	await page.getByTestId('outcomes-filter').getByRole('button', { name: /^SL_HIT/ }).click();
	await expect(rowLinks(page)).toHaveCount(3); // AMD, SNAP, BE
	await expect.poll(() => new URL(page.url()).searchParams.get('class')).toBe('SL_HIT');

	await page.getByTestId('outcomes-clear-all').click();
	await expect(rowLinks(page)).toHaveCount(6);
	await expect.poll(() => new URL(page.url()).searchParams.has('class')).toBe(false);
});

test('URL sync preserves an unrelated query param', async ({ page }) => {
	await stub(page);
	await page.goto('/edge?ref=email');
	await expect(page.getByTestId('outcomes-filter')).toBeVisible();

	await page.getByTestId('outcomes-search').fill('nvda');
	await expect.poll(() => new URL(page.url()).searchParams.get('q')).toBe('nvda');
	// The unrelated param must survive the filter's replaceState.
	expect(new URL(page.url()).searchParams.get('ref')).toBe('email');
});

test('deep-links: a ?q= URL arrives pre-filtered', async ({ page }) => {
	await stub(page);
	await page.goto('/edge?q=snap');
	await expect(page.getByTestId('outcomes-filter')).toBeVisible();
	await expect(rowLinks(page)).toHaveCount(1);
	await expect(rowLinks(page).first()).toHaveText('SNAP');
	await expect(page.getByTestId('outcomes-search')).toHaveValue('snap');
});
