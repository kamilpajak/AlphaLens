<script lang="ts">
	import '../app.css';
	import { page } from '$app/state';
	import { Activity, Database, Triangle } from 'lucide-svelte';
	import favicon from '$lib/assets/favicon.svg';

	let { children } = $props();

	let now = $state('');
	$effect(() => {
		const tick = () => {
			now = new Date().toISOString().slice(0, 19).replace('T', ' ');
		};
		tick();
		const id = setInterval(tick, 1000);
		return () => clearInterval(id);
	});

	const route = $derived(page.url.pathname);

	// Footer ticker chips — context-switch per route so the slogans match
	// the page the user is reading. Dashboard / briefs / brief / about all
	// concern the thematic-tool pipeline (Polygon news + Gemini Pro/Flash +
	// verification gates), while /experiments concerns the active-alpha
	// research ledger (αt thresholds, Bonferroni, multi-phase audit, PIT
	// discipline). Same component, two vocabularies.
	type Chip = { label: string; value: string };
	const tickerThematic: Chip[] = [
		{ label: 'PRESS-GATE', value: 'tri-state ok' },
		{ label: 'CATALYST-FLOOR', value: '0.55' },
		{ label: 'MAGIC-FORMULA', value: 'cohort' },
		{ label: 'PRO-MODEL', value: 'gemini-3-pro-preview' },
		{ label: 'FLASH-MODEL', value: 'gemini-2.5-flash' },
		{ label: 'PRESS-WINDOW', value: '30d' },
		{ label: 'SLIPPAGE', value: '50bps' },
		{ label: 'LIMIT', value: 'polygon 5rpm' }
	];
	const tickerExperiments: Chip[] = [
		{ label: 'DOCTRINE', value: 'αt ≥ 3.5 deploy' },
		{ label: 'MARGINAL', value: 'αt 2.0-3.5 paper' },
		{ label: 'NOISE', value: 'αt < 2.0' },
		{ label: 'BONFERRONI', value: 'escalates per test' },
		{ label: 'MULTI-PHASE', value: 'stride-5 mean ± std' },
		{ label: 'PIT', value: 'point-in-time mandatory' },
		{ label: 'SLIPPAGE-STRESS', value: '50bps half-spread' },
		{ label: 'LITERATURE', value: 'not oracle' }
	];
	const ticker = $derived(route === '/experiments' ? tickerExperiments : tickerThematic);
</script>

<svelte:head>
	<link rel="icon" href={favicon} />
</svelte:head>

<div class="scanlines grain min-h-screen flex flex-col">
	<!-- Top status bar -->
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
				<a href="/about" class="hover:text-amber transition-colors" class:text-amber={route === '/about'}>
					<span class="hidden sm:inline">[03]&nbsp;</span>about
				</a>
				<a href="/experiments" class="hover:text-amber transition-colors" class:text-amber={route === '/experiments'}>
					<span class="hidden sm:inline">[04]&nbsp;</span>experiments
				</a>
				<a
					href="/api/docs"
					target="_blank"
					rel="noreferrer"
					class="hover:text-amber transition-colors"
				>
					<span class="hidden sm:inline">[05]&nbsp;</span>api ↗
				</a>
			</nav>
			<div class="ml-auto flex items-center gap-3 sm:gap-5 text-fg-muted">
				<span class="flex items-center gap-1.5">
					<span class="dot bg-green blink"></span>
					<span class="text-green">live</span>
				</span>
				<span class="hidden md:flex items-center gap-1.5">
					<Database class="size-3" />
					<span>~/.alphalens</span>
				</span>
				<span class="hidden sm:flex items-center gap-1.5">
					<Activity class="size-3" />
					<span>{now} utc</span>
				</span>
			</div>
		</div>
	</header>

	<main class="flex-1">
		{@render children()}
	</main>

	<!-- Bottom ticker / status — chips switch per route via $derived(ticker). -->
	<footer class="border-t border-grid bg-bg-1 text-[10px] uppercase tracking-widest text-fg-muted overflow-hidden">
		<div class="flex items-center gap-6 px-4 py-2 whitespace-nowrap">
			{#each ticker as chip}
				<span class="text-amber">{chip.label}</span><span>{chip.value}</span>
			{/each}
			<span class="ml-auto">v0.1</span>
		</div>
	</footer>
</div>
