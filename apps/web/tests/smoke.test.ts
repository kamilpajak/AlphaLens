import { expect, test, type ConsoleMessage, type Page } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { GLOSSARY } from '../src/lib/data/glossary.js';

const __dirname = dirname(fileURLToPath(import.meta.url));
const FIXTURES_DIR = resolve(__dirname, 'fixtures/api-mock');

/**
 * The app fetches /api/v1/* from the FastAPI service in production; the
 * preview server here has no live api container, so we intercept those
 * requests and serve bodies synthesised from a checked-in fixture set
 * under tests/fixtures/api-mock/. Fixtures are an explicit testing
 * artefact — not a production export — so they don't drift with daily
 * pipeline runs and the test stays fully hermetic.
 */
// Pre-read fixture files at module load — synchronous reads in the route
// handler add per-request latency that races SvelteKit's client-side load
// function against the Playwright test's first DOM query.
const DAYS_INDEX = JSON.parse(readFileSync(`${FIXTURES_DIR}/days.json`, 'utf-8'));
const DAYS_INDEX_BODY = JSON.stringify({
	data: DAYS_INDEX,
	meta: { total: DAYS_INDEX.length, limit: 200, offset: 0 }
});
const DAY_BODIES: Record<string, string> = {};
for (const day of DAYS_INDEX) {
	const path = `${FIXTURES_DIR}/days/${day.date}.json`;
	try {
		DAY_BODIES[day.date] = readFileSync(path, 'utf-8');
	} catch {
		/* missing — handler will surface 404 */
	}
}

// Open-market mock for /v1/market/status — the layout poll fires on every
// page load, so the footer session chip needs a realistic "open" payload.
// A 404 fallback would trip the "Failed to load resource" browser console
// error and fail every route's console-clean assertion. The chip's content
// (open/closed, countdown) is exercised by tests/market-session.test.ts;
// here we only need a well-formed body that renders the "open" branch so
// the smoke routes don't surface a stale closed/2099 chip state.
//
// ``next_open_iso`` is a never-reached sentinel (the chip reads it only when
// closed). ``next_close_iso`` is ~2h out so the open chip's "closes in 2h"
// tail is plausible; it isn't asserted here, so the relative timestamp is
// harmless to determinism.
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
		// /api/v1/market/status — fixed open-market stub for the session chip.
		if (url.pathname === '/api/v1/market/status') {
			return route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: MARKET_STATUS_OPEN_BODY
			});
		}
		// /api/v1/days[?limit=…]
		if (url.pathname === '/api/v1/days') {
			return route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: DAYS_INDEX_BODY
			});
		}
		// /api/v1/days/{date}
		const dayMatch = url.pathname.match(/^\/api\/v1\/days\/(\d{4}-\d{2}-\d{2})$/);
		if (dayMatch) {
			const date = dayMatch[1];
			const body = DAY_BODIES[date];
			if (body) {
				return route.fulfill({ status: 200, contentType: 'application/json', body });
			}
			return route.fulfill({
				status: 404,
				contentType: 'application/json',
				body: JSON.stringify({ detail: `no brief for date=${date}` })
			});
		}
		return route.fulfill({
			status: 404,
			contentType: 'application/json',
			body: JSON.stringify({ detail: `unhandled api mock: ${url.pathname}` })
		});
	});
}

test.beforeEach(async ({ page }) => {
	await installApiMock(page);
});

/**
 * Smoke tests covering every route + interactive surface in the production build.
 *
 * What this catches:
 *   - SSR compile errors (e.g. invalid {@const} placement, missing imports)
 *   - 500 responses from broken prerender entries / load functions
 *   - 404s on routes that should prerender (every brief in days.json, /about,
 *     /, /briefs)
 *   - Client-side console errors / page errors / unhandled rejections
 *   - Missing static data JSON files
 *   - Broken internal links (anything `<a href="/...">` that 404s)
 *   - Major content regressions (header chip text, candidate count)
 *   - SPA navigation hydration failures between nav links
 *   - Filter / checkbox interaction errors on the brief detail page
 *   - Hover tooltip render failures on gate pills
 *
 * What it does NOT catch:
 *   - Visual regressions (Tailwind class drift, layout shifts)
 *   - Accessibility deficiencies
 *   - Data correctness vs source parquet
 */

const days: { date: string; n_candidates: number }[] = DAYS_INDEX;
const latestDay = days[0];

function attachErrorCollectors(consoleErrors: string[], pageErrors: string[]) {
	return {
		onConsole: (msg: ConsoleMessage) => {
			if (msg.type() === 'error') consoleErrors.push(msg.text());
		},
		onPageError: (err: Error) => {
			pageErrors.push(err.message);
		}
	};
}

test.describe('smoke — every route renders without errors', () => {
	for (const path of ['/', '/briefs', '/about', '/experiments']) {
		test(`GET ${path} renders OK`, async ({ page }) => {
			const consoleErrors: string[] = [];
			const pageErrors: string[] = [];
			const { onConsole, onPageError } = attachErrorCollectors(consoleErrors, pageErrors);
			page.on('console', onConsole);
			page.on('pageerror', onPageError);

			const response = await page.goto(path);
			expect(response?.status(), `GET ${path} should be 200`).toBe(200);

			await expect(page.locator('header a[href="/"]').first()).toContainText('ALPHALENS');

			expect(consoleErrors, `${path} console errors`).toEqual([]);
			expect(pageErrors, `${path} page errors`).toEqual([]);
		});
	}

	// Every brief in days.json must be prerendered and load cleanly.
	for (const day of days) {
		test(`GET /brief/${day.date} renders all candidates`, async ({ page }) => {
			const consoleErrors: string[] = [];
			const pageErrors: string[] = [];
			const { onConsole, onPageError } = attachErrorCollectors(consoleErrors, pageErrors);
			page.on('console', onConsole);
			page.on('pageerror', onPageError);

			const response = await page.goto(`/brief/${day.date}`);
			expect(response?.status()).toBe(200);

			await expect(page.getByText(day.date).first()).toBeVisible();

			const candidateCount = await page.locator('article[id]').count();
			expect(candidateCount, `${day.date} candidate count`).toBe(day.n_candidates);

			expect(consoleErrors, `${day.date} console errors`).toEqual([]);
			expect(pageErrors, `${day.date} page errors`).toEqual([]);
		});
	}

	// Under SPA mode the HTTP response is always 200 (nginx serves index.html
	// as fallback); the 404 surfaces as SvelteKit's error page rendering "404"
	// after the client-side router or load function rejects.
	test('GET /brief/2099-01-01 (unknown date) renders SvelteKit 404', async ({ page }) => {
		await page.goto('/brief/2099-01-01');
		await expect(page.getByText('404', { exact: false })).toBeVisible();
	});

	test('GET /unknown-route renders SvelteKit 404', async ({ page }) => {
		await page.goto('/this-route-does-not-exist');
		await expect(page.getByText('404', { exact: false })).toBeVisible();
	});
});

