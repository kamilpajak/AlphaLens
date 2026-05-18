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
		})
	}
};

export default config;
