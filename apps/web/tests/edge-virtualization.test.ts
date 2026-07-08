import { test, expect } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));

// The /edge outcomes table row-virtualizes: only the rows in (and just around)
// the viewport are mounted, with leading/trailing spacer <tr>s holding the
// scrollbar honest. This keeps the table light even at the 500-row server cap.
// These tests drive a large mocked payload and assert (1) the DOM renders far
// fewer rows than the dataset, (2) a spacer row reserves the off-screen height,
// and (3) scrolling swaps which rows are mounted.

const FIXTURES = resolve(__dirname, 'fixtures/api-mock');
const SUMMARY = JSON.parse(readFileSync(resolve(FIXTURES, 'edge-summary.json'), 'utf-8'));

// A big terminal-only dataset with unique, sortable keys. `closed` desc (the
// default sort) orders by matured_at, so ROW0000 (latest date) lands on top.
const N = 300;
function makeOutcomes(count: number) {
	const data = Array.from({ length: count }, (_, i) => {
		const seq = String(i).padStart(4, '0');
		// Descending dates so index 0 sorts to the top under `closed` desc.
		const day = String(28 - (i % 28)).padStart(2, '0');
		const month = String(1 + (i % 9)).padStart(2, '0');
		return {
			ticker: `ROW${seq}`,
			brief_date: `2026-${month}-${day}`,
			matured_at: `2026-${month}-${day}`,
			theme: 'synthetic',
			ladder_classification: 'TP_FULL',
			terminal: true,
			realized_r: 1.2,
			open_r: null,
			market_excess_return: 0.1,
			forward_return: 0.05,
			benchmark_window_return: 0.02,
			holding_days_elapsed: 10,
			realized_return_pct_of_book: 0.15
		};
	});
	return { data, total: count, returned: count, truncated: false };
}

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
	await page.route('**/v1/edge/outcomes**', (route) => route.fulfill({ json: makeOutcomes(N) }));
}

test('renders only a windowed slice of a large outcome list', async ({ page }) => {
	await stubLayout(page);
	await page.goto('/edge');
	await expect(page.getByTestId('outcomes-table')).toBeVisible();

	// Every data row carries a ticker link; count the mounted ones.
	const rowLinks = page.locator('tbody a[href^="/brief/"]');
	await expect(rowLinks.first()).toBeVisible();
	const rendered = await rowLinks.count();

	// Windowing: far fewer than the 300 rows are in the DOM, but the viewport is
	// filled (more than a handful).
	expect(rendered).toBeGreaterThan(5);
	expect(rendered).toBeLessThan(120);

	// A trailing spacer <tr> reserves the height of the un-mounted rows below.
	const spacer = page.locator('tbody tr[aria-hidden="true"] td');
	expect(await spacer.count()).toBeGreaterThan(0);
	const spacerHeight = await spacer.last().evaluate((el) => parseFloat((el as HTMLElement).style.height));
	expect(spacerHeight).toBeGreaterThan(1000);

	// Screen-reader parity: the true row count + 1-based index survive
	// virtualization even though only the slice is mounted.
	await expect(page.getByTestId('outcomes-table').locator('table')).toHaveAttribute(
		'aria-rowcount',
		String(N)
	);
	await expect(page.locator('tbody tr[aria-rowindex="1"]')).toHaveCount(1);
});

test('resets scroll to the top when the sort changes', async ({ page }) => {
	await stubLayout(page);
	await page.goto('/edge');
	await expect(page.getByTestId('outcomes-table')).toBeVisible();

	const scroll = page.getByTestId('outcomes-scroll');
	await scroll.evaluate((el) => {
		el.scrollTop = 4000;
	});
	await expect.poll(async () => scroll.evaluate((el) => el.scrollTop)).toBeGreaterThan(1000);

	// Re-sorting produces a wholesale-different order → jump back to the top.
	await page.getByRole('button', { name: 'ticker' }).click();
	await expect.poll(async () => scroll.evaluate((el) => el.scrollTop)).toBe(0);
});

test('scrolling the container swaps which rows are mounted', async ({ page }) => {
	await stubLayout(page);
	await page.goto('/edge');
	await expect(page.getByTestId('outcomes-table')).toBeVisible();

	const scroll = page.getByTestId('outcomes-scroll');
	const firstBefore = await page.locator('tbody a[href^="/brief/"]').first().textContent();

	// Jump well down the list.
	await scroll.evaluate((el) => {
		el.scrollTop = 4000;
	});
	// Let the scroll handler + re-render settle.
	await expect
		.poll(async () => page.locator('tbody a[href^="/brief/"]').first().textContent())
		.not.toBe(firstBefore);
});
