<script module lang="ts">
	import type { ComponentProps } from 'svelte';
	import { defineMeta } from '@storybook/addon-svelte-csf';
	import { expect, waitFor } from 'storybook/test';
	import LadderStatusLegend from './LadderStatusLegend.svelte';

	type LadderStatusLegendProps = ComponentProps<typeof LadderStatusLegend>;

	const { Story } = defineMeta({
		title: 'Data-viz/LadderStatusLegend',
		component: LadderStatusLegend,
		tags: ['autodocs'],
		parameters: { layout: 'padded' }
	});
</script>

<!-- Collapsed (default) — the disclosure is shut; the summary trigger is visible. -->
<Story
	name="Collapsed"
	play={async ({ canvas }) => {
		await waitFor(() =>
			expect(canvas.getByText('what do these statuses mean?')).toBeVisible()
		);
	}}
>
	{#snippet template()}
		<div style="padding: 4rem 6rem; min-width: 36rem;">
			<LadderStatusLegend />
		</div>
	{/snippet}
</Story>

<!-- Expanded — click the summary to open the disclosure, then assert real status labels. -->
<Story
	name="Expanded"
	play={async ({ canvas }) => {
		// Open the native <details> by clicking its <summary>.
		const trigger = canvas.getByText('what do these statuses mean?');
		trigger.click();
		// Assert labels from all three groups are visible (real values from ladderStatus.ts).
		await waitFor(() => expect(canvas.getByText('running, nothing hit yet')).toBeVisible());
		await waitFor(() => expect(canvas.getByText('hit all targets (win)')).toBeVisible());
		await waitFor(() => expect(canvas.getByText('invalid setup')).toBeVisible());
		// PENDING synthetic placeholder belongs with the ongoing group.
		await waitFor(() => expect(canvas.getByText('not priced yet (queued)')).toBeVisible());
	}}
>
	{#snippet template()}
		<div style="padding: 4rem 6rem; min-width: 36rem;">
			<LadderStatusLegend />
		</div>
	{/snippet}
</Story>
