import { sveltekit } from '@sveltejs/kit/vite';
import tailwindcss from '@tailwindcss/vite';
import { defineConfig } from 'vite';

// `pnpm dev` and `pnpm preview` run against a local api container — the
// operator usually has `docker compose up api` running on 127.0.0.1:8081.
// In production nginx handles this with `location /api/`; the proxy here
// mirrors that behaviour for Vite so dev and production fetch URLs stay
// identical (`/api/v1/*`). Playwright smoke tests intercept the same
// `/api/` paths via page.route() and don't need a live API.
//
// Reading process.env in vite.config requires Node ambient types we don't
// bundle; ignoring the TS check here is cheaper than a new dev dep.
// @ts-expect-error process is defined in Node where vite.config runs.
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
