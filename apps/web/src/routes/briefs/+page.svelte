<script lang="ts">
	import type { PageData } from './$types';
	import { ChevronRight } from 'lucide-svelte';
	import LedgerFilterBar from '$lib/components/LedgerFilterBar.svelte';
	import { buildFilterChips, deriveFacet } from '$lib/faceting';
	import {
		filterDays,
		isBriefsFilterActive,
		type BriefsFilterState
	} from '$lib/briefsFilter';
	import { setToParam, paramToSet } from '$lib/urlFilters';
	import { syncParamsToUrl } from '$lib/urlFilterSync.svelte';
	import { sortDays, defaultDir, type BriefsSortKey, type SortDir } from '$lib/briefsSort';
	import { page } from '$app/state';

	let { data }: { data: PageData } = $props();

	// Filter state seeded from the URL (`?q=`, `?theme=a,b`) so a filtered archive
	// view is deep-linkable, then mirrored back on change.
	let filterState = $state<BriefsFilterState>({
		query: page.url.searchParams.get('q') ?? '',
		themes: paramToSet(page.url.searchParams.get('theme'))
	});

	const filteredDays = $derived(filterDays(data.days, filterState));
	const active = $derived(isBriefsFilterActive(filterState));

	// Client-side sort (ephemeral, like /edge — not URL-synced). Default date desc
	// keeps the newest brief on top.
	let sortKey = $state<BriefsSortKey>('date');
	let sortDir = $state<SortDir>('desc');

	function toggleSort(key: BriefsSortKey) {
		if (sortKey === key) {
			sortDir = sortDir === 'asc' ? 'desc' : 'asc';
		} else {
			sortKey = key;
			sortDir = defaultDir(key);
		}
	}

	const sortedDays = $derived(sortDays(filteredDays, sortKey, sortDir));

	// Top-theme facet chips (count-desc). Themes carry no formal definition, so the
	// ChipTip just names the tag.
	const themeChips = $derived(
		buildFilterChips(
			deriveFacet(data.days, (d) => d.top_theme),
			{
				all: {
					count: data.days.length,
					tone: 'text-fg-muted border-grid',
					def: 'Show every brief day.'
				},
				label: (k) => `#${k}`,
				tone: () => 'text-fg-muted border-grid',
				def: (k) => `Days whose top theme is "${k}".`
			}
		)
	);

	syncParamsToUrl(() => {
		const params = new URLSearchParams(window.location.search);
		const q = filterState.query.trim();
		if (q) params.set('q', q);
		else params.delete('q');
		const theme = setToParam(filterState.themes);
		if (theme) params.set('theme', theme);
		else params.delete('theme');
		return params;
	});

	function clearAll() {
		filterState.query = '';
		filterState.themes = new Set();
	}
</script>

<div class="px-3 sm:px-4 py-8 max-w-[1200px] mx-auto">
	<div class="mb-5 flex items-baseline gap-3 fade-up">
		<h1 class="font-display font-bold text-2xl sm:text-3xl tracking-tight">BRIEFS.ARCHIVE</h1>
		<span class="text-[10px] uppercase tracking-widest text-fg-muted">[{data.days.length}]</span>
		<div class="flex-1 border-b border-dashed border-grid"></div>
	</div>

	<!-- Filters: free-text search over date+theme + shared top-theme chip bar. -->
	<div class="mb-5 flex flex-col gap-2 fade-up" data-testid="briefs-filter">
		<div class="flex flex-wrap items-center gap-2">
			<label class="relative">
				<span class="sr-only">Search date or theme</span>
				<input
					type="search"
					bind:value={filterState.query}
					onblur={() => {
						const t = filterState.query.trim();
						if (t !== filterState.query) filterState.query = t;
					}}
					placeholder="search date or theme…"
					data-testid="briefs-search"
					class="w-52 border border-grid bg-bg-2 px-2 py-1 text-xs text-fg placeholder:text-fg-muted focus:border-amber focus:outline-none"
				/>
			</label>
			<span
				class="text-[10px] uppercase tracking-widest text-fg-muted"
				data-testid="briefs-match-count"
			>
				<span class="font-mono text-fg-dim">{filteredDays.length}</span> of
				<span class="font-mono text-fg-dim">{data.days.length}</span>
			</span>
			{#if active}
				<button
					type="button"
					onclick={clearAll}
					data-testid="briefs-clear-all"
					class="ml-auto text-[10px] uppercase tracking-widest text-fg-muted hover:text-amber"
				>
					clear all ✕
				</button>
			{/if}
		</div>
		{#if themeChips.length > 1}
			<LedgerFilterBar label="top theme" chips={themeChips} bind:selected={filterState.themes} />
		{/if}
	</div>

	{#if filteredDays.length === 0}
		<div class="border border-dashed border-grid-strong px-4 py-8 text-center text-sm text-fg-muted">
			no briefs match the current filter
		</div>
	{:else}
		{#snippet sortHead(key: BriefsSortKey, label: string, cls: string)}
			<th
				class="py-2 {cls}"
				aria-sort={sortKey === key ? (sortDir === 'asc' ? 'ascending' : 'descending') : 'none'}
			>
				<button
					type="button"
					onclick={() => toggleSort(key)}
					class="group/s inline-flex items-center gap-1 uppercase tracking-widest transition-colors {sortKey ===
					key
						? 'text-amber'
						: 'text-fg-muted hover:text-fg-dim'}"
				>
					<span>{label}</span>
					<span
						class="text-[8px] leading-none {sortKey === key
							? 'opacity-100'
							: 'opacity-0 group-hover/s:opacity-50'}"
						aria-hidden="true">{(sortKey === key ? sortDir : defaultDir(key)) === 'asc' ? '▲' : '▼'}</span>
				</button>
			</th>
		{/snippet}

		<table class="w-full text-sm">
			<thead>
				<tr class="text-[10px] uppercase tracking-widest text-fg-muted text-left border-b border-grid">
					{@render sortHead('date', 'date', 'pr-3 sm:pr-4')}
					{@render sortHead('cand', 'cand', 'pr-3 sm:pr-4 text-right')}
					{@render sortHead('themes', 'themes', 'hidden sm:table-cell pr-4 text-right')}
					{@render sortHead('top', 'top theme', 'pr-3 sm:pr-4')}
					<th class="py-2"></th>
				</tr>
			</thead>
			<tbody>
				{#each sortedDays as day, i (day.date)}
					<tr class="border-b border-grid hover:bg-bg-2 group fade-up" style="animation-delay: {i * 0.05}s">
						<td class="py-3 pr-3 sm:pr-4">
							<a
								href="/brief/{day.date}"
								class="font-display font-bold text-lg sm:text-xl text-fg group-hover:text-amber transition-colors whitespace-nowrap"
							>
								{day.date}
							</a>
						</td>
						<td class="py-3 pr-3 sm:pr-4 text-right text-cyan font-bold">{day.n_candidates}</td>
						<td class="hidden sm:table-cell py-3 pr-4 text-right text-fg-dim">{day.n_themes}</td>
						<td class="py-3 pr-3 sm:pr-4 text-amber lowercase truncate max-w-[140px] sm:max-w-none">{day.top_theme ?? '—'}</td>
						<td class="py-3 text-right">
							<a
								href="/brief/{day.date}"
								class="inline-flex items-center gap-1 text-[10px] uppercase tracking-widest text-fg-muted group-hover:text-amber transition-colors"
							>
								<span class="hidden sm:inline">view</span>
								<ChevronRight class="size-3" />
							</a>
						</td>
					</tr>
				{/each}
			</tbody>
		</table>
	{/if}
</div>
