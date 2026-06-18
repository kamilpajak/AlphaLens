import type { StorybookConfig } from '@storybook/sveltekit';

const config: StorybookConfig = {
	// Framework provides the SvelteKit `$lib` alias + `$app/*` auto-mocks, and —
	// crucially — does NOT add vite-plugin-svelte itself. Its viteFinal consumes
	// the app's auto-merged vite.config.ts: it strips the SSR-routing sub-plugins
	// (vite-plugin-sveltekit-compile / -guard) but KEEPS vite-plugin-svelte, so
	// Svelte 5 components (incl. Storybook's own internal .svelte) compile.
	//
	// We therefore rely on the default auto-merge of ../vite.config.ts (no
	// viteConfigPath override). That also pulls in @tailwindcss/vite and the
	// `virtual:formulas` MathML resolver for free, so stories render identically
	// to the app (styled + with typeset formula tooltips). The app's dev-proxy /
	// VITE_API_TARGET are inert under Storybook's own dev server.
	framework: '@storybook/sveltekit',
	stories: ['../src/**/*.stories.@(js|ts|svelte)', '../src/**/*.mdx'],
	// Storybook 10 has no addon-essentials package; docs / controls / actions /
	// backgrounds / viewport ship inside `storybook` core. Only the Svelte-CSF
	// addon (native `.stories.svelte` with snippet props) is external.
	addons: ['@storybook/addon-svelte-csf'],
	// SvelteKit static/ is NOT auto-served by Storybook. app.css @font-face uses
	// the absolute URL /fonts/latinmodernmath.woff2; map static/ to the iframe
	// root so it (and the SPA fonts) resolve — else the math typesets in the
	// fallback font instead of Latin Modern Math.
	staticDirs: ['../static']
};

export default config;
