<script lang="ts">
	// One /experiments ledger row — the shared `<article>` + header + collapsible
	// detail skeleton that the paradigm ledger and the tool.experiments ledger both
	// hand-rolled. Owns: the status-railed article wrapper, the header layout
	// (display numeral + name + optional tags + a right-aligned status/date
	// cluster), and the <Disclosure> that wraps the detail <dl>. The divergent
	// bits are snippets — `status` (the ChipTip + StatusPill), `tags` (paradigm
	// layer/axis, optional), `preface` (story + αt bars / metric + in-sample pill),
	// and `fields` (the <dl> rows, built from <DetailField>s).

	import type { Snippet } from 'svelte';
	import Disclosure from './Disclosure.svelte';

	interface Props {
		id: string;
		/** `statusRail(...)` left-edge class. */
		rail: string;
		/** Dashed rail (the tool ledger). */
		dashed?: boolean;
		/** Display numeral (`#1`, `T1`, …). */
		display: string;
		/** Tailwind width for the display cell. */
		displayWidth?: string;
		name: string;
		date: string;
		/** <Disclosure> detail indent (e.g. `sm:ml-12`). */
		detailMargin?: string;
		/** Noun after show/hide (e.g. `case detail` / `detail`). */
		detailNoun?: string;
		/** Status chip cluster (ChipTip + StatusPill). */
		status: Snippet;
		/** Optional header tags (layer/axis). */
		tags?: Snippet;
		/** Always-visible content between header and detail. */
		preface: Snippet;
		/** The detail <dl> rows. */
		fields: Snippet;
	}

	let {
		id,
		rail,
		dashed = false,
		display,
		displayWidth = 'w-10 sm:w-12',
		name,
		date,
		detailMargin = 'sm:ml-12',
		detailNoun = 'detail',
		status,
		tags,
		preface,
		fields
	}: Props = $props();
</script>

<article
	{id}
	class="px-4 sm:px-5 py-4 hover:bg-bg-2 transition-colors{dashed ? ' border-dashed' : ''} {rail}"
>
	<header class="flex flex-wrap items-baseline gap-2 sm:gap-3 mb-3">
		<span class="font-display font-bold text-base sm:text-lg text-amber {displayWidth} shrink-0">{display}</span>
		<h3 class="font-bold text-fg text-sm sm:text-base">{name}</h3>
		{@render tags?.()}
		<span class="ml-auto flex items-center gap-2">
			{@render status()}
			<span class="text-[10px] uppercase tracking-widest text-fg-muted whitespace-nowrap">{date}</span>
		</span>
	</header>

	{@render preface()}

	<Disclosure
		detailsClass={detailMargin}
		summaryClass="text-[10px] uppercase tracking-widest text-fg-muted hover:text-amber flex items-center gap-2 py-1.5"
	>
		{#snippet summary(open)}
			<span>{open ? `hide ${detailNoun}` : `show ${detailNoun}`}</span>
		{/snippet}
		{#snippet children()}
			<dl class="text-xs sm:text-sm text-fg-dim space-y-1.5 pt-1.5">{@render fields()}</dl>
		{/snippet}
	</Disclosure>
</article>
