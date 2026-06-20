<script lang="ts">
	import { page } from '$app/state';
	import { clearSessionExpired } from '$lib/session.svelte';

	// Shared "session expired" re-auth card. Rendered in two places:
	//   * `+error.svelte` — full-page, when a loader escalated a 401 to the
	//     SvelteKit error boundary (status === 401).
	//   * `+layout.svelte` — inside a global overlay modal driven by the
	//     `sessionExpired()` store, so the prompt is consistent on EVERY route
	//     and for client-side fetches that never hit a loader.
	// The markup (heading + copy + re-auth link + retry button) and the
	// apiBase/reauthHref logic live here once so both call sites stay in sync.

	// Cross-origin deploys (CF Pages SPA + Tunnel API) expose the API on a
	// separate origin. Re-authenticating means hitting that origin so CF
	// Access runs the SSO flow and refreshes the CF_Authorization cookie.
	// Same-origin deploys (local Docker) leave this empty → no re-auth link.
	const apiBase = (import.meta.env.VITE_API_BASE ?? '').trim().replace(/\/+$/, '');

	// The trampoline endpoint on the API origin. Browser must actually visit
	// `api.*` for the CF_Authorization cookie to land — a direct hop from the
	// SSO endpoint back to `app.*` leaves the cookie unset (HTTP cookies are
	// scoped to the response origin). The Django `/auth/start` view validates
	// `return_to` against CORS_ALLOWED_ORIGINS and 302s the browser back to
	// the URL the user was on, with a freshly-minted CF_Authorization cookie.
	// See docs/research/cf_access_reauth_trampoline_design.md.
	const reauthHref = $derived(
		apiBase ? `${apiBase}/auth/start?return_to=${encodeURIComponent(page.url.href)}` : ''
	);

	function retry() {
		// Clear the flag first so a same-origin reload doesn't flash the
		// overlay again before the fresh page state settles.
		clearSessionExpired();
		location.reload();
	}
</script>

<div class="text-[10px] uppercase tracking-[0.3em] text-fg-muted mb-3">// session</div>
<h1 class="font-display font-bold text-4xl sm:text-5xl text-amber tracking-tight">session expired</h1>
<p class="mt-4 max-w-xl text-fg-dim text-sm leading-relaxed">
	Your Cloudflare Access session has expired, so the API rejected the request. Re-authenticate, then
	retry — your briefs are intact.
</p>
<div class="flex flex-wrap gap-3 mt-6">
	{#if reauthHref}
		<a
			href={reauthHref}
			aria-label="re-authenticate"
			class="inline-flex items-center gap-2 px-4 py-2 bg-amber text-bg font-semibold text-xs uppercase tracking-widest hover:bg-amber-dim transition-colors"
		>
			re-authenticate
		</a>
	{/if}
	<button
		onclick={retry}
		class="inline-flex items-center gap-2 px-4 py-2 border border-grid-strong text-fg font-semibold text-xs uppercase tracking-widest hover:border-amber hover:text-amber transition-colors"
	>
		retry
	</button>
</div>
