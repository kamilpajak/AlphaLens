<script module lang="ts">
	import type { ComponentProps } from 'svelte';
	import { defineMeta } from '@storybook/addon-svelte-csf';
	import { expect, waitFor } from 'storybook/test';
	import JargonTip from './JargonTip.svelte';

	type JargonTipProps = ComponentProps<typeof JargonTip>;

	// Force the bubble open for visual review. Focusing the trigger drives
	// `group-focus-within:opacity-100` on TooltipBubble — deterministic under
	// headless capture, unlike :hover.
	const openOnMount = async ({ canvas }: { canvas: any }) => {
		canvas.getByTestId('jargon-tip').focus();
	};

	const { Story } = defineMeta({
		title: 'Tooltips/JargonTip',
		component: JargonTip,
		tags: ['autodocs'],
		parameters: { layout: 'centered' },
		render: template
	});
</script>

<!-- TooltipBubble is position:absolute bottom-full (opens ABOVE the trigger);
     the wrapper padding gives the open bubble headroom so it isn't clipped. -->
{#snippet template(args: JargonTipProps)}
	<div style="padding: 6rem 8rem 3rem;">
		<p style="max-width: 22rem; line-height: 1.7;">
			The screen ranks each name on
			<JargonTip {...args}>
				{#snippet children()}{args.term}{/snippet}
			</JargonTip>
			before the gate runs.
		</p>
	</div>
{/snippet}

<Story
	name="Plain"
	args={{
		term: 'αt',
		full: 't-statistic on Carhart-4F α',
		body: 'Measures how reliably the strategy beats the 4-factor model.'
	}}
	play={openOnMount}
/>

<!-- formula key MUST exist in src/lib/formulas.json (pe, ps, roe, ev_ebitda, …). -->
<Story
	name="With formula"
	args={{
		term: 'PE',
		full: 'price / earnings',
		body: 'Share price divided by trailing 12-month EPS.',
		formula: 'pe'
	}}
	play={openOnMount}
/>

<Story
	name="Formula + bands"
	args={{
		term: 'PS',
		full: 'price / sales',
		body: 'Market cap divided by trailing revenue.',
		formula: 'ps',
		bands: [
			{ range: '< 1', label: 'cheap' },
			{ range: '1–4', label: 'normal' },
			{ range: '> 8', label: 'rich' }
		]
	}}
	play={openOnMount}
/>

<!-- Canonical "verify it renders" story: force-open + assert the bands content
     is actually visible (aria-describedby is set unconditionally, so it is NOT
     a valid open-state assertion). -->
<Story
	name="Open (forced)"
	args={{
		term: 'PS',
		body: 'Market cap divided by trailing revenue.',
		formula: 'ps',
		bands: [
			{ range: '< 1', label: 'cheap' },
			{ range: '> 8', label: 'rich' }
		]
	}}
	play={async ({ canvas }) => {
		canvas.getByTestId('jargon-tip').focus();
		// Wait out the bubble's opacity transition (group-focus-within drives it)
		// before asserting the bands content is actually on screen.
		await waitFor(() => expect(canvas.getByText('cheap')).toBeVisible());
	}}
/>

<!-- bodyRich snippet takes precedence over body/formula/bands. -->
<Story name="Rich body" play={openOnMount}>
	{#snippet template()}
		<div style="padding: 6rem 8rem 3rem;">
			<p style="max-width: 22rem;">
				Defined as a
				<JargonTip term="composite score">
					{#snippet children()}composite score{/snippet}
					{#snippet bodyRich()}
						<span class="block">Weighted blend of three orthogonal lenses:</span>
						<ul class="mt-1 list-disc pl-4">
							<li>value (Buffett)</li>
							<li>momentum (O'Neil)</li>
							<li>insider flow (Form-4)</li>
						</ul>
					{/snippet}
				</JargonTip>
				before ranking.
			</p>
		</div>
	{/snippet}
</Story>
