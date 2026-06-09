<script lang="ts">
	// Candlestick chart for one recommendation's broker-free ladder replay.
	//
	// SSR-safe: Lightweight Charts is DOM-only, so the library is dynamically
	// imported inside onMount behind a `browser` guard. This both avoids the
	// SSR/hydration crash (no `document` server-side) and keeps the ~50kB lib
	// out of the initial bundle — it loads only when a row is expanded.
	//
	// HONESTY (load-bearing, not cosmetic — a user acts on this with real
	// money): every marker is a *modeled* bar-replay fill, never a broker
	// execution. The persistent `SIM` chip, the `(?) how?` popover, and the
	// RTH-only footnote all stay visible so the modeling limits are never
	// hidden. See the design memo §5.
	//
	// TRADE LIFECYCLE (PR-2): the chart reads its visual language from a derived
	// lifecycle state so a glance distinguishes a closed trade (history) from a
	// live one (now) from an untriggered plan (forward-looking):
	//   - CLOSED  = payload.terminal === true.
	//   - OPEN    = !terminal AND ≥1 ENTRY marker (the ladder fired).
	//   - PLANNED = !terminal AND no ENTRY marker (status OK, bars + price
	//               lines, but markers === [] — the plan-preview case). This is
	//               NOT an empty state; we still draw bars + dashed plan lines.

	import { onMount } from 'svelte';
	import { browser } from '$app/environment';
	import type {
		IChartApi,
		ISeriesApi,
		CandlestickData,
		SeriesMarker,
		Time,
		CandlestickSeriesOptions,
		DeepPartial,
		ITimeScaleApi
	} from 'lightweight-charts';
	import type { ChartPayload, ChartMarker } from '$lib/types';
	import { fmtR } from '$lib/edge';
	import JargonTip from './JargonTip.svelte';
	import ChipTip from './ChipTip.svelte';
	import { Crosshair } from 'lucide-svelte';

	interface Props {
		payload: ChartPayload;
	}
	let { payload }: Props = $props();

	// Terminal-aesthetic palette (mirrors app.css CSS variables — Lightweight
	// Charts needs literal colour strings, it cannot read CSS custom props).
	const COLOR = {
		bg2: '#11141b',
		grid: '#1f2430',
		fgMuted: '#7d8498',
		cyan: '#41d8ff',
		green: '#6dffb1',
		red: '#ff5566',
		amber: '#ffb000'
	} as const;

	const hasStructure = $derived(payload.status === 'OK');

	// ── Lifecycle derivation ──────────────────────────────────────────────
	// Markers are the source of truth for "was anything filled". A PLANNED
	// trade has an OK status with bars + price lines but an empty marker list.
	const hasEntryMarker = $derived(payload.markers.some((m) => m.kind === 'ENTRY'));
	type Lifecycle = 'CLOSED' | 'OPEN' | 'PLANNED';
	const lifecycle = $derived<Lifecycle>(
		payload.terminal ? 'CLOSED' : hasEntryMarker ? 'OPEN' : 'PLANNED'
	);

	// In-trade bar count + TP-hit flag drive the freshly-opened caption.
	const hasTpMarker = $derived(payload.markers.some((m) => m.kind === 'TP'));
	const firstEntryTime = $derived(
		payload.markers.find((m) => m.kind === 'ENTRY')?.time ?? null
	);
	const inTradeBarCount = $derived(
		firstEntryTime == null
			? 0
			: payload.bars.filter((b) => b.time >= firstEntryTime).length
	);
	const freshlyOpened = $derived(
		lifecycle === 'OPEN' && !hasTpMarker && inTradeBarCount <= 3
	);

	// Hit-level lookup: a TP/SL price line is "hit" when a marker carries the
	// matching level_id (e.g. tp1, tp2, sl). Hit levels render dimmed; un-hit
	// levels on an OPEN trade render bright/pending.
	const hitLevelIds = $derived(
		new Set(
			payload.markers
				.map((m) => m.level_id?.trim().toLowerCase())
				.filter((id): id is string => !!id)
		)
	);
	function isTpHit(index: number): boolean {
		return hitLevelIds.has(`tp${index + 1}`);
	}
	const stopHit = $derived(
		payload.markers.some((m) => m.kind === 'SL' || m.kind === 'TIME_STOP')
	);

	let chartContainer = $state<HTMLDivElement | undefined>(undefined);
	// Absolutely-positioned in-trade shading band overlaid on the canvas. Bound
	// to a DOM node so the time-range subscription + ResizeObserver can update
	// its left/width without a Svelte re-render (the chart owns its own RAF).
	let shadeBand = $state<HTMLDivElement | undefined>(undefined);
	// Set true only if the band overlay degraded (coordinate mapping failed); a
	// tiny footnote then notes the band was skipped without breaking the chart.
	let shadeDegraded = $state(false);

	onMount(() => {
		// Guard: never instantiate server-side or for the empty states.
		if (!browser || !hasStructure) return;

		let chart: IChartApi | null = null;
		let resizeObserver: ResizeObserver | null = null;
		let timeScale: ITimeScaleApi<Time> | null = null;
		let rangeHandler: (() => void) | null = null;
		let disposed = false;

		(async () => {
			const lib = await import('lightweight-charts');
			// The async import can resolve after the component is destroyed
			// (fast expand → collapse); bail before touching the DOM.
			if (disposed || !chartContainer) return;

			const { createChart, CandlestickSeries, createSeriesMarkers, LineStyle } = lib;

			chart = createChart(chartContainer, {
				width: chartContainer.clientWidth,
				height: 320,
				autoSize: false,
				layout: {
					background: { color: 'transparent' },
					textColor: COLOR.fgMuted,
					fontFamily:
						"'JetBrains Mono Variable', ui-monospace, SFMono-Regular, Menlo, monospace",
					fontSize: 11
				},
				grid: {
					vertLines: { color: COLOR.grid },
					horzLines: { color: COLOR.grid }
				},
				rightPriceScale: { borderColor: COLOR.grid },
				timeScale: { borderColor: COLOR.grid },
				crosshair: {
					vertLine: { color: COLOR.grid, labelBackgroundColor: COLOR.bg2 },
					horzLine: { color: COLOR.grid, labelBackgroundColor: COLOR.bg2 }
				}
			});

			// Native last-value price line — the live "now" marker. Enabled only
			// for non-terminal (open/planned) trades; on a closed trade the price
			// path is history, so there is no "now" line to draw.
			const showLastValue = lifecycle !== 'CLOSED';
			const candleOptions: DeepPartial<CandlestickSeriesOptions> = {
				upColor: COLOR.green,
				downColor: COLOR.red,
				borderUpColor: COLOR.green,
				borderDownColor: COLOR.red,
				wickUpColor: COLOR.green,
				wickDownColor: COLOR.red,
				priceLineVisible: showLastValue,
				lastValueVisible: showLastValue,
				priceLineColor: COLOR.cyan,
				priceLineStyle: LineStyle.Solid,
				priceLineWidth: 1
			};
			const series: ISeriesApi<'Candlestick'> = chart.addSeries(CandlestickSeries, candleOptions);

			const candles: CandlestickData<Time>[] = payload.bars.map((b) => ({
				time: b.time as Time,
				open: b.open,
				high: b.high,
				low: b.low,
				close: b.close
			}));
			series.setData(candles);

			// Horizontal price lines. Styling reads the lifecycle:
			//   PLANNED → ALL lines dashed + dimmed (forward-looking plan).
			//   OPEN    → un-hit levels solid/bright (pending), hit levels dimmed.
			//   CLOSED  → all levels subdued/uniform (history).
			const planned = lifecycle === 'PLANNED';

			// Opacity helpers via 2-digit alpha suffix on the hex colour.
			const dim = (hex: string) => `${hex}66`; // ~40%
			const bright = (hex: string) => hex; // full

			// entry — cyan. The entry is the anchor, not a "pending target", so it
			// reads bright only on a live OPEN trade; dimmed on a closed (history)
			// trade and dashed+dimmed on a planned (not-yet-triggered) preview.
			if (payload.price_lines.entry != null) {
				series.createPriceLine({
					price: payload.price_lines.entry,
					color: lifecycle === 'OPEN' ? bright(COLOR.cyan) : dim(COLOR.cyan),
					lineWidth: 1,
					lineStyle: planned ? LineStyle.Dashed : LineStyle.Solid,
					axisLabelVisible: true,
					title: 'entry'
				});
			}
			// A level is rendered "pending" (solid + bright) only when the trade
			// is OPEN and the level has NOT been hit yet — that is the live,
			// forward-looking target/stop. Everything else (planned plan-preview,
			// closed history, an already-hit level) renders dashed + dimmed.
			const levelStyle = (hit: boolean, baseHex: string) => {
				const pending = lifecycle === 'OPEN' && !hit;
				return {
					color: pending ? bright(baseHex) : dim(baseHex),
					lineStyle: pending ? LineStyle.Solid : LineStyle.Dashed
				};
			};

			payload.price_lines.tp.forEach((tp, i) => {
				const { color, lineStyle } = levelStyle(isTpHit(i), COLOR.green);
				series.createPriceLine({
					price: tp,
					color,
					lineWidth: 1,
					lineStyle,
					axisLabelVisible: true,
					title: `tp${i + 1}`
				});
			});
			if (payload.price_lines.stop != null) {
				const { color, lineStyle } = levelStyle(stopHit, COLOR.red);
				series.createPriceLine({
					price: payload.price_lines.stop,
					color,
					lineWidth: 1,
					lineStyle,
					axisLabelVisible: true,
					title: 'stop'
				});
			}

			// Per-point markers (v5 plugin). Marker time MUST match an existing
			// bar timestamp — the backend computes them from the same bars, so a
			// marker can never land in a non-trading gap.
			const markers = buildMarkers(payload.markers);
			if (markers.length > 0) {
				createSeriesMarkers(series, markers);
			}

			chart.timeScale().fitContent();
			timeScale = chart.timeScale();

			// ── In-trade shading band (best-effort) ─────────────────────────
			// Lightweight Charts v5 has no native rectangle, so the band is an
			// absolutely-positioned div whose left/width are recomputed from the
			// time scale on every visible-range change + resize. If coordinate
			// mapping fails (e.g. the trade region is scrolled off-screen) we
			// hide the band rather than mis-paint it. No band for PLANNED
			// (nothing is held yet).
			const bandStart = firstEntryTime;
			// Band end: the terminal exit marker time on CLOSED, else the last bar
			// (OPEN runs to "now").
			const exitMarker = payload.markers.find(
				(m) => m.kind === 'TP' || m.kind === 'SL' || m.kind === 'TIME_STOP'
			);
			const lastBarTime = payload.bars.length
				? (payload.bars[payload.bars.length - 1].time as Time)
				: null;
			const bandEnd =
				lifecycle === 'CLOSED' && exitMarker ? (exitMarker.time as Time) : lastBarTime;

			const updateBand = () => {
				if (!shadeBand || !timeScale || lifecycle === 'PLANNED') return;
				if (bandStart == null || bandEnd == null) {
					shadeBand.style.display = 'none';
					return;
				}
				try {
					const x1 = timeScale.timeToCoordinate(bandStart as Time);
					const x2 = timeScale.timeToCoordinate(bandEnd as Time);
					if (x1 == null || x2 == null) {
						// Region not in the visible range — hide, don't mis-paint.
						shadeBand.style.display = 'none';
						return;
					}
					const left = Math.min(x1, x2);
					const width = Math.max(1, Math.abs(x2 - x1));
					shadeBand.style.display = 'block';
					shadeBand.style.left = `${left}px`;
					shadeBand.style.width = `${width}px`;
				} catch {
					// Defensive: never let the band break the chart.
					shadeDegraded = true;
					if (shadeBand) shadeBand.style.display = 'none';
				}
			};

			if (lifecycle !== 'PLANNED' && shadeBand) {
				rangeHandler = updateBand;
				timeScale.subscribeVisibleTimeRangeChange(rangeHandler);
				// Initial paint after fitContent has settled the scale.
				updateBand();
			}

			resizeObserver = new ResizeObserver(() => {
				if (chart && chartContainer) {
					chart.applyOptions({ width: chartContainer.clientWidth });
					updateBand();
				}
			});
			resizeObserver.observe(chartContainer);
		})();

		// Cleanup on destroy: stop observing + unsubscribe + dispose the canvas.
		return () => {
			disposed = true;
			if (resizeObserver) resizeObserver.disconnect();
			if (timeScale && rangeHandler) {
				timeScale.unsubscribeVisibleTimeRangeChange(rangeHandler);
			}
			if (chart) chart.remove();
		};
	});

	// Map a replay marker to a Lightweight Charts SeriesMarker.
	//   ENTRY     → cyan  arrowUp   belowBar  (E1, E2, ...)  — prominent
	//   TP        → green arrowDown aboveBar  (TP1, ...)
	//   SL        → red   arrowDown belowBar  (SL)
	//   TIME_STOP → amber arrowDown aboveBar  (TS)
	function buildMarkers(raw: ChartMarker[]): SeriesMarker<Time>[] {
		return raw.map((m, i) => {
			const base = {
				time: m.time as Time,
				text: m.label,
				id: `${m.kind}-${m.level_id ?? i}`,
				size: 1
			};
			switch (m.kind) {
				case 'ENTRY':
					// Larger size + cyan so the trigger reads as the strongest mark.
					return { ...base, size: 2, position: 'belowBar', color: COLOR.cyan, shape: 'arrowUp' };
				case 'TP':
					return { ...base, position: 'aboveBar', color: COLOR.green, shape: 'arrowDown' };
				case 'SL':
					return { ...base, position: 'belowBar', color: COLOR.red, shape: 'arrowDown' };
				case 'TIME_STOP':
					return { ...base, position: 'aboveBar', color: COLOR.amber, shape: 'arrowDown' };
				default:
					return { ...base, position: 'inBar', color: COLOR.fgMuted, shape: 'circle' };
			}
		});
	}

	// ── Status chip copy (terminal aesthetic) ─────────────────────────────
	// CLOSED  → "closed · {realized_r}R"
	// OPEN    → "open · day {n} · {open_r}R unrealized"
	// PLANNED → "planned · not triggered · 0 fills"
	const statusTone = $derived(
		lifecycle === 'CLOSED' ? 'cyan' : lifecycle === 'OPEN' ? 'green' : 'amber'
	);
	const statusChipClasses = $derived(
		statusTone === 'cyan'
			? 'bg-cyan/15 text-cyan border-cyan/40'
			: statusTone === 'green'
				? 'bg-green/15 text-green border-green/40'
				: 'bg-amber/15 text-amber border-amber/40'
	);

	// `(?) how?` popover body — data source + RTH-only + intrabar rule, with an
	// honest ambiguous-bar note when the replay had to resolve a same-bar
	// TP-and-SL touch SL-first.
	const howBody = $derived(
		'Data source: historical OHLC bars from Polygon (regular trading hours only). ' +
			`Intrabar rule: ${payload.intrabar_rule ?? 'SL-first'} — when a single bar touches both a ` +
			'take-profit and the stop we cannot know the order from OHLC alone, so the replay resolves it ' +
			'conservatively (stop first). ' +
			(payload.ambiguous_bars > 0
				? `Note: ${payload.ambiguous_bars} bar(s) here touched both TP and SL and were resolved this way.`
				: 'No ambiguous bars in this window.')
	);
