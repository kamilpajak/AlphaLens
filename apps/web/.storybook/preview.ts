import type { Preview } from '@storybook/sveltekit';
// app.css = Tailwind utilities + @theme tokens + Temml-Local.css + the Latin
// Modern Math @font-face. Importing it pulls the whole terminal look into the
// preview iframe so stories render identically to the app.
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
