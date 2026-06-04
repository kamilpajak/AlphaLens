import type { EntryTier } from '$lib/types';

/**
 * Forward-looking (ex-ante) risk geometry derived from a trade setup.
 *
 * Mirrors the canonical Python in
 * `alphalens_pipeline/feedback/population_ladder_monitor.py`
 * (`_full_ladder_blended_entry` + the signal-time half of `_size_fields`).
 * These are computable from the setup the candidate card already shows — they
 * carry NO realized/outcome data (those are the backward-looking monitor
 * fields and live elsewhere). The bridge from "% of book" (gross exposure) to
 * a risk number is the STOP DISTANCE, never `size` alone.
 */

/**
 * Alloc-weighted blended entry over ALL intended entry tiers — the average
 * fill price if the whole ladder fills. Uses `alloc_pct` weights with an
 * equal-weight fallback when allocs are absent/zero (matches the engine's
 * `_blended_entry`). Returns `null` when no tier has a finite limit price.
 */
export function fullLadderBlendedEntry(tiers: EntryTier[] | null | undefined): number | null {
	if (!tiers || tiers.length === 0) return null;
	const prices: number[] = [];
	let weightSum = 0;
	let priceWeightSum = 0;
	for (const tier of tiers) {
		const price = Number(tier.limit);
		if (!Number.isFinite(price)) continue;
		prices.push(price);
		const weight = Number(tier.alloc_pct);
		if (Number.isFinite(weight) && weight > 0) {
			weightSum += weight;
			priceWeightSum += price * weight;
		}
	}
	if (prices.length === 0) return null;
	if (weightSum > 0) return priceWeightSum / weightSum;
	return prices.reduce((acc, p) => acc + p, 0) / prices.length; // equal-weight fallback
}

/**
 * Stop distance as a FRACTION of the (full-ladder) blended entry:
 * `(blended − stop) / blended`. Positive for a long whose stop sits below the
 * entry. Returns `null` on missing / non-finite / zero blended entry.
 */
export function stopDistanceFracFull(
	blendedEntry: number | null | undefined,
	disasterStop: number | null | undefined
): number | null {
	if (blendedEntry == null || disasterStop == null) return null;
	if (!Number.isFinite(blendedEntry) || !Number.isFinite(disasterStop) || blendedEntry === 0) {
		return null;
	}
	return (blendedEntry - disasterStop) / blendedEntry;
}

/**
 * Risk-on-stop as a PERCENT of book if the full ladder fills:
 * `suggested_size_pct × stop-distance fraction`. `suggested_size_pct` is
 * already a percent (e.g. `4.07`), so the result is also in percent units
 * (4.07 × 0.247 ≈ 1.01 → "1.0% of book"). Returns `null` when either input is
 * missing / non-finite.
 */
export function impliedRiskPctOfBook(
	suggestedSizePct: number | null | undefined,
	stopDistanceFraction: number | null | undefined
): number | null {
	if (suggestedSizePct == null || stopDistanceFraction == null) return null;
	if (!Number.isFinite(suggestedSizePct) || !Number.isFinite(stopDistanceFraction)) return null;
	return suggestedSizePct * stopDistanceFraction;
}
