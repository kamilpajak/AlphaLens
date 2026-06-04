import { describe, expect, it } from 'vitest';
import { renderMarkdown } from '../../src/lib/markdown';

// The evidence drawer renders research markdown through `{@html}`. Without
// sanitization a single careless edit to a synced research doc becomes stored
// XSS. These tests pin that the markdown pipeline neutralizes active content
// while preserving legitimate formatting.
describe('renderMarkdown', () => {
	it('strips a <script> tag from raw HTML embedded in markdown', async () => {
		const html = await renderMarkdown('# Title\n\n<script>window.__pwned = 1;</script>\n\nbody');
		expect(html).not.toContain('<script');
		expect(html).not.toContain('__pwned');
		// Legitimate markdown still renders.
		expect(html).toContain('Title');
		expect(html).toContain('body');
	});

	it('removes inline event-handler attributes (onerror) from img tags', async () => {
		const html = await renderMarkdown('![x](https://example.com/a.png "t")\n\n<img src=x onerror="alert(1)">');
		expect(html).not.toContain('onerror');
		expect(html).not.toContain('alert(1)');
	});

	it('neutralizes a javascript: URL on a markdown link', async () => {
		const html = await renderMarkdown('[click me](javascript:alert(1))');
		expect(html).not.toContain('javascript:alert(1)');
		// The link text survives even though the dangerous href is dropped.
		expect(html).toContain('click me');
	});

	it('preserves benign markdown formatting (links, emphasis, code)', async () => {
		const html = await renderMarkdown(
			'**bold** _em_ `code` and a [link](https://example.com/page)'
		);
		expect(html).toContain('<strong>bold</strong>');
		expect(html).toContain('<code>code</code>');
		expect(html).toContain('href="https://example.com/page"');
	});
});
