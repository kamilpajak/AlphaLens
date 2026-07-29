# Overnight Price Drift — Implications for the Saxo Auto-Manager

**Status:** RESEARCH (decision memo)
**Date:** 2026-07-29
**Authored via:** Workflow (Perplexity market-microstructure research + codebase trace + pre-mortem → synthesis)

## Questions

1. Can price "drift" while the exchange is closed?
2. When should we place orders?
3. What implications does closed-market drift have for our system?

## 1. Drift mechanisms and magnitudes

- Extended-hours trading (after-hours ~16:00-20:00 ET, pre-market ~7:00-9:25 ET): ECN-based, thin, wide spreads, institution/algo dominated; mid-cap single names trade sporadically. Exchanges are extending toward 22-23h sessions (NYSE Arca / Nasdaq proposals).
- AMC/BMO news releases: after-hours earnings cause price jumps in >90% of cases vs ~2-3% baseline jump probability on non-announcement nights (UCSD; Christensen et al. JFE 2025). The >5% gap tail for mid-caps concentrates almost entirely on the earnings calendar, not random nights.
- Global markets / index futures channel: ES/NQ trade nearly 24h and transmit overseas shocks; mid-caps reprice via the index/sector-ETF channel (MDY) even with zero overnight prints in the name itself.
- Opening auction: consolidates all overnight information into one opening print. Our resting GTD limits participate (Nasdaq Opening Cross + NYSE book persistence confirmed); a gapped-through BUY limit is 'better-priced interest' and fills at the auction print, limit-or-better — matching Saxo's documented semantics. Edge case: auction collars (± max($0.15/0.50, 10%)) can leave marketable interest UNEXECUTED and cancelled back in extreme gaps.
- Magnitudes: index-level 1%+ overnight gaps on ~15-20% of sessions (regime-clustered); mid-cap close-to-open stddev ~0.6-1.0% on normal nights (informed approximation from index/ETF evidence — no clean published mid-cap study; compute directly from our grouped-daily parquet store if precision matters). Overnight vol is LOWER than intraday vol on non-event nights; the tails are event-driven and leptokurtic (multi-percent, 5%+ not uncommon for mid-caps on earnings).
- Overnight premium literature: historically most of the equity premium accrued close-to-open (Cooper; Lou-Polk-Skouras — momentum/reversal alpha ~100% overnight), but the headline futures-level drift (Boyarchenko 2-3am ET hour) has averaged ~zero since 2021 per the NY Fed follow-up. Message for us is not 'chase the drift' but 'overnight is where jump risk and adverse selection live': an overnight-resting buy limit is a short put written to informed traders (Copeland-Galai 1983), systematically picked off around earnings (Linnainmaa JF 2010).

## 2. When to place orders

Placement clock time is almost entirely economically neutral for our plain GTD resting BUY limits — 03:00 vs 15:25 vs post-open changes nothing about how the order rests or fills (acceptance checks run against last close; the order sits in the book identically and participates in each day's opening auction). Three real, second-order effects: (1) TTL day-count wobble — broker.py computes GTD expiry from the UTC date at placement via advance_trading_sessions with session-on-or-after semantics, so placing at 00:30 UTC Monday evening NY anchors the 7-session clock on Tuesday while Monday-RTH placement anchors on Monday: an effective ±1-session TTL artifact around UTC midnight. (2) Which session's gap you meet first — an order placed pre-open catches TODAY's opening auction (today's overnight gap); one placed 15:25 misses today's open and rests through tonight into tomorrow's auction. Placing pre-open therefore adds one auction's gap exposure at the front. (3) Midday is the cheapest, least information-asymmetric execution window (Garvey et al. 2009: open ~6% and close ~2.3% costlier) — but this applies to marketable flow, and our limits are passive, so it is nearly irrelevant. RECOMMENDATION: keep the current 'place whenever armed' behavior — it is fine — but make WHAT rests the gate, not WHEN: never let entries rest across a known earnings date in the GTD window, and prefer arming/draining fresh picks (age <= 1-2 sessions) so the geometry placed still describes the market it was computed from. If a tie-break is wanted, draining during RTH midday gives the cleanest TTL anchoring and one fewer auction of exposure vs pre-open placement, at zero cost.

