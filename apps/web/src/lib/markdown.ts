// Markdown → sanitized HTML pipeline for `{@html}` sinks.
//
// The evidence drawer on /experiments renders synced research documents
// (markdown + the occasional embedded HTML block) through Svelte's `{@html}`,
// which performs NO escaping. A research doc is hand-edited content synced at
// build time, so a single careless edit (or a pasted snippet carrying an
// `onerror=` / inline `<script>`) would become stored XSS the moment a reader
// opens the drawer.
//
// `marked` deliberately dropped its built-in `sanitize` option years ago and
// now recommends sanitizing the OUTPUT with a dedicated sanitizer. We run the
// rendered HTML through DOMPurify (isomorphic build = works under SSR/Node and
// in the browser) before it reaches the `{@html}` sink. Every caller that
// feeds untrusted markdown into `{@html}` MUST go through this helper.

import DOMPurify from 'isomorphic-dompurify';
import { marked } from 'marked';

/**
 * Render a markdown string to sanitized HTML safe for `{@html}`.
 *
 * `marked.parse` may return a Promise (async extensions); await it so callers
 * get a plain string. The DOMPurify pass strips `<script>`, inline event
 * handlers (`onerror`, `onclick`, …) and `javascript:` URLs while preserving
 * the standard formatting markdown produces.
 */
export async function renderMarkdown(markdown: string): Promise<string> {
	const rendered = await marked.parse(markdown);
	return DOMPurify.sanitize(rendered);
}

/**
 * Render a JSON evidence file: pretty-print then fence as a ```json block so
 * the drawer shows syntax-highlightable, sanitized output. Falls back to a
 * plain fenced block when the text is not valid JSON.
 */
export async function renderJson(text: string): Promise<string> {
	try {
		const pretty = JSON.stringify(JSON.parse(text), null, 2);
		return renderMarkdown('```json\n' + pretty + '\n```');
	} catch {
		return renderMarkdown('```\n' + text + '\n```');
	}
}
