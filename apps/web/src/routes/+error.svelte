<script lang="ts">
	import { page } from '$app/state';
	import SessionExpiredCard from '$lib/components/SessionExpiredCard.svelte';

	const status = $derived(page.status);
	// 401 is the auth-failure signal: apiFetch normalises an expired
	// Cloudflare Access session (302→login redirect, or a login-HTML body)
	// to a 401 so it lands here as "session expired" rather than a bare 500.
	// This full-page branch is now a harmless fallback — the global overlay in
	// +layout.svelte (driven by the session store) is the primary surface — but
	// it still covers a loader that throws error(401) into the error boundary.
	const isAuth = $derived(status === 401);
</script>

<div class="max-w-[800px] mx-auto px-4 py-16">
	<div class="border border-grid bg-bg-1 corners relative p-6 sm:p-10 fade-up">
		{#if isAuth}
			<SessionExpiredCard />
		{:else}
			<div class="text-[10px] uppercase tracking-[0.3em] text-fg-muted mb-3">// error</div>
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
