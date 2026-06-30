import { test, expect, type Page } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

// Consistency: a label that carries a tooltip must show the same dotted-underline
// affordance as the JargonTip grid labels (PE/PS/MA50/…). Before this, SignalBar
// labels (fcff yield, rsi, …) had a tooltip but no underline, so the hover cue was
// missing on exactly the rows that have extra info. A SignalBar WITHOUT a tooltip
// (e.g. "off 52w high") must stay plain — the underline means "hover for a
// definition", so it must not appear where nothing is offered.

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

// The label span is the first child of a signal-bar's value-line row; match it
// by its (lowercase, CSS-uppercased) text and read the rendered text-decoration.
async function labelDecoration(page: Page, label: string) {
	const span = page.getByText(label, { exact: true }).first();
	await expect(span).toBeVisible();
	return span.evaluate((el) => {
		const s = getComputedStyle(el);
		return { line: s.textDecorationLine, style: s.textDecorationStyle };
	});
}

test('SignalBar label shows a dotted-underline affordance iff it has a tooltip', async ({ page }) => {
	await installApiMock(page);
	await page.goto(`/brief/${DAYS_INDEX[0].date}`);
	await expect(page.locator('article[id]').first()).toBeVisible();

	// Has a tooltip → must carry the dotted underline (matches JargonTip grid labels).
	const withTip = await labelDecoration(page, 'fcff yield (sector %ile)');
	expect(withTip.line).toContain('underline');
	expect(withTip.style).toBe('dotted');

	// No tooltip → must stay plain (no misleading hover cue).
	const noTip = await labelDecoration(page, 'off 52w high');
	expect(noTip.line).toBe('none');
});
