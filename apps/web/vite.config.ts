import { sveltekit } from '@sveltejs/kit/vite';
import tailwindcss from '@tailwindcss/vite';
import { defineConfig, type Plugin } from 'vite';
import { readFile } from 'node:fs/promises';
import { renderTex } from './src/lib/temmlRender.js';

// Pre-render every tooltip formula (src/lib/formulas.json) to a MathML string
// at BUILD time and expose them as `virtual:formulas`. Temml runs only here in
// Node — where its lexer works — so no temml JS is shipped to the browser
// (esbuild's dep pre-bundle + minifier corrupt temml's lexer regex; see
// src/lib/temmlRender.js). Components import the deterministic strings instead
// of rendering LaTeX at runtime.
function virtualFormulas(): Plugin {
	const VIRTUAL_ID = 'virtual:formulas';
	const RESOLVED_ID = '\0' + VIRTUAL_ID;
	return {
		name: 'alphalens-virtual-formulas',
		resolveId(id) {
			if (id === VIRTUAL_ID) return RESOLVED_ID;
		},
		async load(id) {
			if (id !== RESOLVED_ID) return;
			const raw = await readFile(new URL('./src/lib/formulas.json', import.meta.url), 'utf-8');
			const registry: Record<string, string> = JSON.parse(raw);
			const rendered: Record<string, string> = {};
			for (const [key, tex] of Object.entries(registry)) {
				rendered[key] = renderTex(tex);
			}
			return `export default ${JSON.stringify(rendered)};`;
		}
	};
}

// `pnpm dev` and `pnpm preview` run against a local API. Pick the target
// with `VITE_API_TARGET`:
//   * legacy FastAPI:  http://127.0.0.1:8081  (default during migration)
//   * new Django app:  http://127.0.0.1:8000
// In production nginx handles `location /api/`; the proxy here mirrors
// that behaviour for Vite so dev and production fetch URLs stay identical
// (`/api/v1/*`). Playwright smoke tests intercept the same `/api/` paths
// via page.route() and don't need a live API.
//
// `VITE_API_BASE` (read at runtime by `$lib/api`) overrides the proxy
// entirely for cross-origin deploys — leave unset for same-origin nginx.

// Local ambient declaration so the config compiles without @types/node.
// Replace with a real Node typedef dep if any other Node global lands here.
declare const process: { env: Record<string, string | undefined> };

const apiTarget: string = process.env.VITE_API_TARGET ?? 'http://127.0.0.1:8081';

const apiProxy = {
	'/api': {
		target: apiTarget,
		changeOrigin: true,
		rewrite: (path: string) => path.replace(/^\/api/, '')
	}
};

export default defineConfig({
	plugins: [tailwindcss(), sveltekit(), virtualFormulas()],
	server: { proxy: apiProxy },
	preview: { proxy: apiProxy }
});
