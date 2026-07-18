<script lang="ts">
	/**
	 * Per-exchange session chip — ambient footer telemetry.
	 *
	 * Reads the shared ``/v1/market/status`` poll state and renders one
	 * compact chip per venue: "US MARKET ● live · closes in 2h" while trading,
	 * "US MARKET ○ closed · opens mon 09:30" otherwise. The chip shows a human
	 * market label (via ``marketLabel``) rather than the raw ISO 10383 MIC: the
	 * US candidate universe spans XNYS + XNAS + XASE, which share one session
	 * calendar, so a lone "XNYS" would under-sell the scope. It answers the only
	 * market-state question this read-only brief tool actually has — are the
	 * prices live or anchored to the last close — so it lives in the footer
	 * as ambient context, not a full-width alert (the old closed-market
	 * banner + its dead "submission deferred" copy were removed once the
	 * paper-trade/broker chain was decommissioned, ADR 0012). The green "live"
	 * label is also the app's sole liveness indicator: the always-on standalone
	 * footer "live" dot was merged here so the cue is strictly per-exchange.
	 *
	 * Multi-exchange: today only XNYS is wired server-side (labelled "US
	 * MARKET"). When more venues arrive the endpoint grows to a per-MIC list
	 * and this renders one chip each — adding e.g. Warsaw is a one-line
	 * ``marketLabel`` entry (``XWAR: 'PL MARKET'``); there is no global
	 * "market closed" claim to disambiguate.
	 *
	 * Renders nothing until the first poll resolves (no flash) or if the
	 * poll failed (fail-silent, mirroring the store).
	 */

	import { marketStatus, formatCountdown } from '$lib/marketStatus.svelte';
	import { formatLocalWeekdayTime } from '$lib/localTime';
	import { marketLabel } from '$lib/marketLabel';

	// One-second tick drives the live countdown. Bound to the component
	// lifecycle via $effect so unmounting clears the interval.
	let nowMs = $state(Date.now());
	$effect(() => {
		const id = setInterval(() => {
			nowMs = Date.now();
		}, 1000);
		return () => clearInterval(id);
	});

	const status = $derived(marketStatus.value);
	const ready = $derived(marketStatus.hasLoaded && status !== null);

	// Next-open label in the *viewer's* local zone (DST-aware), matching the
	// ambient footer clock so the two readings agree. The clock right beside it
	// carries the zone label (e.g. "CEST"), so we omit a tz token here to stay
	// compact — e.g. "mon 15:30" for a Warsaw viewer, "mon 09:30" in New York.
	const nextOpenLabel = $derived.by(() =>
		status ? formatLocalWeekdayTime(status.next_open_iso) : ''
	);

	const closesIn = $derived.by(() =>
		status ? formatCountdown(new Date(status.next_close_iso).getTime() - nowMs) : ''
	);
	const opensIn = $derived.by(() =>
		status ? formatCountdown(new Date(status.next_open_iso).getTime() - nowMs) : ''
	);
</script>

{#if ready && status}
	<span data-testid="market-session" class="flex items-center gap-1.5 whitespace-nowrap">
		{#if status.is_open_now}
			<!-- `blink` pulses the dot while the venue trades; the closed branch
			     below uses a static dot, so the motion is itself per-exchange. -->
			<span class="dot bg-green blink"></span>
			<span class="text-fg-dim">{marketLabel(status.exchange)}</span>
			<!-- "live" doubles as the open-state label and the sole liveness cue
			     (the old always-on footer "live" dot was merged here): prices are
			     live only while this venue trades. -->
			<span class="text-green">live</span>
			<span class="text-fg-muted hidden lg:inline">· closes in {closesIn}</span>
			{#if status.is_half_day}
				<span class="text-amber hidden lg:inline" title="half-day — early close">½</span>
			{/if}
		{:else}
			<span class="dot bg-fg-muted"></span>
			<span class="text-fg-dim">{marketLabel(status.exchange)}</span>
			<span class="text-fg-muted">closed</span>
			<span class="text-fg-muted hidden lg:inline">· opens {nextOpenLabel} · in {opensIn}</span>
		{/if}
	</span>
{/if}
