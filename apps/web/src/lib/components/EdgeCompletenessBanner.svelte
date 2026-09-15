<script lang="ts">
	// /edge global trust-signal banner (Task 6, the settled-watermark stamp).
	// N/M are read off the SUMMARY payload (`n_matured` / `n_terminal` /
	// `n_quarantined`), NEVER off the outcomes list — that list is paginated
	// (server cap), filtered by the terminal/ongoing tab, and narrowed by toolbar
	// filters, so a count off it would swing with an unrelated tab toggle (e.g.
	// ongoing -> 0) instead of reporting the window-stable benchmark-coverage
	// truth. The denominator leaves the quarantined rows out (#1453): a
	// SPLIT_INVALIDATED row gets no benchmark by design, so it is neither
	// matured nor "still to enrich" — it is named beside the ratio instead.
	// `enrichedAt` is the settled watermark stamped by the nightly enrichment
	// pass (Task 4); null before the first watermarked run.

	import JargonTip from '$lib/components/JargonTip.svelte';
	import { completenessCounts } from '$lib/edge';

	interface Props {
		enrichedAt: string | null;
		nTerminal: number;
		nMatured: number;
		nQuarantined: number;
	}

	let { enrichedAt, nTerminal, nMatured, nQuarantined }: Props = $props();

	const MINUTE_MS = 60_000;
	const HOUR_MS = 3_600_000;
	const DAY_MS = 86_400_000;

	// Human "X min/hours/days ago" — no shared helper exists in $lib yet, so
	// this stays local. Deterministic off a fixed literal in stories.
	function relativeTime(iso: string | null): string {
		if (!iso) return '—';
		const thenMs = new Date(iso).getTime();
		if (!Number.isFinite(thenMs)) return '—';
		const deltaMs = Math.max(0, Date.now() - thenMs);
		if (deltaMs < MINUTE_MS) return 'just now';
		if (deltaMs < HOUR_MS) {
			const mins = Math.round(deltaMs / MINUTE_MS);
			return `${mins} min ago`;
		}
		if (deltaMs < DAY_MS) {
			const hours = Math.round(deltaMs / HOUR_MS);
			return `${hours}h ago`;
		}
		const days = Math.round(deltaMs / DAY_MS);
		return `${days}d ago`;
	}

	const relative = $derived(relativeTime(enrichedAt));
	const noClosedYet = $derived(nTerminal === 0);
	const counts = $derived(
		completenessCounts({
			n_terminal: nTerminal,
			n_matured: nMatured,
			n_quarantined: nQuarantined
		})
	);
</script>

<div class="text-[10px] uppercase tracking-widest text-fg-muted flex flex-wrap items-center gap-x-1.5 gap-y-0.5">
	<span>enriched:</span>
	{#if noClosedYet}
		<span>no closed positions yet</span>
	{:else}
		<span class="whitespace-nowrap text-fg-dim">{counts.matured} / {counts.measurable}</span>
		<span>complete</span>
		{#if counts.quarantined > 0}
			<span aria-hidden="true">&middot;</span>
			<!-- nowrap goes INSIDE the trigger (LadderChart precedent): white-space is
			     inherited, so a nowrap wrapper around the JargonTip would reach the
			     absolutely-positioned tooltip bubble and render its body as one
			     unwrapped ~1900 px line. -->
			<JargonTip
				term="quarantined"
				body="Closed positions whose replay window crossed a stock split or a large special dividend (the legend's “not measurable” group). They keep the stock's own return as telemetry but get no benchmark by design, so they are left out of the completeness count rather than shown as still to enrich."
				><span class="whitespace-nowrap">{counts.quarantined} quarantined</span></JargonTip
			>
		{/if}
	{/if}
	<span aria-hidden="true">&middot;</span>
	<span>last computed <span class="whitespace-nowrap">{relative}</span></span>
</div>
