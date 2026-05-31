/**
 * Hermetic smoke for the closed-market banner (PR-C, epic #295).
 *
 * The banner reads ``/v1/market/status`` and renders a persistent strip on
 * non-trading days with a live countdown to the next session open. Tests
 * mock the endpoint deterministically so the banner state is independent
 * of when CI runs.
 *
 * Two scenarios:
 *  - Trading day: banner DOES NOT render (status component returns null).
 *  - Closed day: banner renders with the next-open label + countdown text.
 *
 * Design memo: ``docs/research/paper_trading_non_trading_day_2026_05_29.md``
 * §5 (PR-C sequencing).
 */

import { expect, test, type Page } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const FIXTURES_DIR = resolve(__dirname, 'fixtures/api-mock');

// Re-use the smoke fixture's days index so the dashboard load function
// gets a valid envelope and renders normally. The banner test only cares
// about ``/v1/market/status`` — we mock that, then let other endpoints
// hit either the fixture or a default 404.
const DAYS_INDEX = JSON.parse(readFileSync(`${FIXTURES_DIR}/days.json`, 'utf-8'));
const DAYS_INDEX_BODY = JSON.stringify({
	data: DAYS_INDEX,
	meta: { total: DAYS_INDEX.length, limit: 200, offset: 0 }
});

interface MarketStatusFixture {
	is_trading_day: boolean;
	is_half_day: boolean;
	next_open_iso: string;
	exchange: string;
}

function fixtureNextOpenInFuture(hours: number): string {
	// Pin the next open relative to the test clock so the countdown
	// text has a stable upper bound (banner asserts "in Xh Ym").
	const ms = Date.now() + hours * 60 * 60 * 1000;
	return new Date(ms).toISOString();
}

function installMockedApi(page: Page, marketStatus: MarketStatusFixture) {
	return page.route('**/api/v1/**', (route) => {
		const url = new URL(route.request().url());
		if (url.pathname === '/api/v1/market/status') {
			return route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify(marketStatus)
			});
		}
		if (url.pathname === '/api/v1/days') {
			return route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: DAYS_INDEX_BODY
			});
		}
		// Per-day brief / candidate routes — feed empty so the dashboard
		// renders the no-data state without raising. The banner is
		// orthogonal to brief content.
		return route.fulfill({
			status: 404,
			contentType: 'application/json',
			body: JSON.stringify({ detail: `unmocked: ${url.pathname}` })
		});
	});
}

test.describe('MarketStatusBanner — closed-day visibility', () => {
	test('renders persistent banner with countdown when market is closed', async ({ page }) => {
		await installMockedApi(page, {
			is_trading_day: false,
			is_half_day: false,
			next_open_iso: fixtureNextOpenInFuture(36),
			exchange: 'XNYS'
		});

		await page.goto('/');

		// Banner appears (the poll resolves on layout mount).
		const banner = page.getByTestId('market-status-banner');
		await expect(banner).toBeVisible();

		// Headline copy and the "in HH:MM"-style countdown both render.
		await expect(banner).toContainText(/market closed/i);
		await expect(banner).toContainText(/submission deferred until/i);
		await expect(banner).toContainText(/in \d+[dh]/i);
	});

	test('renders banner on a half-day too (still closed-now until open)', async ({ page }) => {
		// Half-day Black Friday: anchor in the past (market hasn't opened
		// yet for testing purposes); next_open in the near future.
		await installMockedApi(page, {
			is_trading_day: false,
			is_half_day: true,
			next_open_iso: fixtureNextOpenInFuture(4),
			exchange: 'XNYS'
		});

		await page.goto('/');

		// Same chrome — the SPA does not distinguish half-day vs full-closed
		// in the closed-now banner; the half-day signal matters only inside
		// the broader session-info surfaces.
		await expect(page.getByTestId('market-status-banner')).toBeVisible();
	});

	test('countdown format includes a relative duration', async ({ page }) => {
		await installMockedApi(page, {
			is_trading_day: false,
			is_half_day: false,
			next_open_iso: fixtureNextOpenInFuture(2),
			exchange: 'XNYS'
		});

		await page.goto('/');
		const banner = page.getByTestId('market-status-banner');
		await expect(banner).toBeVisible();

		// Expect "in 1h 59m" or "in 2h" — the precise minute is sensitive
		// to fixture/test clock drift, so we anchor on the hour digit and
		// a unit token instead of an exact value.
		await expect(banner).toContainText(/in [12]h(?:\s+\d+m)?/);
	});
});

test.describe('MarketStatusBanner — trading-day hidden', () => {
	test('banner does NOT render when market is open', async ({ page }) => {
		await installMockedApi(page, {
			is_trading_day: true,
			is_half_day: false,
			next_open_iso: fixtureNextOpenInFuture(24),
			exchange: 'XNYS'
		});

		await page.goto('/');

		// The poll resolves to a trading-day response, so the banner
		// component returns null — locator should resolve to 0 elements.
		await expect(page.getByTestId('market-status-banner')).toHaveCount(0);
	});

	test('banner stays hidden on a trading half-day', async ({ page }) => {
		// Black Friday morning during the half-day session: still a
		// trading day, just one with an early close. Banner stays hidden;
		// the half-day signal matters for downstream surfaces, not the
		// closed-now strip.
		await installMockedApi(page, {
			is_trading_day: true,
			is_half_day: true,
			next_open_iso: fixtureNextOpenInFuture(72),
			exchange: 'XNYS'
		});

		await page.goto('/');
		await expect(page.getByTestId('market-status-banner')).toHaveCount(0);
	});
});

test.describe('MarketStatusBanner — coexistence with existing layout', () => {
	test('renders ABOVE main content (banner precedes first article in DOM)', async ({ page }) => {
		// Layout regression guard: a future refactor that moves the banner
		// inside <main> would break the "always-visible" guarantee on long
		// scrolled routes; it must remain a sibling above <main>.
		await installMockedApi(page, {
			is_trading_day: false,
			is_half_day: false,
			next_open_iso: fixtureNextOpenInFuture(12),
			exchange: 'XNYS'
		});

		await page.goto('/');
		await expect(page.getByTestId('market-status-banner')).toBeVisible();

		const bannerY = await page
			.getByTestId('market-status-banner')
			.evaluate((el) => el.getBoundingClientRect().top);
		const mainY = await page.locator('main').evaluate((el) => el.getBoundingClientRect().top);
		expect(bannerY).toBeLessThan(mainY);
	});

	test('does NOT block header navigation', async ({ page }) => {
		// The banner is a non-blocking strip; header nav links must still
		// be clickable above it (header sits above the banner in the
		// layout source order).
		await installMockedApi(page, {
			is_trading_day: false,
			is_half_day: false,
			next_open_iso: fixtureNextOpenInFuture(6),
			exchange: 'XNYS'
		});

		await page.goto('/');
		await expect(page.getByTestId('market-status-banner')).toBeVisible();

		await page.locator('header a[href="/briefs"]').first().click();
		await expect(page).toHaveURL(/\/briefs$/);
	});
});