## 3. Our exposure (ranked)

### Rank 1: A2: gap-open BELOW the disaster stop -> potentially permanently naked position

- **Severity:** HIGH-CRITICAL · **Plausibility:** LOW per ticker (needs >20-30% gap) but the failure mode is unbounded
- **Mechanism:** Marketable resting BUY limits fill at the open below the stop; the daemon then submits a SELL StopIfTraded whose trigger is ABOVE market. If Saxo rejects it (unverified — the pivotal unknown), control_loop.py:2109-2126 _execute_place_stop degenerates into a throttled reject-retry loop forever: naked position guarded only by a Telegram alert. No clamp-to-below-market / market-sell fallback arm exists in the code. There is also no guard preventing new entry placement when market is already below the brief's stop.
- **Mitigation:** Deliberately probe stop-above-market semantics on SIM (place a stop above market, observe reject vs instant fire); then add a fallback arm: on structural stop reject, clamp trigger below current market or market-sell out.
- **SIM-learnable:** YES — the probe settles the Saxo branch for free; run it, do not wait for a real gap.

### Rank 2: G: correlated sector-wide overnight shock (13 defense mid-caps = one factor bet)

- **Severity:** CRITICAL · **Plausibility:** LOW-MED per week, but a standing exposure
- **Mechanism:** 43 resting limits are collectively a short put on the defense factor. A weekend ceasefire/peace headline gaps the whole basket down: worst case ALL tiers of ALL tickers fill in the same opening auctions — maximum capital deployed at the single worst moment, exactly when the shared thesis broke — while simultaneously triggering the burst-load scenario and possibly several A2 loops at once. This costs the account, not a position.
- **Mitigation:** Sizing-policy, not code: cap simultaneous resting notional per factor; compute worst-case aggregate fill-at-open loss (sum over all tiers of tier-to-stop distance x qty) and treat it as the binding position-sizing constraint.
- **SIM-learnable:** NO — mechanics yes, P&L distribution never (rare event; no soak is long enough). This is a policy decision, not an observation.

### Rank 3: J: earnings-calendar blindness — currently scheduled, not tail, risk

- **Severity:** HIGH · **Plausibility:** HIGH (the calendar is KNOWN)
- **Mechanism:** Nothing gates arming/placement across a scheduled AMC/BMO earnings date inside the ~7-session GTD window. Current book expires Aug 4-7 — exactly the Q2 defense reporting window (KTOS/AVAV et al. report early August, per general knowledge — verify against the AV cache). The system is systematically short earnings gaps on all 13 names; per Linnainmaa this is where resting limits are picked off. The T-1-frozen geometry is most wrong exactly then.
- **Mitigation:** At arm/drain: skip, disarm, or shorten TTL for any ticker with a confirmed earnings date inside the GTD window. The AV EARNINGS cache (~/.alphalens/av_cache/) already exists. Cheapest highest-value fix in the whole memo.
- **SIM-learnable:** YES — the August soak runs this experiment whether intended or not; decide consciously.

### Rank 4: B: stale geometry placed verbatim (no brief-age or market-sanity gate)

- **Severity:** MED · **Plausibility:** HIGH
- **Mechanism:** Verified by trace: geometry is frozen from cached T-1 OHLCV at brief time and placed verbatim; picks.py carries only {ticker,date,armed_ts}; no age, re-anchor, or distance-to-market check anywhere (broker.py validates only internal ordering). Upward drift = opportunity cost (theatre). Downward drift is the danger: a week-old pullback tier becomes a breakdown level; a BUY limit can sit ABOVE current market and fill instantly at the open with compressed fill-to-stop distance and a stale thesis. TTL is placement-anchored, so a 3-day-stale brief still rests a fresh 7 full sessions. catalyst_failure_exit is prose-only (zero code consumers).
- **Mitigation:** Max-age gate at drain (refuse picks older than N sessions, e.g. 2) plus a placement-time sanity band of tier-vs-last-close distance.
- **SIM-learnable:** Mechanics yes; whether stale entries actually lose money is EDGE data (months).

