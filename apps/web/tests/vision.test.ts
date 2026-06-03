/**
 * Playwright smoke for the /vision route.
 *
 * The route is static — no API calls, no auth, no live data. It fetches
 * the ideal-shape markdown from /docs/research/, parses with `marked`,
 * and renders TOC + body. Smoke covers: (a) the document loads + the
 * markdown is rendered as actual HTML headings, (b) the TOC sidebar is
 * populated with multiple entries, (c) clicking a TOC entry scrolls
 * the target heading into view.
 */

import { expect, test } from '@playwright/test';

test.describe('vision route', () => {
	test('GET /vision returns 200 and renders the document', async ({ page }) => {
		const response = await page.goto('/vision');
		expect(response?.status()).toBe(200);
		// `# AlphaLens — Ideal Shape` is the doc's h1; marked emits it as <h1>.
		await expect(page.getByRole('heading', { level: 1, name: /ideal shape/i }).first()).toBeVisible();
		// Several section headings should be present (the doc has ~10 §s).
		const h2s = await page.locator('article h2').count();
		expect(h2s).toBeGreaterThanOrEqual(5);
	});

	test('TOC sidebar lists all top-level sections', async ({ page }) => {
		await page.goto('/vision');
		// Wait for hydration: the TOC is computed in a $derived from the
		// markdown fetched by the load function; on first paint the
		// container is empty. Wait for the first entry before counting.
		await expect(
			page.locator('[data-testid="vision-toc-entry"]').first()
		).toBeVisible();
		const count = await page.locator('[data-testid="vision-toc-entry"]').count();
		// Doc has 10 h2 + several h3 → at least 10 entries in TOC.
		expect(count).toBeGreaterThanOrEqual(10);
	});

	test('clicking a TOC entry scrolls the matching section into view', async ({ page }) => {
		await page.goto('/vision');
		// Pick a known §: "9. Roadmap priorities" (h2). Click its TOC entry
		// and assert the corresponding heading is visible after the scroll.
		const entry = page
			.locator('[data-testid="vision-toc-entry"]')
			.filter({ hasText: /roadmap priorities/i })
			.first();
		await entry.click();
		await expect(
			page
				.locator('article h2')
				.filter({ hasText: /roadmap priorities/i })
				.first()
		).toBeInViewport();
	});

	test('header nav includes the /vision link', async ({ page }) => {
		await page.goto('/');
		await expect(page.locator('header nav a[href="/vision"]')).toBeVisible();
	});

	test('mermaid blocks render as SVG diagrams without syntax errors', async ({ page }) => {
		await page.goto('/vision');
		// The doc currently has 3 mermaid blocks (§3 big-picture flow +
		// §4 feedback loop + §5 wieczorny use-case timeline). Mermaid
		// replaces each <pre><code class="language-mermaid"> with a
		// <div class="mermaid"> that wraps an SVG once render completes.
		// Wait for all diagrams to render. Mermaid.run() iterates the
		// matched containers in sequence, so the second/third SVG may
		// still be in flight when the first becomes visible. `toHaveCount`
		// retries against the locator until the assertion passes or the
		// timeout expires — avoids a race where an instant count() reads
		// less than the target.
		//
		// If you add or remove diagrams from the doc, update this count
		// — a mismatch usually means a diagram failed to render.
		await expect(page.locator('article div.mermaid svg')).toHaveCount(3, { timeout: 10_000 });
		// Source pre/code blocks for mermaid should be gone (replaced).
		await expect(page.locator('article code.language-mermaid')).toHaveCount(0);
		// Crucially: NO syntax-error SVG. Mermaid renders the "bomb" error
		// picture as an SVG too, so the count-only assertion above would
		// pass for a broken diagram. The error SVG carries an "aria-roledescription"
		// of "error" — assert that's absent.
		await expect(
			page.locator('article div.mermaid svg[aria-roledescription="error"]')
		).toHaveCount(0);
		// HTML typography (<b>, <small>, <code>, <i>) inside mermaid node
		// labels renders through SVG <foreignObject> elements — a future
		// mermaid version that strips HTML by default would silently
		// downgrade typography without failing the count check above.
		// Assert at least one foreignObject is attached to guarantee the
		// HTML rendering path stays intact.
		await expect(
			page.locator('article div.mermaid svg foreignObject').first()
		).toBeAttached();
	});
});
