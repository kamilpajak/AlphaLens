# NO_FILL root-cause + metric-rethink — findings

**Status:** COMPLETE
**Date:** 2026-06-15
**Spec:** `docs/superpowers/specs/2026-06-15-nofill-rootcause-metric-rethink-design.md`
**Plan:** `docs/superpowers/plans/2026-06-15-nofill-rootcause-metric-rethink.md`
**Tool:** `apps/alphalens-research/scripts/diagnose_nofill.py` (read-only; pure core `alphalens_research/diagnostics/nofill.py`)
**Data:** the three VPS parquet stores rsync'd to the Mac on 2026-06-15 (`population_ladders`, `thematic_briefs`, `grouped_daily_history`; grouped-daily newest session = 2026-06-12).

---

## TL;DR

- `NO_FILL` is **39 of 347** outcome rows (11%). It is real but not the dominant outcome — `OPEN`/`PARTIAL_TP_OPEN` (160) and blank/not-yet-classified (132) make up most rows.
- Of the 39 `NO_FILL`, **26 are not yet evaluable** — their 7-session entry window runs past the grouped-daily store's newest session (2026-06-12), i.e. the window has not fully elapsed. This is a store-freshness boundary, **not** a data-coverage failure or a real "miss". Re-run after the nightly top-up catches up to reclassify them.
- The **13 genuinely evaluable** `NO_FILL` rows confirm the working hypothesis: **the dip-buy entry ladder is mismatched to a momentum/catalyst book.** 7 are `MOMENTUM_RAN` (price never dipped to the shallowest tier) and 6 are `AMBIGUOUS` (daily low reached the tier but the minute-level monitor still recorded no fill). **None** were opening-gap (`GAP_UP_ARRIVAL`) or late-touch (`TOUCHED_AFTER_TTL`).
- The lynchpin: among matured evaluable `NO_FILL`, market-excess skews **positive** (`MOMENTUM_RAN` 4 pos / 1 neg; `AMBIGUOUS` 3 pos / 1 neg). The ladder is systematically failing to enter names that then **beat the market** — it discards winners.
- **Decision:** adopt `market_excess_return` as the primary **selection** feedback (it is fill-independent and already the EDGE headline); treat ladder `realized_r` as a separate **entry-model / execution** question. The entry model itself looks mis-specified for this book — scope a future "how to enter" spec, do not change anything now.

---

## Method

For every `population_ladders` outcome row classified `NO_FILL`, reconstruct the entry-tier price path: pull the full entry ladder (E1..E3 + stop) from `thematic_briefs.brief_trade_setup`, build the 7-session entry window (`paper.calendar`, default `XNYS`, `DEFAULT_ORDER_TTL_DAYS=7`) plus a 10-session post-window tail, and read each session's daily `[low, high]` for the ticker from the split-adjusted `grouped_daily_history` store. Classify the cause by precedence: `DATA_GAP` (no setup or a window session missing) → `AMBIGUOUS` (daily low ≤ E1 yet row is NO_FILL) → `TOUCHED_AFTER_TTL` (only the tail dips to E1) → `GAP_UP_ARRIVAL` (arrival opened > 3% above the anchor) → `MOMENTUM_RAN` (never dipped, no gap). Daily resolution; minute escalation reserved for `AMBIGUOUS`. Read-only; no Polygon, no production-path writes.

## Population mix (all 347 rows)

| classification | count |
|---|---|
| OPEN | 134 |
| (blank / not-yet-classified) | 132 |
| NO_FILL | 39 |
| PARTIAL_TP_OPEN | 26 |
| TP_FULL | 6 |
| SL_HIT | 6 |
| nan | 4 |

## NO_FILL cause distribution (39 rows)

