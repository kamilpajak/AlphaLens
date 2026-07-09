<script lang="ts">
	// Filter toolbar for the /edge outcomes table: a free-text search over
	// ticker+theme plus faceted multi-select on ladder-classification and the
	// scorer-config cohort. Presentational only — the parent owns the row set,
	// derives `matched` via `filterOutcomes`, and holds the (bindable) state; this
	// component just derives the facet chips from the current view and renders the
	// controls. Reuses the shared `LedgerFilterBar` for the chip rows.
	import LedgerFilterBar from './LedgerFilterBar.svelte';
	import type { EdgeOutcome } from '$lib/types';
	import { classificationTone, toneClasses } from '$lib/edge';
	import { ladderStatusBody, ladderStatusLabel } from '$lib/data/ladderStatus';
	import { isFilterActive, type EdgeFilterState } from '$lib/edgeFilter';
	import { buildFilterChips, deriveFacet } from '$lib/faceting';

	interface Props {
		/** The current terminal/ongoing view — the facet universe + counts. */
		rows: EdgeOutcome[];
		/** Rows remaining after `filterOutcomes` (parent-computed), for "N of M". */
		matched: number;
		/** The filter selections. Bindable — the parent syncs these to the URL. */
		state: EdgeFilterState;
	}

	let { rows, matched, state = $bindable() }: Props = $props();

	const NEUTRAL = toneClasses('muted');
	// Shared "all" chip config (neutral tone, view-scoped copy) for both facets.
	const allCfg = $derived({
		count: rows.length,
		tone: NEUTRAL,
		def: 'Show every row in the current view.'
	});

	const classFacet = $derived(deriveFacet(rows, (o) => o.ladder_classification));
	const cohortFacet = $derived(deriveFacet(rows, (o) => o.scorer_config_version));

	const classChips = $derived(
		buildFilterChips(classFacet, {
			all: allCfg,
			label: (k) => ladderStatusLabel(k),
			tone: (k) => toneClasses(classificationTone(k)),
			def: (k) => ladderStatusBody(k)
		})
	);

	const cohortChips = $derived(
		buildFilterChips(cohortFacet, {
			all: allCfg,
			label: (k) => k,
			tone: () => NEUTRAL,
			def: (k) =>
				`Scorer-config cohort ${k} — a poolability key; outcomes from different scorer versions are not directly comparable.`
		})
	);

	const active = $derived(isFilterActive(state));

	function clearAll() {
		state.query = '';
		state.classes = new Set();
		state.cohorts = new Set();
	}
</script>

<div class="mb-3 flex flex-col gap-2" data-testid="outcomes-filter">
	<div class="flex flex-wrap items-center gap-2">
		<label class="relative">
			<span class="sr-only">Search ticker or theme</span>
			<input
				type="search"
				bind:value={state.query}
				onblur={() => (state.query = state.query.trim())}
				placeholder="search ticker or theme…"
				data-testid="outcomes-search"
				class="w-52 border border-grid bg-bg-2 px-2 py-1 text-xs text-fg placeholder:text-fg-muted focus:border-amber focus:outline-none"
			/>
		</label>
		<span class="text-[10px] uppercase tracking-widest text-fg-muted" data-testid="outcomes-match-count">
			<span class="font-mono text-fg-dim">{matched}</span> of
			<span class="font-mono text-fg-dim">{rows.length}</span>
		</span>
		{#if active}
			<button
				type="button"
				onclick={clearAll}
				data-testid="outcomes-clear-all"
				class="ml-auto text-[10px] uppercase tracking-widest text-fg-muted hover:text-amber"
			>
				clear all ✕
			</button>
		{/if}
	</div>

	<LedgerFilterBar label="status" chips={classChips} bind:selected={state.classes} />
	{#if cohortFacet.length > 1}
		<LedgerFilterBar label="cohort" chips={cohortChips} bind:selected={state.cohorts} />
	{/if}
</div>
