<script module lang="ts">
	import type { ComponentProps } from 'svelte';
	import { defineMeta } from '@storybook/addon-svelte-csf';
	import { expect, waitFor } from 'storybook/test';
	import MarketSession from './MarketSession.svelte';
	import { marketStatus } from '$lib/marketStatus.svelte';

	type MarketSessionProps = ComponentProps<typeof MarketSession>;

	const { Story } = defineMeta({
		title: 'Leaf/MarketSession',
		component: MarketSession,
		tags: ['autodocs'],
		parameters: { layout: 'centered' }
	});
</script>

<!--
  Store-driven component: MarketSession reads the shared `marketStatus` rune
  export from marketStatus.svelte.ts and renders nothing until hasLoaded=true.
  Each play() seeds the store before assertions so the story is self-contained.
  The store is a plain $state object, so direct property mutation is reactive.

  NOTE: Because the store is module-singleton, stories share it. The play()
  in each story sets the required state before asserting, so story isolation
  holds as long as stories run sequentially (the default Storybook behaviour).
-->

<!-- Market open: green dot + "live" label + "closes in …" countdown. -->
<Story
	name="Open - US market live"
	play={async ({ canvas }) => {
		// Seed the store: open session, far-future close so countdown is non-zero.
		marketStatus.value = {
			is_trading_day: true,
			is_half_day: false,
			is_open_now: true,
			exchange: 'XNYS',
			// 6 hours from a fixed epoch so formatCountdown yields a stable "Xh Ym"
			next_close_iso: new Date(Date.now() + 6 * 60 * 60 * 1000).toISOString(),
			next_open_iso: new Date(Date.now() + 24 * 60 * 60 * 1000).toISOString()
		};
		marketStatus.hasLoaded = true;

		await waitFor(() => expect(canvas.getByTestId('market-session')).toBeInTheDocument());
		await waitFor(() => expect(canvas.getByText('live')).toBeVisible());
		// The XNYS MIC surfaces as the human label "US MARKET" (via marketLabel).
		await waitFor(() => expect(canvas.getByText('US MARKET')).toBeVisible());
	}}
>
	{#snippet template()}
		<div style="padding: 4rem 6rem;">
			<MarketSession />
		</div>
	{/snippet}
</Story>

<!-- Market closed: static dot + "closed" label + next-open label. -->
<Story
	name="Closed - next open pending"
	play={async ({ canvas }) => {
		// Seed the store: market is closed, opens tomorrow morning ET (~14:30 UTC).
		marketStatus.value = {
			is_trading_day: false,
			is_half_day: false,
			is_open_now: false,
			exchange: 'XNYS',
			next_close_iso: new Date(Date.now() + 24 * 60 * 60 * 1000).toISOString(),
			next_open_iso: new Date(Date.now() + 14 * 60 * 60 * 1000).toISOString()
		};
		marketStatus.hasLoaded = true;

		await waitFor(() => expect(canvas.getByTestId('market-session')).toBeInTheDocument());
		await waitFor(() => expect(canvas.getByText('closed')).toBeVisible());
		await waitFor(() => expect(canvas.getByText('US MARKET')).toBeVisible());
	}}
>
	{#snippet template()}
		<div style="padding: 4rem 6rem;">
			<MarketSession />
		</div>
	{/snippet}
</Story>

<!-- Half-day open: same as open but is_half_day=true — the amber "1/2" badge
     appears (visible only on lg+; the story documents the data path). -->
<Story
	name="Open - half day"
	play={async ({ canvas }) => {
		marketStatus.value = {
			is_trading_day: true,
			is_half_day: true,
			is_open_now: true,
			exchange: 'XNYS',
			next_close_iso: new Date(Date.now() + 2 * 60 * 60 * 1000).toISOString(),
			next_open_iso: new Date(Date.now() + 26 * 60 * 60 * 1000).toISOString()
		};
		marketStatus.hasLoaded = true;

		await waitFor(() => expect(canvas.getByText('live')).toBeVisible());
	}}
>
	{#snippet template()}
		<div style="padding: 4rem 6rem;">
			<MarketSession />
		</div>
	{/snippet}
</Story>

<!-- Not loaded yet: hasLoaded=false — component renders nothing (fail-silent). -->
<Story
	name="Not loaded - renders nothing"
	play={async ({ canvas }) => {
		// Reset to the unloaded state so the component renders nothing.
		marketStatus.value = null;
		marketStatus.hasLoaded = false;

		// The sentinel span must not be in the DOM.
		await waitFor(() =>
			expect(canvas.queryByTestId('market-session')).not.toBeInTheDocument()
		);
	}}
>
	{#snippet template()}
		<div style="padding: 4rem 6rem;">
			<MarketSession />
		</div>
	{/snippet}
</Story>
