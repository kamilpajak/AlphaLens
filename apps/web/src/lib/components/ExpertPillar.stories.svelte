<script module lang="ts">
	import type { ComponentProps } from 'svelte';
	import { defineMeta } from '@storybook/addon-svelte-csf';
	import { expect, waitFor } from 'storybook/test';
	import ExpertPillar from './ExpertPillar.svelte';

	type ExpertPillarProps = ComponentProps<typeof ExpertPillar>;

	// ExpertPillar exposes no data-testid; the focusable trigger is the outer
	// wrapper rendered with role="group" (tabindex=0). Focusing it flips the
	// group-focus-within opacity on the TooltipBubble so the popover shows.
	const openTip = async ({ canvas }: { canvas: any }, body: string) => {
		canvas.getByRole('group').focus();
		await waitFor(() => expect(canvas.getByText(body)).toBeVisible());
	};

	const { Story } = defineMeta({
		title: 'Tooltips/ExpertPillar',
		component: ExpertPillar,
		tags: ['autodocs'],
		parameters: { layout: 'centered' },
		render: template
	});
</script>

{#snippet template(args: ExpertPillarProps)}
	<div style="padding: 6rem 8rem 3rem;">
		<ExpertPillar {...args} />
	</div>
{/snippet}

<Story
	name="Good"
	args={{
		label: 'moat',
		value: 'brand',
		tone: 'good',
		body: 'Durable brand moat: pricing power and customer captivity protect returns on capital.'
	}}
	play={async (ctx) =>
		openTip(ctx, 'Durable brand moat: pricing power and customer captivity protect returns on capital.')}
/>

<Story
	name="Mixed"
	args={{
		label: 'trend',
		value: 'flat',
		tone: 'mixed',
		body: 'Margins and returns are stable but not improving — neither widening nor eroding.'
	}}
	play={async (ctx) =>
		openTip(ctx, 'Margins and returns are stable but not improving — neither widening nor eroding.')}
/>

<Story
	name="Bad"
	args={{
		label: 'moat',
		value: 'none',
		tone: 'bad',
		body: 'No identifiable competitive advantage; commodity economics, returns near cost of capital.'
	}}
	play={async (ctx) =>
		openTip(ctx, 'No identifiable competitive advantage; commodity economics, returns near cost of capital.')}
/>

<Story
	name="Muted"
	args={{
		label: 'candor',
		value: 'n/a',
		tone: 'muted',
		body: 'Not assessed — insufficient filing history to judge management candor.'
	}}
	play={async (ctx) =>
		openTip(ctx, 'Not assessed — insufficient filing history to judge management candor.')}
/>