| cause | count | evaluable? |
|---|---|---|
| DATA_GAP | 26 | **No** — all are `e1 present, window incomplete`; every missing session is ≥ 2026-06-13 (past the store's 2026-06-12 boundary). Window not yet elapsed. |
| MOMENTUM_RAN | 7 | Yes |
| AMBIGUOUS | 6 | Yes (daily-confident; minute escalation would firm them up) |
| TOUCHED_AFTER_TTL | 0 | — |
| GAP_UP_ARRIVAL | 0 | — |

Confirmed programmatically: 0/26 `DATA_GAP` rows lack a trade-setup; 26/26 are "window incomplete"; every missing session falls on/after 2026-06-13. So `DATA_GAP` here means **too recent to score**, not a coverage hole.

## The 13 evaluable NO_FILL

**MOMENTUM_RAN (7)** — price stayed *just* above the shallowest tier E1 (= `close − 0.5·ATR`) and never dipped to it; `arrival_drift ≈ 0` (no opening gap). The miss is small — `gap_to_e1` is **0.3%–2.1%**:

| ticker | brief_date | gap_to_e1 | market_excess | matured |
|---|---|---|---|---|
| AI | 2026-05-27 | +1.0% | **+0.168** | yes |
| MRCY | 2026-05-27 | +1.8% | **+0.110** | yes |
| VRNS | 2026-05-27 | +0.3% | **+0.117** | yes |
| WK | 2026-05-28 | +2.1% | +0.032 | yes |
| BL | 2026-05-27 | +1.6% | −0.001 | yes |
| ETSY | 2026-06-03 | +0.3% | +0.017 | no |
| SCI | 2026-06-03 | +0.9% | +0.114 | no |

**AMBIGUOUS (6)** — daily low reached E1 (within the 0.25% touch band) yet the minute-level RTH monitor recorded no fill (touch within eps / intraday path / RTH filtering). First touch on session 1–3:

| ticker | brief_date | days_to_first_touch | market_excess | matured |
|---|---|---|---|---|
| MUSA | 2026-05-28 | 2 | **+0.205** | yes |
| MANH | 2026-05-27 | 1 | **+0.078** | yes |
| WK | 2026-05-27 | 2 | +0.032 | yes |
| KVYO | 2026-05-28 | 1 | −0.006 | yes |
| HCSG | 2026-06-03 | 1 | +0.131 | no |
| CART | 2026-05-30 | 3 | +0.020 | no |

**Matured NO_FILL — cause × sign(market_excess):**

| cause | neg | pos |
|---|---|---|
| MOMENTUM_RAN | 1 | 4 |
| AMBIGUOUS | 1 | 3 |

9 matured evaluable rows: **7 positive, 2 negative** market-excess. Small N — read as direction, not a test.

## Interpretation

The entry ladder asks for a pullback (`low ≤ close − 0.5·ATR`, a static signal-time anchor, 7-session TTL). On a catalyst/momentum book the names that work tend to rise without giving that pullback back — and the data shows the miss is often a fraction of a percent (`gap_to_e1` as low as 0.3%). The `AMBIGUOUS` group shows even when the *daily* low grazes E1, the minute path frequently doesn't fill. Both groups skew to positive market-excess: **the ladder's no-fills are disproportionately the winners.** That is a structural mismatch between the *entry model* and the *selection edge* — not a screener problem.

Crucially, `market_excess_return` is anchored to `reference_close` (arrival VWAP) and is recorded **regardless of fill**, so the selection signal on these names was never lost — only the ladder-based `realized_r` was.

## Decision (metric-rethink)

1. **`market_excess_return` is the primary SELECTION feedback.** Fill-independent, present on NO_FILL, already the EDGE headline. Selection quality (does the funnel pick names that beat the market?) must be judged on this, not on `realized_r`, which collapses to a handful of filled rows and is confounded by the entry model.
2. **Ladder `realized_r` is demoted to a separate ENTRY-MODEL / EXECUTION question.** It answers "given we tried to enter this way, how did the trade-management do" — a real but distinct question, gated on far more filled rows than the selection metric needs.
3. **The entry model looks mis-specified for this book.** A dip-buy ladder on momentum names structurally misses winners. This warrants a *separate future spec* (candidate ideas: an arrival/market fill tier, a longer entry TTL, or a momentum-aware ladder) — **not changed here.** Any such change is a production-path decision needing its own design + adversarial review.

## Caveats

- **Small N.** 13 evaluable, 9 matured. Everything above is descriptive / directional, never a hypothesis test (per project doctrine: telemetry-only, no self-driving re-weight).
- **Daily resolution.** `AMBIGUOUS` (6) needs minute-bar escalation to split "touched within eps but minute path didn't cross" from "genuine daily-vs-minute disagreement". Not required for the headline conclusion.
- **26 deferred.** Re-run `diagnose_nofill.py` after the grouped-daily top-up advances past these windows to reclassify the `DATA_GAP` rows; expect most to land in `MOMENTUM_RAN`/`AMBIGUOUS`/filled, not a new cause.
- **Split-adjustment.** Grouped-daily is `adjusted=true`; a split between brief date and the window would skew the scale. None surfaced here (no implausible `gap_to_e1`), but it remains a per-ticker caveat.
- **TTL.** Reconstruction used `DEFAULT_ORDER_TTL_DAYS=7`; per-row TTL is carried in `ladder_config_version` for cross-time comparability.

## Next steps

1. Re-run after the grouped-daily top-up catches up (clears the 26 deferred).
2. (Optional) minute-escalate the 6 `AMBIGUOUS` to firm up the daily-vs-minute split.
3. Wire the selection-quality read on `market_excess_return` into the EDGE analysis once N grows (ties into the deferred Buffett×EDGE / panel×EDGE correlation milestone, ~2026-09+).
4. If/when the entry-model question is taken up, open a dedicated design spec (arrival/market tier, TTL, momentum-aware ladder) — out of scope here.
