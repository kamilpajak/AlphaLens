<script module lang="ts">
	import type { ComponentProps } from 'svelte';
	import { defineMeta } from '@storybook/addon-svelte-csf';
	import { expect, waitFor } from 'storybook/test';
	import LedgerFilterBar from './LedgerFilterBar.svelte';

	type LedgerFilterBarProps = ComponentProps<typeof LedgerFilterBar>;

	// Representative chips drawn from real ladder-status codes (OPEN / TP_FULL /
	// SL_HIT / NO_FILL / NO_STRUCTURE) from src/lib/data/ladderStatus.ts.
	// The ALL chip is listed first (key === allKey default 'ALL') so the bar
	// renders exactly as it does on /experiments.
	const SAMPLE_CHIPS = [
		{
			key: 'ALL',
			label: 'all',
			count: 87,
			tone: 'text-fg border-grid',
			def: 'Show every row regardless of status.'
		},
		{
			key: 'OPEN',
			label: 'open',
			count: 21,
			tone: 'text-sky-400 border-sky-400/40',
			def: 'Entered, still running — neither a profit target nor the stop has been hit yet.'
		},
		{
			key: 'TP_FULL',
			label: 'tp full',
			count: 14,
			tone: 'text-green border-green/40',
			def: 'Hit every profit target — the position closed fully in profit.'
		},
		{
			key: 'SL_HIT',
			label: 'sl hit',
			count: 28,
			tone: 'text-red-400 border-red-400/40',
			def: 'The stop was hit before any profit target — the position closed at a loss.'
		},
		{
			key: 'NO_FILL',
			label: 'no fill',
			count: 18,
			tone: 'text-fg-muted border-grid',
			def: 'The entry price was never reached within the entry window — the trade never opened.'
		},
		{
			key: 'NO_STRUCTURE',
			label: 'no structure',
			count: 6,
			tone: 'text-amber border-amber/40',
			def: 'The brief had no entry / target / stop plan to evaluate.'
		}
	];

	const { Story } = defineMeta({
		title: 'Primitives/LedgerFilterBar',
		component: LedgerFilterBar,
		tags: ['autodocs'],
		parameters: { layout: 'padded' }
	});
</script>

<!-- (1) All deselected — the ALL chip is active, no Clear button. -->
<Story
	name="All selected (no Clear button)"
	play={async ({ canvas }) => {
		await waitFor(() => expect(canvas.getByText('filter')).toBeVisible());
		expect(canvas.queryByText(/clear/i)).toBeNull();
	}}
>
	{#snippet template()}
		<div style="padding: 4rem 6rem;">
			<LedgerFilterBar
				label="filter"
				chips={SAMPLE_CHIPS}
				allKey="ALL"
				selected={new Set()}
			/>
		</div>
	{/snippet}
</Story>

<!-- Force the ALL chip's ChipTip open and assert its def text is visible. -->
<Story
	name="All selected tooltip open"
	play={async ({ canvas }) => {
		const tips = canvas.getAllByTestId('chip-tip');
		tips[0].focus();
		await waitFor(() =>
			expect(canvas.getByText('Show every row regardless of status.')).toBeVisible()
		);
	}}
>
	{#snippet template()}
		<div style="padding: 6rem 6rem 3rem;">
			<LedgerFilterBar
				label="filter"
				chips={SAMPLE_CHIPS}
				allKey="ALL"
				selected={new Set()}
			/>
		</div>
	{/snippet}
</Story>

<!-- (2) One chip selected — Clear button appears at the trailing end. -->
<Story
	name="One chip selected (Clear button visible)"
	play={async ({ canvas }) => {
		await waitFor(() => expect(canvas.getByText(/clear/i)).toBeVisible());
	}}
>
	{#snippet template()}
		<div style="padding: 4rem 6rem;">
			<LedgerFilterBar
				label="filter"
				chips={SAMPLE_CHIPS}
				allKey="ALL"
				selected={new Set(['SL_HIT'])}
			/>
		</div>
	{/snippet}
</Story>

<!-- Force the SL_HIT chip (index 3) tooltip open and assert its def. -->
<Story
	name="One chip selected tooltip open"
	play={async ({ canvas }) => {
		const tips = canvas.getAllByTestId('chip-tip');
		tips[3].focus();
		await waitFor(() =>
			expect(
				canvas.getByText(
					'The stop was hit before any profit target — the position closed at a loss.'
				)
			).toBeVisible()
		);
	}}
>
	{#snippet template()}
		<div style="padding: 6rem 6rem 3rem;">
			<LedgerFilterBar
				label="filter"
				chips={SAMPLE_CHIPS}
				allKey="ALL"
				selected={new Set(['SL_HIT'])}
			/>
		</div>
	{/snippet}
</Story>
