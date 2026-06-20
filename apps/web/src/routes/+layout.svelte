<script lang="ts">
	import '../app.css';
	import { page, updated } from '$app/state';
	import { Activity, Database, Triangle } from 'lucide-svelte';
	import favicon from '$lib/assets/favicon.svg';
	import { tickerThematic, tickerExperiments } from '$lib/pipelineFacts';
	import MarketSession from '$lib/components/MarketSession.svelte';
	import SessionExpiredCard from '$lib/components/SessionExpiredCard.svelte';
	import { startMarketStatusPoll } from '$lib/marketStatus.svelte';
	import { sessionExpired } from '$lib/session.svelte';

	let { children } = $props();

	let now = $state('');
	$effect(() => {
		const tick = () => {
			// Minute precision (drop seconds): narrower in the footer and the
			// per-second interval becomes a no-op assignment for 59s of every
			// minute (Svelte skips equal-value $state writes), so no churn.
			now = new Date().toISOString().slice(0, 16).replace('T', ' ');
		};
		tick();
		const id = setInterval(tick, 1000);
		return () => clearInterval(id);
	});

	// Single layout-level mount point for the /v1/market/status poll loop.
	// startMarketStatusPoll is idempotent at the module level — subsequent
	// callers (none currently exist) would get a no-op cleanup — so this
	// remains the canonical owner.
	$effect(() => {
		const stop = startMarketStatusPoll();
		return stop;
	});

	const route = $derived(page.url.pathname);

	// Swagger UI lives on the Django origin (cross-origin in production).
	// Same-origin in local dev uses Vite's `/api/*` proxy.
	const apiBase = (import.meta.env.VITE_API_BASE ?? '').trim().replace(/\/+$/, '');
	const apiDocsHref = apiBase ? `${apiBase}/api/docs/` : '/api/docs/';

	// Footer ticker chips — route-keyed vocabulary (thematic vs research).
	// Definitions + provenance live in $lib/pipelineFacts.
	const ticker = $derived(route === '/experiments' ? tickerExperiments : tickerThematic);
</script>

<svelte:head>
	<link rel="icon" href={favicon} />
</svelte:head>

<!--
	`data-sveltekit-reload` opts every in-app navigation inside this wrapper
	into a full page reload (instead of client-side routing) once
	`updated.current` flips to true. The flip is driven by the version
	poll configured in svelte.config.js — when SvelteKit detects a new
	build, the next click re-fetches the HTML so the browser pulls the
	new chunk URLs instead of trying to import stale hashed modules.
	Default (empty / undefined attr) is "follow SPA routing"; setting
	the attribute to "" forces a reload, "off" keeps SPA routing.

	Scoped at the outer wrapper (not <main>) so header nav links
	(/, /briefs, /about, /experiments) are also covered — they sit in
	<header>, not <main>, and were the primary residual blank-screen
	risk under a child-only scope.
-->
<div
	class="scanlines grain min-h-screen flex flex-col"
	data-sveltekit-reload={updated.current ? '' : 'off'}
