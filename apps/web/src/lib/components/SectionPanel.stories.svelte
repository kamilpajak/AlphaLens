<script module lang="ts">
	import type { ComponentProps } from 'svelte';
	import { defineMeta } from '@storybook/addon-svelte-csf';
	import { expect, waitFor } from 'storybook/test';
	import SectionPanel from './SectionPanel.svelte';

	type SectionPanelProps = ComponentProps<typeof SectionPanel>;

	const { Story } = defineMeta({
		title: 'Primitives/SectionPanel',
		component: SectionPanel,
		tags: ['autodocs'],
		parameters: { layout: 'padded' }
	});
</script>

<Story
	name="Default"
	play={async ({ canvas }) => {
		await waitFor(() => expect(canvas.getByText('research.ledger')).toBeVisible());
		await waitFor(() => expect(canvas.getByText('No paradigm has reached a PASS verdict.')).toBeVisible());
	}}
>
	{#snippet template()}
		<div style="padding: 4rem 6rem; width: 32rem;">
			<SectionPanel title="research.ledger">
				{#snippet meta()}
					<span>15 paradigms</span>
				{/snippet}
				{#snippet children()}
					<div class="px-4 py-3 text-sm text-fg-muted">
						No paradigm has reached a PASS verdict.
					</div>
				{/snippet}
			</SectionPanel>
		</div>
	{/snippet}
</Story>

<Story
	name="Prominent Title"
	play={async ({ canvas }) => {
		await waitFor(() => expect(canvas.getByText('forward.log')).toBeVisible());
		await waitFor(() => expect(canvas.getByText('Selection edge measured from live population monitor.')).toBeVisible());
	}}
>
	{#snippet template()}
		<div style="padding: 4rem 6rem; width: 32rem;">
			<SectionPanel
				title="forward.log"
				titleClass="font-semibold text-fg-default text-xs"
			>
				{#snippet meta()}
					<span class="text-[10px]">in-sample</span>
				{/snippet}
				{#snippet children()}
					<div class="px-4 py-3 text-sm text-fg-muted">
						Selection edge measured from live population monitor.
					</div>
				{/snippet}
			</SectionPanel>
		</div>
	{/snippet}
</Story>

<Story
	name="With Count Meta"
	play={async ({ canvas }) => {
		await waitFor(() => expect(canvas.getByText('tool.experiments')).toBeVisible());
		await waitFor(() => expect(canvas.getByText('5 tests')).toBeVisible());
		await waitFor(() => expect(canvas.getByText('ATR tilt confirmed across 3 exit configurations.')).toBeVisible());
	}}
>
	{#snippet template()}
		<div style="padding: 4rem 6rem; width: 32rem;">
			<SectionPanel title="tool.experiments" id="tool-experiments">
				{#snippet meta()}
					<span>5 tests</span>
				{/snippet}
				{#snippet children()}
					<div class="px-4 py-3 text-sm text-fg-muted">
						ATR tilt confirmed across 3 exit configurations.
					</div>
				{/snippet}
			</SectionPanel>
		</div>
	{/snippet}
</Story>

<Story
	name="No Bottom Margin"
	play={async ({ canvas }) => {
		await waitFor(() => expect(canvas.getByText('about')).toBeVisible());
		await waitFor(() => expect(canvas.getByText('Last section in the page — no bottom margin.')).toBeVisible());
	}}
>
	{#snippet template()}
		<div style="padding: 4rem 6rem; width: 32rem;">
			<SectionPanel title="about" sectionClass="">
				{#snippet meta()}
					<span></span>
				{/snippet}
				{#snippet children()}
					<div class="px-4 py-3 text-sm text-fg-muted">
						Last section in the page — no bottom margin.
					</div>
				{/snippet}
			</SectionPanel>
		</div>
	{/snippet}
</Story>
