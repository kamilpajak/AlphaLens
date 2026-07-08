<script module lang="ts">
	import type { ComponentProps } from 'svelte';
	import { defineMeta } from '@storybook/addon-svelte-csf';
	import { expect, waitFor } from 'storybook/test';
	import SessionExpiredCard from './SessionExpiredCard.svelte';

	type SessionExpiredCardProps = ComponentProps<typeof SessionExpiredCard>;

	const { Story } = defineMeta({
		title: 'Leaf/SessionExpiredCard',
		component: SessionExpiredCard,
		tags: ['autodocs'],
		parameters: { layout: 'centered' },
		render: template
	});
</script>

{#snippet template(args: SessionExpiredCardProps)}
	<div style="padding: 4rem 6rem;">
		<SessionExpiredCard {...args} />
	</div>
{/snippet}

<Story
	name="Default"
	play={async ({ canvas }) => {
		await waitFor(() =>
			expect(canvas.getByRole('heading', { name: 'session expired' })).toBeVisible()
		);
		await waitFor(() =>
			expect(
				canvas.getByText(
					/Your Cloudflare Access session has expired/
				)
			).toBeVisible()
		);
		await waitFor(() =>
			expect(canvas.getByRole('button', { name: 'retry' })).toBeVisible()
		);
	}}
/>