test.describe('session-expiry handling — global re-auth overlay', () => {
	// An expired Cloudflare Access session never reaches the API: CF answers the
	// cross-origin XHR with a 302 to its login origin (the browser blocks that
	// redirect → fetch throws) or serves its login HTML as 200. `apiFetch`
	// normalises both to a synthetic 401 AND flips the global session-expiry
	// store, so a single overlay modal (role="dialog") renders ABOVE page
	// content on EVERY route. The loaders themselves degrade to empty state —
	// the overlay is the only re-auth surface.
	const DATE = latestDay.date;

	const overlay = (page: import('@playwright/test').Page) =>
		page.getByRole('dialog').filter({ hasText: 'session expired' });

	// Under a genuinely expired CF Access session the cookie is rejected at the
	// Access layer, so EVERY path on api.<domain> is blocked identically — the
	// /v1/market/status background poll included. Block it here too (overriding
	// the global open-market stub) so these tests model that all-or-nothing
	// reality. Without this the poll would succeed and apiFetch's proof-of-life
	// clear would race against the data fetch that raises the overlay, making
	// the assertions timing-dependent. The poll opts into `silentAuth`, so a
	// blocked poll neither raises nor clears the overlay — the data fetch is the
	// sole, deterministic trigger.
	test.beforeEach(async ({ page }) => {
		await page.route('**/api/v1/market/status', (route) =>
			route.fulfill({
				status: 200,
				contentType: 'text/html; charset=utf-8',
				body: '<!doctype html><html>cf access login</html>'
			})
		);
	});

	test('aborted API fetch (CF redirect) shows the re-auth overlay, not a 500', async ({
		page
	}) => {
		await page.route(`**/api/v1/days/${DATE}`, (route) => route.abort());
		await page.goto(`/brief/${DATE}`);
		await expect(overlay(page)).toBeVisible();
		await expect(page.locator('main')).not.toContainText('Internal Error');
	});

	test('CF login HTML (200 + text/html) on the brief route fires the overlay', async ({ page }) => {
		// The canonical "valid fetch, expired cookie" signal: CF Access serves
		// its login page as 200 + HTML instead of proxying to Django. apiFetch
		// detects the HTML body and marks the session expired.
		await page.route(`**/api/v1/days/${DATE}`, (route) =>
			route.fulfill({
				status: 200,
				contentType: 'text/html; charset=utf-8',
				body: '<!doctype html><html>cf access login</html>'
			})
		);
		await page.goto(`/brief/${DATE}`);
		await expect(overlay(page)).toBeVisible();
	});

	test('dashboard renders the overlay on session expiry, not a misleading empty state', async ({
		page
	}) => {
		// The index endpoint (pathname /api/v1/days, any query) returns the CF
		// login HTML. apiFetch normalises it to a synthetic 401 + marks the
		// session; the overlay covers the (empty) dashboard rather than letting
		// the bare "no captured sessions" empty state read as data loss.
		await page.route(
			(url) => url.pathname === '/api/v1/days',
			(route) =>
				route.fulfill({
					status: 200,
					contentType: 'text/html; charset=utf-8',
					body: '<!doctype html><html>cf access login</html>'
				})
		);
		await page.goto('/');
		await expect(overlay(page)).toBeVisible();
	});

	test('a failing market-status poll alone does NOT raise the overlay', async ({ page }) => {
		// Regression guard for the "session expired pops up suspiciously often"
		// bug: the 60s /v1/market/status poll (failed here via the describe-level
		// route) is fail-silent noise and opts into `silentAuth`, so its failure
		// must NOT raise the global overlay while the data the user cares about
		// (dashboard index + latest brief) loads fine from the global mock.
		await page.goto('/');
		// The dashboard content renders…
		await expect(page.getByTestId('session-tiles')).toBeVisible();
		// …and the overlay stays down despite the poll failing every cycle.
		await expect(overlay(page)).toBeHidden();
	});
});

test.describe('smoke — SPA navigation', () => {
	test('clicking every header nav link transitions cleanly', async ({ page }) => {
		const consoleErrors: string[] = [];
		const pageErrors: string[] = [];
		const { onConsole, onPageError } = attachErrorCollectors(consoleErrors, pageErrors);
		page.on('console', onConsole);
		page.on('pageerror', onPageError);

		await page.goto('/');

		for (const path of ['/briefs', '/about', '/experiments', '/']) {
			await page.locator(`header a[href="${path}"]`).first().click();
			await expect(page).toHaveURL(new RegExp(path === '/' ? '/$' : path));
		}

		expect(consoleErrors).toEqual([]);
		expect(pageErrors).toEqual([]);
	});

	test('all internal links on every page resolve to 200/404 (no 5xx)', async ({ page, request }) => {
		const seen = new Set<string>();
		for (const path of ['/', '/briefs', '/about', '/experiments', `/brief/${latestDay.date}`]) {
			await page.goto(path);
			// Auto-wait for the layout nav to hydrate before the one-shot
			// evaluateAll. This is a pure client-rendered SPA (ssr=false), so
			// goto resolves on the empty shell; without this wait a slow CI
			// runner reads zero links → the loop below runs no assertions and
			// the test passes while checking nothing (silent coverage gap).
			await expect(page.locator('a[href^="/"]').first()).toBeVisible();
			const hrefs = await page.locator('a[href^="/"]').evaluateAll((nodes) =>
				nodes
					.map((n) => (n as HTMLAnchorElement).getAttribute('href') ?? '')
					// /api/* is served by the FastAPI process behind nginx in
					// production — the SvelteKit preview server doesn't proxy
					// it, so a raw GET would 502/ECONNREFUSED. Path-prefix is
					// narrower than filtering by target="_blank", which would
					// also skip legitimate SvelteKit routes if a future link
					// chose to open in a new tab.
					.filter((h) => !h.startsWith('/api/'))
					.filter((h) => !h.startsWith('//') && !h.startsWith('/#'))
			);
			for (const href of hrefs) seen.add(href.split('#')[0]);
		}
		for (const href of seen) {
			const res = await request.get(href);
			expect(res.status(), `link target ${href}`).toBeLessThan(500);
		}
	});
});

