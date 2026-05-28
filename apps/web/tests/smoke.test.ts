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

function installApiMock(page: Page) {
	return page.route('**/api/v1/**', (route) => {
		const url = new URL(route.request().url());
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

test.describe('brief detail — graceful session-expiry handling', () => {
	// Regression: an expired Cloudflare Access session makes the cross-origin
	// API XHR fail — CF answers the unauthenticated XHR with a 302 to its login
	// origin, the browser blocks following that cross-origin redirect, and the
	// fetch throws. The brief-detail load used to let that throw bubble into a
	// bare "500 Internal Error". It must now surface as a graceful "session
	// expired" page so the operator knows to re-authenticate.
	const DATE = latestDay.date;

	test('aborted API fetch (CF redirect) shows session-expired, not a 500', async ({ page }) => {
		await page.route(`**/api/v1/days/${DATE}`, (route) => route.abort());
		await page.goto(`/brief/${DATE}`);
		await expect(page.getByText('session expired', { exact: false })).toBeVisible();
		await expect(page.locator('main')).not.toContainText('Internal Error');
	});

	test('API 401 shows the session-expired re-auth page', async ({ page }) => {
		await page.route(`**/api/v1/days/${DATE}`, (route) =>
			route.fulfill({
				status: 401,
				contentType: 'application/json',
				body: JSON.stringify({ detail: 'unauthorized' })
			})
		);
		await page.goto(`/brief/${DATE}`);
		await expect(page.getByText('session expired', { exact: false })).toBeVisible();
	});

	test('dashboard surfaces session-expired on 401, not a misleading empty state', async ({
		page
	}) => {
		// The index endpoint (pathname /api/v1/days, any query) → 401. The
		// dashboard load must raise the session-expired error page instead of
		// degrading to the "no briefs yet" empty state (which reads as data loss).
		await page.route(
			(url) => url.pathname === '/api/v1/days',
			(route) =>
				route.fulfill({
					status: 401,
					contentType: 'application/json',
					body: JSON.stringify({ detail: 'unauthorized' })
				})
		);
		await page.goto('/');
		await expect(page.getByText('session expired', { exact: false })).toBeVisible();
		await expect(page.locator('main')).not.toContainText('no captured sessions');
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
	// about page now uses brand-style names ("Gemini 3 Flash", "Gemini 3
	// Pro") so it survives a model bump without re-staling, and the test
	// hard-fails if a retired exact ID ever reappears.
	const RETIRED_MODEL_IDS = [
		'gemini-2.5-flash',
		'gemini-3-pro-preview' // dropped the "-3-" preview line; current is gemini-3.1-pro-preview
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
		await expect(layers).toContainText(/Gemini 3 Flash/);
		await expect(layers).toContainText(/Gemini 3 Pro/);

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

	test('header metric strip renders candidate / verified / theme counts (layout regression guard)', async ({ page }) => {
		const brief = JSON.parse(DAY_BODIES[latestDay.date]);
		const verifiedCount = brief.candidates.filter((c: { verified: boolean }) => c.verified).length;

		await page.goto(`/brief/${latestDay.date}`);

		const strip = page.getByTestId('brief-header-stats');
		await expect(strip).toBeVisible();
		// Each value carries a stable data-testid so the assertion survives
		// Tailwind class / layout refactors of the metric strip.
		await expect(strip.getByTestId('stat-candidates')).toHaveText(String(brief.n_candidates));
		await expect(strip.getByTestId('stat-verified')).toHaveText(String(verifiedCount));
		await expect(strip.getByTestId('stat-themes')).toHaveText(String(brief.n_themes));
		await expect(strip.getByTestId('stat-top-theme')).toContainText(brief.top_theme);
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

	test('signal-bar tooltip renders on hover (CSS regression guard)', async ({ page }) => {
		await page.goto(`/brief/${latestDay.date}`);
		// data-testid is stable across Tailwind class refactors.
		const firstBar = page.locator('article[id] [data-testid="signal-bar"]').first();
		await firstBar.hover();
		const tooltip = page.locator('article[id] [role="tooltip"]').first();
		await expect(tooltip).toBeVisible();
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
		// Core structured sections render: fundamentals table + the trade-execution
		// setup panel (re-added 2026-05-27 in the two-column layout — brief_trade_setup
		// data was always generated server-side, now rendered again).
		// The fundamentals section header now carries a leading icon, so the
		// label div's text content is " fundamentals" — tolerate surrounding
		// whitespace rather than anchoring on the bare word.
		await expect(
			page.locator('article[id] div').filter({ hasText: /^\s*fundamentals\s*$/i }).first()
		).toBeVisible();
		await expect(page.locator('article[id] [data-testid="trade-setup"]').first()).toBeVisible();
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

test.describe('experiments — hybrid tooltip policy', () => {
	// Policy: short acronyms (αt, OOS, IS, FL, CAR, BAB, Q5) get a JargonTip
	// at EVERY occurrence in data text. Multi-word / longer terms (Carhart 4F,
	// Bonferroni, phase-aliasing, multi-phase audit, Phase A-E, R², R2000-PIT,
	// FCFF, PEAD, NW HAC, Form-4, ADR) get a tooltip on FIRST occurrence per
	// section. Each paradigm <article> and each pattern <li> is a section.
	// These assertions guard the policy from silent drift via text edits.

	test('αt always-tooltip — appears as JargonTip across the page', async ({ page }) => {
		await page.goto('/experiments');
		const count = await page.locator('[data-testid="jargon-tip"]:has-text("αt")').count();
		expect(count, 'αt JargonTip count across page').toBeGreaterThanOrEqual(20);
	});

	test('OOS — appears as JargonTip in patterns + each paradigm row mini-bar', async ({ page }) => {
		await page.goto('/experiments');
		const count = await page.locator('[data-testid="jargon-tip"]:has-text("OOS")').count();
		expect(count, 'OOS JargonTip count').toBeGreaterThanOrEqual(20);
	});

	test('IS — appears as JargonTip in patterns + each paradigm row mini-bar', async ({ page }) => {
		await page.goto('/experiments');
		const count = await page.locator('[data-testid="jargon-tip"]:has-text("IS")').count();
		expect(count, 'IS JargonTip count').toBeGreaterThanOrEqual(20);
	});

	test('FL — JargonTip exists at least in how.to.read + paradigm metrics', async ({ page }) => {
		await page.goto('/experiments');
		const count = await page.locator('[data-testid="jargon-tip"]:has-text("FL")').count();
		expect(count, 'FL JargonTip count').toBeGreaterThanOrEqual(2);
	});

	test('CAR — wrapped on every occurrence in P04 row', async ({ page }) => {
		await page.goto('/experiments');
		const count = await page.locator('article#P04 [data-testid="jargon-tip"]:has-text("CAR")').count();
		expect(count, 'CAR JargonTip count in P04').toBe(4);
	});

	test('BAB — exists in P15 row', async ({ page }) => {
		await page.goto('/experiments');
		const count = await page.locator('article#P15 [data-testid="jargon-tip"]:has-text("BAB")').count();
		expect(count, 'BAB JargonTip count in P15').toBeGreaterThanOrEqual(1);
	});

	test('Q5 — exists in S01 row', async ({ page }) => {
		await page.goto('/experiments');
		const count = await page.locator('article#S01 [data-testid="jargon-tip"]:has-text("Q5")').count();
		expect(count, 'Q5 JargonTip count in S01').toBeGreaterThanOrEqual(1);
	});

	test('Bonferroni — first-per-section across patterns + paradigm rows', async ({ page }) => {
		await page.goto('/experiments');
		const count = await page.locator('[data-testid="jargon-tip"]:has-text("Bonferroni")').count();
		expect(count, 'Bonferroni JargonTip count').toBeGreaterThanOrEqual(2);
	});

	test('Carhart 4F — first-per-section in how.to.read + patterns', async ({ page }) => {
		await page.goto('/experiments');
		const count = await page.locator('[data-testid="jargon-tip"]:has-text("Carhart")').count();
		expect(count, 'Carhart 4F JargonTip count').toBeGreaterThanOrEqual(2);
	});

	test('PEAD — exists in P14 row', async ({ page }) => {
		await page.goto('/experiments');
		const count = await page.locator('article#P14 [data-testid="jargon-tip"]:has-text("PEAD")').count();
		expect(count, 'PEAD JargonTip count in P14').toBeGreaterThanOrEqual(1);
	});

	test('NW HAC — exists in P14 row', async ({ page }) => {
		await page.goto('/experiments');
		const count = await page.locator('article#P14 [data-testid="jargon-tip"]:has-text("NW HAC")').count();
		expect(count, 'NW HAC JargonTip count in P14').toBeGreaterThanOrEqual(1);
	});

	test('Form-4 — exists in S01 row (first-per-section)', async ({ page }) => {
		await page.goto('/experiments');
		const count = await page.locator('article#S01 [data-testid="jargon-tip"]:has-text("Form-4")').count();
		expect(count, 'Form-4 JargonTip count in S01').toBeGreaterThanOrEqual(1);
	});

	test('ADR — exists in patterns section', async ({ page }) => {
		await page.goto('/experiments');
		const count = await page.locator('[data-testid="jargon-tip"]:has-text("ADR")').count();
		expect(count, 'ADR JargonTip count').toBeGreaterThanOrEqual(1);
	});

	test('glossary section has NO JargonTips (defines them; tooltips would be self-referential)', async ({ page }) => {
		await page.goto('/experiments');
		// The glossary section comes last on the page; scope by section title text.
		// Each glossary entry is a <dt>/<dd> pair inside the section. JargonTip
		// uses <span data-testid="jargon-tip">, none should appear under the
		// glossary section header.
		const glossarySection = page.locator('section').filter({ hasText: 'glossary.terms' });
		const count = await glossarySection.locator('[data-testid="jargon-tip"]').count();
		expect(count, 'glossary section JargonTip count must be 0').toBe(0);
	});

	test('paradigm detail fields collapsed by default (P0.1)', async ({ page }) => {
		await page.goto('/experiments');
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

		await page.goto('/experiments');
		await expect(page.locator('footer span.text-amber').first()).toBeVisible();
		const expChips = (await page.locator('footer span.text-amber').allTextContents()).join(' ');
		expect(expChips, '/experiments footer shows research vocab').toContain('DOCTRINE');
		expect(expChips, '/experiments footer does not show thematic vocab').not.toContain('PRESS-GATE');
	});

	test('sticky TOC renders on xl viewport with 7 section anchors (P3.1)', async ({ page }) => {
		await page.setViewportSize({ width: 1440, height: 900 });
		await page.goto('/experiments');
		// Aside rail is `hidden xl:block` — invisible below 1280px.
		const tocLinks = await page.locator('nav[aria-label="Section table of contents"] a').count();
		expect(tocLinks, '7 section anchors in TOC').toBe(7);
		// Section ids the TOC points to must exist on the page.
		for (const id of ['status', 'how-to-read', 'paradigms', 'patterns', 'infra', 'methodology', 'glossary']) {
			const ok = await page.locator(`section#${id}`).count();
			expect(ok, `section#${id} exists`).toBe(1);
		}
	});

	test('--color-fg-muted contrast against --color-bg meets WCAG AA (≥4.5:1) (P1.1)', async ({ page }) => {
		await page.goto('/experiments');
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

	test('heading semantics: ≥7 h2 (one per section), ≥31 h3 (paradigms+patterns) (P0.3)', async ({ page }) => {
		await page.goto('/experiments');
		const h2 = await page.locator('h2').count();
		const h3 = await page.locator('h3').count();
		expect(h2, '≥7 h2 (status.legend, how.to.read, paradigms.ledger, failure.patterns, infrastructure.live, methodology.artifacts, glossary.terms)').toBeGreaterThanOrEqual(7);
		expect(h3, '≥31 h3 (18 paradigm names + 13 pattern names)').toBeGreaterThanOrEqual(31);
	});

	test('hash auto-expand opens target paradigm row (P0.2)', async ({ page }) => {
		await page.goto('/experiments#P14');
		// $effect runs after hydration; wait briefly for the details to flip.
		await page.locator('article#P14 details[open]').waitFor({ timeout: 2000 });
		const opened = await page.locator('article#P14 details[open]').count();
		expect(opened, 'P14 details must be open after hash deep-link').toBe(1);
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

test.describe('smoke — Cloudflare Pages _redirects rule order', () => {
	// Regression guard: the `/_app/* /_app/:splat 404` rule MUST appear before
	// the SPA-fallback `/* /index.html 200` rule, otherwise stale chunk URLs
	// fall through to the SPA fallback and CF Pages caches the wrong HTML
	// under the chunk URL with the immutable-asset 4h TTL, producing the
	// "MIME type text/html" blank-screen failure the file exists to prevent.
	test('_redirects places /_app/* 404 rule before SPA fallback', () => {
		const redirects = readFileSync(resolve(__dirname, '../static/_redirects'), 'utf-8');
		const rules = redirects
			.split('\n')
			.map((l) => l.trim())
			.filter((l) => l && !l.startsWith('#'));
		const appRuleIdx = rules.findIndex((l) => l.startsWith('/_app/'));
		const spaRuleIdx = rules.findIndex((l) => l.startsWith('/*'));
		expect(appRuleIdx, '/_app/* rule must be present in _redirects').toBeGreaterThanOrEqual(0);
		expect(spaRuleIdx, '/* SPA fallback rule must be present in _redirects').toBeGreaterThanOrEqual(0);
		expect(
			appRuleIdx,
			'/_app/* rule must precede /* SPA fallback (CF Pages applies rules top-down)'
		).toBeLessThan(spaRuleIdx);
		// The rule must terminate with status 404, not 200 — a 200 would defeat
		// the entire purpose by serving real content under a missing-asset URL.
		expect(rules[appRuleIdx], '/_app/* rule must return 404').toMatch(/\s404\s*$/);
	});
});
