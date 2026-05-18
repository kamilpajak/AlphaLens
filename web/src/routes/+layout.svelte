<script lang="ts">
	import '../app.css';
	import { page } from '$app/state';
	import { Activity, Database, Triangle } from 'lucide-svelte';
	import favicon from '$lib/assets/favicon.svg';

	let { children } = $props();

	const now = $derived(new Date().toISOString().slice(0, 19).replace('T', ' '));
	const route = $derived(page.url.pathname);
</script>

<svelte:head>
	<link rel="icon" href={favicon} />
</svelte:head>

<div class="scanlines grain min-h-screen flex flex-col">
	<!-- Top status bar -->
	<header class="border-b border-grid bg-bg-1 text-[11px] uppercase tracking-widest">
		<div class="flex items-center gap-6 px-4 py-2">
			<a href="/" class="flex items-center gap-2 font-display font-bold text-amber text-base tracking-[0.2em]">
				<Triangle class="size-4 fill-amber stroke-amber" />
				<span>ALPHALENS</span>
				<span class="text-fg-muted font-mono font-normal text-[10px]">// thematic ops</span>
			</a>
			<nav class="flex items-center gap-4 text-fg-dim">
				<a href="/" class="hover:text-amber transition-colors" class:text-amber={route === '/'}>
					[01] dashboard
				</a>
				<a href="/briefs" class="hover:text-amber transition-colors" class:text-amber={route.startsWith('/brief')}>
					[02] briefs
				</a>
				<a href="/about" class="hover:text-amber transition-colors" class:text-amber={route === '/about'}>
					[03] about
				</a>
			</nav>
			<div class="ml-auto flex items-center gap-5 text-fg-muted">
				<span class="flex items-center gap-1.5">
					<span class="dot bg-green blink"></span>
					<span class="text-green">live</span>
				</span>
				<span class="flex items-center gap-1.5">
					<Database class="size-3" />
					<span>~/.alphalens</span>
				</span>
				<span class="flex items-center gap-1.5">
					<Activity class="size-3" />
					<span>{now} utc</span>
				</span>
			</div>
		</div>
	</header>

	<main class="flex-1">
		{@render children()}
	</main>

	<!-- Bottom ticker / status -->
	<footer class="border-t border-grid bg-bg-1 text-[10px] uppercase tracking-widest text-fg-muted overflow-hidden">
		<div class="flex items-center gap-6 px-4 py-2 whitespace-nowrap">
			<span class="text-amber">PRESS-GATE</span><span>tri-state ok</span>
			<span class="text-amber">CATALYST-FLOOR</span><span>0.55</span>
			<span class="text-amber">MAGIC-FORMULA</span><span>cohort</span>
			<span class="text-amber">PRO-MODEL</span><span>gemini-3-pro-preview</span>
			<span class="text-amber">FLASH-MODEL</span><span>gemini-2.5-flash</span>
			<span class="text-amber">PRESS-WINDOW</span><span>30d</span>
			<span class="text-amber">SLIPPAGE</span><span>50bps</span>
			<span class="text-amber">LIMIT</span><span>polygon 5rpm</span>
			<span class="ml-auto">v0.1 // build {now}</span>
		</div>
	</footer>
</div>