### Rank 5: K: oco_unsupported permanence ratchet on volatile opens

- **Severity:** LOW-MED · **Plausibility:** HIGH on gap days
- **Mechanism:** control_loop.py:1943 marks a uic oco_unsupported on ANY clean OrderRejectedError from OCO placement; the journal fold has no un-mark/TTL path. TooFarFromMarket at a violent open is transient and price-dependent, not an instrument incapability — one bad open permanently degrades a ticker to stop-only, silently shifting the live exit distribution away from the researched TP ladder. Safe direction (never naked) but quietly undoes the Stage-2/3 OCO work.
- **Mitigation:** Classify TooFarFromMarket as transient and TTL the marker (pattern already exists: amend_recently_failed); keep permanent marking for structural rejects.
- **SIM-learnable:** YES — count degrade events per gap day during soak.

### Rank 6: A1 + attribution gap: gap-through fills are informationally adverse and untagged

- **Severity:** MED · **Plausibility:** MED per ticker, HIGH portfolio-wide over the soak
- **Mechanism:** Open between deepest tier and stop: all tiers fill at the auction print, protection places validly, position starts at/near max planned loss instantly — machinery works, loss = gap. Per Copeland-Galai/Linnainmaa the gap-through cohort is a winner's-curse trade (price-improved vs limit, into a downward information shock) and should underperform intraday-touch fills — but no published study isolates it, and EDGE currently cannot distinguish the cohorts.
- **Mitigation:** Tag fills at/near the opening print with price-improvement vs limit as a distinct 'gap-through' cohort in EDGE attribution; measuring it on our own ladder data is cheap and decisive.
- **SIM-learnable:** Tagging plumbing yes; the sign of the effect needs EDGE N (months).

### Rank 7: Auction-collar edge case: gapped-through Working order may NOT fill

- **Severity:** LOW-MED · **Plausibility:** LOW
- **Mechanism:** In an extreme gap beyond the auction collar (± max($0.15/0.50, 10%)), marketable interest incl. better-priced buy limits can go unexecuted and be cancelled back. The reconcile loop must not assume a gapped-through Working order always fills at the open.
- **Mitigation:** Verify reconcile.py treats an unexpectedly-cancelled Working entry as benign terminal (it classifies CANCELLED terminal -> CancelRemaining sweep — likely already handled; confirm on the entry path).
- **SIM-learnable:** Partially — SIM auction fidelity is itself uncertain.

### Rank 8: F: auction-burst operational load degrades the never-naked latency bound

- **Severity:** MED · **Plausibility:** MED
- **Mechanism:** Single sequential executor; Saxo client 429/5xx backoffs sleep 1-120s / (5,15,30)s INSIDE the tick. A correlated 13-ticker gap open stacking stop POSTs + OCO attempts + fallbacks can push the last uic's protection latency from <=1 tick to tick + sum-of-backoffs — plausibly minutes. Bounded and alerting, not silent.
- **Mitigation:** Observe burst choreography during soak; if latency is unacceptable, prioritize protective stops ahead of all other actions in the executor queue.
- **SIM-learnable:** Choreography yes; SIM rate-limit budgets and auction realism differ from live — final calibration is real-money-only. SIM fills are idealized, so SIM will UNDERSTATE gap losses and stop-exit slippage.

### Rank 9: H: overnight corporate actions invalidate cached geometry wholesale

- **Severity:** HIGH per event · **Plausibility:** LOW
- **Mechanism:** A 2:1 split leaves BUY limits ~100% above post-split price -> instant off-plan auction fill; stop at ~2x market -> A2 loop or instant fire. Nothing in the drain/placement path is corporate-action aware.
- **Mitigation:** Operational checklist: scan the corporate-action calendar for the 13 names before arming; disarm across the event. Splits are pre-announced.
- **SIM-learnable:** Barely — checklist-mitigated instead.

