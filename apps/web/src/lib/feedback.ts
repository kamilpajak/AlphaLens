// Client wrapper for the /v1/feedback/* REST endpoints.
//
// Endpoints come from `apps/alphalens-django/feedback/views.py`. Cross-field
// validation rules live server-side (DecisionValidationError → 400) so this
// module stays a thin POST/GET/DELETE shim — no duplication of the locked
// taxonomy invariants.

import { apiFetch } from './api';
import type { Decision, DismissCategory, FeedbackAction, FeedbackTaxonomy } from './types';

export interface DecisionPostBody {
	brief_date: string;
	ticker: string;
	theme: string;
	surfaced_at: string;
	action: FeedbackAction;
	dismiss_category?: DismissCategory | null;
	dismiss_reason?: string | null;
	dismiss_note?: string | null;
	confidence_subjective?: number | null;
}

/** POST /v1/feedback/decisions — create or upsert. */
export async function postDecision(
	body: DecisionPostBody,
	fetcher: typeof fetch = fetch
): Promise<Decision> {
	const res = await apiFetch(
		'/v1/feedback/decisions',
		{
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify(body)
		},
		fetcher
	);
	if (!res.ok) {
		const detail = await res
			.json()
			.then((b) => b?.detail ?? 'unknown error')
			.catch(() => `HTTP ${res.status}`);
		throw new Error(`postDecision failed: ${detail}`);
	}
	return res.json();
}

/** GET /v1/feedback/decisions?brief_date=YYYY-MM-DD — list for a brief. */
export async function listDecisions(
	briefDate: string,
	fetcher: typeof fetch = fetch
): Promise<Decision[]> {
	const res = await apiFetch(
		`/v1/feedback/decisions?brief_date=${encodeURIComponent(briefDate)}`,
		{},
		fetcher
	);
	if (!res.ok) {
		// A missing-feedback-endpoint deploy (e.g. mid-rollout) should not
		// brick the brief page — degrade to "no decisions yet" so the
		// candidate cards still render. Auth failures (401) propagate
		// through apiFetch's session-expired path as usual.
		if (res.status === 404) return [];
		throw new Error(`listDecisions failed: HTTP ${res.status}`);
	}
	const body: { data: Decision[] } = await res.json();
	return body.data;
}

/** DELETE /v1/feedback/decisions/<id> — idempotent undo. */
export async function deleteDecision(
	id: string,
	fetcher: typeof fetch = fetch
): Promise<void> {
	const res = await apiFetch(
		`/v1/feedback/decisions/${encodeURIComponent(id)}`,
		{ method: 'DELETE' },
		fetcher
	);
	if (!res.ok && res.status !== 204) {
		throw new Error(`deleteDecision failed: HTTP ${res.status}`);
	}
}

/** GET /v1/feedback/taxonomy — fetch locked enums for SPA dropdowns. */
export async function getTaxonomy(
	fetcher: typeof fetch = fetch
): Promise<FeedbackTaxonomy> {
	const res = await apiFetch('/v1/feedback/taxonomy', {}, fetcher);
	if (!res.ok) {
		// Hard-fail: without the taxonomy the dropdowns can't render. Caller
		// (page loader) decides whether to propagate as an error page or
		// degrade the FeedbackControls to disabled.
		throw new Error(`getTaxonomy failed: HTTP ${res.status}`);
	}
	return res.json();
}
