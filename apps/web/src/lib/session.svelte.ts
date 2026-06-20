/**
 * Global "CF Access session expired" flag — the single source of truth for
 * the re-auth overlay rendered in ``+layout.svelte``.
 *
 * ``apiFetch`` ($lib/api) normalises an expired Cloudflare Access session to a
 * synthetic 401 in two places (a cross-origin redirect that makes ``fetch``
 * throw, and a 200 + login-HTML body). Today only the dashboard + brief page
 * LOADERS surface that 401 (via ``error(401)`` → the full-page +error.svelte);
 * every other route + every client-side fetch (EvidenceDrawer, edge chart,
 * market chip) swallow it silently, so the user gets no re-auth prompt.
 *
 * This tiny rune-backed store decouples "session expired" from the page
 * loader: ``apiFetch`` flips it true on the two auth-expiry returns, and the
 * layout renders one consistent overlay modal on EVERY route. Mirrors the
 * ``marketStatus.svelte.ts`` style (a ``.svelte.ts`` module exporting a
 * ``$state`` rune). Framework-agnostic — no DOM, no fetch.
 */

// Module-level rune. Components read it through the ``sessionExpired()``
// accessor (kept reactive because the read happens inside the rune-aware
// module), tests read it directly after marking.
let _expired = $state(false);

/** Flip the global "session expired" flag on. Idempotent. */
export function markSessionExpired(): void {
	_expired = true;
}

/** Reset the flag — used after a successful re-auth / retry and by tests. */
export function clearSessionExpired(): void {
	_expired = false;
}

/** Reactive accessor: true while a CF Access re-auth is pending. */
export function sessionExpired(): boolean {
	return _expired;
}