>
	<!-- Global re-auth overlay. Renders ONLY while the session-expiry store is
	     set (apiFetch flips it on the two CF-Access synthetic-401 paths), so it
	     never traps the page in the common case. Sits above all route content
	     (z-50) on EVERY route, replacing the old per-loader-only "session
	     expired" full-page error. Matches the +error.svelte card chrome. -->
	{#if sessionExpired()}
		<div
			class="fixed inset-0 z-50 flex items-center justify-center px-4 py-16 bg-bg/80 backdrop-blur-sm"
			role="dialog"
			aria-modal="true"
			aria-labelledby="session-expired-heading"
		>
			<div
				id="session-expired-heading"
				class="w-full max-w-[640px] border border-grid bg-bg-1 corners relative p-6 sm:p-10 fade-up"
			>
				<SessionExpiredCard />
			</div>
		</div>
	{/if}

	<!-- Top bar — identity + navigation only. Ambient telemetry (live /
	     session / db / clock) lives in the footer so this row never wraps
	     accidentally on narrow viewports. -->
	<header class="border-b border-grid bg-bg-1 text-[11px] uppercase tracking-widest">
		<div class="flex flex-wrap items-center gap-x-4 gap-y-1 sm:gap-x-6 px-3 sm:px-4 py-2">
			<a href="/" class="flex items-center gap-2 font-display font-bold text-amber text-base tracking-[0.2em]">
				<Triangle class="size-4 fill-amber stroke-amber" />
				<span>ALPHALENS</span>
				<span class="hidden sm:inline text-fg-muted font-mono font-normal text-[10px]">// thematic ops</span>
			</a>
			<nav class="flex items-center gap-3 sm:gap-4 text-fg-dim">
				<a href="/" class="hover:text-amber transition-colors" class:text-amber={route === '/'}>
					<span class="hidden sm:inline">[01]&nbsp;</span>dashboard
				</a>
				<a href="/briefs" class="hover:text-amber transition-colors" class:text-amber={route.startsWith('/brief')}>
					<span class="hidden sm:inline">[02]&nbsp;</span>briefs
				</a>
				<a href="/edge" class="hover:text-amber transition-colors" class:text-amber={route === '/edge'}>
					<span class="hidden sm:inline">[03]&nbsp;</span>edge
				</a>
				<a href="/experiments" class="hover:text-amber transition-colors" class:text-amber={route === '/experiments'}>
					<span class="hidden sm:inline">[04]&nbsp;</span>experiments
				</a>
				<a href="/about" class="hover:text-amber transition-colors" class:text-amber={route === '/about'}>
					<span class="hidden sm:inline">[05]&nbsp;</span>about
				</a>
				<a
					href={apiDocsHref}
					target="_blank"
					rel="noopener"
					class="hover:text-amber transition-colors"
					aria-label="api documentation (opens in a new tab)"
				>
					<span class="hidden sm:inline">[06]&nbsp;</span>api <span aria-hidden="true">↗</span>
				</a>
			</nav>
		</div>
	</header>

	<main class="flex-1">
		{@render children()}
	</main>

	<!-- Bottom status bar. Left: system telemetry (market session / db /
	     clock) — shrink-0, never clipped. Middle: route-keyed slogan ticker
	     — flex-1, clips first when space is tight. Right: version. -->
	<footer class="border-t border-grid bg-bg-1 text-[10px] uppercase tracking-widest text-fg-muted">
		<div class="flex items-center gap-x-5 px-4 py-2">
			<div class="flex shrink-0 items-center gap-3 sm:gap-4">
				<!-- Liveness is per-exchange, not a global app signal: the
				     MarketSession chip shows a green "● live" only while a venue
				     trades ("○ closed" otherwise), since every brief's prices are
				     anchored to the last close when the market is shut. The old
				     always-on standalone "live" dot was merged in here. -->
				<MarketSession />
				<!-- db path + clock are pure ambient flavour — desktop only (lg+).
				     On mobile the footer keeps just the session chip so it never
				     overflows a narrow viewport. -->
				<span data-testid="footer-db" class="hidden lg:flex items-center gap-1.5">
					<Database class="size-3" />
					<span>~/.alphalens</span>
				</span>
				<span data-testid="footer-clock" class="hidden lg:flex items-center gap-1.5">
					<Activity class="size-3" />
					<span class="whitespace-nowrap">{now} utc</span>
				</span>
			</div>
			<!-- Slogan ticker clips first when space is tight; the right-edge
			     fade mask makes the cut read as intentional (a fade-out, not a
			     mid-word chop) and signals "there's more". The mask only shows
			     over content near the right edge, so it's invisible when the
			     chips fit with room to spare. -->
			<div
				class="hidden lg:flex flex-1 items-center gap-6 overflow-hidden whitespace-nowrap text-fg-muted/80"
				style="mask-image: linear-gradient(to right, #000 calc(100% - 1.5rem), transparent); -webkit-mask-image: linear-gradient(to right, #000 calc(100% - 1.5rem), transparent);"
			>
				{#each ticker as chip}
					<span class="flex items-center gap-1.5"><span class="text-amber">{chip.label}</span><span>{chip.value}</span></span>
				{/each}
			</div>
			<span class="ml-auto shrink-0">v0.1</span>
		</div>
	</footer>
</div>
