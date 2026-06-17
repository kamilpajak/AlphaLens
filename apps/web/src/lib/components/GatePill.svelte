<script lang="ts">
	import { clampToViewport } from '$lib/actions/clampToViewport';
	import TooltipBubble from './TooltipBubble.svelte';

	interface Props {
		name: string;
		status: 'passed' | 'failed' | 'unknown';
	}
	let { name, status }: Props = $props();

	const GATE_INFO: Record<string, { full: string; what: string; pass: string; fail: string; unknown: string }> = {
		tenk: {
			full: '10-K filing',
			what: 'Searches latest 10-K (EDGAR) for the theme keywords supplied by Pro.',
			pass: 'theme keywords found in 10-K',
			fail: '10-K exists but no keyword match',
			unknown: 'no 10-K available for this ticker'
		},
		press: {
			full: 'Recent press (30d)',
			what: 'Polygon news firehose last 30 days — ticker tagged AND theme keywords in title/body.',
			pass: 'press article mentions ticker + theme',
			fail: 'press tagged ticker but no keyword hit (per-ticker fallback)',
			unknown: 'Polygon did not tag ticker in batch frame (PR #150 tri-state)'
		},
		insider: {
			full: 'Insider opportunistic buys (90d)',
			what: 'Form-4 parquet — Cohen-Malloy opportunistic-buy filter on last 90 days.',
			pass: 'at least one opportunistic insider buy',
			fail: 'Form-4 filings exist but none qualify as opportunistic',
			unknown: 'no Form-4 filings for ticker in window'
		},
		etf: {
			full: 'Theme ETF holdings',
			what: 'SEC NPORT-P holdings — ticker held by ETF whose theme matches.',
			pass: 'held by ≥1 theme-relevant ETF',
			fail: 'theme-ETFs hold no position in ticker',
			unknown: 'NPORT-P unavailable (pre-2010 coverage cliff or new ticker)'
		}
	};
	const info = $derived(GATE_INFO[name.toLowerCase()]);
	const statusLine = $derived(
		info
			? status === 'passed'
				? info.pass
				: status === 'failed'
					? info.fail
					: info.unknown
			: ''
	);
	const sym = $derived(status === 'passed' ? '✓' : status === 'failed' ? '✗' : '?');
</script>

<span
	class="group relative inline-block hover:z-50 focus-within:z-50"
	tabindex={info ? 0 : undefined}
	role={info ? 'group' : undefined}
	use:clampToViewport
>
	<span
		class="inline-flex items-center gap-1 px-2 py-0.5 border text-[10px] uppercase tracking-widest cursor-help"
		class:border-green={status === 'passed'}
		class:text-green={status === 'passed'}
		class:border-red={status === 'failed'}
		class:text-red={status === 'failed'}
		class:border-fg-muted={status === 'unknown'}
		class:text-fg-muted={status === 'unknown'}
	>
		<span class="font-bold">{sym}</span>
		{name}
	</span>

	{#if info}
		<TooltipBubble>
			{#snippet header()}{name} // {info.full}{/snippet}
			<span class="block">{info.what}</span>
			<span
				class="block mt-1.5 font-bold"
				class:text-green={status === 'passed'}
				class:text-red={status === 'failed'}
				class:text-fg-muted={status === 'unknown'}
			>
				<span class="font-mono">{sym}</span> {statusLine}
			</span>
		</TooltipBubble>
	{/if}
</span>