</script>

<div class="relative">
	<!-- Decorative crosshair watermark, clipped by its own overflow-hidden
	     wrapper so the JargonTip / ChipTip popovers extending outside the
	     panel are not cut off (matches TradeSetup). -->
	<div class="pointer-events-none absolute inset-0 overflow-hidden" aria-hidden="true">
		<Crosshair class="absolute -right-6 -top-6 size-40 text-grid opacity-40" />
	</div>

	{#if !hasStructure}
		<!-- NO_STRUCTURE / NO_DATA: reuse the dotted-border empty box, no chart.
		     This is now ONLY the genuine no-bars / no-structure case — the
		     plan-preview (status OK, markers === []) renders the chart below. -->
		<div class="relative border border-dashed border-grid-strong px-3 py-4 text-[11px]">
			{#if payload.status === 'NO_STRUCTURE'}
				<div class="text-fg-muted uppercase tracking-widest mb-1">no structured ladder</div>
				<p class="text-fg-dim leading-relaxed">
					No long entry/exit ladder was generated for this recommendation — the deterministic setup
					engine found no valid structure (downtrend or insufficient ATR base), so there is nothing
					to replay.
				</p>
			{:else}
				<div class="text-fg-muted uppercase tracking-widest mb-1">no bars for this window</div>
				<p class="text-fg-dim leading-relaxed">
					No price bars are available for this window
					<span class="whitespace-nowrap">(RTH-only / Polygon free)</span>, so the ladder cannot be
					drawn on the tape.
				</p>
			{/if}
		</div>
	{:else}
		<!-- Header: section label + lifecycle chip + honesty chip + how? popover -->
		<div class="relative flex flex-wrap items-center justify-between gap-2 mb-3">
			<div class="flex items-center gap-2">
				<div class="text-[10px] uppercase tracking-widest text-cyan">ladder.replay</div>
				<!-- Lifecycle status chip: closed (history) / open (live) / planned. -->
				<span
					class="inline-flex items-center px-1.5 py-0.5 text-[9px] uppercase tracking-widest border whitespace-nowrap {statusChipClasses}"
					data-testid="ladder-lifecycle-chip"
				>
					{#if lifecycle === 'CLOSED'}
						closed · {fmtR(payload.realized_r)}
					{:else if lifecycle === 'OPEN'}
						open · day {payload.holding_days_elapsed ?? '?'} · {fmtR(payload.open_r)} unrealized
					{:else}
						planned · not triggered · 0 fills
					{/if}
				</span>
			</div>
			<div class="flex items-center gap-3">
				<!-- Persistent SIM chip — always visible, never collapsible. -->
				<ChipTip
					term="SIM · modeled fills"
					body="All fills and exits are bar-replay modeled from the historical ladder geometry, not executed trades. There is no broker, no slippage and no commission in these markers."
				>
					{#snippet chip()}
						<span
							class="inline-flex items-center gap-1 px-1.5 py-0.5 bg-amber/20 text-amber text-[9px] uppercase tracking-widest border border-amber/40 whitespace-nowrap"
						>
							<Crosshair class="size-2.5" aria-hidden="true" /> SIM · modeled fills
						</span>
					{/snippet}
				</ChipTip>
				<JargonTip term="how?" full="modeled fills &amp; RTH caveat" body={howBody}>
					<span class="text-[10px] uppercase tracking-widest text-fg-muted">(?) how?</span>
				</JargonTip>
			</div>
		</div>

		<!-- Freshly-opened caption: an OPEN trade with no TP yet and only a few
		     in-trade bars hasn't had a chance to reach any target/stop. -->
		{#if freshlyOpened}
			<p class="relative mb-2 text-[10px] tracking-wide text-fg-muted normal-case">
				trade just opened — no targets or stop reached yet
			</p>
		{/if}

		<!-- Chart canvas (dark, transparent background to match bg-2 panel). The
		     shading band is an overlay div positioned in JS over the same box. -->
		<div bind:this={chartContainer} class="relative w-full" style="height: 320px;">
			{#if lifecycle !== 'PLANNED'}
				<!-- In-trade shading band — pale long-side green tint from first
				     ENTRY to exit (closed) / last bar (open). Positioned in JS;
				     starts hidden until the first coordinate computation. -->
				<div
					bind:this={shadeBand}
					class="pointer-events-none absolute inset-y-0 bg-green/5 z-0"
					style="display: none;"
					aria-hidden="true"
				></div>
			{/if}
		</div>

		<!-- RTH-only footnote — material correctness caveat, shown on every
		     render, not buried behind the popover. -->
		<p class="relative mt-3 text-[10px] uppercase tracking-widest text-fg-muted/70">
			RTH-only modeled · overnight/pre-market moves not seen{#if shadeDegraded}
				· in-trade band unavailable{/if}
		</p>
	{/if}
</div>
