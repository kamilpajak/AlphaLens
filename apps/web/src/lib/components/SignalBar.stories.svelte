<script module lang="ts">
	import type { ComponentProps } from 'svelte';
	import { defineMeta } from '@storybook/addon-svelte-csf';
	import { expect, waitFor } from 'storybook/test';
	import SignalBar from './SignalBar.svelte';
	import MetricGrid from './MetricGrid.svelte';
	import BulletList from './BulletList.svelte';

	type SignalBarProps = ComponentProps<typeof SignalBar>;

	// Force the bubble open for visual review + assertion. The bar wrapper is
	// `tabindex=0` only when a tooltip is present, and focusing it drives
	// `group-focus-within:opacity-100` on TooltipBubble — deterministic under
	// headless capture, unlike :hover.
	const openOnMount = async ({ canvas }: { canvas: any }) => {
		canvas.getByTestId('signal-bar').focus();
	};

	const { Story } = defineMeta({
		title: 'Tooltips/SignalBar',
		component: SignalBar,
		tags: ['autodocs'],
		parameters: { layout: 'centered' },
		render: template
	});
</script>

<!-- TooltipBubble is position:absolute bottom-full (opens ABOVE the trigger);
     the wrapper padding gives the open bubble headroom so it isn't clipped.
     The fixed width keeps the bar (and its percentile fill) at a realistic
     card-column size. -->
{#snippet template(args: SignalBarProps)}
	<div style="padding: 6rem 8rem 3rem;">
		<div style="width: 16rem;">
			<SignalBar {...args} />
		</div>
	</div>
{/snippet}

<!-- (1) Plain string tooltip: an insider-percentile bar. value 0–100, the
     default range, formatted as a whole-number percentile. -->
<Story
	name="Plain tooltip (insider percentile)"
	args={{
		label: 'Insider D90',
		value: 82,
		format: (v: number) => `${v.toFixed(0)}th`,
		tooltip:
			'Cluster-buy percentile vs all S&P 1500 issuers over the trailing 90 days. Higher = more unusual recent insider buying.'
	}}
	play={async ({ canvas }: { canvas: any }) => {
		canvas.getByTestId('signal-bar').focus();
		await waitFor(() =>
			expect(canvas.getByText(/Cluster-buy percentile/)).toBeVisible()
		);
	}}
/>

<!-- Muted / no-data state: null value renders the placeholder + an empty bar.
     The plain tooltip still opens (an honest "no buys" explanation). -->
<Story
	name="Plain tooltip (no data)"
	args={{
		label: 'Insider D90',
		value: null,
		placeholder: 'no buys',
		tooltip: 'No qualifying insider purchases in the trailing 90-day window.'
	}}
	play={openOnMount}
/>

<!-- (1b) subValue annotation: the FCFF-yield bar shows the raw yield (dimmed,
     normal weight) just left of the tone-coloured sector-%ile. The %ile stays
     right-anchored so a column of stacked bars keeps a flush right edge. -->
<Story
	name="subValue (FCFF raw yield + sector %ile)"
	args={{
		label: 'FCFF yield (sector %ile)',
		value: 58,
		subValue: '+8.36%',
		format: (v: number) => `${v.toFixed(0)}%ile`,
		tooltip:
			'Free-cash-flow-to-firm yield = FCFF / EV, ranked within sector. Higher = cheaper on a cash-generation basis.'
	}}
	play={async ({ canvas }: { canvas: any }) => {
		await waitFor(() => expect(canvas.getByText('+8.36%')).toBeVisible());
		await expect(canvas.getByText('58%ile')).toBeVisible();
	}}
/>

<!-- (2) Rich tooltip via MetricGrid (align="left"): RSI-style
     threshold -> meaning rows. value 0–100, inverted so a hot RSI reads red. -->
<Story name="Rich tooltip (MetricGrid / RSI thresholds)" play={openOnMount}>
	{#snippet template()}
		<div style="padding: 6rem 8rem 3rem;">
			<div style="width: 16rem;">
				<SignalBar label="RSI(14)" value={72} inverted format={(v) => v.toFixed(0)}>
					{#snippet tooltipRich()}
						<span class="block mb-1">14-day relative strength index.</span>
						<MetricGrid
							align="left"
							rows={[
								{ key: '< 30', value: 'oversold' },
								{ key: '30–70', value: 'neutral' },
								{ key: '> 70', value: 'overbought' }
							]}
						/>
					{/snippet}
				</SignalBar>
			</div>
		</div>
	{/snippet}
</Story>

<!-- Same MetricGrid path, force-open + assert a threshold row is visible. -->
<Story
	name="Rich tooltip (MetricGrid, forced)"
	play={async ({ canvas }: { canvas: any }) => {
		canvas.getByTestId('signal-bar').focus();
		await waitFor(() => expect(canvas.getByText('oversold')).toBeVisible());
	}}
>
	{#snippet template()}
		<div style="padding: 6rem 8rem 3rem;">
			<div style="width: 16rem;">
				<SignalBar label="RSI(14)" value={72} inverted format={(v) => v.toFixed(0)}>
					{#snippet tooltipRich()}
						<span class="block mb-1">14-day relative strength index.</span>
						<MetricGrid
							align="left"
							rows={[
								{ key: '< 30', value: 'oversold' },
								{ key: '> 70', value: 'overbought' }
							]}
						/>
					{/snippet}
				</SignalBar>
			</div>
		</div>
	{/snippet}
</Story>

<!-- (3) Rich tooltip via BulletList: the multiples feeding a valuation
     composite. value 0–100 percentile (higher = cheaper composite). -->
<Story name="Rich tooltip (BulletList / valuation composite)" play={openOnMount}>
	{#snippet template()}
		<div style="padding: 6rem 8rem 3rem;">
			<div style="width: 16rem;">
				<SignalBar label="Valuation composite" value={64} format={(v) => `${v.toFixed(0)}th`}>
					{#snippet tooltipRich()}
						<span class="block mb-1">Cross-sectional percentile blending:</span>
						<BulletList items={['PE', 'PS', 'EV / Revenue']} />
					{/snippet}
				</SignalBar>
			</div>
		</div>
	{/snippet}
</Story>

<!-- Same BulletList path, force-open + assert a component name is visible. -->
<Story
	name="Rich tooltip (BulletList, forced)"
	play={async ({ canvas }: { canvas: any }) => {
		canvas.getByTestId('signal-bar').focus();
		await waitFor(() => expect(canvas.getByText('EV / Revenue')).toBeVisible());
	}}
>
	{#snippet template()}
		<div style="padding: 6rem 8rem 3rem;">
			<div style="width: 16rem;">
				<SignalBar label="Valuation composite" value={64} format={(v) => `${v.toFixed(0)}th`}>
					{#snippet tooltipRich()}
						<span class="block mb-1">Cross-sectional percentile blending:</span>
						<BulletList items={['PE', 'PS', 'EV / Revenue']} />
					{/snippet}
				</SignalBar>
			</div>
		</div>
	{/snippet}
</Story>
