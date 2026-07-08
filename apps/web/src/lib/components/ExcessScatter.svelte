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

	// Explicit x-axis ticks = the real distinct exit dates (subsampled when many).
	// Data is daily, so letting d3 auto-pick ticks over a short span produces
	// sub-day ticks that format to duplicate M/D labels — one tick per real date
	// avoids that in both the dense (ok) and sparse (accumulating) states.
	const xTicks = $derived.by<Date[]>(() => {
		const uniq = [...new Set(telemetry.points.map((p) => p.date))].sort().map(toDate);
		if (uniq.length <= 10) return uniq;
		const step = Math.ceil(uniq.length / 8);
		return uniq.filter((_, i) => i % step === 0);
	});

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
