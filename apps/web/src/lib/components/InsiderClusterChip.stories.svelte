<script module lang="ts">
	import { defineMeta } from '@storybook/addon-svelte-csf';
	import { expect, waitFor } from 'storybook/test';
	import type { Candidate, DayBrief } from '$lib/types';
	import InsiderClusterChip from './InsiderClusterChip.svelte';
	// REAL rows: the first day the insider-cluster lane ran on the VPS
	// (2026-09-05), captured through the Django serializer. No fabricated
	// overlap story — no real overlap row exists yet; add one when it lands.
	import laneDay from '../../../tests/fixtures/api-mock/days/2026-09-05.json';

	const day = laneDay as unknown as DayBrief;
	const byTicker = (t: string): Candidate => {
		const c = day.candidates.find((x) => x.ticker === t);
		if (!c) throw new Error(`fixture row missing: ${t}`);
		return c;
	};
	const eqpt = byTicker('EQPT');
	const enov = byTicker('ENOV');
	const wfrd = byTicker('WFRD');

	const { Story } = defineMeta({
		title: 'Tooltips/InsiderClusterChip',
		component: InsiderClusterChip,
		tags: ['autodocs'],
		parameters: { layout: 'centered' }
	});
</script>

{#snippet frame(candidate: Candidate)}
	<div style="padding: 8rem 10rem 3rem;">
		<InsiderClusterChip {candidate} />
	</div>
{/snippet}

<!-- EQPT — two officer/director buyers, $610k, filed over two sessions -->
<Story
	name="EQPT Two Officer Directors"
	play={async ({ canvas }) => {
		await waitFor(() => expect(canvas.getByText('insiders · 2 buyers · $610k')).toBeVisible());
		canvas.getByRole('group').focus();
		await waitFor(() => expect(canvas.getByText(/Schlacks Jabbok/)).toBeVisible());
		await waitFor(() => expect(canvas.getByText(/Schlacks William J\./)).toBeVisible());
	}}
>
	{#snippet template()}
		{@render frame(eqpt)}
	{/snippet}
</Story>

<!-- ENOV — an officer/director plus a plain officer, $400k, one filing day -->
<Story
	name="ENOV Officer Director Plus Officer"
	play={async ({ canvas }) => {
		await waitFor(() => expect(canvas.getByText('insiders · 2 buyers · $400k')).toBeVisible());
		canvas.getByRole('group').focus();
		await waitFor(() => expect(canvas.getByText(/McDonald Damien · officer\/director/)).toBeVisible());
		await waitFor(() => expect(canvas.getByText(/Engert Oliver · officer$/)).toBeVisible());
	}}
>
	{#snippet template()}
		{@render frame(enov)}
	{/snippet}
</Story>

<!-- WFRD — a thematic row on the same day: no cluster facts, renders nothing -->
<Story
	name="Thematic Row Renders Nothing"
	play={async ({ canvasElement }) => {
		await waitFor(() =>
			expect(canvasElement.querySelector('[data-testid="insider-cluster-chip"]')).toBeNull()
		);
	}}
>
	{#snippet template()}
		{@render frame(wfrd)}
	{/snippet}
</Story>
