<script module lang="ts">
	import type { ComponentProps } from 'svelte';
	import { defineMeta } from '@storybook/addon-svelte-csf';
	import { expect, waitFor } from 'storybook/test';
	import GatePill from './GatePill.svelte';

	type GatePillProps = ComponentProps<typeof GatePill>;

	// The outer <span> carries class="group", tabindex=0 and role="group" (when the
	// gate name is known). TooltipBubble defaults to the `plain` variant whose
	// open mechanism is `group-focus-within:opacity-100`, so focusing that outer
	// span (queried via its group role) reveals the tooltip.
	const openTooltip = async ({ canvas }: { canvas: any }) => {
		canvas.getByRole('group').focus();
	};

	const { Story } = defineMeta({
		title: 'Tooltips/GatePill',
		component: GatePill,
		tags: ['autodocs'],
		parameters: { layout: 'centered' },
		render: template
	});
</script>

{#snippet template(args: GatePillProps)}
	<div style="padding: 6rem 8rem 3rem;">
		<GatePill {...args} />
	</div>
{/snippet}

<Story
	name="Passed"
	args={{ name: 'tenk', status: 'passed' }}
	play={async ({ canvas }) => {
		canvas.getByRole('group').focus();
		await waitFor(() => expect(canvas.getByText('theme keywords found in 10-K')).toBeVisible());
	}}
/>

<Story
	name="Failed"
	args={{ name: 'press', status: 'failed' }}
	play={async ({ canvas }) => {
		canvas.getByRole('group').focus();
		await waitFor(() =>
			expect(canvas.getByText('press tagged ticker but no keyword hit (per-ticker fallback)')).toBeVisible()
		);
	}}
/>

<Story
	name="Unknown"
	args={{ name: 'insider', status: 'unknown' }}
	play={async ({ canvas }) => {
		canvas.getByRole('group').focus();
		await waitFor(() => expect(canvas.getByText('no Form-4 filings for ticker in window')).toBeVisible());
	}}
/>
