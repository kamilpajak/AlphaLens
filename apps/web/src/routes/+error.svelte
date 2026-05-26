<script lang="ts">
	import { page } from '$app/state';
	import { ArrowUpRight } from 'lucide-svelte';

	const status = $derived(page.status);
	// 401 is the auth-failure signal: apiFetch normalises an expired
	// Cloudflare Access session (302→login redirect, or a login-HTML body)
	// to a 401 so it lands here as "session expired" rather than a bare 500.
	const isAuth = $derived(status === 401);

	// Cross-origin deploys (CF Pages SPA + Tunnel API) expose the API on a
	// separate origin. Re-authenticating means hitting that origin so CF
	// Access runs the SSO flow and refreshes the CF_Authorization cookie.
	// Same-origin deploys (local Docker) leave this empty → no re-auth link.
	const apiBase = (import.meta.env.VITE_API_BASE ?? '').trim().replace(/\/+$/, '');

	function retry() {
		location.reload();
	}
</script>

<div class="max-w-[800px] mx-auto px-4 py-16">
	<div class="border border-grid bg-bg-1 corners relative p-6 sm:p-10 fade-up">
		<div class="text-[10px] uppercase tracking-[0.3em] text-fg-muted mb-3">// error</div>
		{#if isAuth}
			<h1 class="font-display font-bold text-4xl sm:text-5xl text-amber tracking-tight">
				session expired
			</h1>
			<p class="mt-4 max-w-xl text-fg-dim text-sm leading-relaxed">
				Your Cloudflare Access session has expired, so the API rejected the request.
				Re-authenticate, then retry — your briefs are intact.
			</p>
			<div class="flex flex-wrap gap-3 mt-6">
				{#if apiBase}
					<a
						href={apiBase}
						target="_blank"
						rel="noreferrer"
						class="inline-flex items-center gap-2 px-4 py-2 bg-amber text-bg font-semibold text-xs uppercase tracking-widest hover:bg-amber-dim transition-colors"
					>
						re-authenticate
						<ArrowUpRight class="size-3" />
					</a>
				{/if}
				<button
					onclick={retry}
					class="inline-flex items-center gap-2 px-4 py-2 border border-grid-strong text-fg font-semibold text-xs uppercase tracking-widest hover:border-amber hover:text-amber transition-colors"
				>
					retry
				</button>
			</div>
		{:else}
			<h1 class="font-display font-bold text-5xl sm:text-6xl text-amber tracking-tight">
				{status}
			</h1>
			<p class="mt-4 text-fg-dim text-sm leading-relaxed">
				{page.error?.message ?? 'Something went wrong.'}
			</p>
			<a
				href="/"
				class="inline-flex items-center gap-2 px-4 py-2 mt-6 border border-grid-strong text-fg font-semibold text-xs uppercase tracking-widest hover:border-amber hover:text-amber transition-colors"
			>
				← dashboard
			</a>
		{/if}
	</div>
</div>
