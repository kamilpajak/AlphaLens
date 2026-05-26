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
		<!-- Top band: session + date + day-nav on the left, a 2x2 metric grid on
		     the right. The grid fills what used to be dead horizontal space
		     beside the date, so the header collapses to roughly half its old
		     height instead of stacking a full-width strip underneath. On mobile
		     (flex-wrap) the grid drops below the date and spans full width. -->
		<div class="flex flex-wrap items-center justify-between gap-x-6 gap-y-4 px-4 sm:px-6 py-5">
			<div class="min-w-0 flex flex-col">
				<div class="text-[10px] uppercase tracking-[0.3em] text-fg-muted">// session</div>
				<h1
					data-testid="brief-date"
					class="font-display font-bold text-3xl sm:text-4xl lg:text-5xl text-amber tracking-tight mt-1"
				>
					{data.brief.date}
				</h1>
				{#if prevDay || nextDay}
					<div class="flex gap-2 mt-3">
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
				{/if}
			</div>

			<!-- 2x2 metric grid. gap-px over bg-grid paints clean 1px separators;
			     the outer border makes it a self-contained block beside the date.
			     grow on mobile (full width when wrapped) → grow-0 from sm up. -->
			<dl
				data-testid="brief-header-stats"
				class="grid grid-cols-2 gap-px bg-grid border border-grid self-center grow sm:grow-0 sm:min-w-[18rem]"
			>
				<!-- dt-before-dd keeps the DOM/spec order (and a sensible "label,
				     value" screen-reader read); flex-col-reverse renders the value
				     on top. Column 1 = volume (candidates / verified), column 2 =
				     themes (count / top), reading top-to-bottom per column. -->
				<div class="bg-bg-1 px-4 py-2.5 flex flex-col-reverse gap-0.5">
					<dt class="text-[10px] uppercase tracking-widest text-fg-muted">candidates</dt>
					<dd data-testid="stat-candidates" class="font-display font-bold text-2xl text-cyan">
						{data.brief.n_candidates}
					</dd>
				</div>
				<div class="bg-bg-1 px-4 py-2.5 flex flex-col-reverse gap-0.5">
					<dt class="text-[10px] uppercase tracking-widest text-fg-muted">themes</dt>
					<dd data-testid="stat-themes" class="font-display font-bold text-2xl text-amber">
						{data.brief.n_themes}
					</dd>
				</div>
				<div class="bg-bg-1 px-4 py-2.5 flex flex-col-reverse gap-0.5">
					<dt class="text-[10px] uppercase tracking-widest text-fg-muted">verified</dt>
					<dd data-testid="stat-verified" class="font-display font-bold text-2xl text-green">
						{verifiedCount}
					</dd>
				</div>
				<div class="bg-bg-1 px-4 py-2.5 flex flex-col-reverse gap-0.5 min-w-0">
					<dt class="text-[10px] uppercase tracking-widest text-fg-muted">top theme</dt>
					<dd
						data-testid="stat-top-theme"
						class="font-display font-bold text-xl text-amber lowercase truncate"
						title={data.brief.top_theme}
					>
						{data.brief.top_theme}
					</dd>
				</div>
			</dl>
		</div>

		<!-- Catalyst footer — full width for the long headline -->
		{#if firstCatalystUrl}
			<div class="border-t border-grid px-4 sm:px-6 py-3 text-xs">
				<span class="text-fg-muted uppercase tracking-widest text-[10px]">top catalyst: </span>
				<a href={firstCatalystUrl} target="_blank" rel="noreferrer" class="text-cyan hover:text-amber transition-colors">
					{firstCatalystTitle}
				</a>
			</div>
		{/if}
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
