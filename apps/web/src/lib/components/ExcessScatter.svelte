<script lang="ts">
	// SPY-relative signal telemetry — per-trade excess-over-SPY scatter + a
	// trailing-mean smoother + a ticker-clustered bootstrap band, rendered with
	// LayerChart (composable Svelte 5 chart components). HONESTY (load-bearing):
	// telemetry over the surfaced-candidate population, gross of cost, in-sample —
	// NOT a portfolio track record. The smoother/band are withheld below the
	// N-gate; raw points are always shown. All statistics come from the server;
	// this component only maps them to marks — no client-side recompute.
	import 'layerchart/core.css';
	import { Area, Axis, Chart, Highlight, Layer, Points, Rule, Spline, Tooltip } from 'layerchart';
	import { evenTimeTicks } from '$lib/chartTicks';
	import type { EdgeExcessTelemetry } from '$lib/types';

	let { telemetry }: { telemetry: EdgeExcessTelemetry } = $props();

	const toDate = (iso: string) => new Date(iso + 'T00:00:00Z');
	const pctTick = (v: number) => `${(v * 100).toFixed(0)}%`;
	const pctFull = (v: number) => `${(v * 100).toFixed(2)}%`;
	const dayShort = (d: Date) => `${d.getUTCMonth() + 1}/${d.getUTCDate()}`;
	const dayFull = (d: Date) => d.toISOString().slice(0, 10);

	const points = $derived(telemetry.points.map((p) => ({ ...p, date: toDate(p.date) })));
	const trend = $derived(telemetry.trend.map((t) => ({ ...t, date: toDate(t.date) })));
	const showTrend = $derived(telemetry.status === 'ok' && trend.length > 0);

	// y-domain spans the points, the CI band, and always the 0 parity line.
	const yDomain = $derived.by<[number, number]>(() => {
		const ys = [...telemetry.points.map((p) => p.excess), 0];
		for (const t of telemetry.trend) ys.push(t.lo, t.hi);
		return [Math.min(...ys), Math.max(...ys)];
	});

	// Explicit x-axis ticks at even whole-day intervals across the date domain.
	// The x-scale is a time scale, so a tick lands at its true calendar pixel
	// position — subsampling the distinct exit-dates by index would place ticks
	// at uneven gaps (weekends / days with no exits). Even time-steps keep the
	// gaps uniform, and the whole-day granularity avoids duplicate M/D labels
	// that d3 auto-ticks produce on a short span.
	const xTicks = $derived(evenTimeTicks(telemetry.points.map((p) => p.date)));

	// LayerChart's axis default is font-weight 300 (thin) — force the app's normal
	// mono weight so ticks match the rest of the terminal UI.
	const tickText = { class: 'fill-fg-muted text-[10px] font-mono !font-normal' };
</script>

<div data-testid="excess-scatter" class="relative">
	<div class="mb-2 flex flex-wrap items-center gap-2">
		<span
			class="inline-flex items-center border border-amber/40 bg-amber/15 px-1.5 py-0.5 text-[9px] uppercase tracking-widest text-amber whitespace-nowrap"
			>in-sample</span
		>
		<span class="text-[10px] tracking-wide text-fg-muted whitespace-nowrap"
			>N={telemetry.n_total} · unique tickers={telemetry.n_effective}</span
		>
		{#if telemetry.median_holding_days != null}
			<span class="text-[10px] tracking-wide text-fg-muted whitespace-nowrap"
				>median hold {telemetry.median_holding_days}d</span
			>
		{/if}
	</div>

	<!-- Legend — a swatch per mark so a first-time reader knows how to read the
	     chart. The mean/band entries only show when the trend is drawn (both are
	     withheld below the N-gate). Swatch fills/strokes mirror the marks exactly. -->
	<div
		data-testid="excess-scatter-legend"
		class="mb-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-[10px] text-fg-dim"
	>
		<span class="inline-flex items-center gap-1.5 whitespace-nowrap">
			<svg width="14" height="8" viewBox="0 0 14 8" aria-hidden="true">
				<circle cx="7" cy="4" r="2.5" class="fill-fg-muted/50 stroke-fg-muted" />
			</svg>
			one closed trade (excess vs SPY)
		</span>
		{#if showTrend}
			<span class="inline-flex items-center gap-1.5 whitespace-nowrap">
				<svg width="16" height="8" viewBox="0 0 16 8" aria-hidden="true">
					<line x1="0" y1="4" x2="16" y2="4" class="stroke-green" stroke-width="2" />
				</svg>
				trailing mean (last {telemetry.smoother_window})
			</span>
			<span class="inline-flex items-center gap-1.5 whitespace-nowrap">
				<svg width="16" height="10" viewBox="0 0 16 10" aria-hidden="true">
					<rect x="0" y="1" width="16" height="8" class="fill-cyan/12" />
				</svg>
				95% confidence band
			</span>
		{/if}
		<span class="inline-flex items-center gap-1.5 whitespace-nowrap">
			<svg width="16" height="8" viewBox="0 0 16 8" aria-hidden="true">
				<line
					x1="0"
					y1="4"
					x2="16"
					y2="4"
					class="stroke-fg-muted/50"
					stroke-width="1"
					stroke-dasharray="4 3"
				/>
			</svg>
			SPY parity (0%)
		</span>
	</div>

	<div class="w-full">
		<Chart
			data={points}
			x="date"
			y="excess"
			{yDomain}
			yNice
			padding={{ top: 16, left: 52, bottom: 28, right: 16 }}
			height={300}
		>
			<Layer>
				<Axis
					placement="left"
					grid={{ class: 'stroke-grid/60' }}
					rule={{ class: 'stroke-grid' }}
					format={pctTick}
					tickLabelProps={tickText}
				/>
				<Axis
					placement="bottom"
					rule={{ class: 'stroke-grid' }}
					ticks={xTicks}
					format={dayShort}
					tickLabelProps={tickText}
				/>

				{#if showTrend}
					<g data-testid="excess-scatter-trend">
						<Area data={trend} x="date" y0={(d) => d.lo} y1={(d) => d.hi} class="fill-cyan/12" />
						<Spline data={trend} x="date" y={(d) => d.mean} class="stroke-green stroke-2" />
					</g>
				{/if}

				<!-- SPY parity (0%) — dashed so it reads distinct from the solid smoother. -->
				<Rule y={0} class="stroke-fg-muted/50 [stroke-dasharray:4_3]" />

				<Points class="fill-fg-muted/50 stroke-fg-muted" r={2.5} />
				<Highlight points />
				<Tooltip.Context mode="quadtree" />
			</Layer>

			<Tooltip.Root>
				{#snippet children({ data })}
					<Tooltip.Header value={dayFull(data.date)} />
					<Tooltip.List>
						<Tooltip.Item label="excess vs SPY" value={pctFull(data.excess)} />
						<Tooltip.Item label="ticker" value={data.ticker} />
						{#if data.holding_days != null}
							<Tooltip.Item label="held" value={`${data.holding_days}d`} />
						{/if}
					</Tooltip.List>
				{/snippet}
			</Tooltip.Root>
		</Chart>
	</div>

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
