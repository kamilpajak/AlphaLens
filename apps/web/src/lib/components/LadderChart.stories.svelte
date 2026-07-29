<script module lang="ts">
	import { defineMeta } from '@storybook/addon-svelte-csf';
	import { expect, waitFor } from 'storybook/test';
	import LadderChart from './LadderChart.svelte';
	import type { ChartPayload, ChartBar, ChartMarker } from '$lib/types';

	// ── Payload builders (shapes taken from tests/unit/ladderChart.test.ts) ──

	function bar(time: string, close: number): ChartBar {
		return { time, open: close * 0.99, high: close * 1.01, low: close * 0.98, close, volume: 500_000 };
	}

	function marker(
		kind: ChartMarker['kind'],
		time: string,
		label: string,
		level_id: string | null = label.toLowerCase()
	): ChartMarker {
		return { time, kind, level_id, price: 0, label, ambiguous: false };
	}

	// CLOSED: terminal=true, realized_r set, entry+tp markers fired.
	// Multi-tranche scale-out shape mirrors the test file.
	const CLOSED_PAYLOAD: ChartPayload = {
		status: 'OK',
		ticker: 'NVDA',
		brief_date: '2026-06-13',
		ladder_classification: 'BULLISH_REVERSAL',
		terminal: true,
		holding_days_elapsed: 10,
		realized_r: 1.45,
		open_r: null,
		ambiguous_bars: 0,
		intrabar_rule: 'SL-first',
		rth_only: true,
		bars: [
			bar('2026-06-10', 118),
			bar('2026-06-11', 117),
			bar('2026-06-12', 116),
			bar('2026-06-13', 115),
			bar('2026-06-16', 113),
			bar('2026-06-17', 112),
			bar('2026-06-18', 114),
			bar('2026-06-19', 116),
			bar('2026-06-20', 118),
			bar('2026-06-23', 121)
		],
		price_lines: {
			entry: 113.0,
			tp: [117.0, 121.0],
			stop: 110.5
		},
		markers: [
			marker('ENTRY', '2026-06-16', 'E1', 'e1'),
			marker('TP', '2026-06-19', 'TP1', 'tp1'),
			marker('TP', '2026-06-23', 'TP2', 'tp2')
		]
	};

	// OPEN: terminal=false, ENTRY fired, one pending TP not hit, stop pending.
	const OPEN_PAYLOAD: ChartPayload = {
		status: 'OK',
		ticker: 'AAPL',
		brief_date: '2026-06-20',
		ladder_classification: 'BULLISH_REVERSAL',
		terminal: false,
		holding_days_elapsed: 4,
		realized_r: null,
		open_r: 0.38,
		ambiguous_bars: 0,
		intrabar_rule: 'SL-first',
		rth_only: true,
		bars: [
			bar('2026-06-18', 192),
			bar('2026-06-19', 191),
			bar('2026-06-20', 190),
			bar('2026-06-23', 189),
			bar('2026-06-24', 191),
			bar('2026-06-25', 193),
			bar('2026-06-26', 192)
		],
		price_lines: {
			entry: 189.5,
			tp: [194.0],
			stop: 186.5
		},
		markers: [
			marker('ENTRY', '2026-06-23', 'E1', 'e1')
		]
	};

	// PLANNED: terminal=false, no markers (plan preview — not triggered yet).
	// Still renders bars + dashed price lines — NOT an empty-box fallback.
	const PLANNED_PAYLOAD: ChartPayload = {
		status: 'OK',
		ticker: 'MSFT',
		brief_date: '2026-06-27',
		ladder_classification: 'BULLISH_REVERSAL',
		terminal: false,
		holding_days_elapsed: null,
		realized_r: null,
		open_r: null,
		ambiguous_bars: 0,
		intrabar_rule: 'SL-first',
		rth_only: true,
		bars: [
			bar('2026-06-25', 442),
			bar('2026-06-26', 440),
			bar('2026-06-27', 439),
			bar('2026-06-30', 437),
			bar('2026-07-01', 436)
		],
		price_lines: {
			entry: 436.0,
			tp: [445.0, 452.0],
			stop: 431.5
		},
		markers: []
	};

	// PARTIAL CAPTURE (DFIN case): only E1 filled, so TP1 sold the whole held
	// position and TP2/TP3 were TOUCHED but sold nothing (TP_TOUCHED, drawn dimmed).
	// TP_FULL by class, but capture is really only tp1 — the case the marker split
	// exists for.
	const PARTIAL_CAPTURE_PAYLOAD: ChartPayload = {
		status: 'OK',
		ticker: 'DFIN',
		brief_date: '2026-07-12',
		ladder_classification: 'TP_FULL',
		terminal: true,
		holding_days_elapsed: 4,
		realized_r: 0.19,
		open_r: null,
		ambiguous_bars: 0,
		intrabar_rule: 'SL-first',
		rth_only: true,
		bars: [
			bar('2026-07-10', 44),
			bar('2026-07-13', 46),
			bar('2026-07-14', 48),
			bar('2026-07-15', 49.6),
			bar('2026-07-16', 51)
		],
		price_lines: { entry: 44.13, tp: [46.0, 49.55, 50.91], stop: 34.32 },
		markers: [
			marker('ENTRY', '2026-07-13', 'E1', 'e1'),
			marker('TP', '2026-07-14', 'TP1', 'tp1'),
			marker('TP_TOUCHED', '2026-07-15', 'TP2', 'tp2'),
			marker('TP_TOUCHED', '2026-07-16', 'TP3', 'tp3')
		]
	};

	// GAP-THROUGH (PEGA 2026-07-14 case): a fast gap-down open trades through all
	// three entry rungs AND the stop in ONE session (2026-07-22). Every tier + the
	// SL land on the same bar, so the entry markers collapse into one "E1·E2·E3"
	// arrow and E2/E3 get their own dashed tier lines — the case that motivated
	// both. Real numbers from the population-ladder payload. Note the wide-range
	// plunge bar (25.10–27.50) sits far below the E1 entry line at 29.90.
	const GAP_THROUGH_PAYLOAD: ChartPayload = {
		status: 'OK',
		ticker: 'PEGA',
		brief_date: '2026-07-14',
		ladder_classification: 'SL_HIT',
		terminal: true,
		holding_days_elapsed: 0,
		realized_r: -1.0,
		open_r: null,
		// Verbatim from the real payload: the plunge bar is ambiguous because the
		// entries AND the stop resolve on that one session (sl_first), so the SL
		// marker below carries ambiguous:true — broader than the type doc's "TP∧SL"
		// phrasing, which is why the entries stay ambiguous:false.
		ambiguous_bars: 1,
		intrabar_rule: 'sl_first',
		rth_only: true,
		bars: [
			bar('2026-07-14', 31.07),
			bar('2026-07-15', 31.6),
			bar('2026-07-16', 32.2),
			bar('2026-07-17', 31.4),
			bar('2026-07-20', 30.8),
			bar('2026-07-21', 30.1),
			{ time: '2026-07-22', open: 25.32, high: 27.5, low: 25.1, close: 25.99, volume: 3_200_000 }
		],
		price_lines: { entry: 29.9, tp: [38.14, 43.27, 44.62], stop: 25.91 },
		markers: [
			{ time: '2026-07-22', kind: 'ENTRY', level_id: 'e1', price: 29.9, label: 'E1', ambiguous: false },
			{ time: '2026-07-22', kind: 'ENTRY', level_id: 'e2', price: 28.66, label: 'E2', ambiguous: false },
			{ time: '2026-07-22', kind: 'ENTRY', level_id: 'e3', price: 27.57, label: 'E3', ambiguous: false },
			{ time: '2026-07-22', kind: 'SL', level_id: 'sl', price: 25.91, label: 'SL', ambiguous: true }
		]
	};

	const { Story } = defineMeta({
		title: 'Data-viz/LadderChart',
		component: LadderChart,
		tags: ['autodocs'],
		parameters: { layout: 'padded' }
	});
