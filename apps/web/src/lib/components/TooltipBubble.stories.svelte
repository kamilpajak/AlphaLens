<script module lang="ts">
	import type { ComponentProps } from 'svelte';
	import { defineMeta } from '@storybook/addon-svelte-csf';
	import { expect, waitFor } from 'storybook/test';
	import TooltipBubble from './TooltipBubble.svelte';

	type TooltipBubbleProps = ComponentProps<typeof TooltipBubble>;

	const { Story } = defineMeta({
		title: 'Leaf/TooltipBubble',
		component: TooltipBubble,
		tags: ['autodocs'],
		parameters: { layout: 'centered' }
	});

	// TooltipBubble is chrome-only: it opens via the ancestor's group-hover /
	// group-focus-within. The trigger span carries tabindex/role="group" purely so
	// the headless play() can focus it — the a11y tabindex hint is expected here.
	const openGroup = async ({ canvas }: { canvas: any }, header: string) => {
		canvas.getByRole('group').focus();
		await waitFor(() => expect(canvas.getByText(header)).toBeVisible());
	};
</script>

<!-- Plain group, above (default placement) -->
<Story name="Plain Above" play={async (ctx) => openGroup(ctx, 'plain above')}>
	{#snippet template()}
		<div style="padding: 8rem 10rem 4rem;">
			<!-- svelte-ignore a11y_no_noninteractive_tabindex -->
			<span class="group relative inline-block" tabindex="0" role="group">
				<span class="underline cursor-default text-sm text-fg-dim">hover or focus me</span>
				<TooltipBubble group="plain" placement="above">
					{#snippet header()}
						plain above
					{/snippet}
					{#snippet children()}
						<span class="block">Tooltip opens above the trigger on hover or focus.</span>
					{/snippet}
				</TooltipBubble>
			</span>
		</div>
	{/snippet}
</Story>

<!-- Plain group, below -->
<Story name="Plain Below" play={async (ctx) => openGroup(ctx, 'plain below')}>
	{#snippet template()}
		<div style="padding: 4rem 10rem 8rem;">
			<!-- svelte-ignore a11y_no_noninteractive_tabindex -->
			<span class="group relative inline-block" tabindex="0" role="group">
				<span class="underline cursor-default text-sm text-fg-dim">hover or focus me</span>
				<TooltipBubble group="plain" placement="below">
					{#snippet header()}
						plain below
					{/snippet}
					{#snippet children()}
						<span class="block"
							>Tooltip opens below the trigger — use for triggers near the top of the viewport.</span
						>
					{/snippet}
				</TooltipBubble>
			</span>
		</div>
	{/snippet}
</Story>

<!-- Chip group, above — named group variant used by ChipTip -->
<Story name="Chip Above" play={async (ctx) => openGroup(ctx, 'chip above')}>
	{#snippet template()}
		<div style="padding: 8rem 10rem 4rem;">
			<!-- svelte-ignore a11y_no_noninteractive_tabindex -->
			<span class="group/chip relative inline-block" tabindex="0" role="group">
				<span class="underline cursor-default text-sm text-fg-dim">chip trigger</span>
				<TooltipBubble group="chip" placement="above">
					{#snippet header()}
						chip above
					{/snippet}
					{#snippet children()}
						<span class="block"
							>Named group/chip — only this bubble opens, even when nested inside another group
							context.</span
						>
					{/snippet}
				</TooltipBubble>
			</span>
		</div>
	{/snippet}
</Story>

<!-- Chip group, below -->
<Story name="Chip Below" play={async (ctx) => openGroup(ctx, 'chip below')}>
	{#snippet template()}
		<div style="padding: 4rem 10rem 8rem;">
			<!-- svelte-ignore a11y_no_noninteractive_tabindex -->
			<span class="group/chip relative inline-block" tabindex="0" role="group">
				<span class="underline cursor-default text-sm text-fg-dim">chip trigger below</span>
				<TooltipBubble group="chip" placement="below">
					{#snippet header()}
						chip below
					{/snippet}
					{#snippet children()}
						<span class="block">Named group/chip variant opening below the trigger.</span>
					{/snippet}
				</TooltipBubble>
			</span>
		</div>
	{/snippet}
</Story>

<!-- Rich body content (lists + multiple lines) -->
<Story name="Rich Body" play={async (ctx) => openGroup(ctx, 'signal breakdown')}>
	{#snippet template()}
		<div style="padding: 8rem 10rem 4rem;">
			<!-- svelte-ignore a11y_no_noninteractive_tabindex -->
			<span class="group relative inline-block" tabindex="0" role="group">
				<span class="underline cursor-default text-sm text-fg-dim">rich body</span>
				<TooltipBubble group="plain" placement="above">
					{#snippet header()}
						signal breakdown
					{/snippet}
					{#snippet children()}
						<span class="block">First line of body content.</span>
						<span class="block mt-1">Second line with additional detail.</span>
						<span class="block mt-1">Third line — rich callers can pass multiple children.</span>
					{/snippet}
				</TooltipBubble>
			</span>
		</div>
	{/snippet}
</Story>
