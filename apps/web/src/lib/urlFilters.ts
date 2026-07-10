// Serializing facet selections to/from URL query params + computing the minimal
// `replaceState` target. Pure — the SvelteKit runes glue lives in
// `urlFilterSync.svelte.ts`. Shared by /edge, /experiments and /brief so the
// three deep-linkable filters serialize the same way.

/** A Set → sorted, comma-joined param value (stable regardless of click order). */
export function setToParam(set: Set<string>): string {
	return [...set].sort((a, b) => a.localeCompare(b, 'en')).join(',');
}

/** A param value → Set (empty for null/blank; blank segments dropped). */
export function paramToSet(value: string | null): Set<string> {
	if (!value) return new Set();
	return new Set(value.split(',').filter(Boolean));
}

/** The URL to `replaceState` to so the query reflects `nextParams`, or `null` if
 *  the query already matches (skip the write — no history churn). The hash is
 *  preserved; a cleared query drops the `?` entirely (falls back to `pathname`).
 *  `currentSearch` is `location.search` (may include the leading `?`); `hash`
 *  is `location.hash` (may include the leading `#`, or be empty). */
export function nextUrlTarget(
	currentSearch: string,
	nextParams: string,
	pathname: string,
	hash: string
): string | null {
	const current = currentSearch.replace(/^\?/, '');
	if (nextParams === current) return null;
	return (nextParams ? `?${nextParams}` : pathname) + hash;
}
