<script module lang="ts">
	import type { ComponentProps } from 'svelte';
	import { defineMeta } from '@storybook/addon-svelte-csf';
	import MetricGrid from './MetricGrid.svelte';

	type MetricGridProps = ComponentProps<typeof MetricGrid>;

	const { Story } = defineMeta({
		title: 'Tooltips/Parts/MetricGrid',
		component: MetricGrid,
		tags: ['autodocs'],
		parameters: { layout: 'fullscreen' },
		render: template
	});
</script>

{#snippet template(args: MetricGridProps)}
	<div style="padding: 6rem 8rem 3rem;" class="bg-bg">
		<div class="max-w-xs rounded border border-border bg-surface p-3 text-sm text-muted shadow-lg">
			<MetricGrid {...args} />
		</div>
	</div>
{/snippet}

<Story
	name="Label to value (right)"
	args={{
		align: 'right',
		rows: [
			{ key: 'P/E', value: '18.4×' },
			{ key: 'P/S', value: '3.1×' },
			{ key: 'EV / Rev', value: '2.7×' },
			{ key: 'EV / EBITDA', value: '11.9×' },
			{ key: 'ROE', value: '24.6%' },
			{ key: 'FCF margin', value: '17.2%' }
		]
	}}
/>

<Story
	name="Threshold to meaning (left)"
	args={{
		align: 'left',
		rows: [
			{ key: '< 30', value: 'oversold — stretched to the downside' },
			{ key: '30 – 50', value: 'weak but recovering off the lows' },
			{ key: '50 – 70', value: 'healthy uptrend, room to run' },
			{ key: '> 70', value: 'overbought — extended, prone to a pullback' }
		]
	}}
/>
