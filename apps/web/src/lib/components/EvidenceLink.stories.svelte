<script module lang="ts">
	import type { ComponentProps } from 'svelte';
	import { defineMeta } from '@storybook/addon-svelte-csf';
	import { expect, fn, waitFor } from 'storybook/test';
	import EvidenceLink from './EvidenceLink.svelte';

	type EvidenceLinkProps = ComponentProps<typeof EvidenceLink>;

	const { Story } = defineMeta({
		title: 'Primitives/EvidenceLink',
		component: EvidenceLink,
		tags: ['autodocs'],
		parameters: { layout: 'centered' },
		render: template
	});
</script>

{#snippet template(args: EvidenceLinkProps)}
	<div style="padding: 4rem 6rem;">
		<EvidenceLink {...args} />
	</div>
{/snippet}

<Story
	name="Short Path"
	args={{ path: 'docs/backtest/pead_v2.md', onopen: () => {} }}
	play={async ({ canvas }) => {
		await waitFor(() =>
			expect(canvas.getByRole('button', { name: /open evidence/ })).toBeVisible()
		);
		await waitFor(() =>
			expect(canvas.getByText(/docs\/backtest\/pead_v2\.md/)).toBeVisible()
		);
	}}
/>

<Story
	name="Long Path"
	args={{
		path: 'docs/research/paradigm_failures_postmortem.md',
		onopen: () => {}
	}}
	play={async ({ canvas }) => {
		await waitFor(() =>
			expect(
				canvas.getByText(/docs\/research\/paradigm_failures_postmortem\.md/)
			).toBeVisible()
		);
	}}
/>

<Story
	name="Click Fires Handler"
	args={{ path: 'docs/adr/0007-layer-architecture.md', onopen: fn() }}
	play={async ({ canvas, args }) => {
		const btn = canvas.getByRole('button', { name: /open evidence/ });
		await waitFor(() => expect(btn).toBeVisible());
		btn.click();
		await waitFor(() => expect(args.onopen).toHaveBeenCalledWith('docs/adr/0007-layer-architecture.md'));
	}}
/>
