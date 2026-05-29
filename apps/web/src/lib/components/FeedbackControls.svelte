<script lang="ts">
	import { Eye, X, Check, RotateCcw, ChevronDown } from 'lucide-svelte';
	import type {
		Decision,
		DismissCategory,
		FeedbackAction,
		FeedbackTaxonomy
	} from '$lib/types';
	import { deleteDecision, postDecision } from '$lib/feedback';

	interface Props {
		briefDate: string;
		ticker: string;
		theme: string;
		surfacedAt: string;
		taxonomy: FeedbackTaxonomy;
		existing: Decision | null;
	}
	let {
		briefDate,
		ticker,
		theme,
		surfacedAt,
		taxonomy,
		existing
	}: Props = $props();

	// Local mirror of the persisted decision so optimistic updates after
	// click flow through without waiting for a parent re-fetch. Initialised
	// from the loader-provided `existing` value and re-synced after every
	// successful POST/DELETE. When the user navigates to a different brief
	// date the parent's keyed `{#each}` remounts CandidateCard with a fresh
	// `existing`, so the snapshot semantics are intentional here.
	// svelte-ignore state_referenced_locally
	let current = $state<Decision | null>(existing);
	let inflight = $state(false);
	let errorText = $state<string | null>(null);

	// Progressive disclosure state — only one of these is open at a time.
	type Mode = 'idle' | 'more' | 'pick-category' | 'pick-reason' | 'pick-other-note';
	let mode = $state<Mode>('idle');
	let pickedCategory = $state<DismissCategory | null>(null);
	let otherNote = $state('');

	const CATEGORY_LABELS: Record<DismissCategory, string> = {
		thesis_setup: 'thesis & setup',
		risk_quality: 'risk & quality',
		portfolio_style: 'portfolio & style',
		other: 'other'
	};

	// Reason labels — the UI shows these instead of raw snake_case enum
	// values. Kept here (not pushed to backend) so a label tweak doesn't
	// need a Django roundtrip.
	const REASON_LABELS: Record<string, string> = {
		wrong_theme: 'wrong theme',
		too_expensive: 'too expensive / priced in',
		bad_setup: 'bad setup / timing',
		business_management: 'management / governance',
		risk_jurisdiction: 'risk / jurisdiction',
		dont_understand: "don't understand",
		already_have_exposure: 'already have exposure',
		liquidity_too_low: 'too illiquid',
		not_my_style: 'not my style',
		other: 'other'
	};

	async function submit(action: FeedbackAction, dismissCategory: DismissCategory | null = null, dismissReason: string | null = null, dismissNote: string | null = null) {
		inflight = true;
		errorText = null;
		try {
			const stored = await postDecision({
				brief_date: briefDate,
				ticker,
				theme,
				surfaced_at: surfacedAt,
				action,
				dismiss_category: dismissCategory,
				dismiss_reason: dismissReason,
				dismiss_note: dismissNote
			});
			current = stored;
			mode = 'idle';
			pickedCategory = null;
			otherNote = '';
		} catch (err) {
			errorText = err instanceof Error ? err.message : String(err);
		} finally {
			inflight = false;
		}
	}

	async function undo() {
		if (!current) return;
		inflight = true;
		errorText = null;
		const oldId = current.id;
		try {
			await deleteDecision(oldId);
			current = null;
		} catch (err) {
			errorText = err instanceof Error ? err.message : String(err);
		} finally {
			inflight = false;
		}
	}

	function pickCategory(cat: DismissCategory) {
		pickedCategory = cat;
		mode = cat === 'other' ? 'pick-other-note' : 'pick-reason';
	}

	function back() {
		mode = mode === 'pick-reason' || mode === 'pick-other-note' ? 'pick-category' : 'idle';
		if (mode === 'idle') pickedCategory = null;
	}

	function actionLabel(d: Decision): string {
		if (d.action === 'dismissed' && d.dismiss_reason) {
			return `dismissed (${REASON_LABELS[d.dismiss_reason] ?? d.dismiss_reason})`;
		}
		return d.action;
	}
</script>

<div
	class="border-t border-grid bg-bg-2/30 px-4 sm:px-5 py-3"
	data-testid="feedback-controls"
	data-ticker={ticker}
	data-theme={theme}
