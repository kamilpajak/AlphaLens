<script lang="ts">
	const layers = [
		{ id: 'L1', name: 'EDGAR Watchdog', what: 'detects S&P 100 filings + macro news', model: 'rule-based + launchd' },
		{ id: 'L2', name: 'Theme Extraction', what: 'distills news → tradeable thematic narrative', model: 'gemini-2.5-flash' },
		{ id: 'L3', name: 'Beneficiary Mapping', what: 'theme → 6-12 small-cap second-order beneficiaries', model: 'gemini-3-pro-preview' },
		{ id: 'V', name: 'Verification Gates', what: 'press · insider · ETF · 10-K (tri-state, post-PR #150)', model: 'polygon + form-4 parquet + EDGAR' },
		{ id: 'L4', name: 'Quant Scorer', what: 'insider × FCFF × Magic Formula × technicals × catalyst-floor', model: 'reused paradigm #11 + #13 scorers' },
		{ id: 'L5', name: 'Brief Generator', what: 'per-candidate WhatsApp-format markdown', model: 'gemini-3-pro-preview / gemini-2.5-flash' }
	];

	const doctrine = [
		'NEVER ask LLM for numerical / real-time data — always pre-compute via authoritative source (yfinance / SimFin / SEC / Form-4 parquet)',
		'tri-state gates: True / False / None — silent false-negatives killed empty briefs',
		'Pro-supplied search keywords (PR #148) instead of hand-curated YAML buckets',
		'cohort-based ranking: Magic Formula on per-day cohort, not universe-wide',
		'zen pre-MERGE codereview on shared surfaces, fixes land as additional commits'
	];
</script>

<div class="max-w-[1100px] mx-auto px-4 py-10">
	<header class="mb-10 fade-up">
		<div class="text-[10px] uppercase tracking-[0.3em] text-fg-muted mb-2">// about</div>
		<h1 class="font-display font-bold text-4xl tracking-tight text-fg">
			How the <span class="text-amber">pipeline</span> works
		</h1>
		<p class="text-fg-dim mt-3 max-w-2xl text-sm leading-relaxed">
			AlphaLens thematic is an event-driven research assistant. It augments — not replaces — a
			WhatsApp investing group's existing workflow. Each pipeline layer is independent and inspectable.
		</p>
	</header>

	<section class="border border-grid bg-bg-1 mb-8 fade-up" style="animation-delay: 0.1s">
		<div class="px-5 py-3 border-b border-grid text-[10px] uppercase tracking-widest text-fg-muted">
			pipeline.layers
		</div>
		<table class="w-full text-sm">
			<tbody>
				{#each layers as l, i}
					<tr class="border-b border-grid last:border-b-0 hover:bg-bg-2">
						<td class="px-5 py-3 w-20 align-top">
							<div class="font-display font-bold text-2xl text-amber">{l.id}</div>
						</td>
						<td class="px-2 py-3 align-top">
							<div class="font-bold text-fg">{l.name}</div>
							<div class="text-fg-dim text-xs mt-0.5">{l.what}</div>
						</td>
						<td class="px-5 py-3 text-right align-top">
							<div class="text-[10px] uppercase tracking-widest text-cyan">{l.model}</div>
						</td>
					</tr>
				{/each}
			</tbody>
		</table>
	</section>

	<section class="border border-grid bg-bg-1 fade-up" style="animation-delay: 0.2s">
		<div class="px-5 py-3 border-b border-grid text-[10px] uppercase tracking-widest text-fg-muted">
			operating.doctrine
		</div>
		<ul class="divide-y divide-grid">
			{#each doctrine as d, i}
				<li class="px-5 py-3 text-sm text-fg-dim flex gap-3">
					<span class="text-amber font-display font-bold">{String(i + 1).padStart(2, '0')}</span>
					<span>{d}</span>
				</li>
			{/each}
		</ul>
	</section>
</div>
