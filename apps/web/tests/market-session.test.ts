/**
 * Hermetic smoke for the per-exchange market-session chip.
 *
 * The chip reads ``/v1/market/status`` and renders ambient footer
 * telemetry: "XNYS ● open · closes in 2h" while the venue trades,
 * "XNYS ○ closed · opens mon 09:30 · in 2d" otherwise. Tests mock the
 * endpoint deterministically so the chip state is independent of when CI
 * runs.
 *
 * It replaces the old full-width closed-market banner (and its dead
 * "submission deferred" copy — the paper-trade/broker chain was
 * decommissioned, ADR 0012). A guard below pins that the banner test-id is
 * gone so a future re-introduction is a conscious choice, not a regression.
 *
 * The detail tail ("· closes in …" / "· opens …") is ``hidden lg:inline``;
 * the Playwright default viewport (1280×720) is ≥ the lg breakpoint
 * (1024px) so it renders here.
 */

import { expect, test, type Page } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const FIXTURES_DIR = resolve(__dirname, 'fixtures/api-mock');

// Re-use the smoke fixture's days index so the dashboard load function
// gets a valid envelope and renders normally. The chip test only cares
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
	is_open_now: boolean;
	next_open_iso: string;
	next_close_iso: string;
	exchange: string;
}

function isoInFuture(hours: number): string {
	// Pin a boundary relative to the test clock so the countdown text has a
	// stable upper bound (the chip asserts "in Xh"/"in Xd").
	return new Date(Date.now() + hours * 60 * 60 * 1000).toISOString();
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
		// renders the no-data state without raising. The chip is orthogonal
		// to brief content.
		return route.fulfill({
			status: 404,
			contentType: 'application/json',
			body: JSON.stringify({ detail: `unmocked: ${url.pathname}` })
		});
	});
}

test.describe('MarketSession chip — open', () => {
	test('shows exchange + "live" + closes-in countdown when trading', async ({ page }) => {
		await installMockedApi(page, {
			is_trading_day: true,
			is_half_day: false,
			is_open_now: true,
			next_open_iso: isoInFuture(24),
			next_close_iso: isoInFuture(2),
			exchange: 'XNYS'
		});

		await page.goto('/');

		const chip = page.getByTestId('market-session');
		await expect(chip).toBeVisible();
		await expect(chip).toContainText('XNYS');
		// "live" is the per-exchange open indicator (it replaced the chip's old
		// "open" label when the always-on standalone footer "live" dot was
		// merged in here).
		await expect(chip).toContainText(/live/i);
		// Counts down to the next close (~2h out).
		await expect(chip).toContainText(/closes in [12]h(?:\s+\d+m)?/i);
		// The merge is one-way: "live" appears ONLY inside the chip, so the
		// footer carries exactly one "live" (a re-added standalone dot fails this).
		await expect(page.locator('footer').getByText('live', { exact: true })).toHaveCount(1);
		// The green open-dot pulses (restored liveness animation). Gating it to the
		// open branch makes the motion itself per-exchange, not a global signal.
		await expect(chip.locator('.dot.blink')).toHaveCount(1);
	});
});

test.describe('MarketSession chip — closed', () => {
	test('shows "closed" + opens-at label + countdown when not trading', async ({ page }) => {
		await installMockedApi(page, {
			is_trading_day: false,
			is_half_day: false,
			is_open_now: false,
			next_open_iso: isoInFuture(36),
			next_close_iso: isoInFuture(40),
			exchange: 'XNYS'
		});

		await page.goto('/');

		const chip = page.getByTestId('market-session');
		await expect(chip).toBeVisible();
		await expect(chip).toContainText('XNYS');
		await expect(chip).toContainText(/closed/i);
		// Next-open label + relative countdown (~1d 12h out).
		await expect(chip).toContainText(/opens/i);
		await expect(chip).toContainText(/in \d+[dh]/i);
		// Regression: the old standalone always-green "live" dot is gone. When the
		// venue is closed the prices are anchored to the last close, so NOTHING in
		// the footer reads "live" — the indicator is now strictly per-exchange and
		// lights only while trading. Match any "live" substring (not \blive\b) so a
		// re-added dot is caught even if it ever sits mid-token.
		await expect(page.locator('footer')).not.toContainText(/live/i);
		// The pulse is per-exchange too: a closed venue's dot is static (no animation).
		await expect(chip.locator('.blink')).toHaveCount(0);
	});
});

