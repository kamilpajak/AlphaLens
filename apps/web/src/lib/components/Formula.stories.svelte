<script module lang="ts">
	import type { ComponentProps } from 'svelte';
	import { defineMeta } from '@storybook/addon-svelte-csf';
	import { expect, waitFor } from 'storybook/test';
	import Formula from './Formula.svelte';

	type FormulaProps = ComponentProps<typeof Formula>;

	const { Story } = defineMeta({
		title: 'Leaf/Formula',
		component: Formula,
		tags: ['autodocs'],
		parameters: { layout: 'centered' }
	});

	// Formula renders build-time Temml MathML into a <math> element. The play
	// asserts that element mounted (the named-formula happy path). canvasElement
	// is the story's DOM root — MathML is not reachable via testing-library's
	// text queries, so query the DOM directly.
	const assertMath = async ({ canvasElement }: { canvasElement: HTMLElement }) => {
		await waitFor(() => expect(canvasElement.querySelector('math')).not.toBeNull());
	};
</script>

<!-- margin_of_safety: two-term valuation formula with text labels — exercises the named-formula happy path -->
<Story name="Margin Of Safety" play={assertMath}>
	{#snippet template()}
		<div style="padding: 4rem 6rem;">
			<Formula name="margin_of_safety" />
		</div>
	{/snippet}
</Story>

<!-- risk_at_stop: inline fraction used in trade-setup tooltips -->
<Story name="Risk At Stop" play={assertMath}>
	{#snippet template()}
		<div style="padding: 4rem 6rem;">
			<Formula name="risk_at_stop" />
		</div>
	{/snippet}
</Story>

<!-- full_ladder_entry: weighted-average entry price — longest formula in the registry -->
<Story name="Full Ladder Entry" play={assertMath}>
	{#snippet template()}
		<div style="padding: 4rem 6rem;">
			<Formula name="full_ladder_entry" />
		</div>
	{/snippet}
</Story>