### Rank 10: I: no live time-stop + GTC exits = overnight risk accumulates (design drift, not bug)

- **Severity:** MED · **Plausibility:** CERTAIN
- **Mechanism:** Every held position re-exposes to every future overnight event until TP or disaster stop, with no 42-session time-stop as in research — the live P&L object diverges from the researched object specifically through overnight events. Deliberate per design.
- **Mitigation:** No code change; log as a prime suspect for the ~2026-09 EDGE-vs-live comparison when outcomes diverge.
- **SIM-learnable:** N/A — a flag for future attribution, not an experiment.

## Recommendation

Keep the architecture — resting GTD pullback limits with never-naked post-fill protection is sound, the multi-tier gap-fill machinery is genuinely well-covered, and any surviving overnight premium accrues to our held GTC positions passively. Do NOT switch to place-at-open-only or day-orders-only (alternatives considered: day-orders re-placed each open would forfeit auction participation and add daily churn for no adverse-selection benefit, since the pick-off risk is event-driven, not session-boundary-driven; midday-only placement optimizes a cost we do not pay as passive limits). The problem is not WHEN we place but WHAT we allow to rest and what happens at the two unguarded edges. Fix, in order: (1) probe and then guard the A2 stop-above-market branch — it is the only path to a silently-permanent naked position; (2) stop resting entries across known earnings dates — this converts our largest scheduled loss channel (Linnainmaa pick-off, currently live with Aug 4-7 expiries sitting on the Q2 defense reporting window) into a one-line gate using a cache we already have; (3) cap worst-case aggregate fill-at-open loss across the correlated 13-name book as a sizing policy, because SIM can never price that tail; (4) add the cheap freshness gates (pick max-age, TooFarFromMarket-transient TTL on oco_unsupported) and the gap-through EDGE cohort tag so the soak produces decisive data instead of anecdotes. Honest uncertainties that cap confidence: Saxo's stop-above-market behavior and SIM's auction-fill fidelity are unverified (SIM likely understates gap losses); mid-cap gap magnitudes are extrapolated from index/ETF evidence; early-August earnings dates for the 13 names are from general knowledge and must be checked against the AV cache before acting.

## Action items (ranked)

- 1. SIM probe (this week, ~1h): deliberately place a SELL StopIfTraded with trigger above current market on SIM and observe reject vs instant-fire. This settles the pivotal A2 branch for free. Then implement the fallback arm in _execute_place_stop (control_loop.py:2109-2126): on structural stop reject, clamp trigger below current market or market-sell out — never loop naked.
- 2. Earnings gate (small PR): at arm and/or drain, skip or shorten TTL for any ticker with a confirmed earnings date inside the GTD window, sourced from the existing AV EARNINGS cache (~/.alphalens/av_cache/). First verify the actual early-August dates for the 13 names against the cache. Highest value-per-line in the memo — the current book (expiries Aug 4-7) is live exposure.
- 3. Sizing policy decision (no code): compute worst-case aggregate fill-at-open loss = sum over all 43 resting tiers of (tier price - stop) x qty, and adopt it as a per-factor cap on simultaneous resting notional. SIM cannot learn this; decide it as policy.
- 4. Pick max-age gate at drain (small PR): refuse to place picks older than N sessions (suggest 2); optionally add a placement-time sanity band of tier-vs-last-close distance. Kills the stale-geometry downside (BUY limit above market, breakdown-level 'pullbacks').
- 5. TooFarFromMarket transient classification (small PR): TTL the oco_unsupported marker for TooFarFromMarket rejects (reuse the amend_recently_failed pattern), keep permanent marking for structural rejects. Count degrade events per gap day during soak.
- 6. EDGE gap-through cohort tag (small PR): tag fills at/near the opening print with price-improvement vs limit as a distinct cohort in EDGE attribution; Linnainmaa predicts underperformance vs intraday-touch fills — our own ladder data decides at N.
- 7. Operational checklist: before arming, scan the corporate-action calendar (splits/ex-div/spin-offs) for the 13 names and disarm across events; confirm reconcile.py treats a collar-cancelled Working entry as benign terminal.
- 8. Soak observation list (free during August SIM): burst choreography latency at gap opens, oco_unsupported degrade counts, TTL ±1-session day-count wobble, multi-tier auction-fill + additive-stop behavior. Note explicitly in the soak log that SIM fills are idealized and will understate gap losses.
- 9. Direct Saxo support inquiry (async, low effort): on-exchange vs broker-server residency of overnight GTD orders, and TooFarFromMarket reference rules while closed — the two behaviors public docs do not cover.

