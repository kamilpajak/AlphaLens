<script module lang="ts">
	import type { ComponentProps } from 'svelte';
	import { defineMeta } from '@storybook/addon-svelte-csf';
	import { expect, waitFor } from 'storybook/test';
	import ChipTip from './ChipTip.svelte';
	import MetricGrid from './MetricGrid.svelte';

	type ChipTipProps = ComponentProps<typeof ChipTip>;

	// Force the bubble open for visual review. Focusing the trigger drives
	// `group-focus-within/chip:opacity-100` on TooltipBubble (ChipTip uses the
	// NAMED `group/chip`, not the anonymous `group`) — deterministic under
	// headless capture, unlike :hover.
	const openOnMount = async ({ canvas }: { canvas: any }) => {
		canvas.getByTestId('chip-tip').focus();
	};

	const { Story } = defineMeta({
		title: 'Tooltips/ChipTip',
		component: ChipTip,
		tags: ['autodocs'],
		parameters: { layout: 'centered' },
		render: template
	});
</script>

<!-- ChipTip ALWAYS needs a `chip` snippet (the visible badge); snippet props
     can't ride in plain `args`, so every Story supplies the slot inline. The
     default `template` covers the args-only `args.chip` path the autodocs
     control would exercise; the per-Story templates below pass real chips.
     TooltipBubble is position:absolute bottom-full (opens ABOVE the trigger);
     the wrapper padding gives the open bubble headroom so it isn't clipped. -->
{#snippet template(args: ChipTipProps)}
	<div style="padding: 6rem 8rem 3rem;">
		<ChipTip {...args}>
			{#snippet chip()}
				<span
					class="inline-flex items-center gap-1 px-1.5 py-0.5 bg-amber/20 text-amber text-[9px] uppercase tracking-widest border border-amber/40 cursor-help"
				>
					{args.term}
				</span>
			{/snippet}
		</ChipTip>
	</div>
{/snippet}

<!-- (1) Plain-text body chip — a bordered REVERSAL-style badge. -->
<Story
	name="Plain body"
	args={{
		term: 'REVERSAL pattern',
		body: 'Deep-drawdown-reversal: ≥30% off 52-week high + fresh thematic catalyst (news URL present) + volume z-score ≥ +2σ. Heuristic — not validated alpha; use as decision-support signal.'
	}}
	play={openOnMount}
/>

<!-- Canonical "verify it renders" story: force-open + assert the plain body is
     actually on screen (aria-describedby is set unconditionally, so it is NOT a
     valid open-state assertion — must waitFor past the opacity transition). -->
<Story
	name="Plain body (forced)"
	args={{
		term: 'sic-3 cohort',
		body: 'Peer set is the 3-digit SIC industry cohort. Thin cohorts widen the percentile bands; read the rank as directional, not precise.'
	}}
	play={async ({ canvas }) => {
		canvas.getByTestId('chip-tip').focus();
		await waitFor(() =>
			expect(canvas.getByText(/Peer set is the 3-digit SIC/)).toBeVisible()
		);
	}}
/>

<!-- (2) bodyRich = a right-aligned MetricGrid — the buffett-quality chip. -->
<Story name="Rich body (buffett MetricGrid)" play={openOnMount}>
	{#snippet template()}
		<div style="padding: 6rem 8rem 3rem;">
			<ChipTip term="buffett quality">
				{#snippet chip()}
					<span class="inline-flex items-baseline gap-1.5 whitespace-nowrap cursor-help">
						<span class="text-[9px] uppercase tracking-widest text-fg-muted">buffett</span>
						<span class="text-xs font-bold text-green">64</span>
					</span>
				{/snippet}
				{#snippet bodyRich()}
					<MetricGrid
						align="right"
						rows={[
							{ key: 'owner-earnings yield', value: '6.4%' },
							{ key: 'ROIC', value: '18.2%' },
							{ key: 'FCF margin', value: '21.0%' },
							{ key: 'margin of safety', value: '+12%' }
						]}
					/>
				{/snippet}
			</ChipTip>
		</div>
	{/snippet}
</Story>

<!-- Force-open variant of the MetricGrid body: assert a grid row is visible. -->
<Story name="Rich body (forced)" play={async ({ canvas }) => {
		canvas.getByTestId('chip-tip').focus();
		await waitFor(() => expect(canvas.getByText('owner-earnings yield')).toBeVisible());
	}}>
	{#snippet template()}
		<div style="padding: 6rem 8rem 3rem;">
			<ChipTip term="o'neil momentum">
				{#snippet chip()}
					<span class="inline-flex items-baseline gap-1.5 whitespace-nowrap cursor-help">
						<span class="text-[9px] uppercase tracking-widest text-fg-muted">o'neil</span>
						<span class="text-xs font-bold text-amber">35</span>
					</span>
				{/snippet}
				{#snippet bodyRich()}
					<MetricGrid
						align="right"
						rows={[
							{ key: 'owner-earnings yield', value: '6.4%' },
							{ key: 'ROIC', value: '18.2%' }
						]}
					/>
				{/snippet}
			</ChipTip>
		</div>
	{/snippet}
</Story>
