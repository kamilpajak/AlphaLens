import { describe, expect, it } from 'vitest';
import { renderJson, renderMarkdown } from '../../src/lib/markdown';

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

// `renderJson` fences the input as a ```json block and routes it through
// `renderMarkdown`, so it inherits the same sanitization. This pins the
// end-to-end path so a future refactor that bypasses the render chain
// (e.g. building the HTML directly) fails loudly.
describe('renderJson', () => {
	it('escapes active content from a JSON string rendered as a code block', async () => {
		const html = await renderJson('{"x": "<script>alert(1)</script>"}');
		// No live <script> element — the payload is HTML-escaped inside the
		// code block (inert text), proven by the escaped form being present.
		expect(html).not.toContain('<script>');
		expect(html).toContain('&lt;script&gt;');
		// The legitimate key survives inside the rendered code block.
		expect(html).toContain('"x"');
	});

	it('falls back to a plain fenced block for non-JSON text', async () => {
		const html = await renderJson('not <b>json</b> at all');
		expect(html).toContain('<pre');
		expect(html).toContain('not');
	});
});
