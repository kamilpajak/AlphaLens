<script module lang="ts">
	import type { ComponentProps } from 'svelte';
	import { defineMeta } from '@storybook/addon-svelte-csf';
	import { expect, within } from 'storybook/test';
	import StatusPill from './StatusPill.svelte';
	import { toneClass } from '$lib/tone';

	type StatusPillProps = ComponentProps<typeof StatusPill>;

	const { Story } = defineMeta({
		title: 'Primitives/StatusPill',
		component: StatusPill,
		tags: ['autodocs'],
		parameters: { layout: 'centered' }
	});
</script>

<!-- All seven semantic tones side-by-side in their default (size=10, solid, non-interactive) form. -->
<Story
	name="All Tones"
	play={async ({ canvasElement }) => {
		const canvas = within(canvasElement);
		await expect(canvas.getByText('PASS')).toBeVisible();
		await expect(canvas.getByText('FAIL')).toBeVisible();
	}}
>
	{#snippet template()}
		<div style="padding: 4rem 6rem;" class="flex flex-wrap gap-3 items-center">
			<StatusPill tone={toneClass('green')} label="PASS" />
			<StatusPill tone={toneClass('red')} label="FAIL" />
			<StatusPill tone={toneClass('amber')} label="PENDING" />
			<StatusPill tone={toneClass('cyan')} label="ACTIVE" />
			<StatusPill tone={toneClass('violet')} label="IN-SAMPLE" />
			<StatusPill tone={toneClass('magenta')} label="FORWARD-LOG" />
			<StatusPill tone={toneClass('muted')} label="UNKNOWN" />
		</div>
	{/snippet}
</Story>

<!-- size="9": compact variant used in dense ledger rows. -->
<Story
	name="Size 9 Compact"
	play={async ({ canvasElement }) => {
		const canvas = within(canvasElement);
		await expect(canvas.getByText('PASS')).toBeVisible();
	}}
>
	{#snippet template()}
		<div style="padding: 4rem 6rem;" class="flex flex-wrap gap-3 items-center">
			<StatusPill tone={toneClass('green')} label="PASS" size="9" />
			<StatusPill tone={toneClass('red')} label="FAIL" size="9" />
			<StatusPill tone={toneClass('amber')} label="PENDING" size="9" />
			<StatusPill tone={toneClass('muted')} label="UNKNOWN" size="9" />
		</div>
	{/snippet}
</Story>

<!-- dashed=true: border-dashed styling for pending / forward-looking statuses. -->
<Story
	name="Dashed"
	play={async ({ canvasElement }) => {
		const canvas = within(canvasElement);
		await expect(canvas.getByText('FORWARD-LOG')).toBeVisible();
	}}
>
	{#snippet template()}
		<div style="padding: 4rem 6rem;" class="flex flex-wrap gap-3 items-center">
			<StatusPill tone={toneClass('magenta')} label="FORWARD-LOG" dashed />
			<StatusPill tone={toneClass('amber')} label="PENDING" dashed />
			<StatusPill tone={toneClass('muted')} label="DEFERRED" dashed />
		</div>
	{/snippet}
</Story>

<!-- nowrap=true: adds whitespace-nowrap so atomic tokens never break across lines. -->
<Story
	name="Nowrap"
	play={async ({ canvasElement }) => {
		const canvas = within(canvasElement);
		await expect(canvas.getByText('IN-SAMPLE')).toBeVisible();
	}}
>
	{#snippet template()}
		<div style="padding: 4rem 6rem; max-width: 8rem;">
			<StatusPill tone={toneClass('violet')} label="IN-SAMPLE" nowrap />
		</div>
	{/snippet}
</Story>

<!-- interactive=true: adds cursor-help to signal that the pill carries a tooltip. -->
<Story
	name="Interactive"
	play={async ({ canvasElement }) => {
		const canvas = within(canvasElement);
		await expect(canvas.getByText('ACTIVE')).toBeVisible();
	}}
>
	{#snippet template()}
		<div style="padding: 4rem 6rem;" class="flex flex-wrap gap-3 items-center">
			<StatusPill tone={toneClass('cyan')} label="ACTIVE" interactive />
			<StatusPill tone={toneClass('green')} label="PASS" interactive />
		</div>
	{/snippet}
</Story>
