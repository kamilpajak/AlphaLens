import { test, expect, type Page } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

// Regression for the "tooltip looks transparent" report: TooltipBubble used
// bg-bg-1 (#0b0d12) — the SAME colour as the candidate card it floats over
// (article.bg-bg-1). An opaque fill that matches the surface beneath it has no
// elevation, so the popover reads as transparent and underlying card text
// appears to bleed through. A real popover must (1) be fully opaque and (2) sit
// on a surface visibly distinct from the card it overlays.

const __dirname = dirname(fileURLToPath(import.meta.url));
const FIXTURES_DIR = resolve(__dirname, 'fixtures/api-mock');
const DAYS_INDEX = JSON.parse(readFileSync(`${FIXTURES_DIR}/days.json`, 'utf-8'));
const DAYS_INDEX_BODY = JSON.stringify({
	data: DAYS_INDEX,
	meta: { total: DAYS_INDEX.length, limit: 200, offset: 0 }
});
const DAY_BODIES: Record<string, string> = {};
for (const day of DAYS_INDEX) {
	try {
		DAY_BODIES[day.date] = readFileSync(`${FIXTURES_DIR}/days/${day.date}.json`, 'utf-8');
	} catch {
		/* missing */
	}
}
const MARKET_STATUS_OPEN_BODY = JSON.stringify({
	is_trading_day: true,
	is_half_day: false,
	is_open_now: true,
	next_open_iso: '2099-01-01T13:30:00+00:00',
	next_close_iso: new Date(Date.now() + 2 * 60 * 60 * 1000).toISOString(),
	exchange: 'XNYS'
});
function installApiMock(page: Page) {
	return page.route('**/api/v1/**', (route) => {
		const url = new URL(route.request().url());
		if (url.pathname === '/api/v1/market/status')
			return route.fulfill({ status: 200, contentType: 'application/json', body: MARKET_STATUS_OPEN_BODY });
		if (url.pathname === '/api/v1/days')
			return route.fulfill({ status: 200, contentType: 'application/json', body: DAYS_INDEX_BODY });
		const m = url.pathname.match(/^\/api\/v1\/days\/(\d{4}-\d{2}-\d{2})$/);
		if (m && DAY_BODIES[m[1]])
			return route.fulfill({ status: 200, contentType: 'application/json', body: DAY_BODIES[m[1]] });
		return route.fulfill({ status: 404, contentType: 'application/json', body: '{}' });
	});
}

function alpha(rgb: string): number {
	const m = rgb.match(/rgba?\(([^)]+)\)/);
	if (!m) return 1;
	const parts = m[1].split(',').map((s) => parseFloat(s.trim()));
	return parts.length === 4 ? parts[3] : 1;
}

test('tooltip popover is an opaque, elevated surface distinct from the card', async ({ page }) => {
	await installApiMock(page);
	await page.goto(`/brief/${DAYS_INDEX[0].date}`);
	const card = page.locator('article[id]').first();
	await expect(card).toBeVisible();

	const chip = page.locator('[data-testid="chip-tip"][data-term="buffett quality"]').first();
	await expect(chip).toBeVisible();
	await chip.focus();

	const bubbleBg = await chip.evaluate((wrapper) => {
		const inner = wrapper.querySelector('[role="tooltip"] > span') as HTMLElement;
		return getComputedStyle(inner).backgroundColor;
	});
	const cardBg = await card.evaluate((el) => getComputedStyle(el).backgroundColor);

	// (1) fully opaque — nothing behind should ever show through
	expect(alpha(bubbleBg)).toBe(1);
	// (2) elevated — a distinct surface from the card it floats over, so the
	//     popover reads as a real layer rather than transparent
	expect(bubbleBg).not.toBe(cardBg);
});
