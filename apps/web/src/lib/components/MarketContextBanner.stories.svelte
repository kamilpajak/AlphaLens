<script module lang="ts">
	import type { ComponentProps } from 'svelte';
	import { defineMeta } from '@storybook/addon-svelte-csf';
	import { expect, waitFor } from 'storybook/test';
	import MarketContextBanner from './MarketContextBanner.svelte';

	type BannerProps = ComponentProps<typeof MarketContextBanner>;

	// Click the "axes" toggle so the trend/vol/vix/squeeze breakdown is visible
	// for review + asserted present. The toggle is a real <button>, so a click
	// is deterministic under headless capture.
	const openAxes = async ({ canvas }: { canvas: any }) => {
		canvas.getByRole('button', { name: /axes/i }).click();
		await waitFor(() => expect(canvas.getByText(/unvalidated · context-only/i)).toBeTruthy());
	};

	const { Story } = defineMeta({
		title: 'Briefs/MarketContextBanner',
		component: MarketContextBanner,
		tags: ['autodocs'],
		parameters: { layout: 'padded' },
		render: template
	});
</script>

{#snippet template(args: BannerProps)}
	<div style="padding: 1.5rem;">
		<MarketContextBanner {...args} />
	</div>
{/snippet}

<!-- The four regime states, each with representative telemetry. Tone is
     display-only (green / amber / red / red-dim); never a buy/avoid signal. -->
<Story
	name="bull-quiet (green)"
	args={{
		marketState: 'bull_quiet',
		atrPctQ: 0.28,
		dist200: 0.043,
		vix: 13.4,
		vixDecile: 0.18,
		squeezeOn: true
	}}
/>

<Story
	name="bull-volatile (amber)"
	args={{
		marketState: 'bull_volatile',
		atrPctQ: 0.82,
		dist200: 0.021,
		vix: 22.1,
		vixDecile: 0.71,
		squeezeOn: false
	}}
/>

<Story
	name="bear-volatile (red)"
	args={{
		marketState: 'bear_volatile',
		atrPctQ: 0.91,
		dist200: -0.064,
		vix: 31.8,
		vixDecile: 0.94,
		squeezeOn: false
	}}
/>

<Story
	name="bear-quiet (muted red)"
	args={{
		marketState: 'bear_quiet',
		atrPctQ: 0.34,
		dist200: -0.028,
		vix: 17.9,
		vixDecile: 0.42,
		squeezeOn: true
	}}
/>

<!-- Dates that predate the signal (or an incomplete input window) — the
     first-class `unknown` state, muted, telemetry all null → em-dashes. -->
<Story
	name="unknown (muted)"
	args={{
		marketState: 'unknown',
		atrPctQ: null,
		dist200: null,
		vix: null,
		vixDecile: null,
		squeezeOn: null
	}}
/>

<!-- Axes expanded: the optional trend/vol/vix/squeeze breakdown + the
     unvalidated label. -->
<Story
	name="axes expanded"
	args={{
		marketState: 'bull_quiet',
		atrPctQ: 0.28,
		dist200: 0.043,
		vix: 13.4,
		vixDecile: 0.18,
		squeezeOn: true
	}}
	play={openAxes}
/>
