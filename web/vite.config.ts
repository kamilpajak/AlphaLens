import { sveltekit } from '@sveltejs/kit/vite';
import tailwindcss from '@tailwindcss/vite';
import { defineConfig } from 'vite';

// `pnpm dev` and `pnpm preview` run against a local api container — the
// operator usually has `docker compose up api` running on 127.0.0.1:8081.
// In production nginx handles this with `location /api/`; the proxy here
// mirrors that behaviour for Vite so dev and production fetch URLs stay
// identical (`/api/v1/*`). Playwright smoke tests intercept the same
// `/api/` paths via page.route() and don't need a live API.

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
	plugins: [tailwindcss(), sveltekit()],
	server: { proxy: apiProxy },
	preview: { proxy: apiProxy }
});
