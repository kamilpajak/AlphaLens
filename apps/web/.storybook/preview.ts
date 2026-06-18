import type { Preview } from '@storybook/sveltekit';
// app.css = Tailwind utilities + @theme tokens + Temml-Local.css + the Latin
// Modern Math @font-face. Importing it pulls the whole terminal look into the
// preview iframe so stories render identically to the app. Story classes are
// scanned via app.css's own src-relative @source globs (the .stories.svelte
// files live under src/lib). If a Storybook-only decorator/chrome file under
// .storybook/ ever needs Tailwind utilities, add a Storybook-scoped
// `@source "../.storybook/**"` HERE (a preview-only stylesheet) rather than in
// app.css — keeping it out of app.css keeps the production CSS app-scoped.
import '../src/app.css';

const preview: Preview = {
	parameters: {
		backgrounds: {
			default: 'terminal',
			// matches the app --color-bg so tooltips sit on the real backdrop.
			values: [{ name: 'terminal', value: '#06070a' }]
		},
		controls: { matchers: { color: /(background|color)$/i, date: /Date$/i } }
	}
};

export default preview;
