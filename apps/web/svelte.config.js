import adapter from '@sveltejs/adapter-static';

/** @type {import('@sveltejs/kit').Config} */
const config = {
	compilerOptions: {
		runes: ({ filename }) => (filename.split(/[/\\]/).includes('node_modules') ? undefined : true)
	},
	kit: {
		// Pure SPA — nginx serves index.html as fallback for every unknown
		// path. The app fetches /data/*.json at runtime so pipeline-rewritten
		// briefs surface without rebuilding the image.
		adapter: adapter({
			fallback: 'index.html',
			strict: false
		}),
		// Poll for a new build every 60s so open tabs detect a deploy before
		// the user tries to navigate and hits a stale chunk hash. SvelteKit
		// emits /_app/version.json at build time; the client compares its
		// bundled version against the poll result and flips `updated.current`
		// to true on a mismatch. Combined with `data-sveltekit-reload` on the
		// layout's <main> the next in-app navigation triggers a full reload
		// that fetches new HTML + new chunk URLs — the canonical fix for
		// the "Failed to load module script: Expected JavaScript-or-Wasm
		// module but got text/html" blank-screen bug after a Cloudflare
		// Pages deploy. Docs:
		//   https://svelte.dev/docs/kit/configuration#version
		//   https://github.com/sveltejs/kit/issues/9089
		version: {
			pollInterval: 60_000
		}
	}
};

export default config;
