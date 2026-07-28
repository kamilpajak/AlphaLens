<script module lang="ts">
	import type { ComponentProps } from 'svelte';
	import { defineMeta } from '@storybook/addon-svelte-csf';
	import { expect, within } from 'storybook/test';
	import EdgeCompletenessBanner from './EdgeCompletenessBanner.svelte';

	type BannerProps = ComponentProps<typeof EdgeCompletenessBanner>;

	// Fixed literal so relative-time rendering is deterministic under headless
	// capture — never Date.now(). Far enough in the past to always read "d ago".
	const FIXED_ENRICHED_AT = '2026-01-01T06:30:00Z';

	const { Story } = defineMeta({
		title: 'Edge/EdgeCompletenessBanner',
		component: EdgeCompletenessBanner,
		tags: ['autodocs'],
		parameters: { layout: 'padded' }
	});
</script>

<!-- All matured terminals have benchmark coverage: N === M. -->
<Story
	name="Complete"
	args={{ enrichedAt: FIXED_ENRICHED_AT, nTerminal: 118, nMatured: 118 } satisfies BannerProps}
	play={async ({ canvasElement }) => {
		const canvas = within(canvasElement);
		await expect(canvas.getByText('118 / 118')).toBeVisible();
	}}
/>

<!-- The real edge-summary.json fixture shape: n_terminal=121, n_matured=118 —
     3 terminal rows still lack a finite market_excess_return. -->
<Story
	name="Partial"
	args={{ enrichedAt: FIXED_ENRICHED_AT, nTerminal: 121, nMatured: 118 } satisfies BannerProps}
	play={async ({ canvasElement }) => {
		const canvas = within(canvasElement);
		await expect(canvas.getByText('118 / 121')).toBeVisible();
	}}
/>

<!-- No watermarked enrichment run yet — "last computed —". The em dash sits in
     its own nested `whitespace-nowrap` span, so DOM Testing Library's default
     getByText (which only concatenates an element's DIRECT text-node
     children) can never match the combined "last computed —" string — use a
     function matcher against the full normalized textContent instead. -->
<Story
	name="NoWatermark"
	args={{ enrichedAt: null, nTerminal: 121, nMatured: 118 } satisfies BannerProps}
	play={async ({ canvasElement }) => {
		const canvas = within(canvasElement);
		await expect(
			canvas.getByText(
				(_, el) => el?.textContent?.replace(/\s+/g, ' ').trim() === 'last computed —'
			)
		).toBeVisible();
	}}
/>

<!-- No closed positions yet (nTerminal 0) — avoid a bare "0 / 0". -->
<Story
	name="NoClosedYet"
	args={{ enrichedAt: FIXED_ENRICHED_AT, nTerminal: 0, nMatured: 0 } satisfies BannerProps}
	play={async ({ canvasElement }) => {
		const canvas = within(canvasElement);
		await expect(canvas.getByText('no closed positions yet')).toBeVisible();
	}}
/>
