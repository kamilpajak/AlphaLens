<script lang="ts">
	// Shared 2-column key→value table for tooltip bodies — the single source for
	// the layout that was previously copy-pasted into the JargonTip bands and the
	// buffett / o'neil / RSI / vol-z snippets. Two modes:
	//   align="left"  — range/threshold → meaning: bold key in a shared left
	//                   column; the meaning starts at the same x in column 2 and
	//                   wraps cleanly within its own column. (bands, RSI, vol-z)
	//   align="right" — label → value: plain label left, bold value flushed right
	//                   like the FUNDAMENTALS grid. (buffett, o'neil)
	// `class` passes through to the <dl> so callers set their own top margin.

	interface Row {
		key: string;
		value: string;
	}

	let {
		rows,
		align = 'left',
		class: cls = ''
	}: { rows: Row[]; align?: 'left' | 'right'; class?: string } = $props();
</script>

{#if align === 'right'}
	<dl class="grid grid-cols-[1fr_auto] gap-x-4 gap-y-0.5 {cls}">
		{#each rows as row}
			<dt>{row.key}</dt>
			<dd class="text-right font-bold text-fg whitespace-nowrap">{row.value}</dd>
		{/each}
	</dl>
{:else}
	<dl class="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 {cls}">
		{#each rows as row}
			<dt class="whitespace-nowrap font-bold text-fg">{row.key}</dt>
			<dd>{row.value}</dd>
		{/each}
	</dl>
{/if}
