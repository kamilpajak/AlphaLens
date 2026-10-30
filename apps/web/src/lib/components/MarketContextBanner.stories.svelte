<script module lang="ts">
	import type { ComponentProps } from 'svelte';
	import { defineMeta } from '@storybook/addon-svelte-csf';
	import { expect, waitFor } from 'storybook/test';
	import MarketContextBanner from './MarketContextBanner.svelte';
	import laneDay from '../../../tests/fixtures/api-mock/days/2026-09-05.json';

	type BannerProps = ComponentProps<typeof MarketContextBanner>;

	// EVERY NUMBER IN THIS FILE IS MEASURED, NOT INVENTED.
	//
	// The index-level payload is identical on every candidate of a date, so the
	// first one carries it. This is the only day fixture that holds a
	// market_state payload at all (the other three predate the signal).
	const ctx = laneDay.candidates[0];

	// The three 5-session index returns for the SAME asof (2026-09-05), computed
	// from the real grouped-daily store on the author's machine on 2026-10-30 by
	// the production function itself:
	//
	//   five_session_index_returns(date(2026, 9, 5))
	//   -> {spy: 0.0026844767215665755, qqq: -0.004770423375074606,
	//       iwm: -0.015409759514359078}
	//
	// Honest caveat kept here rather than smoothed away: that mirror's newest
	// session is 2026-09-03, so the anchor rolled back two sessions (inside the
	// staleness bound). The VPS store reaches 2026-09-05 and would therefore
	// answer a slightly different trio for the same date. These are real numbers
	// this code really produced, not the number production will hold.
	const MEASURED_RET5 = {
		ret5Spy: 0.0026844767215665755,
		ret5Qqq: -0.004770423375074606,
		ret5Iwm: -0.015409759514359078
	};

	const fromFixture: BannerProps = {
		marketState: ctx.market_state,
		atrPctQ: ctx.market_state_atr_pct_q,
		dist200: ctx.market_state_dist200,
		vix: ctx.market_state_vix,
		vixDecile: ctx.market_state_vix_decile,
		squeezeOn: ctx.market_state_squeeze_on
	};

	// The tone matrix varies ONLY the label. Every driver is null, because no
	// fixture exists for bull_volatile / bear_volatile / bear_quiet and
	// plausible-looking telemetry would be invented data on the page.
	const labelOnly = (marketState: string): BannerProps => ({ marketState });

	// Open the "more" disclosure so the residual drawer is visible for review and
	// asserted present. The toggle is a real <button>, so a click is
	// deterministic under headless capture.
	const openMore = async ({ canvas }: { canvas: any }) => {
		canvas.getByRole('button', { name: /more/i }).click();
		await waitFor(() => expect(canvas.getByText(/label unvalidated/i)).toBeTruthy());
	};

	const { Story } = defineMeta({
		title: 'Briefs/MarketContextBanner',
		component: MarketContextBanner,
		tags: ['autodocs'],
		parameters: { layout: 'padded' },
		render: template
	});
</script>

{#snippet template(args: BannerProps)}
	<!-- Wrapped in a header-like container: the strip is a border-t footer, so it
	     previews accurately as the bottom row of the brief session box. -->
	<div style="padding: 1.5rem;">
		<div class="border border-grid bg-bg-1">
			<div class="px-4 sm:px-6 py-5">
				<div class="text-[10px] uppercase tracking-[0.3em] text-fg-muted">// session</div>
				<div class="font-display font-bold text-3xl text-amber tracking-tight mt-1">2026-09-05</div>
			</div>
			<MarketContextBanner {...args} />
		</div>
	</div>
{/snippet}

<!-- THE DEFAULT: the real captured fixture, exactly as the API serves it today.
     The three index returns are em dashes because no brief parquet carries them
     until the backfill runs — which is the true state of every published date
     before that. -->
<Story name="real fixture — returns not yet backfilled" args={fromFixture} />

<!-- The same date WITH the three returns, so the full face can be reviewed. -->
<Story
	name="real fixture + measured 5-session returns"
	args={{ ...fromFixture, ...MEASURED_RET5 }}
/>

<!-- Everything absent: a date that predates both families. Proves every cell
     renders an em dash rather than a 0 or a blank. -->
<Story name="all absent (em dashes)" args={labelOnly('unknown')} />

<!-- THE DOMINANT HISTORICAL STATE after the backfill, and the one nobody designed:
     the label is `unknown` because market_state only ships from 2026-07-05, while
     the three returns ARE real because the price store reaches back to 2024-09-11.
     Measured on production Postgres 2026-10-30: 163 brief dates, of which the ~47
     from 2026-05-19 to 2026-07-04 land here — 29% of everything a reader can scroll
     back to. The strip must read as "no regime word for this date, but here is what
     the market did", never as an error. -->
<Story
	name="unknown label, real returns (pre-2026-07-05 dates)"
	args={{ ...labelOnly('unknown'), ...MEASURED_RET5 }}
/>

<!-- Tone matrix — label only, drivers deliberately null (see labelOnly). Tone is
     display-only (green / amber / red / red-dim); never a buy/avoid signal. -->
<Story name="tone — bull-quiet (green)" args={labelOnly('bull_quiet')} />
<Story name="tone — bull-volatile (amber)" args={labelOnly('bull_volatile')} />
<Story name="tone — bear-volatile (red)" args={labelOnly('bear_volatile')} />
<Story name="tone — bear-quiet (muted red)" args={labelOnly('bear_quiet')} />

<!-- The residual drawer expanded: vix %ile + squeeze + the unvalidated caption.
     The promoted drivers are NOT repeated in it. -->
<Story
	name="drawer expanded"
	args={{ ...fromFixture, ...MEASURED_RET5 }}
	play={openMore}
/>
