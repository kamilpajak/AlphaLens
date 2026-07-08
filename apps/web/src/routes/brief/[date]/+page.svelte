<script lang="ts">
	import type { PageData } from './$types';
	import CandidateCard from '$lib/components/CandidateCard.svelte';
	import MarketContextBanner from '$lib/components/MarketContextBanner.svelte';
	import LedgerFilterBar from '$lib/components/LedgerFilterBar.svelte';
	import { buildFilterChips, facetMatches } from '$lib/faceting';
	import { ChevronLeft, ChevronRight } from 'lucide-svelte';

	let { data }: { data: PageData } = $props();

	// The market-state label is index-level — identical on every candidate for
	// the date — so read it from the first candidate (undefined on a 0-candidate
	// day → the banner falls back to the `unknown` state).
	const marketCtx = $derived(data.brief.candidates[0]);

	// Multi-select theme filter (empty set = all), rendered as the shared
	// LedgerFilterBar chip bar. Plus the bespoke "verified only" boolean (a toggle,
	// not a facet — deliberately left as its own control).
	let selectedThemes = $state<Set<string>>(new Set());
	let onlyVerified = $state(false);

	// SvelteKit reuses this component across /brief/[date] navigations (same
	// route, changed param), so the filter $state would bleed onto the next
	// day — showing an empty candidate list if that day lacks the selected
	// theme. Reset the filters whenever the brief date changes.
	// Start undefined (don't capture data in the $state initializer — Svelte
	// flags that) so the first run just records the date; later date changes
	// clear the filters.
	let lastDate = $state<string | undefined>(undefined);
	$effect(() => {
		if (data.brief.date !== lastDate) {
			lastDate = data.brief.date;
			selectedThemes = new Set();
			onlyVerified = false;
		}
	});

	const filtered = $derived(
		data.brief.candidates.filter((c) => {
			if (!facetMatches(selectedThemes, c.theme)) return false;
			if (onlyVerified && !c.verified) return false;
			return true;
		})
	);

	// Theme facet chips for the shared LedgerFilterBar, in count-desc (then key)
	// order. Themes carry no formal definition, so the ChipTip just names the tag.
	const themeChips = $derived(
		buildFilterChips(
			Object.entries(data.brief.theme_counts)
				.map(([key, count]) => ({ key, count }))
				.sort((a, b) => b.count - a.count || a.key.localeCompare(b.key)),
			{
				all: {
					count: data.brief.n_candidates,
					tone: 'text-fg-muted border-grid',
					def: 'Show every candidate.'
				},
				label: (k) => `#${k}`,
				tone: () => 'text-fg-muted border-grid',
				def: (k) => `Candidates tagged "${k}".`
			}
		)
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
					class="font-display font-bold text-3xl sm:text-4xl lg:text-5xl text-amber tracking-tight mt-1 whitespace-nowrap"
				>
					{data.brief.date}
				</h1>
				{#if prevDay || nextDay}
					<div class="flex gap-2 mt-3">
						{#if prevDay}
							<a
								href="/brief/{prevDay.date}"
								class="inline-flex items-center gap-1 px-2 py-1 border border-grid hover:border-amber hover:text-amber text-[10px] uppercase tracking-widest text-fg-dim transition-colors whitespace-nowrap"
							>
								<ChevronLeft class="size-3" /> {prevDay.date}
							</a>
						{/if}
						{#if nextDay}
							<a
								href="/brief/{nextDay.date}"
								class="inline-flex items-center gap-1 px-2 py-1 border border-grid hover:border-amber hover:text-amber text-[10px] uppercase tracking-widest text-fg-dim transition-colors whitespace-nowrap"
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
				class="grid grid-cols-3 gap-px bg-grid border border-grid grow sm:grow-0 sm:min-w-[22rem]"
			>
				<!-- dt-before-dd keeps the DOM/spec order (and a sensible "label,
				     value" screen-reader read); flex-col-reverse renders the value
				     on top. Three day-level counts read left-to-right: candidates,
				     themes, top theme. (The verified count lives in the "verified
				     only" filter + per-card badges, not this headline strip.) -->
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

		<!-- Market context strip — index-level regime, display-only (PR-3). Lives
		     inside the header as a footer strip, above the catalyst headline. -->
		<MarketContextBanner
			marketState={marketCtx?.market_state}
			atrPctQ={marketCtx?.market_state_atr_pct_q}
			dist200={marketCtx?.market_state_dist200}
			vix={marketCtx?.market_state_vix}
			vixDecile={marketCtx?.market_state_vix_decile}
			squeezeOn={marketCtx?.market_state_squeeze_on}
		/>

		<!-- Catalyst footer — full width for the long headline -->
		{#if firstCatalystUrl}
			<div class="border-t border-grid px-4 sm:px-6 py-3 text-xs">
				<span class="text-fg-muted uppercase tracking-widest text-[10px]">top catalyst: </span>
				<a href={firstCatalystUrl} target="_blank" rel="noreferrer" aria-label={`${firstCatalystTitle ?? 'top catalyst'} (opens in a new tab)`} class="text-cyan hover:text-amber transition-colors">
					{firstCatalystTitle}
				</a>
			</div>
		{/if}
	</header>

	<!-- Filters: shared multi-select theme chip bar + the bespoke "verified only"
	     toggle (a boolean, not a facet, so it stays its own control). -->
	<div class="mb-5 flex flex-col gap-2 fade-up" style="animation-delay: 0.1s">
		<LedgerFilterBar label="theme" chips={themeChips} bind:selected={selectedThemes} />
		{#if verifiedCount < data.brief.n_candidates}
			<label class="flex items-center justify-end gap-2 text-[10px] uppercase tracking-widest text-fg-dim cursor-pointer">
				<input type="checkbox" bind:checked={onlyVerified} class="accent-amber" />
				verified only
			</label>
		{/if}
	</div>

	<!-- Candidates -->
	<div class="space-y-4">
		{#each filtered as c, i (c.ticker)}
			<CandidateCard
				candidate={c}
				index={i}
			/>
		{/each}
	</div>

	{#if filtered.length === 0}
		<div class="text-center py-12 text-fg-muted text-sm uppercase tracking-widest">
			no candidates match the active filter
		</div>
	{/if}
</div>
