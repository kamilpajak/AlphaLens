<script lang="ts">
	// Shared native-<details> disclosure with the terminal chevron chrome that was
	// hand-rolled at 5 sites (/experiments ×4 + the ladder legend). Owns the
	// fiddly-to-get-right parts: `list-none [&::-webkit-details-marker]:hidden`
	// (so the native triangle never leaks) and the amber `▸` that rotates 90° when
	// open. The chevron rotates off a reactive `bind:open` instead of the brittle
	// per-site `group-open/NAME` scope, so external `details.open = true` (e.g. the
	// /experiments hash deep-link) still rotates it via the toggle event.
	//
	// The caller supplies the summary content (via the `summary` snippet, which
	// receives `open` so a "show ↔ hide" label can swap) and the body (`children`).
	// The chevron is rendered first inside the summary; put `ml-auto` on a trailing
	// span to push meta to the right (replaces the old justify-between layout).

	import type { Snippet } from 'svelte';

	interface Props {
		/** Open state; bindable. Also syncs from a programmatic `details.open`. */
		open?: boolean;
		/** Classes for the <details> wrapper. */
		detailsClass?: string;
		/** Classes for the <summary> — its flex layout / padding / colour. */
		summaryClass?: string;
		/** Chevron colour (+ any transition tweak); default amber. */
		chevronClass?: string;
		/** Summary content after the chevron; receives `open` (for show/hide swaps). */
		summary: Snippet<[boolean]>;
		/** Disclosure body. */
		children: Snippet;
		[key: string]: unknown;
	}

	let {
		open = $bindable(false),
		detailsClass = '',
		summaryClass = '',
		chevronClass = 'text-amber',
		summary,
		children,
		...rest
	}: Props = $props();
</script>

<details {...rest} bind:open class={detailsClass}>
	<summary
		class="cursor-pointer select-none list-none [&::-webkit-details-marker]:hidden {summaryClass}"
	>
		<span
			class="inline-block shrink-0 transition-transform {chevronClass}"
			class:rotate-90={open}
			aria-hidden="true">▸</span
		>
		{@render summary(open)}
	</summary>
	{@render children()}
</details>
