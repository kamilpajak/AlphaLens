<script lang="ts">
	/**
	 * Closed-market banner — persistent, non-dismissible, live countdown.
	 *
	 * Rendered in ``+layout.svelte`` above ``<main>`` so every route picks it
	 * up; the component itself decides whether to render anything based on
	 * ``marketStatus.value``. Reads the shared poll state (no fetch here)
	 * so multiple mounts wouldn't multiply API load if the parent layout
	 * tree ever grew.
	 *
	 * UX copy mirrors the design memo §4 (Perplexity research):
	 *   "Market closed. Submission deferred until Mon Jun 1 09:30 ET (in 1h 24m).
	 *    Ladder based on Fri close."
	 *
	 * Three render states:
	 *   - Banner HIDDEN — trading day OR poll hasn't loaded yet OR poll failed.
	 *     (We fail silent — see lib/marketStatus.ts.)
	 *   - Banner VISIBLE on closed-day, with countdown ticking each second.
	 *   - Banner VISIBLE on half-day — same chrome, different copy line.
	 */

	import { AlertCircle } from 'lucide-svelte';
	import { marketStatus, formatCountdown } from '$lib/marketStatus.svelte';

	// One-second tick driving the countdown. Bound to the component
	// lifecycle via $effect so unmounting cleans up.
	let nowMs = $state(Date.now());
	$effect(() => {
		const id = setInterval(() => {
			nowMs = Date.now();
		}, 1000);
		return () => clearInterval(id);
	});

	const status = $derived(marketStatus.value);
	const visible = $derived(
		marketStatus.hasLoaded && status !== null && !status.is_trading_day
	);

	const nextOpenLabel = $derived.by(() => {
		if (!status) return '';
		// Format as "Mon Jun 1 09:30 ET" — short weekday + short month
		// keeps the banner compact at narrow viewports. America/New_York
		// is the venue tz (XNYS); future Polish/Asian wiring will need
		// a per-exchange tz map but for now XNYS is the only caller.
		const d = new Date(status.next_open_iso);
		return new Intl.DateTimeFormat('en-US', {
			weekday: 'short',
			month: 'short',
			day: 'numeric',
			hour: '2-digit',
			minute: '2-digit',
			hour12: false,
			timeZone: 'America/New_York',
			timeZoneName: 'short'
		}).format(d);
	});

	const countdown = $derived.by(() => {
		if (!status) return '';
		const target = new Date(status.next_open_iso).getTime();
		return formatCountdown(target - nowMs);
	});
</script>

{#if visible && status}
	<!--
		Single horizontal strip. ``role="status"`` + ``aria-live="polite"``
		so screen readers announce the closed-market state on first render
		but don't re-announce every countdown tick. ``data-testid`` is the
		hook the Playwright smoke uses to assert visibility — keep stable.
	-->
	<aside
		role="status"
		aria-live="polite"
		data-testid="market-status-banner"
		class="border-b border-grid bg-bg-1 text-[11px] uppercase tracking-widest text-fg-dim"
	>
		<div class="flex flex-wrap items-center gap-x-4 gap-y-1 px-3 sm:px-4 py-2">
			<span class="flex items-center gap-1.5 text-amber">
				<AlertCircle class="size-3" />
				<span class="font-display font-bold">market closed</span>
			</span>
			<span class="text-fg-muted">
				submission deferred until
				<span class="text-fg-1 whitespace-nowrap">{nextOpenLabel}</span>
				<span class="text-fg-muted">·</span>
				<span class="text-amber whitespace-nowrap">in {countdown}</span>
			</span>
			<span class="hidden sm:inline text-fg-muted ml-auto">
				ladder anchors last close
			</span>
		</div>
	</aside>
{/if}
