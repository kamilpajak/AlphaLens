# Thematic Trade-Setup (Entry/TP Ladder) — Design Memo v1

**Date:** 2026-05-27
**Status:** **SHIPPED** (PR #262, merged to `main` `ff9322b` 2026-05-27). Zen design + code review passed; SonarCloud QG green. VPS Django + pipeline image deployed; trade-setup data populates from the next daily pipeline run. v1.1 deferred: LLM per-tier "strategic logic" prose (v1 uses deterministic derivation tags).
**Track:** Thematic event-driven decision-support tool (parallel to factor-paradigm-search). NOT a paradigm test under project doctrine 3.5. Augments the WhatsApp investing group workflow — tool proposes, group discusses, each member decides.
**Supersedes:** the legacy Layer-5 trade-management block (`brief_position_pct`, `brief_time_exit_weeks`, `brief_time_exit_on_catalyst_failure_weeks`, `brief_disaster_stop_pct`, `brief_entry_price_note`) — removed end-to-end in the same feature PR.

---

## §0. Context — why this rebuild

The current brief ships a 5-field trade-management block: a confidence-laddered `position_pct` (1.0–2.5%), two hardcoded exit constants (8w / 4w), a constant `disaster_stop_pct = −25%`, and an LLM-written `entry_price_note`. It is deterministic but thin — a single position size and a flat stop, with no concrete entry/exit levels the group can actually act on or argue about.

Inspiration: a WhatsApp-group member's bot emits a tiered **entry ladder** (limit prices + allocations + rationale) and a **take-profit ladder** (targets + tranches + rationale). That structure is genuinely more useful for group discussion. But the friend's bot almost certainly lets an LLM invent the price levels ("currently chopping around $215.33", limits $215.30/$212.00/$208.50, targets $325/$335/$345) and frames them as confident predictions ("sets up a sharp mean-reversion gap-fill", "secures immediate relief profits").

Two hard constraints shape our version differently:

1. **No LLM-generated numbers** (`feedback_llm_training_cutoff_numerical_data_2026_05_17`, CLAUDE.md). Every price level is computed in Python from authoritative data BEFORE the LLM call. The LLM writes only the per-level prose rationale over injected facts.
2. **No edge claim.** This is decision-support augmentation, not a validated signal. The empirical literature (§2) is clear that technical levels carry no short-horizon predictive edge after data-snooping correction. Our differentiator vs the friend's hype-bot is **honesty**: levels are labelled as coordination/reference points, allocations are shown alongside fill-probabilities, and partial exits are labelled as drawdown-management, not return enhancement.

---

## §1. Locked decisions

| # | Decision | Lock |
|---|---|---|
| 1 | Replace old 5-field block with one **Trade Setup** block | LOCKED |
| 2 | Entry ladder: ≤3 tiers, limit prices ≤ last close | LOCKED |
| 3 | Take-profit ladder: ≤3 tranches, targets > last close | LOCKED |
| 4 | **Tier sizing = equal-risk** (`size ∝ 1/(entry − stop)`) — user choice 2026-05-27; implemented as a configurable `risk_distribution` array defaulting to equal (zen §4a) | LOCKED |
| 5 | Disaster stop = structural (below deepest support − 1×ATR) + jitter, hard floor −25%; **min stop-distance rule** + **max-notional cap** guard the sizing (zen §3) | LOCKED |
| 6 | **Drop** round-number anchors and Fibonacci extensions (decorative per §2) | LOCKED |
| 7 | Display **volatility-adjusted distance in ATR units** (e.g. "−1.5 ATR"), NOT a "fill-probability" % — avoids false precision; ATR over-states close-to-close σ so a probability number would be misleading (zen §4b) | LOCKED (revised post-zen) |
| 8 | All numbers Python-deterministic; LLM writes only per-level "strategic logic" prose | LOCKED |
| 9 | Honest framing baked into card footer + prose ("reference, not forecast") | LOCKED |

**Open item flagged for confirmation:** `brief_catalyst_failure_exit` (qualitative "exit if Q1 earnings shows wider cash burn…") is a *fundamental thesis-invalidation* exit, orthogonal to the price-based TP/stop. **Proposed: KEEP it** (it answers "exit on thesis break", which the price ladder does not). Only the 5 numeric/entry-note fields are removed. Zen + user to confirm.

---

## §2. Evidence base (two Perplexity passes, 2026-05-27)

Reasoning pass (Sonar Reasoning Pro) = practitioner craft; Deep Research pass = empirical/academic. **Caveat:** Sonar Deep Research fabricates citations — several "papers" do not map to real work. Directional conclusions are weighted (they match the established EMH/TA literature); specific effect-size numbers and citation details are NOT trusted. Real anchors: Brock-Lakonishok-LeBaron 1992, Sullivan-Timmermann-White 1999, Park-Irwin 2007 meta-analysis, Osler 2003, Sonnemans 2006, Parkinson 1980, Garman-Klass 1980.

| Component | Verdict | Implication for design |
|---|---|---|
| **Vol-normalized risk units (ATR sizing)** | ✅ genuine support (range estimators; lower maxDD, higher Sharpe) | **Core of the design.** Value is in *sizing*, not in level prediction. |
| Support/resistance, swing levels, MA-as-support | ❌ no daily-horizon predictive edge after data-snooping (STW'99 vs BLL'92) | Keep ONLY as labelled "reference / order-coordination zones", never as forecasts. |
| Swing/pivot detection | ⚠️ no validated method; "1.5×ATR threshold lacks empirical justification" | Use ZigZag-ATR as a *convention*, parameters fixed globally (no per-symbol tuning). |
| Round-number anchors | ❌ arbitraged away by HFT post-decimalization | **Dropped.** |
| Fibonacci 1.272/1.618 | ❌ no significance vs random extensions | **Dropped.** |
| Tiered limit entry | ❌ "execution technique, not a strategy"; adverse-fill/selection drag | Keep as discussion structure; surface fill-probability honestly; no edge claim. |
| Partial profit-taking (TP ladder) | ⚠️ no alpha, slight drag, but lower crisis maxDD | Keep; label as drawdown-management / discipline, not return boost. |
| Specific R-multiples (1R/2R/3R) | ❌ folklore; any multiple 1.0–2.5 ≈ identical; MMs hunt round ×ATR stops | Use R-multiples only as a *fallback* tag; jitter stops off round ATR multiples. |

**Net:** the rebuild does not claim an edge. It is a deterministic, honestly-labelled *discussion scaffold* whose one evidence-based component is volatility-normalized equal-risk sizing.

---

## §3. Design — the Trade Setup block

Computed per candidate at `asof` from the cached daily OHLCV (`~/.alphalens/thematic_ohlcv/{TICKER}_{asof}.parquet`, 400-day lookback). Inputs already available from the Layer-4 technicals signal: last close `C`, `ATR(14)` (absolute), `SMA50`/`SMA200` (absolute), 52w high/low; raw bars allow swing detection.

**Pipeline order:** detect levels → build disaster stop → build entry tiers → equal-risk sizing → fill-probabilities → build TP tranches → emit `TradeSetup` → LLM narrates.

1. **Suggested size** — total position sized so loss-to-disaster-stop ≈ fixed risk budget `B` (config, default 1.0% of book). Reported as % of book and as the per-tier share split (below).
2. **Entry ladder (1–3 tiers, ≤ C)** — geometry-safe (§7.1): candidate supports strictly below `C` (nearest swing-low zone; `SMA50`/`SMA200` only if `< C`; `C − k·ATR` volatility fallback), forced monotone `T1>T2>T3` with min-spacing `δ = max(spread_proxy, 0.5·ATR)`. Each tier carries: limit price, equal-risk allocation %, **distance in ATR units** (e.g. "−1.5 ATR"), and a derivation tag. The ladder **degrades gracefully to 1–2 tiers** when fewer valid supports exist; if none, the setup is emitted with `status="NO_STRUCTURE"` and no entry ladder (§6).
3. **Take-profit ladder (≤3 tranches, > C)** — nearest overhead resistance zones (swing-high clusters); fallback to ATR R-multiples (`avg_entry + {2,3,4}·R`) when breaking into new highs (no overhead structure). Each: target, tranche %, R-multiple, derivation tag. Labelled drawdown-management.
4. **Disaster stop** — `S = deepest_support − 1·ATR`, nudged by small jitter off round ATR multiples (anti stop-hunt). Hard floor `S ≥ blended_entry·(1 − 0.25)`.
5. **Honesty layer** — card footer: *"Reference levels as of {date} close — coordination points, not a forecast. Verify against live price."* Per-tier prose from the LLM describes the derivation, never a reversal claim.

---

## §4. Worked example (illustrative — numbers NOT real FDS quotes)

Candidate **FDS** passes the screen. Computed at `asof` from cached OHLCV (illustrative): `C=$420`, `ATR=$12`, `SMA50=$409`, `SMA200=$392` (both < C → valid supports), swing-low clusters `~$405` (≈SMA50) and `~$386` (≈SMA200); overhead resistance `$440`, `$456`, 52w high `~$470`.

**Disaster stop:** `386 − 12 = $374` (+ jitter). Floor −25% from ~$400 = $300 → does not bind.

**Entry tiers** (monotone, spacing ≥ $6):

| Tier | Entry | risk/share (E−$374) | weight ∝ 1/risk | alloc | distance | tag |
|---|---|---|---|---|---|---|
| 1 | $414 | $40 | 1/40 | ~19% | −0.5 ATR | shallow pullback (C−0.5·ATR) |
| 2 | $407 | $33 | 1/33 | ~23% | −1.1 ATR | swing-low + SMA50 cluster |
| 3 | $389 | $15 | 1/15 | ~58% | −2.6 ATR | swing-low + SMA200 cluster |

Equal-risk: `shares_i = (B/n)/(E_i−S)`. If all fill and price hits stop, each tier loses ~equal $. **Honest tension surfaced in the card:** equal-risk loads the deep tier (cheapest risk/share) — exactly the tier furthest away (−2.6 ATR) and least likely to fill. Per zen §4a this is a *capital-efficiency* property, not a loss-tail-risk (max loss is bounded by the stop on every tier). Realistic outcome: usually only T1+T2 fill (~42% of planned capital, ~⅔ of risk budget); the large T3 deploys only on a deep flush. The ATR-distance column makes this visible without a false-precision probability number.

**Take-profit** (blended entry ≈ $398; `R = 398 − 374 = $24`):

| TP | Target | ≈R | tranche | tag |
|---|---|---|---|---|
| 1 | $440 | 1.7R | 40% | first overhead resistance |
| 2 | $456 | 2.4R | 30% | consolidation zone |
| 3 | $470 | 3.0R | 30% | 52w high |

---

## §5. Deterministic vs LLM split

- **Python (deterministic):** every number — entries, stop, equal-risk allocations, fill-probabilities, TP targets, R-multiples — plus a derivation tag string per level.
- **LLM (prose only):** consumes `{level, tag}` pairs and writes per-level "strategic logic" (e.g. *"Tier 2 ($407) sits on the 50-day average and the March swing low — a historical order-coordination zone"*). No invented numbers; no reversal/prediction language. Enforced by the existing no-numbers prompt discipline + a new test asserting the prompt injects (not requests) the levels.

---

## §6. Build scope (one feature PR, TDD)

**New module** `apps/alphalens-pipeline/alphalens_pipeline/thematic/trade_setup/` (as built):
- `model.py` — `TradeSetup` / `EntryTier` / `TpTranche` dataclasses + JSON contract (`schema_version`, `status`)
- `levels.py` — ZigZag-ATR swing detection + clustering → support/resistance zones (§7.2)
- `ladder.py` — geometry-safe entry tiers + TP tranches + monotonicity guard (§7.1)
- `sizing.py` — equal-risk allocation + suggested size + blended entry (§7.3)
- `builder.py` — orchestrates cached OHLCV → `TradeSetup` (ATR-distance computed here, §7.4 — no separate probability module)

**Generation wiring:** `argumentation/orchestrator.py` builds the setup per row via a cache-only OHLCV loader (reuses the scorer's `thematic_ohlcv` cache, no network) and persists `brief_trade_setup` (JSON string). **LLM per-tier "strategic logic" prose is DEFERRED to v1.1** — v1 uses the deterministic derivation tags ("swing-low + 50-day MA", "overhead resistance") as the rationale, which keeps the whole block deterministic (more aligned with the honesty doctrine). The LLM brief loses only `entry_price_note`; `catalyst_failure_exit` prose stays.

**Schema:** remove the 5 legacy columns from `_EMPTY_OUT_COLUMNS` (parquet), `briefs/models.py` + migration (Django), `types.ts` (frontend), and `LEGACY_CONTRACT_COLUMNS` → `INTENTIONALLY_DROPPED` in `test_schema_parity.py`. Add `brief_trade_setup` (JSON) — `JSONField` in Django, JSON string in parquet. **JSON shape (zen §5):** root carries `schema_version` (e.g. `"1.0.0"`) + `status` (`"OK"` | `"NO_STRUCTURE"`); `entry_tiers[]` (limit, alloc_pct, atr_distance, tag), `tp_tranches[]` (target, tranche_pct, r_multiple, tag), `disaster_stop`, `suggested_size_pct`, `order_ttl_days` (limit-cancel horizon, ≠ trade horizon), `asof_close`, `atr`. **Consumers MUST check `schema_version` and reject unknown versions** rather than parse-and-fail. Keep `brief_catalyst_failure_exit` (see §1 open item). `order_ttl_days` (when unfilled limits expire) is distinct from the 4–8w **trade horizon** (hold once filled) — both are informational fields, not the same number (zen §6).

**Frontend:** `CandidateCard.svelte` — replace the `<dl>` trade-management block + entry-note section with a Trade Setup table (entry ladder w/ alloc + fill-prob columns, TP ladder, size, stop) + honesty footer. `types.ts` updated to the JSON shape. Apply the `whitespace-nowrap` date/number convention (`feedback_web_nowrap_atomic_tokens_2026_05_27`).

**Tests (mirror under `tests/thematic/trade_setup/`):** swing detection on synthetic series; monotonicity guard (incl. downtrend where SMA>close — must NOT invert); equal-risk weights sum to 1 and load nearest-stop tier; first-passage monotonic in distance; builder end-to-end on a fixture; prompt injects-not-requests levels; schema-parity drop. Update `tests/thematic/argumentation/{test_common,test_prompts,test_orchestrator}.py` and web api-mock fixtures.

---

## §7. Math specs

**§7.1 Geometry-safe tiers.** Collect candidate supports `{c}` strictly `< C`: nearest swing-low zone, `SMA50` if `<C`, `SMA200` if `<C`, `C − k·ATR` (k=0.5 shallow fallback). For longs in a downtrend, MAs above price are resistance, not support, so they are excluded by the `<C` filter. Sort candidates descending, then enforce `T_{i+1} ≤ T_i − δ`, `δ = max(spread_proxy, 0.5·ATR)`; `spread_proxy` from recent (high−low)/close. This kills the pathological `max()` inversion from the v0 sketch (a downtrend `max(swing_low, SMA50)` could land ABOVE close).

**§7.2 ZigZag-ATR swing detection.** Reversal threshold `τ = m·ATR` (m≈2.5, fixed globally — no per-symbol tuning, per multiple-testing discipline). A new pivot is confirmed only after price reverses ≥ `τ` from the running extreme. Cluster pivots within `0.5·ATR` into zones; a zone's strength = touch count. Use rolling 20/50/100/252-day extrema as confirmation only.

**§7.3 Equal-risk allocation + sizing guards.** Risk budget `B`, configurable `risk_distribution` weights `q_i` (default equal `q_i = 1/n`). Per tier `i` with entry `E_i` and stop `S`: `shares_i = (B·q_i) / (E_i − S)`; `notional_i = shares_i·E_i`; `alloc_i = notional_i / Σ notional`. Closer-to-stop tiers get more notional.
**Numerical-stability clamps (zen §3, CRITICAL):**
- **Min stop-distance rule** — `ladder.py` discards any candidate tier with `E_i − S < 0.5·ATR` BEFORE sizing, so `(E_i − S)` can never approach zero. (`S` is computed first, so this is a hard pre-filter.)
- **Max-notional cap** — `sizing.py` caps `notional_i` (and `Σ notional`) at a configured fraction of book as a redundant safety net even if the distance rule is bypassed.

**§7.4 Volatility-adjusted distance (displayed metric, revised post-zen §4b).** Display each tier's distance below close in ATR units: `atr_distance_i = (C − E_i) / ATR`. This is the honest, defensible heuristic. The earlier plan to display an analytic first-passage *probability* (`2·Φ(−d/(σ√N))` with `σ≈ATR`) is **dropped**: ATR is a range estimator that over-states close-to-close σ (typically ~1.3–1.6×), so the implied probability would be biased high AND the "%" framing manufactures false precision the §2 evidence cannot support. ATR-distance conveys the same "deep tier is far / rarely fills" signal without the unbacked probabilistic claim.

**§7.5 Disaster stop.** `S = min(deepest_support − 1·ATR, structural_invalidation)`; jitter off exact `{1.0,1.5,2.0}·ATR` distances; floor `S ≥ blended_entry·0.75`. Computed before tiers so §7.3's min-distance pre-filter can run.

---

## §8. Honest limitations (surface in PR `## Known issues`)

- **No edge.** Levels are coordination/reference points, not forecasts (§2). The card and prose must not imply prediction.
- **Staleness / gap risk.** Levels are as-of the brief-build close; the user may act 1–3 days later. Card shows the date and a "verify against live price" note. No live re-fetch in v1.
- **Adverse-fill drag.** Limit-ladder entries fill on adverse moves; this is execution, not alpha. Fill-prob column makes the deep-tier-rarely-fills reality visible.
- **Equal-risk ↔ fill-prob tension.** Equal-risk concentrates in the least-likely-to-fill deep tier (by design); both columns are shown so the user sees it.
- **Earnings/event proximity.** v1 does not blackout pre-earnings (gap risk inflates near events). Deferred; the brief already surfaces `next_earnings_date` — a future guard can suppress/flag setups within N days.
- **Liquidity/spread guard.** v1 relies on the existing $-volume floor from the screen; no explicit spread-in-ATR guard yet.

---

## §9. Out of scope / deferred to v2

- **Empirical fill/hit-frequency calibration** of allocations — would require a PIT historical panel + forward window (a backtest), inheriting survivorship + multiple-testing discipline. Rejected for v1: analytic first-passage (§7.4) yields ≈ the same numbers (no edge), so a backtest adds data-mining risk without information gain.
- Earnings blackout, explicit spread guard, NATR-regime-conditioned ATR multiples, live-price re-anchor on the action day.

---

## §10. Zen adversarial review (gemini-2.5-pro, thinking high, 2026-05-27)

`gemini-3.1-pro-preview` (CLAUDE.md convention) and `gemini-3-pro-preview` both returned 404 "no longer available" — confirms `reference_gemini_model_retirement_silent_failure`; fell back to `gemini-2.5-pro` (top model available to zen's key). Review was constructive (not kill-switch-biased). Findings applied to this memo BEFORE build:

| # | Finding | Severity | Resolution |
|---|---|---|---|
| 1 | Divide-by-near-zero in equal-risk when a tier sits near the stop → unbounded allocation | **CRITICAL** | §7.3 min stop-distance pre-filter (`E−S ≥ 0.5·ATR`) + max-notional cap |
| 2 | ATR-as-σ over-states volatility → "fill-probability %" is biased high + false precision | **HIGH** | §7.4 replaced probability with ATR-distance (decision #7 revised) |
| 3 | Equal-risk loads deepest/least-fillable tier | MED | Reframed as capital-efficiency (loss bounded by stop on every tier); `risk_distribution` made configurable, equal default |
| 4 | JSON blob migration fragility | MED | §6 `schema_version` in root; consumers reject unknown versions |
| 5 | Edge cases / graceful degradation | MED | §3/§6 `status="NO_STRUCTURE"`; ladder degrades 1–N tiers; `order_ttl_days` ≠ trade horizon |
| 6 | 5-module build over-engineered? | — | Zen: appropriately engineered (testability/swappability); KEEP, with versioned module contracts |

Not raised by zen (noted): zen did not contest the feature's existence given "no edge" — treated it as a product decision already made (path A). `brief_catalyst_failure_exit` keep-decision (§1) stands.

## §11. Process

LOCKED memo → **zen adversarial review (gemini-3.1-pro-preview, thinking high)** → apply findings → TDD build → zen codereview on the PR → CI green → merge → CF Pages auto-deploy. Removal of the legacy block + the new build land in one feature PR.