test.describe('MarketSession chip — placement + banner removal', () => {
	test('chip lives in the footer, not above main content', async ({ page }) => {
		await installMockedApi(page, {
			is_trading_day: true,
			is_half_day: false,
			is_open_now: true,
			next_open_iso: isoInFuture(24),
			next_close_iso: isoInFuture(3),
			exchange: 'XNYS'
		});

		await page.goto('/');
		await expect(page.getByTestId('market-session')).toBeVisible();

		const chipY = await page
			.getByTestId('market-session')
			.evaluate((el) => el.getBoundingClientRect().top);
		const mainY = await page.locator('main').evaluate((el) => el.getBoundingClientRect().top);
		// Footer telemetry sits below <main>, unlike the old banner which
		// preceded it.
		expect(chipY).toBeGreaterThan(mainY);
	});

	test('the old closed-market banner no longer renders', async ({ page }) => {
		await installMockedApi(page, {
			is_trading_day: false,
			is_half_day: false,
			is_open_now: false,
			next_open_iso: isoInFuture(12),
			next_close_iso: isoInFuture(16),
			exchange: 'XNYS'
		});

		await page.goto('/');
		// Removal guard: the full-width banner + its "submission deferred"
		// copy are gone for good. A re-introduction must be deliberate.
		await expect(page.getByTestId('market-status-banner')).toHaveCount(0);
		await expect(page.getByText(/submission deferred/i)).toHaveCount(0);
	});
});

test.describe('MarketSession chip — mobile footer density', () => {
	// On a narrow viewport the footer must keep only the essentials (the
	// per-exchange session chip + version); the ambient clock + db path are desktop-only
	// (lg+). 700px is below the lg breakpoint (1024px) but above sm (640px) —
	// the width where the old `sm:flex` clock used to appear and could push
	// the shrink-0 telemetry cluster into horizontal overflow.
	const OPEN = {
		is_trading_day: true,
		is_half_day: false,
		is_open_now: true,
		next_open_iso: isoInFuture(24),
		next_close_iso: isoInFuture(3),
		exchange: 'XNYS'
	};

	test('below lg: chip shown, clock + db hidden, no horizontal overflow', async ({ page }) => {
		await page.setViewportSize({ width: 700, height: 800 });
		await installMockedApi(page, OPEN);
		await page.goto('/');

		// The session chip is the one piece of market info we always keep.
		await expect(page.getByTestId('market-session')).toBeVisible();
		// Ambient flavour is hidden until lg.
		await expect(page.getByTestId('footer-clock')).toBeHidden();
		await expect(page.getByTestId('footer-db')).toBeHidden();

		// The page must not scroll sideways — the whole point of the cut.
		const overflow = await page.evaluate(
			() => document.documentElement.scrollWidth > document.documentElement.clientWidth
		);
		expect(overflow).toBe(false);
	});

	test('at lg+: clock + db become visible', async ({ page }) => {
		await page.setViewportSize({ width: 1280, height: 800 });
		await installMockedApi(page, OPEN);
		await page.goto('/');

		await expect(page.getByTestId('market-session')).toBeVisible();
		await expect(page.getByTestId('footer-clock')).toBeVisible();
		await expect(page.getByTestId('footer-db')).toBeVisible();
		// The clock now renders in the viewer's local zone WITH a trailing zone
		// label (CEST / JST / EDT / GMT+8 / UTC …) so it no longer mixes an
		// unlabelled UTC stamp with the exchange-local open time. Assert the
		// shape — date + 24h time + a non-empty zone token — without pinning the
		// zone itself (it tracks the CI runner's system zone, not a fixed value).
		await expect(page.getByTestId('footer-clock')).toHaveText(
			/\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}\s+\S+/
		);
		// Slogan ticker (now sourced from $lib/pipelineFacts) still renders —
		// the first chip is left-aligned and fully visible at this width.
		await expect(page.getByText('PRESS-GATE')).toBeVisible();
	});
});
