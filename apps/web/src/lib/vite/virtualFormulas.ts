import type { Plugin } from 'vite';
import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { renderTex } from '../temmlRender.js';

// Pre-render every tooltip formula (src/lib/formulas.json) to a MathML string
// at BUILD time and expose them as `virtual:formulas`. Temml runs only here in
// Node — where its lexer works — so no temml JS is shipped to the browser
// (esbuild's dep pre-bundle + minifier corrupt temml's lexer regex; see
// src/lib/temmlRender.js). Components import the deterministic strings instead
// of rendering LaTeX at runtime.
//
// Extracted from vite.config.ts so BOTH the app config and the thin
// Storybook-only Vite config (.storybook/vite.config.ts) can register the same
// plugin — Storybook's @storybook/sveltekit framework owns the SvelteKit
// compile but does not re-supply this resolver, so stories that render a
// `Formula` would otherwise fail with "failed to resolve import virtual:formulas".
export function virtualFormulas(): Plugin {
	const VIRTUAL_ID = 'virtual:formulas';
	const RESOLVED_ID = '\0' + VIRTUAL_ID;
	return {
		name: 'alphalens-virtual-formulas',
		resolveId(id) {
			if (id === VIRTUAL_ID) return RESOLVED_ID;
		},
		async load(id) {
			if (id !== RESOLVED_ID) return;
			const formulasPath = fileURLToPath(new URL('../formulas.json', import.meta.url));
			// Re-run this loader when formulas.json changes so `vite dev` HMR picks
			// up edits without a restart (the build path is unaffected either way).
			this.addWatchFile(formulasPath);
			const raw = await readFile(formulasPath, 'utf-8');
			const registry: Record<string, string> = JSON.parse(raw);
			const rendered: Record<string, string> = {};
			for (const [key, tex] of Object.entries(registry)) {
				rendered[key] = renderTex(tex);
			}
			return `export default ${JSON.stringify(rendered)};`;
		}
	};
}
