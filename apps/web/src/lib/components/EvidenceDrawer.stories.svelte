<script module lang="ts">
	import { defineMeta } from '@storybook/addon-svelte-csf';
	import { expect, waitFor } from 'storybook/test';
	import EvidenceDrawer from './EvidenceDrawer.svelte';

	// EvidenceDrawer has no props — it is opened imperatively via open(path).
	// Stories render the drawer alongside a trigger button. play() stubs
	// globalThis.fetch and calls the exposed open() method via the bound ref,
	// then asserts the expected drawer state is visible.

	const { Story } = defineMeta({
		title: 'Leaf/EvidenceDrawer',
		component: EvidenceDrawer,
		tags: ['autodocs'],
		parameters: { layout: 'padded' }
	});
</script>

<script lang="ts">
	import type EvidenceDrawerInstance from './EvidenceDrawer.svelte';

	// Instance-level reactive refs — one per story so each play() closes over the
	// correct bound component instance. Aliased type import avoids the name
	// collision with the value import in the module script above.
	let drawerContent = $state<EvidenceDrawerInstance | null>(null);
	let drawerLoading = $state<EvidenceDrawerInstance | null>(null);
	let drawerError = $state<EvidenceDrawerInstance | null>(null);
</script>

<!--
  "Opens with content" — stubs fetch to return a small HTML body, then calls
  open(). Asserts the dialog, the path header, and the fetched text are visible.
-->
<Story
	name="Opens with content"
	play={async ({ canvas }) => {
		const originalFetch = globalThis.fetch;
		globalThis.fetch = async (_url: RequestInfo | URL) =>
			({ ok: true, status: 200, text: async () => '<p>evidence body</p>' }) as Response;

		try {
			const btn = canvas.getByRole('button', { name: /open evidence drawer/i });
			await waitFor(() => expect(btn).toBeVisible());

			btn.click();

			await waitFor(() =>
				expect(canvas.getByRole('dialog', { name: /evidence/i })).toBeVisible()
			);
			await waitFor(() => expect(canvas.getByText(/docs\/example\.md/)).toBeVisible());
			await waitFor(() => expect(canvas.getByText(/evidence body/)).toBeVisible());
		} finally {
			globalThis.fetch = originalFetch;
		}
	}}
>
	{#snippet template()}
		<div style="padding: 4rem 6rem; position: relative; min-height: 20rem;">
			<EvidenceDrawer bind:this={drawerContent} />
			<button
				type="button"
				aria-label="open evidence drawer"
				onclick={async () => {
					await drawerContent?.open('docs/example.md');
				}}
				class="px-4 py-2 text-sm border border-grid text-fg hover:text-amber transition-colors"
			>
				open evidence drawer
			</button>
		</div>
	{/snippet}
</Story>

<!--
  "Shows loading state" — never-resolving fetch keeps the drawer in its
  loading=true state so the "loading..." indicator is assertable.
-->
<Story
	name="Shows loading state"
	play={async ({ canvas }) => {
		const originalFetch = globalThis.fetch;
		globalThis.fetch = () => new Promise(() => {}) as Promise<Response>;

		try {
			const btn = canvas.getByRole('button', { name: /open evidence drawer/i });
			await waitFor(() => expect(btn).toBeVisible());

			btn.click();

			await waitFor(() =>
				expect(canvas.getByRole('dialog', { name: /evidence/i })).toBeVisible()
			);
			await waitFor(() => expect(canvas.getByText(/loading/)).toBeVisible());
		} finally {
			globalThis.fetch = originalFetch;
		}
	}}
>
	{#snippet template()}
		<div style="padding: 4rem 6rem; position: relative; min-height: 20rem;">
			<EvidenceDrawer bind:this={drawerLoading} />
			<button
				type="button"
				aria-label="open evidence drawer"
				onclick={() => {
					drawerLoading?.open('docs/example.md');
				}}
				class="px-4 py-2 text-sm border border-grid text-fg hover:text-amber transition-colors"
			>
				open evidence drawer
			</button>
		</div>
	{/snippet}
</Story>

<!--
  "Shows error state" — stubs fetch with a non-OK HTTP 404 response so the
  drawer renders its error message containing the status code.
-->
<Story
	name="Shows error state"
	play={async ({ canvas }) => {
		const originalFetch = globalThis.fetch;
		globalThis.fetch = async (_url: RequestInfo | URL) =>
			({ ok: false, status: 404, text: async () => 'Not Found' }) as Response;

		try {
			const btn = canvas.getByRole('button', { name: /open evidence drawer/i });
			await waitFor(() => expect(btn).toBeVisible());

			btn.click();

			await waitFor(() =>
				expect(canvas.getByRole('dialog', { name: /evidence/i })).toBeVisible()
			);
			await waitFor(() => expect(canvas.getByText(/HTTP 404/)).toBeVisible());
		} finally {
			globalThis.fetch = originalFetch;
		}
	}}
>
	{#snippet template()}
		<div style="padding: 4rem 6rem; position: relative; min-height: 20rem;">
			<EvidenceDrawer bind:this={drawerError} />
			<button
				type="button"
				aria-label="open evidence drawer"
				onclick={() => {
					drawerError?.open('docs/missing.md');
				}}
				class="px-4 py-2 text-sm border border-grid text-fg hover:text-amber transition-colors"
			>
				open evidence drawer
			</button>
		</div>
	{/snippet}
</Story>
