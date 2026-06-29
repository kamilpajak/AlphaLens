<script module lang="ts">
	// The hover/focus popover chrome shared by every tooltip in the app
	// (ChipTip, JargonTip, GatePill, ExpertPillar, SignalBar). Before this
	// extraction the same ~10 lines of amber-bordered bubble + arrow markup
	// were copy-pasted byte-for-byte into all five components; a class tweak
	// meant editing five files and the Playwright CSS-regression guard could
	// only catch drift after the fact. The bubble now lives here once.
	//
	// What stays in each caller (NOT here): the focusable trigger wrapper
	// (`group/… relative inline-block`, tabindex, pointerdown→focus,
	// use:clampToViewport, aria-describedby) and the trigger's own visible
	// chip/badge/underline. This component renders ONLY the popover that the
	// wrapper reveals on hover/focus, so clampToViewport still finds its
	// `[role="tooltip"]` as a descendant of the unchanged wrapper.

	// Visibility is driven by Tailwind `group-*` modifiers on the bubble that
	// react to the caller's `group/…` wrapper. The modifier suffix must match
	// the wrapper's group name, so the full class strings live here as
	// literals (Tailwind v4 scans this file's text, so both variants are
	// generated even though only one is emitted at runtime). ChipTip uses a
	// NAMED group (`group/chip`) so a ChipTip nested inside another tooltip's
	// group context reveals only its own bubble; the other four use the
	// anonymous `group`.
	const VISIBILITY = {
		plain: 'group-hover:opacity-100 group-focus-within:opacity-100',
		chip: 'group-hover/chip:opacity-100 group-focus-within/chip:opacity-100'
	} as const;
</script>

<script lang="ts">
	import type { Snippet } from 'svelte';

	interface Props {
		/** Links the trigger's aria-describedby to this bubble. Omitted by
		 *  GatePill (its tooltip is conditional + unlinked, preserved as-is). */
		id?: string;
		/** Which wrapper group name reveals this bubble. */
		group?: keyof typeof VISIBILITY;
		/** Amber uppercase header line (term / label / "name // full"). */
		header: Snippet;
		/** Body content below the header. Plain callers pass a single
		 *  `<span class="block">…</span>`; rich callers pass lists / formulas. */
		children: Snippet;
	}

	let { id, group = 'plain', header, children }: Props = $props();
</script>

<span
	{id}
	class="pointer-events-none absolute bottom-full left-1/2 mb-2 w-[min(20rem,calc(100vw-2rem))] z-50 opacity-0 transition-opacity duration-150 {VISIBILITY[group]}"
	style="transform: translateX(calc(-50% + var(--tt-shift, 0px)))"
	role="tooltip"
>
	<span
		class="block border border-amber bg-surface-pop px-3 py-2 text-[11px] leading-snug text-fg-dim normal-case tracking-normal shadow-2xl"
	>
		<span class="block text-amber font-bold uppercase tracking-widest text-[10px] mb-1">
			{@render header()}
		</span>
		{@render children()}
	</span>
	<span
		class="absolute left-1/2 top-full w-2 h-2 border-r border-b border-amber bg-surface-pop -mt-1"
		style="transform: translateX(calc(-50% + var(--tt-arrow, 0px))) rotate(45deg)"
	></span>
</span>