test.describe('smoke — about page accuracy', () => {
	// Regression for the silent-failure class documented in
	// reference_gemini_model_retirement_silent_failure.md: retired Gemini
	// model IDs in user-facing copy advertise a state the pipeline left
	// behind, and the copy quietly rots every time a model is bumped. The
	// about page now uses brand-style names ("DeepSeek V4 Flash", "DeepSeek
	// V4 Pro") so it survives a model bump without re-staling, and the test
	// hard-fails if a retired exact ID or retired brand label ever reappears.
	const RETIRED_MODEL_IDS = [
		'gemini-2.5-flash',
		'gemini-3-pro-preview', // dropped the "-3-" preview line; current is gemini-3.1-pro-preview
		// Retired brand labels — the thematic pipeline migrated Gemini → DeepSeek
		// V4 (PR-G #318), so the SPA must no longer display the old Gemini brand
		// names anywhere.
		'Gemini 3 Pro',
		'Gemini 3 Flash'
	];

	test('lists every pipeline layer with current model labels', async ({ page }) => {
		await page.goto('/about');

		const layers = page.locator('section').filter({ hasText: /pipeline\.layers/i });
		await expect(layers).toBeVisible();

		// Layer rows by ID — covers L1/L2/L3/V/L4/L5 presence.
		for (const id of ['L1', 'L2', 'L3', 'V', 'L4', 'L5']) {
			await expect(layers.getByText(id, { exact: true }).first()).toBeVisible();
		}

		// Current model labels (brand-style, not version-pinned).
		await expect(layers).toContainText(/DeepSeek V4 Flash/);
		await expect(layers).toContainText(/DeepSeek V4 Pro/);

		// L3 candidate-range matches the actual prompt + diversity cap.
		await expect(layers).toContainText(/5-15/);
	});

	test('no retired Gemini model IDs appear anywhere on any user-facing route', async ({
		page
	}) => {
		// Earlier this test scanned only /about via .innerText() — but the
		// global footer ticker (rendered by +layout.svelte on every route) had
		// its own hardcoded retired IDs that .innerText() missed because
		// overflow-hidden clipped the chips off the rendered visual layout.
		// Use document.body.textContent (raw DOM text, no CSS visibility
		// honoring) and iterate every static route + the latest brief.
		const routes = ['/', '/briefs', '/about', '/experiments', `/brief/${latestDay.date}`];
		for (const route of routes) {
			await page.goto(route);
			await expect(page.locator('header a[href="/"]').first()).toContainText('ALPHALENS');
			// Wait for client-side hydration to settle so the scan covers the
			// dynamic parts of the page (e.g. candidate cards on /brief/<date>,
			// session tiles on /), not just the SSR'd shell. Per zen review on
			// PR #270: the previous fixed-route test on /about happened to be
			// fully static; this multi-route test needs to honour deferred
			// rendering.
			await page.waitForLoadState('networkidle');
			const text = (await page.evaluate(() => document.body.textContent)) ?? '';
			for (const dead of RETIRED_MODEL_IDS) {
				expect(
					text,
					`retired model id "${dead}" leaked into ${route} — bump $lib/models constants`
				).not.toContain(dead);
			}
		}
	});

	test('doctrine describes Pro-supplied keywords as a verification-gate fix, not a YAML replacement', async ({
		page
	}) => {
		// PR #148 added Pro-supplied search keywords to feed the verification
		// gates (press gate synonym matching) — it did NOT replace the
		// hand-curated GDELT theme buckets (config/gdelt_themes.yaml is still
		// the live news-ingest query source). The old copy claimed the
		// opposite. Pin the corrected framing.
		await page.goto('/about');
		const doctrine = page.locator('section').filter({ hasText: /operating\.doctrine/i });
		await expect(doctrine).toBeVisible();
		await expect(doctrine).toContainText(/verification gates/i);
		await expect(doctrine).toContainText(/PR #148/);
		await expect(doctrine).not.toContainText(/instead of hand-curated YAML buckets/i);
	});
});

test.describe('smoke — brief detail interactions', () => {
	test('theme filter chips and verified-only checkbox toggle without errors', async ({ page }) => {
		const consoleErrors: string[] = [];
		const pageErrors: string[] = [];
		const { onConsole, onPageError } = attachErrorCollectors(consoleErrors, pageErrors);
		page.on('console', onConsole);
		page.on('pageerror', onPageError);

		await page.goto(`/brief/${latestDay.date}`);

		// Click each theme filter chip; assert the candidate list narrows or
		// the empty-state placeholder appears — guarantees Svelte's $derived
		// filtered-list reactivity ran before we move on.
		const chips = page.getByRole('button', { name: /^#/ });
		// Auto-wait for the first theme chip to hydrate before the snapshot
		// count(). The brief route is client-rendered (ssr=false) with two
		// async fetches; goto resolves before the chips mount, so on slow CI
		// count() returns 0 → the click loop never runs and the test passes
		// vacuously without exercising any chip. latestDay always has themes.
		await expect(chips.first()).toBeVisible();
		const count = await chips.count();
		for (let i = 0; i < count; i++) {
			await chips.nth(i).click();
			await expect(page.locator('article[id], .text-center')).not.toHaveCount(0);
		}
		await page.getByRole('button', { name: /^all \(/ }).click();
		await expect(page.locator('article[id]')).toHaveCount(latestDay.n_candidates);

		// Toggle verified-only — same reactivity guarantee. The filter is
		// suppressed when every candidate is already verified (#238), so the
		// checkbox only renders on a mixed day. Exercise the toggle when it
		// exists; otherwise assert it stays hidden (the #238 contract). Every
		// fixture day is currently all-verified, so the else-branch runs.
		const brief = JSON.parse(DAY_BODIES[latestDay.date]);
		const verifiedCount = brief.candidates.filter((c: { verified: boolean }) => c.verified).length;
		const cb = page.locator('input[type="checkbox"]');
		if (verifiedCount < latestDay.n_candidates) {
			await cb.first().check();
			await expect(page.locator('article[id], .text-center')).not.toHaveCount(0);
			await cb.first().uncheck();
			await expect(page.locator('article[id]')).toHaveCount(latestDay.n_candidates);
		} else {
			await expect(cb).toHaveCount(0);
		}

		expect(consoleErrors).toEqual([]);
		expect(pageErrors).toEqual([]);
	});

	test('header metric strip renders candidate / theme counts (layout regression guard)', async ({ page }) => {
		const brief = JSON.parse(DAY_BODIES[latestDay.date]);

		await page.goto(`/brief/${latestDay.date}`);

		const strip = page.getByTestId('brief-header-stats');
		await expect(strip).toBeVisible();
		// Each value carries a stable data-testid so the assertion survives
		// Tailwind class / layout refactors of the metric strip.
		await expect(strip.getByTestId('stat-candidates')).toHaveText(String(brief.n_candidates));
		await expect(strip.getByTestId('stat-themes')).toHaveText(String(brief.n_themes));
		await expect(strip.getByTestId('stat-top-theme')).toContainText(brief.top_theme);
		// The verified count moved out of the headline strip — it is low-signal
		// there (often == candidates) and stays available via the "verified only"
		// filter + per-card badges. Pin its removal so it can't silently return.
		await expect(strip.getByTestId('stat-verified')).toHaveCount(0);
	});

	test('header metric grid sits beside the date, not below it (vertical-space layout guard)', async ({
		page
	}) => {
		await page.goto(`/brief/${latestDay.date}`);

		const date = page.getByTestId('brief-date');
		const stats = page.getByTestId('brief-header-stats');
		await expect(date).toBeVisible();
		await expect(stats).toBeVisible();

		const dateBox = await date.boundingBox();
		const statsBox = await stats.boundingBox();
		if (!dateBox || !statsBox) throw new Error('header date / stats bounding box missing');

		// At desktop width the metric grid shares the date's horizontal band
		// (vertical overlap) and sits to its right — the compact "metrics
		// beside date" header. The old full-width strip rendered the grid
		// BELOW the date (no overlap); this guards against regressing to that
		// taller layout that wasted vertical space.
		const verticalOverlap =
			statsBox.y < dateBox.y + dateBox.height && dateBox.y < statsBox.y + statsBox.height;
		expect(verticalOverlap, 'metric grid should share the date row').toBe(true);
		expect(statsBox.x, 'metric grid should sit to the right of the date').toBeGreaterThan(
			dateBox.x
		);
	});

	test('signal-bar tooltip opens on the underlined label, not the bar track', async ({ page }) => {
		await page.goto(`/brief/${latestDay.date}`);
		// data-testid is stable across Tailwind class refactors. Pick a bar that
		// actually carries a tooltip (its dotted-underlined label is the trigger).
		const bar = page
			.locator('article[id] [data-testid="signal-bar"]:has([role="tooltip"])')
			.first();
		const trigger = bar.locator('[role="group"]').first();
		const tooltip = trigger.locator('> [role="tooltip"]');

		// Hovering the bar track must NOT reveal the bubble — the trigger is the
		// name only, not the whole bar row (the hover-anchor contract). Asserting
		// computed opacity rather than toBeVisible(), which ignores opacity.
		await bar.getByTestId('signal-bar-track').hover();
		await expect(tooltip).toHaveCSS('opacity', '0');

		// Hovering the dotted-underlined label opens it (group-hover:opacity-100 —
		// doubles as the CSS regression guard the old test provided).
		await trigger.hover();
		await expect(tooltip).toHaveCSS('opacity', '1');
	});

	test('jargon-tip tooltip renders on hover (CSS regression guard)', async ({ page }) => {
		await page.goto('/experiments');
		// The "how.to.read" block contains the first inline JargonTip wrappers.
		// Hover the wrapper (.group) to trigger group-hover:opacity-100 on the
		// sibling tooltip. data-testid is stable across Tailwind class refactors.
		// The first αt JargonTip wraps a nested Carhart 4F JargonTip, so the
		// outer tip contains 2 tooltips — assert the OUTER one (direct child) is
		// visible on hover via the `>` direct-child selector.
		const firstTip = page.locator('[data-testid="jargon-tip"]').first();
		await firstTip.hover();
		const tooltip = firstTip.locator('> [role="tooltip"]');
		await expect(tooltip).toBeVisible();
	});

	test('gate-pill tooltip renders on hover (CSS regression guard)', async ({ page }) => {
		await page.goto(`/brief/${latestDay.date}`);
		// Inner pill carries the status symbol. Hover its wrapper (.group) to
		// trigger group-hover:opacity-100 on the sibling tooltip.
		const firstPill = page
			.locator('article[id] .group:has(.cursor-help)')
			.first();
		await firstPill.hover();
		const tooltip = page.locator('article[id] [role="tooltip"]').first();
		// toBeVisible() enforces opacity > 0; this catches a regression of the
		// group-hover:opacity-100 transition as well as DOM presence.
		await expect(tooltip).toBeVisible();
	});

	test('candidate cards drop the markdown expander and render core sections', async ({ page }) => {
		await page.goto(`/brief/${latestDay.date}`);
		// Auto-wait until the client-side load function has rendered the
		// candidate cards before asserting on their contents.
		await expect(page.locator('article[id]').first()).toBeVisible();
		// The full-markdown <details> expander was retired (2026-05-26);
		// candidate cards must no longer render any <details>.
		await expect(page.locator('article[id] details')).toHaveCount(0);
		// Core structured sections render: domain blocks (valuation/momentum) + trade-
		// execution setup panel. The detail grids inside each domain block are split
		// from the percentile bars by a quiet divider rule, not a text sub-heading.
		await expect(
			page.locator('article[id] [data-testid="block-valuation"]').first()
		).toBeVisible();
		await expect(page.locator('article[id] [data-testid="trade-setup"]').first()).toBeVisible();
	});

	test('expert lens anchors render in their domain blocks (buffett in valuation, o\'neil in momentum)', async ({ page }) => {
		// After the domain-regroup (Task 3) the Buffett and O'Neil scores moved from
		// the meta bar into their respective domain blocks. Both still always render
		// (shows "—" when absent) — but now as block-level anchors, not inline chips.
		await page.goto(`/brief/${latestDay.date}`);
		await expect(page.locator('article[id]').first()).toBeVisible();
		const card = page.locator('article[id]').first();
		await expect(card.locator('[data-testid="block-valuation"]')).toContainText('buffett');
		await expect(card.locator('[data-testid="block-momentum"]')).toContainText("o'neil");
	});

	test('trade-setup percentages render with bounded precision (no raw floats)', async ({ page }) => {
		// Regression: prod data arrives as raw floats (e.g. suggested_size_pct =
		// 4.065583485277316, alloc_pct = 27.98308726424079) from the equal-risk
		// ATR-allocation arithmetic. The 2026-05-18 fixture pins the prod
		// shape so a no-format slip is caught by the smoke suite.
		//
		// Per-field precision (broker-style):
		//   suggested_size_pct → 2 decimals (position-size "money number")
		//   alloc_pct / tranche_pct → integers (classic ladder weights;
		//     trailing decimals on a normalised ATR ratio carry no info)
		//   r_multiple / atr_distance → 1 decimal (already done at call site)
		//
		// Regex catches 3+ decimal places, which is the unbounded-float bug
		// class. 2-decimal values like "4.07%" are valid and pass.
		await page.goto(`/brief/${latestDay.date}`);
		await expect(page.locator('article[id]').first()).toBeVisible();
		const panels = page.locator('article[id] [data-testid="trade-setup"]');
		const n = await panels.count();
		expect(n).toBeGreaterThan(0);
		const offenders: string[] = [];
		for (let i = 0; i < n; i++) {
			const text = await panels.nth(i).innerText();
			for (const m of text.matchAll(/\d+\.\d{3,}%/g)) offenders.push(m[0]);
		}
		expect(offenders, `trade-setup must not render percentages with 3+ decimals; offenders: ${offenders.join(', ')}`).toEqual([]);
	});

	test('external (target=_blank) links announce the new tab to screen readers', async ({
		page
	}) => {
		await page.goto(`/brief/${latestDay.date}`);
		await expect(page.locator('article[id]').first()).toBeVisible();
		// Every link that opens a new tab must carry an aria-label ending in
		// "(opens in a new tab)" so screen-reader users get the same cue the
		// ExternalLink / ArrowUpRight icon gives sighted users. Covers the
		// layout API-docs link, the candidate source-event link, and the
		// per-day top-catalyst link.
		const blankLinks = page.locator('a[target="_blank"]');
		const n = await blankLinks.count();
		expect(n).toBeGreaterThan(0);
		for (let i = 0; i < n; i++) {
			await expect(blankLinks.nth(i)).toHaveAttribute('aria-label', /opens in a new tab/i);
		}
	});
});

// /experiments is client-rendered (SSR off), so the paradigm rows and their
// JargonTips appear only after Svelte's hydration pass. A bare `goto` followed
// by an immediate `.count()` race-loses against hydration on slower CI runners
// — the suite passes locally but flakes in CI (this is why the Playwright suite
// only started gating in CI with the migration-check PR). Navigate, then wait
// for the tooltip tree to be populated (≥20 tips and the trailing glossary
// section ⇒ the single atomic hydration pass finished) before counting.
async function gotoExperiments(page: Page) {
	await page.goto('/experiments');
	await page.locator('section#glossary').waitFor({ state: 'attached' });
	await expect(page.locator('[data-testid="jargon-tip"]').nth(19)).toBeAttached();
}

test.describe('experiments — hybrid tooltip policy', () => {
	// Policy: short acronyms (αt, OOS, IS, FL, CAR, BAB, Q5) get a JargonTip
	// at EVERY occurrence in data text. Multi-word / longer terms (Carhart 4F,
	// Bonferroni, phase-aliasing, multi-phase audit, Phase A-E, R², R2000-PIT,
	// FCFF, PEAD, NW HAC, Form-4, ADR) get a tooltip on FIRST occurrence per
	// section. Each paradigm <article> and each pattern <li> is a section.
	// These assertions guard the policy from silent drift via text edits.

	test('αt always-tooltip — appears as JargonTip across the page', async ({ page }) => {
		await gotoExperiments(page);
		const count = await page.locator('[data-testid="jargon-tip"]:has-text("αt")').count();
		expect(count, 'αt JargonTip count across page').toBeGreaterThanOrEqual(20);
	});

	test('OOS — appears as JargonTip in patterns + each paradigm row mini-bar', async ({ page }) => {
		await gotoExperiments(page);
		const count = await page.locator('[data-testid="jargon-tip"]:has-text("OOS")').count();
		expect(count, 'OOS JargonTip count').toBeGreaterThanOrEqual(20);
	});

	test('IS — appears as JargonTip in patterns + each paradigm row mini-bar', async ({ page }) => {
		await gotoExperiments(page);
		const count = await page.locator('[data-testid="jargon-tip"]:has-text("IS")').count();
		expect(count, 'IS JargonTip count').toBeGreaterThanOrEqual(20);
	});

	test('FL — JargonTip exists at least in how.to.read + paradigm metrics', async ({ page }) => {
		await gotoExperiments(page);
		const count = await page.locator('[data-testid="jargon-tip"]:has-text("FL")').count();
		expect(count, 'FL JargonTip count').toBeGreaterThanOrEqual(2);
	});

	test('CAR — wrapped on every occurrence in P04 row', async ({ page }) => {
		await gotoExperiments(page);
		const count = await page.locator('article#P04 [data-testid="jargon-tip"]:has-text("CAR")').count();
		expect(count, 'CAR JargonTip count in P04').toBe(4);
	});

	test('BAB — exists in P15 row', async ({ page }) => {
		await gotoExperiments(page);
		const count = await page.locator('article#P15 [data-testid="jargon-tip"]:has-text("BAB")').count();
		expect(count, 'BAB JargonTip count in P15').toBeGreaterThanOrEqual(1);
	});

	test('Q5 — exists in S01 row', async ({ page }) => {
		await gotoExperiments(page);
		const count = await page.locator('article#S01 [data-testid="jargon-tip"]:has-text("Q5")').count();
		expect(count, 'Q5 JargonTip count in S01').toBeGreaterThanOrEqual(1);
	});

	test('Bonferroni — first-per-section across patterns + paradigm rows', async ({ page }) => {
		await gotoExperiments(page);
		const count = await page.locator('[data-testid="jargon-tip"]:has-text("Bonferroni")').count();
		expect(count, 'Bonferroni JargonTip count').toBeGreaterThanOrEqual(2);
	});

	test('Carhart 4F — first-per-section in how.to.read + patterns', async ({ page }) => {
		await gotoExperiments(page);
		const count = await page.locator('[data-testid="jargon-tip"]:has-text("Carhart")').count();
		expect(count, 'Carhart 4F JargonTip count').toBeGreaterThanOrEqual(2);
	});

	test('PEAD — exists in P14 row', async ({ page }) => {
		await gotoExperiments(page);
		const count = await page.locator('article#P14 [data-testid="jargon-tip"]:has-text("PEAD")').count();
		expect(count, 'PEAD JargonTip count in P14').toBeGreaterThanOrEqual(1);
	});

	test('NW HAC — exists in P14 row', async ({ page }) => {
		await gotoExperiments(page);
		const count = await page.locator('article#P14 [data-testid="jargon-tip"]:has-text("NW HAC")').count();
		expect(count, 'NW HAC JargonTip count in P14').toBeGreaterThanOrEqual(1);
	});

	test('Form-4 — exists in S01 row (first-per-section)', async ({ page }) => {
		await gotoExperiments(page);
		const count = await page.locator('article#S01 [data-testid="jargon-tip"]:has-text("Form-4")').count();
		expect(count, 'Form-4 JargonTip count in S01').toBeGreaterThanOrEqual(1);
	});

	test('ADR — exists in patterns section', async ({ page }) => {
		await gotoExperiments(page);
		const count = await page.locator('[data-testid="jargon-tip"]:has-text("ADR")').count();
		expect(count, 'ADR JargonTip count').toBeGreaterThanOrEqual(1);
	});

	test('glossary section has NO JargonTips (defines them; tooltips would be self-referential)', async ({ page }) => {
		await gotoExperiments(page);
		// The glossary section comes last on the page; scope by section title text.
		// Each glossary entry is a <dt>/<dd> pair inside the section. JargonTip
		// uses <span data-testid="jargon-tip">, none should appear under the
		// glossary section header.
		const glossarySection = page.locator('section').filter({ hasText: 'glossary.terms' });
		const count = await glossarySection.locator('[data-testid="jargon-tip"]').count();
		expect(count, 'glossary section JargonTip count must be 0').toBe(0);
	});

	test('paradigm detail fields collapsed by default (P0.1)', async ({ page }) => {
		await gotoExperiments(page);
		// Every paradigm row's <details> must start closed so the page is
		// scannable on initial render. If a future edit flips a default to
		// open, scroll depth balloons back to the pre-P0.1 level.
		const opened = await page.locator('article details[open]').count();
		expect(opened, 'no paradigm details should be open on initial load').toBe(0);
		// And every paradigm row must contain exactly one <details>.
		const articles = await page.locator('article').count();
		const details = await page.locator('article details').count();
		expect(details, 'one <details> per paradigm article').toBe(articles);
	});

	test('status filter chips filter the ledger and do not pin their tooltip on click (P3.2)', async ({ page }) => {
		await gotoExperiments(page);
		const failChip = page.locator('#paradigms button', { hasText: /^fail 15$/i });
		await failChip.click();
		// The filter applied: only the 15 FAIL rows remain in the paradigm ledger.
		const shown = await page.locator('#paradigms article').count();
		expect(shown, 'FAIL filter shows only the 15 FAIL paradigms').toBe(15);
		// The click must NOT leave the chip's ChipTip wrapper focused — otherwise
		// its tooltip stays pinned (focus-within) while the next chip is hovered,
		// showing two tooltips at once. selectFilter() blurs the button + wrapper.
		const pinned = await page.evaluate(() =>
			[...document.querySelectorAll('#paradigms [data-testid="chip-tip"]')].some((w) =>
				w.matches(':focus-within')
			)
		);
		expect(pinned, 'no filter chip tooltip stays pinned via focus after a click').toBe(false);
	});

	test('status filter is multi-select and CLEAR resets to all (P3.3)', async ({ page }) => {
		await gotoExperiments(page);
		const fail = page.locator('#paradigms button', { hasText: /^fail 15$/i });
		const inconclusive = page.locator('#paradigms button', { hasText: /^inconclusive 2$/i });
		// Selecting two statuses shows the union (15 FAIL + 2 INCONCLUSIVE = 17),
		// and both chips read pressed — proving multi-select, not single-select.
		await fail.click();
		await inconclusive.click();
		expect(await page.locator('#paradigms article').count(), 'FAIL + INCONCLUSIVE = 17 rows').toBe(17);
		expect(await fail.getAttribute('aria-pressed')).toBe('true');
		expect(await inconclusive.getAttribute('aria-pressed')).toBe('true');
		// Toggling a selected chip off narrows back to just the other.
		await fail.click();
		expect(await page.locator('#paradigms article').count(), 'toggling FAIL off leaves 2 INCONCLUSIVE').toBe(2);
		// CLEAR resets to the full ledger.
		await page.locator('#paradigms button', { hasText: /^clear/i }).click();
		expect(await page.locator('#paradigms article').count(), 'CLEAR shows all 18').toBe(18);
	});

	test('tool.experiments has its own independent multi-select filter (P3.4)', async ({ page }) => {
		await gotoExperiments(page);
		// Both ledgers use the shared LedgerFilterBar. The tool filter is a second,
		// independent instance — filtering it must NOT touch the paradigm ledger.
		const noGo = page.locator('#tool-experiments button', { hasText: /^no-go 1$/i });
		const finding = page.locator('#tool-experiments button', { hasText: /^finding 1$/i });
		await noGo.click();
		await finding.click();
		expect(await page.locator('#tool-experiments article').count(), 'NO-GO + FINDING = 2 tool rows').toBe(2);
		expect(await noGo.getAttribute('aria-pressed')).toBe('true');
		expect(await finding.getAttribute('aria-pressed')).toBe('true');
		// The paradigm ledger is a separate filter instance — untouched (all 18).
		expect(await page.locator('#paradigms article').count(), 'paradigm ledger unaffected by tool filter').toBe(18);
		// CLEAR restores the full tool ledger (5 rows).
		await page.locator('#tool-experiments button', { hasText: /^clear/i }).click();
		expect(await page.locator('#tool-experiments article').count(), 'CLEAR shows all 5 tool rows').toBe(5);
	});

	test('footer ticker switches vocabulary on /experiments vs other routes (P1.2)', async ({ page }) => {
		await page.goto('/');
		// Auto-wait until the layout's $derived ticker computation has
		// flushed and the chip spans are in the DOM with text-amber class.
		// Without this the sync `.allTextContents()` race-loses on warm
		// Vite preview runs that boot the preview server's hot-class scan
		// after the navigation completes.
		await expect(page.locator('footer span.text-amber').first()).toBeVisible();
		const dashChips = (await page.locator('footer span.text-amber').allTextContents()).join(' ');
		expect(dashChips, 'dashboard footer keeps thematic vocab').toContain('PRESS-GATE');
		expect(dashChips, 'dashboard footer does not show research vocab').not.toContain('DOCTRINE');

		await gotoExperiments(page);
		await expect(page.locator('footer span.text-amber').first()).toBeVisible();
		const expChips = (await page.locator('footer span.text-amber').allTextContents()).join(' ');
		expect(expChips, '/experiments footer shows research vocab').toContain('DOCTRINE');
		expect(expChips, '/experiments footer does not show thematic vocab').not.toContain('PRESS-GATE');
	});

	test('sticky TOC renders on xl viewport with 6 section anchors (P3.1)', async ({ page }) => {
		await page.setViewportSize({ width: 1440, height: 900 });
		await gotoExperiments(page);
		// Aside rail is `hidden xl:block` — invisible below 1280px.
		const tocLinks = await page.locator('nav[aria-label="Section table of contents"] a').count();
		expect(tocLinks, '6 section anchors in TOC').toBe(6);
		// Section ids the TOC points to must exist on the page. (status.legend was
		// replaced by on-hover ChipTip tooltips on the status chips themselves;
		// infrastructure.live was removed — it lives in deploy/systemd + CLAUDE.md.)
		for (const id of ['how-to-read', 'paradigms', 'tool-experiments', 'patterns', 'methodology', 'glossary']) {
			const ok = await page.locator(`section#${id}`).count();
			expect(ok, `section#${id} exists`).toBe(1);
		}
	});

	test('--color-fg-muted contrast against --color-bg meets WCAG AA (≥4.5:1) (P1.1)', async ({ page }) => {
		await gotoExperiments(page);
		const ratio = await page.evaluate(() => {
			const cs = getComputedStyle(document.documentElement);
			const fg = cs.getPropertyValue('--color-fg-muted').trim();
			const bg = cs.getPropertyValue('--color-bg').trim();
			const hexToRgb = (h: string) => {
				const m = h.replace('#', '');
				return [0, 2, 4].map((i) => parseInt(m.slice(i, i + 2), 16));
			};
			const lum = (rgb: number[]) => {
				const c = rgb.map((v) => {
					const s = v / 255;
					return s <= 0.03928 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4);
				});
				return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
			};
			const la = lum(hexToRgb(fg));
			const lb = lum(hexToRgb(bg));
			return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
		});
		expect(ratio, '--color-fg-muted vs --color-bg WCAG ratio').toBeGreaterThanOrEqual(4.5);
	});

	test('heading semantics: ≥6 h2 (one per section), ≥31 h3 (paradigms+patterns) (P0.3)', async ({ page }) => {
		await gotoExperiments(page);
		const h2 = await page.locator('h2').count();
		const h3 = await page.locator('h3').count();
		expect(h2, '≥6 h2 (how.to.read, paradigms.ledger, tool.experiments, failure.patterns, methodology.artifacts, glossary.terms)').toBeGreaterThanOrEqual(6);
		expect(h3, '≥31 h3 (18 paradigm names + 13 pattern names)').toBeGreaterThanOrEqual(31);
	});

	test('hash auto-expand opens target paradigm row (P0.2)', async ({ page }) => {
		await page.goto('/experiments#P14');
		// $effect runs after hydration; wait briefly for the details to flip.
		await page.locator('article#P14 details[open]').waitFor({ timeout: 2000 });
		const opened = await page.locator('article#P14 details[open]').count();
		expect(opened, 'P14 details must be open after hash deep-link').toBe(1);
		// The shared <Disclosure> chevron rotates off a reactive bind:open, so a
		// programmatic hash-open must also rotate it (not just CSS group-open).
		const chevron = page.locator('article#P14 summary span[aria-hidden="true"]').first();
		await expect(chevron).toHaveClass(/rotate-90/);
	});

	test('Disclosure hides the native marker and toggles the chevron on click (P3.5)', async ({ page }) => {
		await gotoExperiments(page);
		const details = page.locator('section#how-to-read details');
		const summary = details.locator('summary');
		const chevron = summary.locator('span[aria-hidden="true"]').first();
		// Native disclosure triangle is suppressed via the shared component.
		await expect(summary).toHaveClass(/\[&::-webkit-details-marker\]:hidden/);
		expect(await chevron.getAttribute('class'), 'chevron not rotated while closed').not.toContain('rotate-90');
		await summary.click();
		await expect(details).toHaveAttribute('open', '');
		await expect(chevron, 'chevron rotates when opened').toHaveClass(/rotate-90/);
	});

	// The fix is a global CSS rule, so the test sweeps every route that ships
	// a native <button> (per `grep -rl '<button' src/`): error card on /error
	// is not directly reachable without auth failure, but /experiments holds
	// the most buttons and /brief/[date] holds the candidate-card action
	// buttons. Loop guards against a future per-route style that re-breaks
	// the pointer convention silently.
	for (const url of ['/experiments', `/brief/${latestDay.date}`]) {
		test(`native <button> elements default to cursor:pointer on ${url}`, async ({ page }) => {
			await page.goto(url);
			// /brief loads candidates via client-side fetch — wait for the first
			// button to be present in the DOM before evaluating styles. Use
			// `state: 'attached'` because /experiments buttons live inside
			// collapsed <details> and are hidden but DOM-present; computed
			// cursor still resolves correctly on hidden elements.
			await page
				.locator('button:not(:disabled)')
				.first()
				.waitFor({ state: 'attached', timeout: 5000 });
			const cursors = await page.locator('button:not(:disabled)').evaluateAll((els) =>
				els.map((el) => getComputedStyle(el).cursor)
			);
			expect(cursors.length, `${url} must render ≥1 native <button>`).toBeGreaterThan(0);
			const nonPointer = cursors.filter((c) => c !== 'pointer');
			expect(
				nonPointer,
				`every enabled button on ${url} should default to cursor:pointer (got ${nonPointer.join(', ')})`
			).toEqual([]);
		});
	}
});

test.describe('experiments — appendix card layout', () => {
	// The reference appendix holds independent peer items, not sequences: the
	// failure patterns and the surviving methodology artifacts read as cards, not
	// a stacked list / table. Infrastructure stays a genuine table (id/status/
	// deploy is tabular). These pins guard the container choice from silent
	// reversion to the old list/table markup.

	test('failure.patterns renders 13 lesson cards in a grid (P-cards.1)', async ({ page }) => {
		await gotoExperiments(page);
		const cards = await page.locator('section#patterns [data-testid="pattern-card"]').count();
		expect(cards, '13 lesson cards under failure.patterns').toBe(13);
	});

	test('methodology.artifacts renders 4 feature cards, not table rows (P-cards.2)', async ({ page }) => {
		await gotoExperiments(page);
		const cards = await page.locator('section#methodology [data-testid="artifact-card"]').count();
		expect(cards, '4 artifact cards under methodology.artifacts').toBe(4);
		// The artifact section must NOT fall back to a table.
		const tables = await page.locator('section#methodology table').count();
		expect(tables, 'methodology.artifacts is card grid, not a table').toBe(0);
	});

	test('infrastructure.live section is gone (lived in deploy/systemd + CLAUDE.md) (P-cards.3)', async ({ page }) => {
		await gotoExperiments(page);
		// The static live-infra snapshot drifted from reality and duplicated the
		// authoritative VPS-backfills table in CLAUDE.md, so it was removed.
		const infra = await page.locator('section#infra').count();
		expect(infra, 'no infrastructure.live section on /experiments').toBe(0);
	});

	test('card grids use items-start so cards hug their content height (P-cards.4)', async ({ page }) => {
		await gotoExperiments(page);
		// Default CSS-grid rows stretch every card to the tallest in its row, which
		// left up to ~59px of empty space in the shorter card. align-items:start
		// makes each card only as tall as its own content.
		for (const sel of ['section#patterns', 'section#methodology']) {
			// Tailwind `items-start` computes to `flex-start` (grid treats it as
			// start). The point is it must NOT be the stretching default.
			const align = await page
				.locator(`${sel} ul[role="list"]`)
				.evaluate((el) => getComputedStyle(el).alignItems);
			expect(['start', 'flex-start'], `${sel} card grid must not stretch cards (got ${align})`).toContain(align);
		}
	});
});

test.describe('experiments — paradigms.ledger header', () => {
	// The paradigms ledger is the page's primary payload, so its header must read
	// louder than the utility section headers, and its intro must not send the
	// reader off to the other (live-tool) track.

	test('both ledger headers (paradigms + tool.experiments) read louder than the appendix headers (P-hdr.1)', async ({ page }) => {
		await gotoExperiments(page);
		const sizePx = (sel: string) =>
			page.locator(sel).first().evaluate((el) => parseFloat(getComputedStyle(el).fontSize));
		const weight = (sel: string) =>
			page.locator(sel).first().evaluate((el) => Number(getComputedStyle(el).fontWeight));
		// A quiet appendix section header (methodology.artifacts) is the baseline.
		const appendix = await sizePx('section#methodology h2');
		for (const sel of ['section#paradigms h2', 'section#tool-experiments h2']) {
			expect(await sizePx(sel), `${sel} is larger than the appendix header`).toBeGreaterThan(appendix);
			expect(await weight(sel), `${sel} is bold`).toBeGreaterThanOrEqual(700);
		}
	});

	test('paradigms intro drops the live-tool cross-reference (P-hdr.2)', async ({ page }) => {
		await gotoExperiments(page);
		const text = (await page.locator('section#paradigms').innerText()).toLowerCase();
		expect(text, 'paradigms intro no longer mentions the live-tool track').not.toContain('live-tool');
		const crossLink = await page.locator('section#paradigms a[href="#tool-experiments"]').count();
		expect(crossLink, 'no tool.experiments cross-link inside the paradigms section').toBe(0);
	});
});

test.describe('glossary auto-discovery (per-page coverage)', () => {
	// For every entry in $lib/data/glossary, assert at least one inline
	// JargonTip with the corresponding data-term attribute renders on every
	// page in entry.pages (default ['experiments']). The data-term attribute
	// (set on JargonTip wrapper) is stable across text-label variants — so
	// [Bonferroni correction|Bonferroni] still passes the "Bonferroni
	// correction" assertion.
	//
	// Adding a new glossary entry without an inline reference will fail the
	// matching test here, surfacing orphan glossary entries automatically.
	//
	// pages mapping → URL:
	//   - 'experiments' → '/experiments'
	//   - 'briefs'      → '/brief/2026-05-18' (a known-good fixture)
	const PAGE_URL: Record<'experiments' | 'briefs', string> = {
		experiments: '/experiments',
		briefs: '/brief/2026-05-18'
	};

	for (const entry of GLOSSARY) {
		const targetPages = entry.pages ?? ['experiments'];
		for (const tp of targetPages) {
			const url = PAGE_URL[tp];
			test(`glossary term "${entry.term}" has ≥1 inline JargonTip on ${url}`, async ({ page }) => {
				await page.goto(url);
				// Brief detail loads candidates via client-side fetch; wait for at
				// least the first CandidateCard to mount before counting tooltips.
				// /experiments is fully prerendered, so the wait resolves instantly
				// for that route.
				await page.locator('[data-testid="jargon-tip"]').first().waitFor({ timeout: 5000 });
				const count = await page
					.locator(`[data-testid="jargon-tip"][data-term="${entry.term}"]`)
					.count();
				expect(
					count,
					`term "${entry.term}" should appear as inline JargonTip somewhere on ${url} (glossary section excluded — those terms appear in <dt>, not JargonTip)`
				).toBeGreaterThanOrEqual(1);
			});
		}
	}
});

test.describe('smoke — mobile (390 + 360 viewports)', () => {
	// Catches: layout/header/nav/footer that forces a wider viewport than the
	// device; CandidateCard header strip that can't collapse; tooltips whose
	// absolute layout pushes scrollWidth past the viewport edge.
	const PHONES = [
		{ name: 'iphone-13', width: 390, height: 844 },
		{ name: 'small-android', width: 360, height: 800 }
	];
	for (const { name, width, height } of PHONES) {
		for (const path of ['/', '/briefs', '/about', '/experiments', `/brief/${latestDay.date}`]) {
			test(`${name} (${width}x${height}) — no horizontal scroll on ${path}`, async ({ page }) => {
				await page.setViewportSize({ width, height });
				const response = await page.goto(path);
				expect(response?.status()).toBe(200);
				const scrollW = await page.evaluate(() => document.documentElement.scrollWidth);
				expect(scrollW, `${path} scrollWidth must be ≤ viewport ${width}`).toBeLessThanOrEqual(width);
			});
		}
	}

	test('360px — every hovered tooltip stays inside the viewport (edge-clamp guard)', async ({
		page
	}) => {
		// Regression guard for the clampToViewport action. The popover is centered
		// on its trigger (`left-1/2 translateX(-50%)`); a trigger near the right
		// edge would push the bubble off-screen, and an `overflow-hidden` ancestor
		// CLIPS the overflow (text cut off) rather than growing scrollWidth — so
		// the no-horizontal-scroll test above cannot catch it. Hover every tooltip
		// trigger in the first card and assert its popover box is within [0, width].
		const width = 360;
		await page.setViewportSize({ width, height: 800 });
		await page.goto(`/brief/${latestDay.date}`);
		const card = page.locator('article[id]').first();
		await expect(card).toBeVisible();

		// Trigger wrappers carry role="group" with a direct-child [role="tooltip"]
		// (gate pills, signal bars, chip tips). Direct-child `>` avoids matching a
		// nested tip's popover.
		const triggers = card.locator('[role="group"]:has(> [role="tooltip"])');
		const count = await triggers.count();
		expect(count, 'first card should expose at least one tooltip trigger').toBeGreaterThan(0);

		for (let i = 0; i < count; i++) {
			const trigger = triggers.nth(i);
			await trigger.scrollIntoViewIfNeeded();
			await trigger.hover();
			const tip = trigger.locator('> [role="tooltip"]');
			await expect(tip).toBeVisible();
			const box = await tip.boundingBox();
			expect(box, `tooltip ${i} should have a layout box`).not.toBeNull();
			// 1px tolerance for sub-pixel rounding.
			expect(box!.x, `tooltip ${i} left edge must stay on-screen`).toBeGreaterThanOrEqual(-1);
			expect(
				box!.x + box!.width,
				`tooltip ${i} right edge must stay within the ${width}px viewport`
			).toBeLessThanOrEqual(width + 1);
		}
	});

	test('market-context tooltip opens below so its tall body is not clipped at the viewport top', async ({
		page
	}) => {
		// The market-context banner sits high on the brief page and its glossary
		// tooltip is tall (definition + five state bands). A default upward-opening
		// popover overflows the top of the viewport and gets clipped (the term is
		// unreadable). The banner opts its JargonTip into `placement="below"`; this
		// pins the popover top edge on-screen.
		await page.setViewportSize({ width: 1280, height: 800 });
		await page.goto(`/brief/${latestDay.date}`);
		const trigger = page.locator('[data-testid="jargon-tip"][data-term="market context"]');
		await expect(trigger).toBeVisible();
		await trigger.hover();
		const tip = trigger.locator('> [role="tooltip"]');
		await expect(tip).toBeVisible();
		const box = await tip.boundingBox();
		expect(box, 'market-context tooltip should have a layout box').not.toBeNull();
		expect(
			box!.y,
			'market-context tooltip top edge must stay on-screen (opens below the trigger)'
		).toBeGreaterThanOrEqual(-1);
	});
});

test.describe('experiments — evidence drawer files reachable', () => {
	// Plugs the gap that hid PR #218: the link-spider tests above only crawl
	// `<a href="/...">` references; the Evidence drawer is a `<button>` with
	// a JS handler that fetches `/docs/research/{file}` on click. Without
	// this test the sync script could silently emit "0/15 synced" (which
	// is exactly what the monorepo refactor in commit ca378a5 produced for
	// two days) and CI would pass. Gemini 3 Pro post-merge review on
	// commit 01ae4fb flagged the gap.
	test('every evidence button targets a reachable /docs/research file', async ({ page, request }) => {
		await page.goto('/experiments');
		// Auto-wait for the first evidence button to be ATTACHED before the
		// one-shot evaluateAll. `/experiments` mounts these buttons after
		// client-side hydration, so an immediate evaluateAll can run before any
		// button exists on a slow CI runner → reads 0 → the count assertion
		// below fails spuriously (recurring CI flake on PRs that touch no web
		// file). `attached` (not `visible`) because each button lives inside a
		// `<details>` that is collapsed by default — present in the DOM but not
		// visible — and evaluateAll reads hidden nodes too. A visibility wait
		// would never resolve without opening every drawer.
		await page
			.locator('button[aria-label^="open evidence: "]')
			.first()
			.waitFor({ state: 'attached' });
		// Buttons are labelled ``open evidence: <path>`` — extract the path
		// from the aria-label and verify the corresponding static asset
		// returns 200 over real HTTP (not the api-mock — these files are
		// served by SvelteKit from /static/).
		const labels = await page.locator('button[aria-label^="open evidence: "]').evaluateAll(
			(nodes) =>
				nodes.map((n) => (n.getAttribute('aria-label') ?? '').replace('open evidence: ', ''))
		);
		expect(labels.length, 'experiments page must render at least one evidence button').toBeGreaterThan(0);
		for (const file of labels) {
			const res = await request.get(`/docs/research/${file}`);
			expect(res.status(), `evidence file ${file} should return 200`).toBe(200);
		}
	});
});

test.describe('dashboard — captured sessions tile cap', () => {
	// The CAPTURED.SESSIONS grid on the dashboard renders one tile per
	// brief day. With a long history (the index returns up to 200) the grid
	// would grow unbounded, so it is capped at 6 recent tiles — the full
	// list lives behind the "all briefs" link. This test feeds the index
	// endpoint 10 synthetic days and asserts only the 6 newest tiles render.
	const MANY_DAYS = Array.from({ length: 10 }, (_, i) => ({
		date: `2026-05-${String(20 - i).padStart(2, '0')}`,
		n_candidates: 10 + i,
		n_themes: 3,
		top_theme: `theme-${i}`
	}));

	test('renders at most 6 session tiles even when index returns more', async ({ page }) => {
		// Override only the index route; per-day fetches fall through to the
		// beforeEach mock (404 → latestBrief null), which is fine because the
		// session grid renders straight from data.days regardless of latestBrief.
		await page.route(
			(url) => url.pathname === '/api/v1/days',
			(route) =>
				route.fulfill({
					status: 200,
					contentType: 'application/json',
					body: JSON.stringify({
						data: MANY_DAYS,
						meta: { total: MANY_DAYS.length, limit: 200, offset: 0 }
					})
				})
		);

		await page.goto('/');
		const tiles = page.locator('[data-testid="session-tiles"] > a');
		await expect(tiles).toHaveCount(6);
		// The 6 newest are kept (index is newest-first).
		await expect(tiles.first()).toContainText('2026-05-20');
		await expect(tiles.last()).toContainText('2026-05-15');
	});
});

test.describe('smoke — api fixture integrity', () => {
	test('every fixture day listed in days.json has a per-day file', () => {
		// Sanity check the mock fixture set itself — if a day is missing
		// from DAY_BODIES we want to fail at test discovery, not silently
		// 404 inside one of the route handlers downstream.
		const missing = days.filter((day) => !DAY_BODIES[day.date]);
		expect(missing, `fixtures missing per-day JSON for: ${missing.map((d) => d.date)}`).toEqual([]);
	});
});

test.describe('card — domain grouping', () => {
	test(`first card on /brief/${latestDay.date} is domain-grouped`, async ({ page }) => {
		await page.goto(`/brief/${latestDay.date}`);
		const card = page.locator('article[id]').first();
		await expect(card).toBeVisible();

		// Domain section headings present (dot-separated WORD.WORD, matching the
		// card's other cyan headers like live.equity.thesis / trade.execution.setup).
		for (const heading of ['catalyst.event', 'valuation.quality', 'momentum.technicals']) {
			await expect(card.getByText(heading, { exact: false })).toBeVisible();
		}
		// Insider renders ONLY on a net opportunistic buy (rare, ~1/400). The fixture's
		// first card has no buys, so there is no insider row at all (the old `insider 90d`
		// / `insider buys` row and the `insider / flow` heading are both absent).
		await expect(card.getByText('insider buys · 180d', { exact: false })).toHaveCount(0);
		await expect(card.getByText('insider / flow', { exact: false })).toHaveCount(0);

		// Dedup: each label renders exactly once in the card BODY. getByText does
		// case-insensitive substring matching and also matches hidden tooltip text
		// (the [role="tooltip"] bubbles render at opacity-0), so subtract tooltip
		// occurrences — the real duplication guard is about visible rows, not the
		// incidental mention of a metric inside a lens tooltip.
		for (const label of ['off 52w high', 'ma200 dist', 'ma200 slope', 'fcff yield']) {
			const total = await card.getByText(label, { exact: false }).count();
			const inTooltip = await card.locator('[role="tooltip"]').getByText(label, { exact: false }).count();
			expect(total - inTooltip, `${label} body occurrences`).toBe(1);
		}

		// Meta bar slimmed: no buffett/o'neil/catalyst chip in the meta row.
		const meta = card.locator('[data-testid="card-meta"]');
		await expect(meta.getByText('buffett', { exact: false })).toHaveCount(0);
		await expect(meta.getByText("o'neil", { exact: false })).toHaveCount(0);
		await expect(meta.getByText('catalyst', { exact: false })).toHaveCount(0);
		// The headline badge is the operative ranking score (selection_score),
		// labelled `score` — not the raw `layer-4` input. `layer-4` now lives only in
		// the badge's hover tooltip, so exclude [role="tooltip"] when checking the face.
		const scoreBadge = meta.locator('[data-testid="chip-tip"][data-term="ranking score"]');
		await expect(scoreBadge).toBeVisible();
		const l4Total = await meta.getByText('layer-4', { exact: false }).count();
		const l4InTip = await meta.locator('[role="tooltip"]').getByText('layer-4', { exact: false }).count();
		expect(l4Total - l4InTip, 'layer-4 on the visible meta face').toBe(0);

		// Lens scores anchor their domain blocks. toContainText asserts the block's
		// text includes the lens name regardless of how many descendants match (the
		// chip label + its hidden tooltip title both contain it).
		await expect(card.locator('[data-testid="block-valuation"]')).toContainText('buffett');
		await expect(card.locator('[data-testid="block-momentum"]')).toContainText("o'neil");
	});

	test(`expert.panel drawer omits the O'Neil numeric grid on /brief/${latestDay.date}`, async ({ page }) => {
		await page.goto(`/brief/${latestDay.date}`);
		const card = page.locator('article[id]').first();
		// Open the drawer.
		await card.getByRole('button', { name: /expert.panel/i }).click();
		const drawer = card.locator('[data-testid="expert-panel-body"]');
		await expect(drawer).toBeVisible();
		// Scorer breakdown moved OUT of the drawer into the score-badge tooltip.
		await expect(drawer.getByText('scorer breakdown', { exact: false })).toHaveCount(0);
		// O'Neil numeric readout grid is gone (rel strength now only in the momentum block).
		await expect(drawer.getByText('rel strength', { exact: false })).toHaveCount(0);
		// Pointer to the momentum block present.
		await expect(drawer.getByText('momentum & technicals', { exact: false })).toBeVisible();
	});

	test(`score badge tooltip carries the scorer breakdown on /brief/${latestDay.date}`, async ({ page }) => {
		await page.goto(`/brief/${latestDay.date}`);
		const card = page.locator('article[id]').first();
		const badge = card.locator('[data-testid="chip-tip"][data-term="ranking score"]');
		await expect(badge).toBeVisible();
		await badge.hover();
		// Derivation + the not-yet-validated caveat live in the badge's own tooltip.
		const tip = badge.locator('[role="tooltip"]');
		await expect(tip.getByText('selection score', { exact: false })).toBeVisible();
		await expect(tip.getByText('suggestive', { exact: false })).toBeVisible();
	});

	test(`insider-buys row renders only on a net buy on /brief/${latestDay.date}`, async ({ page }) => {
		await page.goto(`/brief/${latestDay.date}`);
		// The DFIN fixture has insider_score_usd > 0 (a 180-day opportunistic buy) on a
		// ✗ 90-day INSIDER gate — the GME-like divergence. The 180d row renders for it...
		const dfin = page.locator('article[id="DFIN"]');
		// Match the full row label so it doesn't also catch the tooltip header
		// ("opportunistic insider buys (180d)").
		await expect(dfin.getByText('insider buys · 180d', { exact: false })).toBeVisible();
		await expect(dfin.getByText('88%ile', { exact: false })).toBeVisible();
		// ...but NOT for the no-buy first card.
		const first = page.locator('article[id]').first();
		await expect(first.getByText('insider buys · 180d', { exact: false })).toHaveCount(0);
	});

	test(`buffett anchor tooltip reveals on hover on /brief/${latestDay.date}`, async ({ page }) => {
		await page.goto(`/brief/${latestDay.date}`);
		const card = page.locator('article[id]').first();
		const anchor = card.locator('[data-testid="chip-tip"][data-term="buffett quality"]');
		await expect(anchor).toBeVisible();
		await anchor.hover();
		// A bodyRich row label from buffRows — proves the tooltip is wired to the
		// score token after moving the flex row out of ChipTip. Scoped to the
		// anchor's own tooltip so it cannot false-pass if the label ever appears
		// on the card face.
		await expect(
			anchor.locator('[role="tooltip"]').getByText('owner-earnings yield', { exact: false })
		).toBeVisible();
	});

	test(`buffett drawer card is symmetric (score + empty state, no pillars) on /brief/${latestDay.date}`, async ({ page }) => {
		await page.goto(`/brief/${latestDay.date}`);
		const card = page.locator('article[id]').first();
		await card.getByRole('button', { name: /expert.panel/i }).click();
		const drawer = card.locator('[data-testid="expert-panel-body"]');
		await expect(drawer).toBeVisible();
		// The Buffett card renders its empty state (numeric score, no qual).
		await expect(drawer.getByText('qualitative read not computed', { exact: false })).toBeVisible();
		// No qualitative pillars for this name.
		await expect(drawer.getByText('moat', { exact: false })).toHaveCount(0);
	});

	test(`lens-score labels are stacked in separate rows on /brief/${latestDay.date}`, async ({ page }) => {
		await page.goto(`/brief/${latestDay.date}`);
		const card = page.locator('article[id]').first();
		await card.getByRole('button', { name: /expert.panel/i }).click();
		const buf = card.locator('[data-testid="lens-label-buffett"]');
		const oneil = card.locator('[data-testid="lens-label-oneil"]');
		await expect(buf).toBeVisible();
		await expect(oneil).toBeVisible();
		await expect(buf).toContainText('Buffett');
		await expect(oneil).toContainText("O'Neil");
		// Stacked, not overlapping: the buffett label sits above the o'neil label.
		const b = await buf.boundingBox();
		const o = await oneil.boundingBox();
		expect(b && o && b.y < o.y).toBeTruthy();
	});

	test(`no TTL chip when there is no structured ladder on /brief/${latestDay.date}`, async ({ page }) => {
		await page.goto(`/brief/${latestDay.date}`);
		const setup = page.locator('article[id]').first().locator('[data-testid="trade-setup"]');
		await expect(setup.getByText('no structured ladder', { exact: false })).toBeVisible();
		await expect(setup.getByText('ttl:', { exact: false })).toHaveCount(0);
	});
});

test.describe('smoke — SvelteKit stale-import recovery (version polling)', () => {
	// Canonical fix for the post-deploy blank-screen bug per
	// https://github.com/sveltejs/kit/issues/9089 — the build emits
	// /_app/version.json, the client polls it on `kit.version.pollInterval`,
	// and when a mismatch is detected `updated.current` flips true. The
	// layout opts <main> into a full reload on the next navigation via
	// `data-sveltekit-reload` so the browser fetches the new HTML + new
	// hashed chunk URLs instead of trying to import the stale ones.

	test('/_app/version.json is emitted by adapter-static', async ({ page }) => {
		// page.request bypasses page.route() so the mock fixture installed in
		// beforeEach doesn't interfere with this raw asset fetch.
		const response = await page.request.get('/_app/version.json');
		expect(response.status(), '/_app/version.json must be served').toBe(200);
		const body = await response.json();
		expect(body, 'version.json must include a `version` field').toHaveProperty('version');
		expect(typeof body.version, 'version must be a string').toBe('string');
	});

	test('layout wrapper wires data-sveltekit-reload to updated.current', async ({ page }) => {
		await page.goto('/');
		// updated.current starts false in a fresh tab, so the attribute renders
		// as "off" (keep SPA routing). After a deploy the poll flips it to
		// true and the attribute becomes "" (force full reload). Either value
		// proves the directive is wired up; absence would indicate the layout
		// regressed and the version-polling recovery path is dead.
		// Scoped at the outer wrapper (header + main both inside) so header
		// nav links also reload on stale-build, not just <main> children.
		const attr = await page
			.locator('[data-sveltekit-reload]')
			.first()
			.getAttribute('data-sveltekit-reload');
		expect(
			['off', ''],
			`data-sveltekit-reload wrapper must be "off" or "" (got ${attr})`
		).toContain(attr);
	});
});
