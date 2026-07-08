<script module lang="ts">
	import type { ComponentProps } from 'svelte';
	import { defineMeta } from '@storybook/addon-svelte-csf';
	import { expect, waitFor } from 'storybook/test';
	import TemplateFacts from './TemplateFacts.svelte';

	type TemplateFactsProps = ComponentProps<typeof TemplateFacts>;

	const { Story } = defineMeta({
		title: 'Leaf/TemplateFacts',
		component: TemplateFacts,
		tags: ['autodocs'],
		parameters: { layout: 'centered' },
		render: template
	});
</script>

{#snippet template(args: TemplateFactsProps)}
	<div style="padding: 4rem 6rem;">
		<TemplateFacts {...args} />
	</div>
{/snippet}

<Story
	name="M and A press release"
	args={{
		templateId: 'm_and_a_press_release',
		facts: {
			acquirer: 'Broadcom Inc.',
			deal_value_usd: 69000000000,
			target: 'VMware Inc.'
		}
	}}
	play={async ({ canvas }) => {
		await waitFor(() => expect(canvas.getByTestId('template-facts')).toBeInTheDocument());
		await waitFor(() => expect(canvas.getByText('acquirer')).toBeVisible());
		await waitFor(() => expect(canvas.getByText('$69.00B')).toBeVisible());
		canvas.getByRole('group').focus();
		await waitFor(() =>
			expect(canvas.getByText(/m_and_a_press_release/)).toBeVisible()
		);
	}}
/>

<Story
	name="Null facts renders nothing"
	args={{
		templateId: null,
		facts: null
	}}
	play={async ({ canvas }) => {
		await waitFor(() =>
			expect(canvas.queryByTestId('template-facts')).not.toBeInTheDocument()
		);
	}}
/>
