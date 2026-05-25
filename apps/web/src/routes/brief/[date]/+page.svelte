<script lang="ts">
	import type { PageData } from './$types';
	import CandidateCard from '$lib/components/CandidateCard.svelte';
	import { ChevronLeft, ChevronRight, Filter } from 'lucide-svelte';

	let { data }: { data: PageData } = $props();

	let activeTheme = $state<string | null>(null);
	let onlyVerified = $state(false);

	const filtered = $derived(
		data.brief.candidates.filter((c) => {
			if (activeTheme && c.theme !== activeTheme) return false;
			if (onlyVerified && !c.verified) return false;
			return true;
		})
	);

	const themes = $derived(
		Object.entries(data.brief.theme_counts).sort(([, a], [, b]) => b - a)
	);

	const currentIdx = $derived(data.days.findIndex((d) => d.date === data.brief.date));
	const prevDay = $derived(currentIdx >= 0 && currentIdx < data.days.length - 1 ? data.days[currentIdx + 1] : null);
	const nextDay = $derived(currentIdx > 0 ? data.days[currentIdx - 1] : null);

	const verifiedCount = $derived(data.brief.candidates.filter((c) => c.verified).length);
	const firstCatalystUrl = $derived(data.brief.candidates[0]?.source_event_url ?? null);
	const firstCatalystTitle = $derived(data.brief.candidates[0]?.source_event_title ?? null);
</script>

<div class="max-w-[1400px] mx-auto px-3 sm:px-4 py-6">
	<!-- Header -->
	<header class="border border-grid bg-bg-1 corners relative fade-up mb-5">
		<div class="grid grid-cols-12 gap-4 px-4 sm:px-6 py-5">
			<div class="col-span-12 lg:col-span-8">
				<div class="text-[10px] uppercase tracking-[0.3em] text-fg-muted">// session</div>
				<div class="flex flex-wrap items-baseline gap-x-4 gap-y-2 mt-1">
					<h1 class="font-display font-bold text-3xl sm:text-4xl lg:text-5xl text-amber tracking-tight">{data.brief.date}</h1>
					<div class="text-[11px] uppercase tracking-widest text-fg-muted">
						<div><span class="text-cyan font-bold">{data.brief.n_candidates}</span> candidates</div>
						<div><span class="text-green font-bold">{verifiedCount}</span> verified</div>
						<div><span class="text-amber font-bold">{data.brief.n_themes}</span> themes</div>
					</div>
				</div>
				{#if firstCatalystUrl}
					<div class="mt-3 text-xs">
						<span class="text-fg-muted uppercase tracking-widest text-[10px]">top catalyst: </span>
						<a href={firstCatalystUrl} target="_blank" rel="noreferrer" class="text-cyan hover:text-amber transition-colors">
							{firstCatalystTitle}
						</a>
					</div>
				{/if}
			</div>

			<div class="col-span-12 lg:col-span-4 flex flex-col gap-2 lg:items-end justify-between">
				<div class="flex gap-2">
					{#if prevDay}
						<a
							href="/brief/{prevDay.date}"
							class="inline-flex items-center gap-1 px-2 py-1 border border-grid hover:border-amber hover:text-amber text-[10px] uppercase tracking-widest text-fg-dim transition-colors"
						>
							<ChevronLeft class="size-3" /> {prevDay.date}
						</a>
					{/if}
					{#if nextDay}
						<a
							href="/brief/{nextDay.date}"
							class="inline-flex items-center gap-1 px-2 py-1 border border-grid hover:border-amber hover:text-amber text-[10px] uppercase tracking-widest text-fg-dim transition-colors"
						>
							{nextDay.date} <ChevronRight class="size-3" />
						</a>
					{/if}
				</div>
				<div class="text-[10px] uppercase tracking-widest text-fg-muted">
					top theme: <span class="text-amber lowercase">{data.brief.top_theme}</span>
				</div>
			</div>
		</div>
	</header>

	<!-- Filters -->
	<div class="flex flex-wrap items-center gap-3 mb-5 fade-up" style="animation-delay: 0.1s">
		<div class="flex items-center gap-2 text-[10px] uppercase tracking-widest text-fg-muted">
			<Filter class="size-3" /> filter:
		</div>
		<button
			type="button"
			onclick={() => (activeTheme = null)}
			class="px-2 py-1 text-[10px] uppercase tracking-widest border transition-colors"
			class:border-amber={activeTheme === null}
			class:text-amber={activeTheme === null}
			class:border-grid={activeTheme !== null}
			class:text-fg-dim={activeTheme !== null}
		>
			all ({data.brief.n_candidates})
		</button>
		{#each themes as [theme, count]}
			<button
				type="button"
				onclick={() => (activeTheme = activeTheme === theme ? null : theme)}
				class="px-2 py-1 text-[10px] uppercase tracking-widest border transition-colors lowercase"
				class:border-amber={activeTheme === theme}
				class:text-amber={activeTheme === theme}
				class:border-grid={activeTheme !== theme}
				class:text-fg-dim={activeTheme !== theme}
			>
				#{theme} <span class="text-fg-muted">({count})</span>
			</button>
		{/each}
		{#if verifiedCount < data.brief.n_candidates}
			<label class="ml-auto flex items-center gap-2 text-[10px] uppercase tracking-widest text-fg-dim cursor-pointer">
				<input
					type="checkbox"
					bind:checked={onlyVerified}
					class="accent-amber"
				/>
				verified only
			</label>
		{/if}
	</div>

	<!-- Candidates -->
	<div class="space-y-4">
		{#each filtered as c, i (c.ticker)}
			<CandidateCard candidate={c} index={i} />
		{/each}
	</div>

	{#if filtered.length === 0}
		<div class="text-center py-12 text-fg-muted text-sm uppercase tracking-widest">
			no candidates match the active filter
		</div>
	{/if}
</div>
