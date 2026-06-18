import { sveltekit } from '@sveltejs/kit/vite';
import tailwindcss from '@tailwindcss/vite';
import { defineConfig } from 'vite';
import { virtualFormulas } from './src/lib/vite/virtualFormulas';

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
