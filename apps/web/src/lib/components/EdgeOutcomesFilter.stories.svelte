<script module lang="ts">
	import { defineMeta } from '@storybook/addon-svelte-csf';
	import { expect, waitFor } from 'storybook/test';
	import EdgeOutcomesFilter from './EdgeOutcomesFilter.svelte';
	import type { EdgeOutcome } from '$lib/types';
	import { emptyFilterState, filterOutcomes } from '$lib/edgeFilter';

	// The 5 TERMINAL rows from tests/fixtures/api-mock/edge-outcomes.json, verbatim
	// (ticker / theme / ladder_classification). scorer_config_version is absent in
	// that fixture (null here), so the cohort facet legitimately does not render —
	// the default state of the real /edge data today. Only the fields the toolbar
	// reads (ticker, theme, ladder_classification, scorer_config_version) matter;
	// the rest carry neutral placeholders.
	function mk(ticker: string, theme: string | null, cls: string): EdgeOutcome {
		return {
			ticker,
			brief_date: '2026-05-18',
			matured_at: '2026-05-29',
			theme,
			scorer_config_version: null,
			ladder_classification: cls,
			captured_tp_count: null,
			touched_tp_count: null,
			terminal: true,
			realized_r: 1,
			open_r: null,
			market_excess_return: 0.1,
			forward_return: 0.05,
			benchmark_window_return: 0.02,
			holding_days_elapsed: 10,
			realized_return_pct_of_book: 0.15
		};
	}

	const ROWS: EdgeOutcome[] = [
		mk('AMPL', 'high-gas', 'TP_FULL'),
		mk('RGTI', 'quantum_computing', 'SL_HIT'),
		mk('IONQ', 'quantum_computing', 'TIME_STOP'),
		mk('PLTR', 'enterprise AI', 'PARTIAL_TP_THEN_SL'),
		mk('BBAI', null, 'TP_FULL')
	];

	// The server-truth window facets from tests/fixtures/api-mock/edge-outcomes.json,
	// verbatim — every count derives from the 8 rows in that file (5 terminal +
	// 3 ongoing; the blank-classification PEND row is dropped from the
	// classification map by the server).
	const FACETS = {
		status: { terminal: 5, ongoing: 3 },
		classification: { TP_FULL: 2, SL_HIT: 1, TIME_STOP: 1, PARTIAL_TP_THEN_SL: 1, OPEN: 2 }
	};

	const { Story } = defineMeta({
		title: 'Edge/EdgeOutcomesFilter',
		component: EdgeOutcomesFilter,
		tags: ['autodocs'],
		parameters: { layout: 'padded' }
	});
</script>

<!-- Default — no filter active, no server facets (the null-fallback path:
     chips derive from the rows): every facet chip present, count reads
     "5 of 5", no clear-all, cohort bar absent (no scorer cohort). -->
<Story
	name="Default (no filter)"
	play={async ({ canvas }) => {
		await waitFor(() => expect(canvas.getByTestId('outcomes-match-count')).toHaveTextContent('5 of 5'));
		expect(canvas.queryByTestId('outcomes-clear-all')).toBeNull();
		expect(canvas.getByText(/^TP_FULL/)).toBeVisible();
	}}
>
	{#snippet template()}
		<div style="padding: 2rem;">
			<EdgeOutcomesFilter rows={ROWS} matched={ROWS.length} view="terminal" state={emptyFilterState()} />
		</div>
	{/snippet}
</Story>

<!-- A classification facet selected — matched count drops, clear-all appears. -->
<Story
	name="Class facet selected"
	play={async ({ canvas }) => {
		await waitFor(() => expect(canvas.getByTestId('outcomes-clear-all')).toBeVisible());
		// TP_FULL matches AMPL + BBAI → "2 of 5".
		expect(canvas.getByTestId('outcomes-match-count')).toHaveTextContent('2 of 5');
	}}
>
	{#snippet template()}
		{@const state = { ...emptyFilterState(), classes: new Set(['TP_FULL']) }}
		<div style="padding: 2rem;">
			<EdgeOutcomesFilter
				rows={ROWS}
				matched={filterOutcomes(ROWS, state).length}
				view="terminal"
				{state}
			/>
		</div>
	{/snippet}
</Story>

<!-- A free-text query pre-filled — the search input carries it, count reflects
     the ticker/theme substring match (quantum → RGTI + IONQ). -->
<Story
	name="Search query active"
	play={async ({ canvas }) => {
		await waitFor(() =>
			expect(canvas.getByTestId('outcomes-search')).toHaveValue('quantum')
		);
		expect(canvas.getByTestId('outcomes-match-count')).toHaveTextContent('2 of 5');
	}}
>
	{#snippet template()}
		{@const state = { ...emptyFilterState(), query: 'quantum' }}
		<div style="padding: 2rem;">
			<EdgeOutcomesFilter
				rows={ROWS}
				matched={filterOutcomes(ROWS, state).length}
				view="terminal"
				{state}
			/>
		</div>
	{/snippet}
</Story>

<!-- Server facets — chip counts come from the pre-filter WINDOW population
     (facets.status / facets.classification), not the fetched rows: the ALL
     chip reads 5 (facets.status.terminal), TP_FULL reads 2, and the ongoing
     OPEN class is sliced OUT of the terminal view's chip bar. -->
<Story
	name="Server facets (window truth)"
	play={async ({ canvas }) => {
		// Chip buttons render "{label} {count}" — counts are the server facets.
		await waitFor(() => expect(canvas.getByRole('button', { name: 'all 5' })).toBeVisible());
		expect(canvas.getByRole('button', { name: 'TP_FULL 2' })).toBeVisible();
		expect(canvas.queryByRole('button', { name: /^OPEN/ })).toBeNull();
	}}
>
	{#snippet template()}
		<div style="padding: 2rem;">
			<EdgeOutcomesFilter
				rows={ROWS}
				matched={ROWS.length}
				facets={FACETS}
				view="terminal"
				state={emptyFilterState()}
			/>
		</div>
	{/snippet}
</Story>

<!-- A class the server dropped from the (capped/filtered) row listing still
     renders its chip with the server count — the defect this feature fixes:
     TIME_STOP is absent from `rows` but present in facets.classification. -->
<Story
	name="Facet class absent from fetched rows"
	play={async ({ canvas }) => {
		await waitFor(() =>
			expect(canvas.getByRole('button', { name: 'TIME_STOP 1' })).toBeVisible()
		);
	}}
>
	{#snippet template()}
		{@const rows = ROWS.filter((r) => r.ladder_classification !== 'TIME_STOP')}
		<div style="padding: 2rem;">
			<EdgeOutcomesFilter
				{rows}
				matched={rows.length}
				facets={FACETS}
				view="terminal"
				state={emptyFilterState()}
			/>
		</div>
	{/snippet}
</Story>
