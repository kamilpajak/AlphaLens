# arm-manual immediate-entry tier ("now") — design memo

**Status:** LOCKED (adversarial review passed 2026-09-03 — §6)
**Date:** 2026-09-03
**Issue:** #1247 (related: #1235 arm-manual v1, #1246/#1248 manual day-1 semantics, #1236 per-pick exit policy — separate track)
**Scope:** entry mechanism only. The LOCKED live-market execution model (broker holds ONLY the disaster stop; TP tranches and breakeven_trail are daemon-managed) is untouched. Pure-executor doctrine holds: no selection filtering.

## 1. Problem

A WhatsApp-group signal can be "we buy RHI, NOW" during an open session — not a pullback ladder. Today no supported path exists:

- `arm-manual` (#1235) compiles resting limit tiers below the market (pullback hunting); on production config every eligible drained pick takes the entry-WATCH path (`ALPHALENS_BROKER_ENTRY_TRAIL_BPS=50` on both SIM and LIVE), which by design waits for a touch-and-trail entry.
- `broker submit` requires a brief row and also places resting pullback brackets.
- A hand-placed order in the Saxo app creates a position the daemon does not manage: no tranche plan, no TP, no breakeven_trail, no never-naked protection provenance.

## 2. Locked decisions (owner, 2026-09-03)

These were decided before this memo and are its fixed inputs:

1. **Cap is operator-mandatory.** Tier grammar `--tier now@<cap>:<alloc_pct>` (e.g. `now@43.00:40`). `arm-manual` stays a zero-I/O compile+append command — no price probe, no derived default. The group quotes a price with the signal; the operator types it.
2. **The "now" tranche is fully independent of the day-1 gap gate.** The group's decision IS the timing, so the gate never defers or refuses the now tranche. Sibling pullback tiers keep full gate semantics (#1246/#1248), anchored on the first PULLBACK tier — the now tier is excluded from the gate's view of the ladder.

## 3. Non-negotiable constraints (from the terrain)

These are facts of the current code that any implementation must respect; each was verified against origin/main on 2026-09-03.

### 3.1 Schema: the mode must live in broker-contract, not in the CLI

`EntryTierSpec` (`broker_contract/trade_intent/schema.py:47-60`) carries only `limit_price, alloc_pct, tag`. The codec's `_filtered()` (`codec.py:71`) **drops unknown keys with a WARNING** — a CLI-only field would be silently discarded by the daemon at drain. Therefore the tier form needs an additive schema field, proposed:

```python
@dataclass(frozen=True)
class EntryTierSpec:
    limit_price: float          # for a "now" tier: the CAP (max acceptable fill)
    alloc_pct: float
    tag: str
    entry_mode: Literal["pullback", "immediate"] = "pullback"
```

- Default `"pullback"` keeps every existing intent decoding byte-compatible (absent key → default).
- Reusing `limit_price` as the cap (rather than a separate `cap_price`) keeps `TradeSpec` invariants (`stop < min(limit_price)`) and the sizing arithmetic (`qty = floor(tier_notional / limit_price)`) working unchanged; the semantic shift is documented on the field. Sizing at the cap is conservative (fills at or below the cap can only buy ≥ the computed qty's worth).
- `schema_version` bump per broker-contract convention; decode of old lines unchanged.

### 3.2 Order primitive: cap-bounded LIMIT, marketability checked at drain

No current code path places an immediately-marketable BUY. Existing primitives:

| Primitive | Shape today | Fit |
|---|---|---|
| `place_bracket_order` | `OrderType: Limit`, `GoodTillDate` over `entry_ttl_days` sessions | Resting; a "now" order must not rest for days |
| `place_market_order` | Market, DayOrder; used ONLY by the exit engine (SELL) | Unbounded price — violates "never a naked market order" |
| `place_stop_limit` | StopLimit trigger+ceiling, DayOrder; currently UNUSED | Designed for stop-entry above market, not for immediate entry |

**Chosen primitive: a LIMIT at the cap with the shortest immediate duration the instrument supports — `IOC` when available, else `DayOrder` — placed only after a marketability check.** Mechanics:

1. At drain, the daemon reads a current price for the uic (the price feed the entry-watch pass already consumes; see §3.6 for the no-price and stale-quote cases). The gate quote must be **real-time and tradable**: `DelayedByMinutes == 0`, a tradable (not indicative) price type, market open. A delayed quote must never authorize an immediate entry (adversarial-review finding; matches the existing "entitlements lie, trust DelayedByMinutes" doctrine).
2. **Duration capability read (per instrument):** Saxo exposes IOC/FOK in the duration enum but supports them mainly for FX/CFD — cash-stock support must be read from the instrument's `SupportedOrderTypeSettings` (the adapter already has the order-TYPE capability-check pattern; duration is a second axis of the same read). When `Limit + IOC` is supported: use it — it is the honest "now" (fills what is available at ≤ cap, cancels the residual, no unattended rest). When not: `DayOrder` with the residual policy below.
3. **Cap quantization:** quantize the operator's cap DOWNWARD to the instrument's limit-order tick (`floor` — never round up above the cap). Journal both `operator_cap` and `submitted_cap`, and echo the effective cap in the placement journal/alert.
4. If `price <= submitted_cap`: place `Limit @ submitted_cap`. Marketable on arrival (cap ≥ ask ⇒ fills at ask or better, never above the cap — the cap is the worst case, not the target).
5. If `price > submitted_cap`: **loud refusal** (Telegram + journal), pick handling per §3.5.
6. **Residual policy (DayOrder branch), fixed here:** `WORK_UNTIL_CLOSE` — if the order rests or partially fills (price ran between check and POST, or thin book at ≤ cap), the remainder keeps working and dies at the session close (a limit order in the closing auction still fills only at a clearing price ≤ the cap). It never becomes a multi-day rest. At close, a non-zero residual **pages the operator** (partial entry — position smaller than planned; exits size to the FILLED quantity as always). Re-driving after a crash never re-POSTs the full quantity: the deterministic request-id makes Saxo's dedup catch a double-POST, and any residual re-work is `requested − filled`, never more.
7. Reject taxonomy: both `TooFarFromMarket` AND `PriceExceedsAggressiveTolerance` (the "too executable" mirror-image reject) are normal race outcomes — journal the gate quote alongside the reject and convert to the loud refusal of point 5, never to a market order and never to a silent retry loop.

Why not skip the pre-check and always POST `Limit @ cap`: without the check, a "now" signal armed while price is 10% above the cap silently becomes a day-long resting limit — the operator believes they entered and they did not. The pre-check converts that into an immediate, actionable refusal. Check-then-POST is non-atomic and is documented as a best-effort client-side gate; the hard price protection is the limit price itself.

Adapter additions: an entry order duration parameter (`IOC`/`DayOrder`; today the Saxo adapter hardcodes `GoodTillDate` for bracket entries) + the duration-support read. Additive, defaults preserve current behavior.

**Observability:** the placement journal line stamps the gate quote (`ask`, `quote_ts`, `DelayedByMinutes`) and the outcome class (`filled` / `rested` / `refused_cap` / `refused_reject`), so the gate-passed-but-rested rate — the TOCTOU metric — is readable from the journal.

### 3.3 Cost-gate parity: the now tranche must pay the #1112 gate

The "TP1 must clear round-trip cost" gate lives ONLY on the watch path (`_brief_plan_arm_refusal`, at native-trail arm time). The classic `_place_tiers` path has NO cost gate (known debt — `_geometry_without_entry_trail_note` warns about exactly this). A now tranche bypasses the watch, so it must run the same gate at drain, priced with:

- fill estimate = the cap (worst case, conservative),
- the tranche's apportioned share count,
- the existing `costs.cost_gate_facts` machinery (venue fee card, FX).

Refusal here is terminal for the pick (same class as fee-floor/gross-cap refusals): a plan whose TP1 cannot clear costs at the cap is mis-designed, not mistimed.

### 3.4 Day-1 gate wiring: exclude the now tier from the gate's ladder view

The gate is evaluated once per pick at the top of `_place_pick` and its entire view of the ladder is `spec.entry_tiers[0].limit_price` (documented assumption: tiers strictly descending). A now tier prepended to the tuple would silently become E1 and redefine the threshold. Implementation:

- `_day1_gap_gate_decision` receives the first tier with `entry_mode == "pullback"` as its `e1_limit` (a pick with ONLY a now tier skips the gate entirely).
- The gate's verdict applies only to the pullback siblings' routing; the now tranche proceeds regardless (locked decision 2).
- `parse_entry_tiers` refuses a now tier that is not the FIRST tier listed and refuses more than one now tier per pick (one immediate decision per signal; a second "now" is a new signal → new arm). This also keeps the pullback tiers' descending-order assumption intact: the now tier is stripped before any ordering-sensitive consumer sees the tuple.

### 3.5 Pick atomicity: split placement, journal per-tranche

Today the pick is atomic: one `submitted_pick_keys` join, one `mark_refused`, a write-ahead dedup record before the first POST, and any `defer_*` from the gate leaves the WHOLE pick armed. "Now filled + siblings deferred" has no representation. Design:

- The drain handles the now tranche FIRST (it is time-critical), then routes pullback siblings to the entry-watch path exactly as today.
- The write-ahead submission record gains a per-tranche marker (`tranche: "now"` vs `tranche: "pullback"`) so a crash between the now POST and the sibling routing re-drives only the missing half. The pick key joins as submitted once BOTH halves have durable records (or terminal refusals).
- A now-tranche refusal (cap breached at drain, §3.2.3) is **terminal for the now tranche only**: journaled + paged, while pullback siblings continue their normal watch routing (they were validly armed regardless of the group's intraday timing). The operator can re-issue the immediate entry as a fresh arm if the group still wants in (see §3.7).

### 3.6 No-price case

If the drain cannot read a price for the uic (feed outage, halted instrument), the now tranche is NOT placed and NOT terminally refused: it defers with a page (`defer_no_price` semantics, infrastructure failure), and retries next tick. Rationale: a halt is temporary; converting an outage into a terminal refusal punishes the operator for infrastructure noise. The DayOrder lifetime starts only once placed.

**Staleness bound (same-session only):** the pre-check must use a quote from the CURRENT session — on the first drain after an overnight arm, yesterday's close does not satisfy the check. This falls out of the DayOrder framing (an immediate entry is meaningful only intra-session), and prevents the check from passing on a stale cache while the market opens 10% higher.

### 3.7 Re-arm after refusal: fresh-tier semantics, not sticky-terminal deadlock

Known trap: crids are deterministic (`{ticker}-{brief_date}-entry-t{i}`), terminal kinds are sticky in the entry-trail fold, and for arm-manual `brief_date` IS the arm date — a same-day re-arm reuses identical crids, so previously-terminal watch tiers stay dead. For the now tranche this is not a blocker under this design:

- The now tranche never opens a WATCH tier (it is a direct order), so its refusal writes no entry-trail terminal. A same-day re-arm re-drains the now tranche cleanly as long as the submissions join treats the previous terminal refusal as "not submitted" for a NEW arm generation.
- Concretely: the now-tranche dedup key includes the pick line's `armed_ts` (already present in `IntentMeta`), so re-arming mints a fresh dedup identity without touching the crid scheme. Pullback siblings keep today's semantics (re-arm same day does NOT reopen terminal watch tiers — fresh date is the path back), unchanged by this feature.
- NOTE: #1252 renames `brief_date` → `trade_date` across the journal; this memo uses the current name and the implementation follows whichever lands first. Do not couple the two PRs.

### 3.8 Rails interactions

- **Fee floor** (whole-plan, at drain): unchanged, already covers the now tranche.
- **Gross cap:** the now tranche is counted as `candidate` gross at drain (as today for classic tiers); after the fill it moves to `filled_positions` gross. Pullback siblings continue to reserve `watching_virtual` gross. No double-count: the two halves are disjoint terms in the existing sum.
- **MAX_OPEN:** a filled now tranche is a real position — counted by `_net_open_position_uics` automatically. The pick's watch tiers count via `_open_watch_picks_for_max_open` as today.
- **Watch slots (`ENTRY_WATCH_MAX_PICKS`):** the now tranche does NOT consume a watch slot (it is not a watch). Slot accounting applies to the pullback siblings only.
- **`_has_live_long_on_uic`:** today a live long on the uic DEFERS watch routing — after the now fill this would freeze the pullback siblings, breaking "remaining tiers stay normal pullback watches". **Resolution: the defer is keyed to picks OTHER than the position's own.** The guard exists to stop a second pick stacking onto an instrument another pick already owns; the same pick adding its own pre-planned pullback tranches below its own entry is the designed behavior (same shape as a partially-filled ladder today, where deeper tiers rest below a filled E1). Implementation: the guard compares the position's governing pick key (tranche-plan fold) with the routing pick's key and defers only on mismatch.

### 3.9 Exit management after the fill: unchanged

The filled now tranche gets the standard treatment, driven by the same journal shapes the bracket path writes today: standalone disaster stop at `--stop` (never-naked pass), TP tranches from `--tp`, daemon-wide breakeven_trail (per-pick static override is #1236, separate track). The tranche plan for the uic is written by the same code path that stamps it for classic fills.

### 3.10 Explicitly out of scope / lifecycle notes

- **No cap modification in flight.** Changing the cap of a working now order is cancel + fresh arm, never an amend (a partially-filled order's aggregate-vs-residual cap semantics are not worth defining for this feature). Note the existing `broker disarm` refuses while a resting entry order exists (`DisarmRestingOrderError`) — the operator path is: cancel the order (broker CLI/app), then disarm, then re-arm.
- **No T+1 resubmission.** A DayOrder residual dies at the close and stays dead; re-entry the next day is a fresh operator decision with a fresh quote — the daemon never re-submits against a gapped open.

## 4. What this memo rejects

- **Naked market order** — rejected in the issue itself; every entry is price-bounded.
- **StopLimit as the primitive** — its trigger semantics are for stop-entries above market; for an immediate entry the trigger adds a failure mode (never triggers if price falls) with no protective benefit over Limit-at-cap.
- **Derived cap default (last + N bps)** — rejected by locked decision 1: first network I/O in `arm-manual`, dependency on the price-reader socket (single elevated LIVE Saxo session — an independent CLI stream would demote both daemons to delayed quotes), and a wrong-listing hazard the day-1 probe already had to solve (#1240 venue asymmetry).
- **Gate relief for siblings after a now fill** — rejected by locked decision 2 (fully independent); simpler, and keeps the gate's discriminator (session-open timing evidence) untouched.

## 5. Test plan (sketch, red-first at implementation)

- **Parser (`manual_intent.py`):** `now@43.00:40` grammar; refuse >1 now tier; refuse a non-first now tier; refuse `now@` without a cap; cap must exceed `--stop`; alloc arithmetic unchanged (now participates in the 100% sum); a now-only pick is valid.
- **Codec/schema:** `entry_mode` round-trips; absent key decodes to `"pullback"`; old journal lines decode unchanged.
- **Day-1 gate:** `e1_limit` anchors on the first pullback tier when a now tier is present; a now-only pick skips the gate; sibling verdicts unaffected by the now tranche's outcome.
- **Drain:** price ≤ cap → one capped Limit (IOC when supported, else DayOrder) + normal sibling watch routing; price > cap → terminal now-refusal (journal + page) with siblings still routed; no price / delayed quote (`DelayedByMinutes > 0`) / previous-session quote → defer + retry, nothing placed.
- **Cap quantization:** `operator_cap` floors to `submitted_cap` on the limit tick (never up); both journaled.
- **Rejects:** `TooFarFromMarket` and `PriceExceedsAggressiveTolerance` both convert to the loud refusal, never a retry loop.
- **Cost gate:** TP1 below round-trip cost at the cap → terminal refusal before any POST.
- **Crash re-drive:** now POSTed, crash before sibling routing → next tick routes only the siblings (no duplicate now order; request-id dedup pinned).
- **Residual:** partial fill at close pages once; exits size to the filled quantity.
- **`_has_live_long_on_uic`:** own-pick pullback tiers route after the now fill; a DIFFERENT pick on the same uic still defers.
- **Re-arm:** same-day re-arm after a cap-breach refusal re-drains the now tranche.
- **Submissions join:** the per-tranche marker (§3.5) is additive — existing all-pullback picks join `submitted_pick_keys` exactly as before (regression pin).

## 6. Adversarial review record (2026-09-03)

Reviewed pre-LOCK per repo doctrine: Perplexity (Saxo OpenAPI docs + microstructure grounding, high context) + zen thinkdeep (`deepseek/deepseek-v4-pro`, thinking=high). Resolutions folded into §3.2/§3.10 above:

1. **IOC vs DayOrder** (Perplexity): IOC-at-cap is the honest "now" but cash-stock support is per-instrument — resolved: duration capability read, IOC preferred, DayOrder fallback with a fixed `WORK_UNTIL_CLOSE` residual policy + close-time page.
2. **Delayed-quote authorization hazard** (Perplexity): resolved — gate requires `DelayedByMinutes == 0` + tradable price type + market open.
3. **Tick quantization direction** (Perplexity): resolved — floor to the limit tick, journal operator vs submitted cap.
4. **Second reject class** (Perplexity): `PriceExceedsAggressiveTolerance` added beside `TooFarFromMarket`.
5. **Observability of the TOCTOU window** (zen): resolved — gate quote + outcome class stamped on the placement journal line.
6. **Cap-modification and T+1 lifecycle** (zen): resolved — out of scope, §3.10.
7. Zen's claim that a closing-auction fill can exceed the cap was **rejected** (a limit order participates in the auction only at a clearing price within its limit); zen did not embed the memo file, so its generic findings were adjudicated individually.

Implementation-time verification items (not design blockers): §3.5 marker additivity for legacy picks (test-plan pin above); §3.8 own-pick guard relaxation (confirm no scenario depends on the defer applying to the position's own pick before relaxing).

## 7. Implementation addendum (PR-C, 2026-09-03)

Decisions taken at implementation time, within the LOCKED envelope:

- **Feed-off / no-quote behavior:** `ALPHALENS_SAXO_LIVE_PRICES` unset (or a
  quote outage/halt/stale read) DEFERS the whole pick with a throttled page
  naming the config lever — never a terminal refusal, never a silent
  forever-defer. The per-uic `now-entry:<uic>` feed scope is KEPT on defer (a
  fresh subscribe's snapshot arrives async; release-on-defer would starve the
  gate) and released after placement/refusal. A pick disarmed mid-defer leaks
  one scope until restart — bounded over-subscription, not a safety hazard.
- **`--no-tp` picks:** the cost gate is vacuous (logged INFO) — a stop-only
  plan has no TP1 to clear.
- **Mixed-pick same-day re-arm limitation:** once the pullback siblings'
  submission record joins, the pick does not re-drain the same day (sibling
  semantics — "fresh date is the path back" — win). The §3.7 scenario fully
  works for a now-ONLY pick: its cap-breach refusal marks the pick refused
  and a fresh arm (new `armed_ts`, latest-wins) cleanly re-drains.
- **Write-ahead crash-loss:** a crash between the now half's write-ahead
  attempt record and the POST leaves an alertable non-retried attempt —
  the same contract `_place_tiers` has always had; never a re-POST.
- **Duration capability:** IOC is selected only when the adapter's fail-open
  `SupportedOrderTypeSettings` read affirmatively reports it; unknown shape
  ⇒ DayOrder. The wire shape is pinned on the SIM probe via the one-time
  INFO log (PR-B).
- **Acceptance-suite deferral:** the fake-broker acceptance world stubs
  `place_pick`, so a drain-level now-entry acceptance scenario has no
  existing harness; the drain is covered by 14 integration tests on the
  real `_place_pick` closure (real entry-trail journals). Extending the
  world to the real drain is follow-up work, not part of PR-C.
