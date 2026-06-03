import type { PageLoad } from './$types';
import { error } from '@sveltejs/kit';

// Source file is the LIVING DOCUMENT at
// `docs/research/alphalens_ideal_shape_2026_05_29.md`. The pre-build hook
// (`scripts/sync-research-docs.mjs`) copies it into `static/docs/research/`
// so the SPA can fetch the bytes at runtime — same pattern as the
// /experiments evidence drawer. Parsing happens client-side in
// +page.svelte via `marked`, so this loader stays a thin fetch + 404.
const SOURCE_PATH = '/docs/research/alphalens_ideal_shape_2026_05_29.md';

export const load: PageLoad = async ({ fetch }) => {
	const res = await fetch(SOURCE_PATH);
	if (!res.ok) {
		error(res.status, 'Could not load vision document.');
	}
	const markdown = await res.text();
	return { markdown };
};
