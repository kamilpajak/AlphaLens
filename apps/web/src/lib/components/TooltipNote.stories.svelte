<script module lang="ts">
	import type { ComponentProps } from 'svelte';
	import { defineMeta } from '@storybook/addon-svelte-csf';
	import { expect, waitFor } from 'storybook/test';
	import TooltipNote from './TooltipNote.svelte';

	type TooltipNoteProps = ComponentProps<typeof TooltipNote>;

	const { Story } = defineMeta({
		title: 'Leaf/TooltipNote',
		component: TooltipNote,
		tags: ['autodocs'],
		parameters: { layout: 'centered' }
	});
</script>

<!-- TooltipNote is a static leaf — no trigger needed. Rendered inside a dark
     surface container matching the tooltip body context where it is used. -->
<Story
	name="With Text"
	play={async ({ canvas }) => {
		await waitFor(() =>
			expect(canvas.getByText('higher is better - threshold 60')).toBeVisible()
		);
	}}
>
	{#snippet template()}
		<div style="padding: 4rem 6rem; background: #1a1a1a; border-radius: 8px;">
			<TooltipNote>higher is better - threshold 60</TooltipNote>
		</div>
	{/snippet}
</Story>
