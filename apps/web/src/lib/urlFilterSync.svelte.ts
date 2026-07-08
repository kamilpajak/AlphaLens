import { replaceState } from '$app/navigation';
import { page } from '$app/state';
import { nextUrlTarget } from './urlFilters';

/**
 * Mirror filter state into the URL query without re-running `load`. Call ONCE in
 * a component's `<script>`; `buildParams` reads the reactive filter state and
 * returns the desired `URLSearchParams` (merge into
 * `new URLSearchParams(location.search)` to preserve unrelated params).
 *
 * The diff baseline is the LIVE `location.search`, NOT `$app/state`'s `page.url`
 * — after `replaceState`, `page.url` can lag, which would make a later "clear"
 * compare against a stale baseline and skip removing the param. Reading only the
 * filter state (via `buildParams`) keeps the effect free of a `page.url`
 * dependency, so there is no replace -> re-run loop. Client-only: `$effect`
 * never runs during SSR. The hash is preserved.
 */
export function syncParamsToUrl(buildParams: () => URLSearchParams): void {
	$effect(() => {
		const target = nextUrlTarget(
			window.location.search,
			buildParams().toString(),
			window.location.pathname,
			window.location.hash
		);
		if (target !== null) replaceState(target, page.state);
	});
}
