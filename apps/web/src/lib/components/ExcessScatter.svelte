<script lang="ts">
	// SPY-relative signal telemetry — per-trade excess scatter + trailing-mean
	// smoother + ticker-clustered bootstrap band. Hand-rolled inline SVG (no chart
	// lib: multiple trades share a matured_at date, which Lightweight Charts cannot
	// represent). HONESTY (load-bearing): telemetry over the surfaced-candidate
	// population, gross of cost, in-sample — NOT a portfolio track record. The
	// smoother/band are withheld below the N-gate; raw points are always shown.
	import type { EdgeExcessTelemetry } from '$lib/types';
	import { buildScales, pointCircles, trendPolyline, bandPath } from '$lib/excessScatter';

	let { telemetry }: { telemetry: EdgeExcessTelemetry } = $props();

	const BOX = { width: 640, height: 260, padLeft: 44, padRight: 12, padTop: 14, padBottom: 24 };
	const COLOR = { cyan: '#41d8ff', green: '#6dffb1', muted: '#7d8498', grid: '#1f2430' } as const;

	const showTrend = $derived(telemetry.status === 'ok' && telemetry.trend.length > 0);
	const scales = $derived(buildScales(telemetry.points, telemetry.trend, BOX));
	const circles = $derived(pointCircles(telemetry.points, scales.x, scales.y));
	const smoother = $derived(showTrend ? trendPolyline(telemetry.trend, scales.x, scales.y) : '');
	const band = $derived(showTrend ? bandPath(telemetry.trend, scales.x, scales.y) : '');
	const pctZero = $derived(`${(0).toFixed(1)}%`);
</script>

<div data-testid="excess-scatter" class="relative">
	<div class="mb-2 flex flex-wrap items-center gap-2">
		<span class="text-[10px] uppercase tracking-widest text-cyan"
			>spy-relative signal telemetry</span
		>
		<span
			class="inline-flex items-center border border-amber/40 bg-amber/15 px-1.5 py-0.5 text-[9px] uppercase tracking-widest text-amber whitespace-nowrap"
			>in-sample</span
		>
		<span class="text-[10px] tracking-wide text-fg-muted whitespace-nowrap"
			>N={telemetry.n_total} · unique tickers={telemetry.n_effective}</span
		>
	</div>

	<svg
		viewBox={`0 0 ${BOX.width} ${BOX.height}`}
		class="w-full"
		role="img"
		aria-labelledby="excess-scatter-title excess-scatter-desc"
	>
		<title id="excess-scatter-title">Per-trade excess over SPY by exit date</title>
		<desc id="excess-scatter-desc"
			>Scatter of each surfaced candidate's excess return over SPY across its holding window, with a
			trailing-mean smoother and a bootstrap uncertainty band. Telemetry only, not a portfolio.</desc
		>

		<!-- Uncertainty band (drawn first, under everything). -->
		{#if band}
			<path d={band} fill={COLOR.cyan} fill-opacity="0.12" stroke="none" />
		{/if}

		<!-- Zero / SPY-parity reference — DASHED so it reads distinct from the solid
		     smoother without relying on colour alone. -->
		<line
			x1={BOX.padLeft}
			x2={BOX.width - BOX.padRight}
			y1={scales.zeroY}
			y2={scales.zeroY}
			stroke={COLOR.muted}
			stroke-width="1"
			stroke-dasharray="4 3"
		/>
		<text x={BOX.padLeft} y={scales.zeroY - 3} font-size="9" fill={COLOR.muted}>
			SPY parity ({pctZero})
		</text>

		<!-- Scatter points. Repeat-ticker episodes render hollow so pseudo-
		     replication is visible. -->
		{#each circles as c (c.cx + ':' + c.cy)}
			<circle
				cx={c.cx}
				cy={c.cy}
				r="2.5"
				fill={c.repeat ? 'none' : COLOR.muted}
				stroke={COLOR.muted}
				stroke-width="1"
				fill-opacity="0.7"
			/>
		{/each}

		<!-- Trailing-mean smoother (solid). The data-testid lives on this <path>;
		     no <polyline> element is present — <polyline> does not accept a d
		     attribute and would be invalid SVG. -->
		{#if smoother}
			<path
				d={smoother}
				fill="none"
				stroke={COLOR.green}
				stroke-width="1.5"
				data-testid="excess-scatter-trend"
			/>
		{/if}
	</svg>

	{#if !showTrend}
		<p class="mt-1 text-[10px] tracking-wide text-fg-muted normal-case">
			trend hidden — accumulating <span class="whitespace-nowrap"
				>{telemetry.n_total}/{telemetry.gate_threshold}</span
			> matured ({telemetry.n_effective} unique tickers). Points shown are raw observations.
		</p>
	{/if}

	<p class="mt-2 text-[10px] leading-relaxed text-fg-dim normal-case">
		{telemetry.metric_note}
		{telemetry.benchmark_note}
	</p>
</div>
