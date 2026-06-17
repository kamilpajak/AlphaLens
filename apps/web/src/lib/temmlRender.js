// LaTeX → MathML, run at BUILD time only (Node), never in the browser.
//
// Temml's lexer regex is corrupted by esbuild — both Vite's dev dep
// pre-bundle AND the production esbuild minifier truncate every LaTeX control
// word (`\alpha` → an error node `\a` + literal "lpha"), while the identical
// temml.mjs is correct under raw Node. Rather than fight the bundler, the
// `virtual:formulas` Vite plugin calls this helper at build time and ships the
// resulting MathML strings; temml is never bundled for the browser. That also
// means zero client JS for math (lighter) — only the small MathML + the Temml
// stylesheet ride along.
//
// This module is imported by the Vite plugin (vite.config.ts) and the unit
// test ONLY. App/runtime code consumes the pre-rendered strings from the
// virtual module, so importing this file does NOT pull temml into the client
// bundle. Plain JS (not TS) so vite.config can import it directly in Node.
//
// SECURITY: `tex` is always a build-time literal from src/lib/formulas.json,
// authored in this repo — never user/API input. `trust: false` disables the
// markup-emitting macros (\href, \includegraphics, …); `throwOnError: false`
// renders a visible error node instead of throwing on a malformed formula.

import temml from 'temml';

/**
 * Render a LaTeX expression to a MathML string.
 * @param {string} tex Trusted, build-time-literal LaTeX source.
 * @param {boolean} [display=false] Block (display-style) vs inline math.
 * @returns {string}
 */
export function renderTex(tex, display = false) {
	return temml.renderToString(tex, {
		displayMode: display,
		throwOnError: false,
		trust: false
	});
}
