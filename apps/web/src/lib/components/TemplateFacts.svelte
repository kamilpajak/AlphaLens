<script lang="ts">
	/**
	 * Typed-fact panel — PR-3 of epic #321.
	 *
	 * Renders the deterministic key/value payload extracted by the template
	 * engine (`alphalens_pipeline/thematic/extraction/templates/`) when a
	 * brief's catalyst event matched a YAML template. Hidden entirely on
	 * Flash-extracted catalysts (`brief_template_id == null`) so flash-only
	 * briefs keep their existing shape.
	 *
	 * The contract is "show what was extracted, do not interpret" — values
	 * render raw, no unit conversion, no rounding, no formatting beyond
	 * column alignment. Matches the prompt-side citation contract that
	 * tells the LLM to use these values verbatim.
	 */
	import { FileCode2 } from 'lucide-svelte';
	import ChipTip from './ChipTip.svelte';
	import StatusPill from './StatusPill.svelte';
	import { fmtUsdCompact } from '$lib/format';

	interface Props {
		templateId: string | null;
		facts: Record<string, unknown> | null;
	}
	let { templateId, facts }: Props = $props();

	const hasFacts = $derived(
		templateId != null && templateId !== '' && facts != null && Object.keys(facts).length > 0
	);

	// Stable display order: alphabetical on key. Matches the prompt-side
	// sort_keys=True so the audit trail and the rendered output line up.
	const entries = $derived(
		hasFacts && facts != null
			? Object.entries(facts).sort(([a], [b]) => a.localeCompare(b))
			: []
	);

	/**
	 * Friendly template_id labels for the badge. The internal id stays
	 * snake_case (Prometheus label-value safety, JSON Schema regex
	 * `^[a-z][a-z0-9_]*$` enforced at template load). The UI badge gets
	 * a hand-tuned label for the 5 ship templates; any future template
	 * falls back to a simple underscore→space transform.
	 *
	 * Friendly ≠ canonical: the raw id stays in the `title=` attribute so
	 * the audit trail is one hover away.
	 */
	const TEMPLATE_ID_LABELS: Record<string, string> = {
		m_and_a_press_release: 'M&A press release',
		earnings_surprise: 'earnings surprise',
		financing_announcement: 'financing announcement',
		guidance_update: 'guidance update',
		regulatory_action: 'regulatory action'
	};

	function templateLabel(id: string): string {
		return TEMPLATE_ID_LABELS[id] ?? id.replace(/_/g, ' ');
	}

	/**
	 * Compact currency for *_usd fields via the shared `fmtUsdCompact`.
	 * Doctrine: the LLM prompt-side instructs verbatim citation (no unit
	 * conversion). The SPA-side renderer is allowed to format for
	 * readability — that contract scopes only the LLM, not the renderer;
	 * the raw integer stays on `data-raw` for the audit trail. Using the
	 * one house formatter keeps a $9B revenue fact reading the same as a
	 * $9B market cap on the same card (billions as `$9.00B`), instead of
	 * the old local 1-decimal `$9.0B` that diverged from every other USD
	 * site. Fact `_usd` values are structurally >= $1M (extractor floor),
	 * so only the shared formatter's B/M bands ever render here.
	 */
	function formatValue(key: string, v: unknown): string {
		if (v == null) return '—';
		if (typeof v === 'number' && key.endsWith('_usd')) return fmtUsdCompact(v);
		if (typeof v === 'object') return JSON.stringify(v);
		return String(v);
	}
</script>

{#if hasFacts}
	<section data-testid="template-facts" class="relative">
		<div class="flex items-center gap-2 mb-2">
			<FileCode2 class="size-3 text-cyan" aria-hidden="true" />
			<div class="text-[10px] uppercase tracking-widest text-cyan">typed.facts</div>
			<ChipTip
				term={`${templateLabel(templateId!)} template`}
				body={`Every fact below was extracted by a deterministic YAML rule — no LLM in the loop, replayable, audited. Cite these values verbatim; the surrounding prose is LLM-generated, the typed facts are not. Internal id: ${templateId}.`}
			>
				{#snippet chip()}
					<StatusPill
						tone="border-grid text-fg-muted"
						label={templateLabel(templateId!)}
						size="9"
						nowrap
						interactive
						data-testid="template-id"
						data-template-id={templateId}
					/>
				{/snippet}
			</ChipTip>
		</div>
		<dl class="grid grid-cols-[max-content_1fr] gap-x-3 gap-y-1 text-[11px] font-mono">
			{#each entries as [key, value] (key)}
				<dt
					class="text-fg-muted uppercase tracking-widest"
					data-testid="template-fact-key"
					data-key={key}
				>
					{key}
				</dt>
				<dd
					class="text-fg whitespace-nowrap"
					data-testid="template-fact-value"
					data-raw={String(value ?? '')}
				>
					{formatValue(key, value)}
				</dd>
			{/each}
		</dl>
	</section>
{/if}
