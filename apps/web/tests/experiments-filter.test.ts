import { test, expect, type Page } from '@playwright/test';

// The /experiments ledger status filters are deep-linkable: `?p=` (paradigm
// statuses) and `?t=` (tool statuses) seed the chip selection on load and are
// mirrored back on change. The ledger data is static (no API), so only the
// layout endpoints need stubbing.

async function stubLayout(page: Page) {
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
}

// The paradigm "fail" chip (unique to the paradigm bar; tool statuses have no
// "fail"). `/^fail\b/` matches "fail <count>" but not "slippage-fail <count>".
const failChip = (page: Page) => page.getByRole('button', { name: /^fail\b/ });

test('a ?p= deep link seeds the paradigm status filter', async ({ page }) => {
	await stubLayout(page);
	await page.goto('/experiments?p=FAIL');
	await expect(failChip(page)).toBeVisible();
	// Seeded active — aria-pressed reflects the selection restored from the URL.
	await expect(failChip(page)).toHaveAttribute('aria-pressed', 'true');
});

test('toggling a status chip mirrors into ?p= and back to a clean URL', async ({ page }) => {
	await stubLayout(page);
	await page.goto('/experiments');
	await expect(failChip(page)).toHaveAttribute('aria-pressed', 'false');

	await failChip(page).click();
	await expect(failChip(page)).toHaveAttribute('aria-pressed', 'true');
	await expect.poll(() => new URL(page.url()).searchParams.get('p')).toBe('FAIL');

	// Deselect → the param is removed (clean URL), not left stale.
	await failChip(page).click();
	await expect.poll(() => new URL(page.url()).searchParams.has('p')).toBe(false);
});