</script>

<!-- CLOSED: history view — trade fully exited, realized R displayed in the chip. -->
<Story
	name="Closed Trade"
	play={async ({ canvas }) => {
		await waitFor(() => expect(canvas.getByTestId('ladder-lifecycle-chip')).toBeVisible());
		await waitFor(() =>
			expect(canvas.getByTestId('ladder-lifecycle-chip').textContent).toMatch(/closed/i)
		);
	}}
>
	{#snippet template()}
		<div style="width: 34rem; height: 18rem; padding: 2rem 3rem;">
			<LadderChart payload={CLOSED_PAYLOAD} />
		</div>
	{/snippet}
</Story>

<!-- PARTIAL CAPTURE: TP_FULL by class, but only TP1 sold; TP2/TP3 are touched-not-
     sold (dimmed circles, not solid arrows) — the honest signal that three green
     arrows overstate what the ladder captured. -->
<Story name="Partial Capture (touched not sold)">
	{#snippet template()}
		<div style="width: 34rem; height: 18rem; padding: 2rem 3rem;">
			<LadderChart payload={PARTIAL_CAPTURE_PAYLOAD} />
		</div>
	{/snippet}
</Story>

<!-- GAP-THROUGH: all three entry tiers + the stop fill in one gap-down session.
     The entry arrows collapse to one "E1·E2·E3" mark and E2/E3 draw as dashed
     tier lines below the E1 entry line — the fix for "entry looks level with SL"
     and "no E2/E3". -->
<Story
	name="Gap-through (all tiers one bar)"
	play={async ({ canvas }) => {
		await waitFor(() => expect(canvas.getByTestId('ladder-lifecycle-chip')).toBeVisible());
		await waitFor(() =>
			expect(canvas.getByTestId('ladder-lifecycle-chip').textContent).toMatch(/closed/i)
		);
	}}
>
	{#snippet template()}
		<div style="width: 34rem; height: 18rem; padding: 2rem 3rem;">
			<LadderChart payload={GAP_THROUGH_PAYLOAD} />
		</div>
	{/snippet}
</Story>

<!-- OPEN: live view — entry fired, unrealized R shown in the chip. -->
<Story
	name="Open Trade"
	play={async ({ canvas }) => {
		await waitFor(() => expect(canvas.getByTestId('ladder-lifecycle-chip')).toBeVisible());
		await waitFor(() =>
			expect(canvas.getByTestId('ladder-lifecycle-chip').textContent).toMatch(/open/i)
		);
	}}
>
	{#snippet template()}
		<div style="width: 34rem; height: 18rem; padding: 2rem 3rem;">
			<LadderChart payload={OPEN_PAYLOAD} />
		</div>
	{/snippet}
</Story>

<!-- PLANNED: plan preview — bars + dashed price lines, no fills, not triggered yet. -->
<Story
	name="Planned Not Triggered"
	play={async ({ canvas }) => {
		await waitFor(() => expect(canvas.getByTestId('ladder-lifecycle-chip')).toBeVisible());
		await waitFor(() =>
			expect(canvas.getByTestId('ladder-lifecycle-chip').textContent).toMatch(/planned/i)
		);
	}}
>
	{#snippet template()}
		<div style="width: 34rem; height: 18rem; padding: 2rem 3rem;">
			<LadderChart payload={PLANNED_PAYLOAD} />
		</div>
	{/snippet}
</Story>

<!-- SIM chip tooltip: focuses the chip trigger and asserts the honesty body appears.
     Extra vertical padding ensures the tooltip bubble is not clipped above. -->
<Story
	name="SIM Chip Tooltip"
	play={async ({ canvas }) => {
		canvas.getByTestId('chip-tip').focus();
		await waitFor(() =>
			expect(canvas.getByText(/All fills and exits are bar-replay modeled/i)).toBeVisible()
		);
	}}
>
	{#snippet template()}
		<div style="width: 34rem; height: 24rem; padding: 6rem 4rem 2rem;">
			<LadderChart payload={CLOSED_PAYLOAD} />
		</div>
	{/snippet}
</Story>

<!-- No Structure: status NO_STRUCTURE — renders dotted-border empty box, not the chart. -->
<Story
	name="No Structure"
	play={async ({ canvas }) => {
		await waitFor(() =>
			expect(canvas.getByText(/no structured ladder/i)).toBeVisible()
		);
	}}
>
	{#snippet template()}
		<div style="width: 34rem; height: 18rem; padding: 2rem 3rem;">
			<LadderChart
				payload={{
					status: 'NO_STRUCTURE',
					ticker: 'XYZ',
					brief_date: '2026-06-27',
					ladder_classification: 'NO_STRUCTURE',
					terminal: false,
					holding_days_elapsed: null,
					realized_r: null,
					open_r: null,
					ambiguous_bars: 0,
					intrabar_rule: null,
					rth_only: true,
					bars: [],
					price_lines: { entry: null, tp: [], stop: null },
					markers: []
				}}
			/>
		</div>
	{/snippet}
</Story>

<!-- No Data (computing): status NO_DATA — the honest "chart computing — not
     available yet" caption instead of a generic empty box (the daily bars
     backfill on the nightly pipeline run). Same empty-box shape as No
     Structure, only the status + copy differ. -->
<Story
	name="No Data (computing)"
	play={async ({ canvas }) => {
		await waitFor(() => expect(canvas.getByText(/chart computing/i)).toBeVisible());
	}}
>
	{#snippet template()}
		<div style="width: 34rem; height: 18rem; padding: 2rem 3rem;">
			<LadderChart
				payload={{
					status: 'NO_DATA',
					ticker: 'XYZ',
					brief_date: '2026-06-27',
					ladder_classification: 'NO_STRUCTURE',
					terminal: false,
					holding_days_elapsed: null,
					realized_r: null,
					open_r: null,
					ambiguous_bars: 0,
					intrabar_rule: null,
					rth_only: true,
					bars: [],
					price_lines: { entry: null, tp: [], stop: null },
					markers: []
				}}
			/>
		</div>
	{/snippet}
</Story>

<!-- Context Reused: OK payload (CLOSED fixture — real bars/markers, unchanged)
     but the producer-side lead-in/trailing CONTEXT band was deadline-starved
     (PR #912). The trade itself renders exactly as "Closed Trade"; only the
     subtle footnote below qualifies the surrounding band. -->
<Story
	name="Context Reused"
	play={async ({ canvas }) => {
		await waitFor(() =>
			expect(canvas.getByText(/context band incomplete/i)).toBeVisible()
		);
	}}
>
	{#snippet template()}
		<div style="width: 34rem; height: 18rem; padding: 2rem 3rem;">
			<LadderChart payload={{ ...CLOSED_PAYLOAD, context: 'reused' }} />
		</div>
	{/snippet}
</Story>

<!-- Context In-Trade-Only: same freshness footnote, different producer-side
     reason (band computed only through the in-trade window). OPEN fixture —
     real bars/markers, unchanged. -->
<Story
	name="Context In-Trade-Only"
	play={async ({ canvas }) => {
		await waitFor(() =>
			expect(canvas.getByText(/context band incomplete/i)).toBeVisible()
		);
	}}
>
	{#snippet template()}
		<div style="width: 34rem; height: 18rem; padding: 2rem 3rem;">
			<LadderChart payload={{ ...OPEN_PAYLOAD, context: 'in_trade_only' }} />
		</div>
	{/snippet}
</Story>
