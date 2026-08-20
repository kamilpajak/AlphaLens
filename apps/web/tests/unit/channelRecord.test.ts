import { describe, it, expect } from 'vitest';
import { channelRecord, CAUSAL_SUPPORT_NOT_A_FORECAST } from '$lib/format';
import type { Candidate } from '$lib/types';

/**
 * #1069 — the card must show what the prose was written against.
 *
 * Two axes, deliberately kept apart (#1068): SUPPORT is how well the event text
 * carries a mechanism, GROUNDING is whether the question applied at all. A
 * misrouted event can still elicit a confident chain, so grounding is never a
 * fourth support level and the display must never fold them into one word.
 *
 * Emphasis policy: quiet by default — honest uncertainty is the COMMON case and
 * flagging it would train the reader to ignore the flag. Emphasis is reserved
 * for a broken measurement (grounding failed, or the assessor never answered)
 * and for prose the guard had to withhold.
 */

function candidate(overrides: Partial<Candidate> = {}): Candidate {
	return {
		brief_causal_support: 'suggestive',
		brief_channel_grounding: 'grounded',
		brief_support_guard_status: 'not_applicable',
		channel_type: 'category_attention',
		channel_text: 'LNP royalty demand rises if the platform is validated.',
		channel_evidence: 'First mRNA Cancer Vaccine to Succeed in a Phase 3 Trial.',
		channel_falsifier: 'If the LNP patents are not required, the channel fails.',
		channel_grounding_quote: 'Moderna and Merck Just Made History',
		channel_grounding_reason: '',
		...overrides
	} as unknown as Candidate;
}

describe('channelRecord', () => {
	it('returns null for a row that predates the assessor', () => {
		// A pre-#1066 row has NULL status columns. It must render nothing at all —
		// growing a "not assessed" line on every historical card would state a
		// finding about rows the instrument never saw.
		expect(channelRecord(candidate({ brief_causal_support: null }))).toBeNull();
	});

	it('distinguishes suggestive from not_established in words, not only in tone', () => {
		const suggestive = channelRecord(candidate({ brief_causal_support: 'suggestive' }));
		const notEstablished = channelRecord(candidate({ brief_causal_support: 'not_established' }));
		expect(suggestive?.headline).not.toEqual(notEstablished?.headline);
		expect(suggestive?.headline).toMatch(/implies/);
		expect(notEstablished?.headline).toMatch(/does not state/);
	});

	it('states the established level without claiming a forecast', () => {
		const rec = channelRecord(candidate({ brief_causal_support: 'established' }));
		expect(rec?.headline).toMatch(/states a link/);
		expect(rec?.headline).not.toMatch(/will|expect|forecast|upside/i);
	});

	it('keeps honest uncertainty QUIET when the measurement was sound', () => {
		// not_established + grounded is the instrument working correctly and
		// reporting a weak link. Emphasising it would make the flag meaningless
		// on the rows that genuinely need it.
		const rec = channelRecord(
			candidate({ brief_causal_support: 'not_established', brief_channel_grounding: 'grounded' })
		);
		expect(rec?.emphasis).toBe('quiet');
		expect(rec?.groundingNote).toBeNull();
	});

	it('flags a theme misroute and names it as a pipeline defect', () => {
		const rec = channelRecord(
			candidate({
				brief_causal_support: 'not_established',
				brief_channel_grounding: 'theme_misroute'
			})
		);
		expect(rec?.emphasis).toBe('flag');
		expect(rec?.groundingNote).toMatch(/does not concern this theme/);
	});

	it('flags a candidate misfit', () => {
		const rec = channelRecord(candidate({ brief_channel_grounding: 'candidate_misfit' }));
		expect(rec?.emphasis).toBe('flag');
		expect(rec?.groundingNote).toMatch(/does not involve this company/);
	});

	it('flags an assessor outage as not assessed, never as a weak link', () => {
		// no_record means no model ever answered. Rendering it as "the event does
		// not state a link" would assert a judgement nothing made.
		const rec = channelRecord(
			candidate({ brief_causal_support: 'no_record', brief_channel_grounding: 'unknown' })
		);
		expect(rec?.emphasis).toBe('flag');
		expect(rec?.headline).toMatch(/not assessed/);
		expect(rec?.headline).not.toMatch(/does not state/);
	});

	it('flags a grounding the instrument could not answer', () => {
		const rec = channelRecord(candidate({ brief_channel_grounding: 'unknown' }));
		expect(rec?.emphasis).toBe('flag');
		expect(rec?.groundingNote).toMatch(/not assessed/);
	});

	it('flags withheld prose but does not repeat the blockquote label', () => {
		// A withheld row always carries brief_status "unavailable" +
		// brief_error_kind "unsupported_benefit_claim", and the blockquote already
		// says the wording was withheld. A second sentence saying it again reads as
		// two separate problems, so the record raises the flag and stays silent.
		const rec = channelRecord(candidate({ brief_support_guard_status: 'withheld' }));
		expect(rec?.emphasis).toBe('flag');
		expect(rec?.guardNote).toBeNull();
	});

	it('reports a guard fire the row would otherwise never mention', () => {
		// fired_unrecovered: the guard fired, then the retry died for an unrelated
		// reason, so brief_error_kind names the LLM's failure and nothing else on
		// the card would ever surface the fire.
		const rec = channelRecord(candidate({ brief_support_guard_status: 'fired_unrecovered' }));
		expect(rec?.emphasis).toBe('flag');
		expect(rec?.guardNote).toMatch(/no compliant rewrite/);
	});

	it('reports a repaired draft without raising the alarm', () => {
		// The shipped text is the compliant rewrite, so nothing is wrong with what
		// the reader sees — but the fact that a first draft overclaimed belongs in
		// the record.
		//
		// The fixture is not_established + grounded ON PURPOSE. The guard only runs
		// when guard_applies() is true (bottom support level, no record, or a
		// grounding failure), so `repaired` beside the default `suggestive` would
		// be a state the pipeline cannot produce. This combination is the one
		// reachable way for a repair to happen on a row that still reads quiet.
		const rec = channelRecord(
			candidate({
				brief_causal_support: 'not_established',
				brief_channel_grounding: 'grounded',
				brief_support_guard_status: 'repaired'
			})
		);
		expect(rec?.emphasis).toBe('quiet');
		expect(rec?.guardNote).toMatch(/rewritten/);
	});

	it('says nothing about the guard when it never applied', () => {
		const rec = channelRecord(candidate({ brief_support_guard_status: 'not_applicable' }));
		expect(rec?.guardNote).toBeNull();
	});

	it('carries the raw tokens through for the audit trail', () => {
		const rec = channelRecord(candidate());
		expect(rec?.support).toBe('suggestive');
		expect(rec?.grounding).toBe('grounded');
	});

	it('states plainly that causal support is not a return forecast', () => {
		expect(CAUSAL_SUPPORT_NOT_A_FORECAST).toMatch(/not a forecast of the share price/);
	});
});