## Non-issues (deliberately not acted on)

- Placement clock time within the same UTC date, and market-closed placement acceptance — empirically fine, economically neutral for resting entry limits (only the ±1-session TTL wobble and first-auction exposure are real, both second-order).
- Protection 'churn' from multi-tier simultaneous gap fills — snapshot-based reconcile + FifoRealTime netting + B0/B1/additive-stop arms handle it by construction; the 15s request-id dedup under-cover window self-heals next tick with a deep-OTM stop.
- Partial fills in a thin auction — the realized-qty rule sizes protection to actual netted fill; PARTIALLY_FILLED raises an operator alert.
- Gap UP through nothing — no position, pure opportunity cost; GTD expiry -> EXPIRED terminal -> CancelRemaining sweep works.
- KILL-gate interference with protection — protective stops are deliberately exempt (control_loop.py:2101).
- Holding filled positions overnight — the overnight-premium literature does not argue against it; to the extent any premium survives it accrues to us. The overnight problem is specifically the resting ENTRY limits, not held positions.
- 'Chasing the overnight drift' as a strategy — the headline futures anomaly (2-3am ET hour) has averaged ~zero since 2021; it is an index-level, before-costs phenomenon with poor single-name mid-cap tradeability. No redesign implied.
- Gap-through fill PRICE semantics — limit-or-better at the auction print is confirmed by exchange rulebooks and Saxo's own glossary; the fill price is mechanically favorable (the informational adversity is a separate, real issue handled by the cohort tag).

## Open questions

- Does Saxo (SIM and live) REJECT a SELL StopIfTraded with trigger above current market, or accept and fire it immediately as a market sell? Pivotal for A2; probe deliberately on SIM (action item 1).
- How faithful is SIM's opening-auction fill model to live? SIM likely fills idealized limit-or-better prints, understating gap losses and stop-exit slippage — calibration is real-money-only.
- What reference price does Saxo use for TooFarFromMarket in the first seconds after the open (moving vs last close)? Affects how often the oco_unsupported ratchet fires.
- Do overnight GTD orders rest on-exchange or on Saxo servers? Not publicly documented; empirical SIM behavior (accepted as Working pre-open, validated vs last close) is the best current evidence.
- Are plain entry BUY limits subject to any Saxo distance rule at all while closed? Empirically accepted pre-open — assumed none, unconfirmed.
- Exact early-August earnings dates for the 13 defense names — taken from general knowledge; verify against the AV EARNINGS cache before acting on the earnings gate.
- True mid-cap overnight-gap distribution for OUR 13 tickers — the ~0.6-1.0% stddev is extrapolated from index/ETF evidence; computable directly from the grouped-daily parquet store if a precise number is needed for the sizing cap.
- Does the reconcile loop correctly handle a collar-cancelled (unexecuted marketable) Working entry after an extreme gap? Likely yes via the CANCELLED-terminal path; confirm on the entry side.
- Does the gap-through fill cohort actually underperform intraday-touch fills on our population? Theory-driven prediction (Copeland-Galai, Linnainmaa) with no dedicated published study — our own EDGE data at N decides.