>
	{#if current}
		<!-- Recorded state -->
		<div class="flex items-center gap-3 flex-wrap">
			<span
				class="inline-flex items-center gap-1.5 px-2 py-1 text-[10px] uppercase tracking-widest text-cyan border border-cyan/30 bg-cyan/10"
				data-testid="feedback-recorded"
			>
				<Check class="w-3 h-3" />
				{actionLabel(current)}
			</span>
			<button
				type="button"
				onclick={undo}
				disabled={inflight}
				class="inline-flex items-center gap-1 text-[10px] uppercase tracking-widest text-fg-muted hover:text-amber transition-colors disabled:opacity-50"
				data-testid="feedback-undo"
			>
				<RotateCcw class="w-3 h-3" />
				undo
			</button>
		</div>
	{:else if mode === 'idle'}
		<!-- Default 2-button row + "more" affordance -->
		<div class="flex items-center gap-2 flex-wrap">
			<button
				type="button"
				onclick={() => submit('interested')}
				disabled={inflight}
				class="inline-flex items-center gap-1.5 px-3 py-1.5 border border-cyan/40 text-cyan text-[10px] uppercase tracking-widest hover:bg-cyan/10 transition-colors disabled:opacity-50"
				data-testid="feedback-interested"
			>
				<Eye class="w-3 h-3" />
				interested
			</button>
			<button
				type="button"
				onclick={() => (mode = 'pick-category')}
				disabled={inflight}
				class="inline-flex items-center gap-1.5 px-3 py-1.5 border border-red/40 text-red text-[10px] uppercase tracking-widest hover:bg-red/10 transition-colors disabled:opacity-50"
				data-testid="feedback-dismiss"
			>
				<X class="w-3 h-3" />
				dismiss
			</button>
			<button
				type="button"
				onclick={() => (mode = 'more')}
				disabled={inflight}
				class="inline-flex items-center gap-1 px-2 py-1.5 text-[10px] uppercase tracking-widest text-fg-muted hover:text-fg transition-colors disabled:opacity-50"
				data-testid="feedback-more"
			>
				<ChevronDown class="w-3 h-3" />
				more
			</button>
		</div>
	{:else if mode === 'more'}
		<!-- Secondary actions: watching / paper_traded / live_traded -->
		<div class="flex items-center gap-2 flex-wrap">
			<button
				type="button"
				onclick={() => submit('watching')}
				disabled={inflight}
				class="px-3 py-1.5 border border-grid-strong text-fg text-[10px] uppercase tracking-widest hover:border-amber hover:text-amber transition-colors disabled:opacity-50"
				data-testid="feedback-watching"
			>
				watching
			</button>
			<button
				type="button"
				onclick={() => submit('paper_traded')}
				disabled={inflight}
				class="px-3 py-1.5 border border-grid-strong text-fg text-[10px] uppercase tracking-widest hover:border-amber hover:text-amber transition-colors disabled:opacity-50"
			>
				paper traded
			</button>
			<button
				type="button"
				onclick={() => submit('live_traded')}
				disabled={inflight}
				class="px-3 py-1.5 border border-grid-strong text-fg text-[10px] uppercase tracking-widest hover:border-amber hover:text-amber transition-colors disabled:opacity-50"
			>
				live traded
			</button>
			<button
				type="button"
				onclick={() => (mode = 'idle')}
				class="px-2 py-1.5 text-[10px] uppercase tracking-widest text-fg-muted hover:text-fg"
			>
				← back
			</button>
		</div>
	{:else if mode === 'pick-category'}
		<!-- Step 1 of dismiss flow: choose category. Max 4 options per Miller's law. -->
		<div class="flex items-center gap-2 flex-wrap" data-testid="feedback-pick-category">
			<span class="text-[10px] uppercase tracking-widest text-fg-muted mr-1">why dismiss:</span>
			{#each ['thesis_setup', 'risk_quality', 'portfolio_style', 'other'] as cat (cat)}
				<button
					type="button"
					onclick={() => pickCategory(cat as DismissCategory)}
					disabled={inflight}
					class="px-3 py-1.5 border border-grid-strong text-fg text-[10px] uppercase tracking-widest hover:border-amber hover:text-amber transition-colors disabled:opacity-50"
					data-testid="feedback-category-{cat}"
				>
					{CATEGORY_LABELS[cat as DismissCategory]}
				</button>
			{/each}
			<button
				type="button"
				onclick={back}
				class="px-2 py-1.5 text-[10px] uppercase tracking-widest text-fg-muted hover:text-fg"
			>
				← back
			</button>
		</div>
	{:else if mode === 'pick-reason' && pickedCategory && pickedCategory !== 'other'}
		<!-- Step 2: choose specific reason within the picked category. 3 options. -->
		<div class="flex items-center gap-2 flex-wrap" data-testid="feedback-pick-reason">
			<span class="text-[10px] uppercase tracking-widest text-fg-muted mr-1">reason:</span>
			{#each taxonomy.categories[pickedCategory] as reason (reason)}
				<button
					type="button"
					onclick={() => submit('dismissed', pickedCategory, reason)}
					disabled={inflight}
					class="px-3 py-1.5 border border-grid-strong text-fg text-[10px] uppercase tracking-widest hover:border-amber hover:text-amber transition-colors disabled:opacity-50"
					data-testid="feedback-reason-{reason}"
				>
					{REASON_LABELS[reason] ?? reason}
				</button>
			{/each}
			<button
				type="button"
				onclick={back}
				class="px-2 py-1.5 text-[10px] uppercase tracking-widest text-fg-muted hover:text-fg"
			>
				← back
			</button>
		</div>
	{:else if mode === 'pick-other-note'}
		<!-- Step 2 (other branch): free-text required. -->
		<form
			class="flex items-center gap-2 flex-wrap"
			onsubmit={(e) => {
				e.preventDefault();
				if (otherNote.trim()) submit('dismissed', 'other', 'other', otherNote.trim());
			}}
		>
			<input
				type="text"
				bind:value={otherNote}
				placeholder="why? (required)"
				maxlength="200"
				class="flex-1 min-w-[12rem] px-2 py-1.5 bg-bg-1 border border-grid-strong text-fg text-xs focus:outline-none focus:border-amber"
				data-testid="feedback-other-note"
			/>
			<button
				type="submit"
				disabled={inflight || !otherNote.trim()}
				class="px-3 py-1.5 border border-amber text-amber text-[10px] uppercase tracking-widest hover:bg-amber/10 transition-colors disabled:opacity-50"
			>
				submit
			</button>
			<button
				type="button"
				onclick={back}
				class="px-2 py-1.5 text-[10px] uppercase tracking-widest text-fg-muted hover:text-fg"
			>
				← back
			</button>
		</form>
	{/if}

	{#if errorText}
		<p
			class="mt-2 text-[10px] uppercase tracking-widest text-red"
			data-testid="feedback-error"
		>
			error: {errorText}
		</p>
	{/if}
</div>
