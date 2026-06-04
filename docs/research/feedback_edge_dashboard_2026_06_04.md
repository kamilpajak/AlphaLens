# Feedback presentation — market-behavior edge dashboard (design memo)

**Status:** PHASE-1 BACKEND SHIPPED (2026-06-04) — DRAFT v2 design + first impl (§10 records the one binding deviation)
**Author:** session 436cc26d
**Supersedes framing of:** the "feedback page" as a user-action ledger view

---

## 0. TL;DR

The right primary "feedback" surface is **market behavior on the full surfaced
population**, not the user-action decision ledger. The population ladder monitor already
produces that signal automatically (no user input, all candidates) but it lives only in
VPS parquets — invisible to the SPA. This memo specifies a thin **data bridge** + an
**edge dashboard**, with the statistical guardrails a disciplined shop requires.

**Two hard invariants (revised after adversarial review):**
1. The dashboard is **EXPLORATORY / hypothesis-generation only — NOT confirmatory**. The
   mere act of a human reading "which gates/themes have high R" is itself an informal
   data-snooping channel. So the anti-overfitting doctrine is reframed: any change
   *inspired* by the dashboard becomes a **new paradigm**, evaluated only on **future,
   untouched** data (a versioned firebreak). Historical telemetry = hypothesis-gen, never
   statistical validation. (Carries [ADR 0012](../adr/0012-decommission-paper-trading-and-broker-chain.md).)
2. **No automated re-weighting loop.** Combined with (1): displays only, no "re-weight"
   action, limited live slicing.

---

## 1. Why this memo (the triggering question)

"Does the feedback ledger really require user input? Isn't the best feedback the market's
behavior?" — answered by code + methodology:

**Code (verified):** `feedback.db` `decisions` row exists only on a human click (POST);
user-writable = `action`/`dismiss_*`/`confidence_subjective`; v2 outcome cols dead
post-decommission; gen-4 ladder cols job-set but only for *clicked* names → ledger is
user-gated, low-volume, selection-biased, empty. `population_ladders/{date}.parquet` =
every plannable candidate, automatic, full population — "market behavior", already exists.

**Methodology (Perplexity-reasoned):** for **screener edge**, market behavior (B) is the
ground-truth forward-test; user-action (A) is revealed preference / relevance, a poor edge
estimator, **not necessary for edge**. A keeps a narrow value (relevance/UX, dismiss-reason
labels the market can't give, confidence calibration, tacit knowledge) → keep but demote.
Closed-loop tuning on B = data-snooping (White, Harvey-Liu-Zhu, deflated Sharpe) — the exact
hazard behind 14 paradigm failures.

---

## 2. Decision

- **Primary = market-behavior EDGE dashboard** (population monitor outcomes),
  user-independent, full population, **exploratory** (per §0.1).
- **User-action ledger = secondary "relevance/UX" lens**, NOT edge; optional; waits on
  clicks; used (with propensity-aware methods, §3.10) for UX/preference research, never to
  optimize the screener.
- **No closed loop**; versioned firebreak between "data that inspires a change" and "data
  that evaluates it".

---

## 3. Statistical guardrails the dashboard MUST enforce (revised — these are binding)

1. **Benchmark-relative is the PRIMARY metric — Phase 1, not deferred.** Raw long-only R
   over 42 sessions mostly reports market beta + regime, and in a bull market falsely
   confirms "the tool works". Headline = **excess R** = `R_stock − R_benchmark` over the
   SAME window (market index v1; sector/factor v2). Raw R is shown only as a
   de-emphasized "gross P&L proxy (includes beta — NOT edge)".
2. **Hard N-gate (hide, don't caveat).** A textual "n=3" does not counteract anchoring.
   For ANY slice: if `n_matured < 30` → show only counts + "insufficient data", NO
   mean/median/expectancy. `30 ≤ n < 100` → show but visually mark "early / high-variance".
   **Per-name expectancy is NEVER a headline number.** (Consequence: the EDGE panel will
   honestly read "insufficient data" for weeks until N accrues — by design.)
3. **Drop the open_R mean entirely.** A mean over ongoing positions is censoring- +
   survivorship-biased (fast losers close, slow winners stay open). Open positions appear
   ONLY as a descriptive distribution (count near-TP vs near-SL; R vs days-since-entry),
   explicitly "descriptive only, excluded from expectancy". Matured (terminal) outcomes are
   the only input to expectancy. (Open and realized are never pooled — and open is never
   reduced to a scalar.)
4. **Gross-of-cost labelling.** The replay is costless (no spread/slippage/commission), so
   all R is gross and optimistic. Every R surface labelled "gross, pre-cost". P2 adds a
   per-trade cost haircut (liquidity-scaled).
5. **No naive t-stats / SEs in the UI.** 42-session windows overlap heavily (same name on
   multiple days + common factors) → trades are NOT iid; naive SEs are too small. The UI
   shows means/medians/quantiles + N only; any formal inference (block / Newey-West robust,
   deflated Sharpe) is offline research, never a live dashboard number.
6. **Limited, pre-registered live slicing.** Live dashboard shows only the overall
   population + a small pre-registered set (3–5 theme buckets). All other slicing
   (by gate, sector, regime, ad-hoc filters) is offline with explicit multiple-testing
   adjustment — exposing many live slices = empowering untracked human multiple-testing.
7. **Distributional, not just central.** Show R quantiles (10/50/90) + tail + a per-ladder
   max-drawdown proxy, not only mean/median — two slices with equal mean R can have very
   different risk.
8. **Regime context.** Present overall edge alongside coarse regime stratification (VIX
   bucket / simple trend); never let users extrapolate an aggregate into a different regime.
9. **Random-universe baseline (methodological commitment; UI in P2).** Simulate baseline
   ladders on a random/naive pick from the same universe; "edge" is interpreted relative to
   that baseline, to separate signal from "we surface hot sectors in a bull run".
10. **A used with propensity awareness.** The user-action ledger is logged + analyzed for
    UX/preference research with off-policy / inverse-propensity methods (acknowledging
    selection bias), never as a direct screener objective.
11. **Disclaimer — mechanical ladder ≠ actual execution ≠ group P&L.** B is the hypothetical
    performance of a mechanical ladder triggered by the screener (idealized entry timing,
    full population). It is NOT realized decisions, execution timing, or desk P&L. Stated
    explicitly, not as a one-liner.

---

## 4. Data bridge (`population_ladders` → Postgres → API)

Mirrors `rebuild_briefs_cache` (parquet → Postgres, same nightly cadence).

- **Table `ladder_outcomes`** (one row per `(brief_date, ticker)`): the parquet schema +
  the new size fields; upsert; idempotent; migration-skew guard (mirrors briefs).
- **Ingest `manage.py rebuild_ladder_outcomes_cache`** wired into the existing nightly path
  (confirm host-venv vs Docker bind-mount + HOME — R4). No new systemd unit.
- **Endpoints (read-only, auth_cf):** `GET /v1/edge/summary` (the gated, benchmark-relative
  aggregate per §3) + `GET /v1/edge/outcomes` (per-candidate rows). `/v1/edge/*` lexically
  distinct from `/v1/feedback/*` (A). **Benchmark return per window must be computed at
  ingest** (the parquet has raw R only) — a Polygon/yfinance index-return fetch keyed to the
  same arrival→horizon window.

---

## 5. Frontend — edge dashboard page

**Route** `/edge`; nav `[06] EDGE`. Aesthetic = extend the existing terminal-ops language
(no new design system).

```
EDGE // MARKET-BEHAVIOR LEDGER (exploratory · gross · telemetry)   [pop 102 · matured 3]
  ⚠ n matured below threshold — edge stats hidden · excess-of-benchmark · ladder≠execution≠P&L
──────────────────────────────────────────────────────────────────────────────
┌── EDGE (excess R, matured) ─┐ ┌── PORTFOLIO (size-wtd) ─┐ ┌─ DEPLOYMENT ─┐
│   ⓘ insufficient data        │ │   ⓘ insufficient data    │ │ fill-rate 74%│
│   n matured = 3  (< 30)      │ │   n matured = 3          │ │ tiers x̄ 1.4  │
│   [unlocks at n ≥ 30]        │ │                          │ │ NO_FILL 26%  │
└──────────────────────────────┘ └──────────────────────────┘ └──────────────┘
   (once unlocked: excess expectancy · median · 10/50/90 quantiles · by-classification)
── OPEN POSITIONS (descriptive only — excluded from expectancy) ────────────────
   open 99 · near-TP 22 · near-SL 14 · [R × days-since-entry scatter]
──────────────────────────────────────────────────────────────────────────────
PER-CANDIDATE OUTCOMES                          [filter: terminal · ongoing · θ]
  AMPL ▮TP_FULL  excess +0.41R  ▓▓▓▓░  hold 11d  +0.21% book  #high-gas  (gross)
  RGTI ▮SL_HIT   excess −0.88R  ▓░░░░  hold  7d  −0.33% book  #quantum
  BLBD ▮OPEN     open  +0.16R   ▓▓░░░  hold  5d            #high-gas  ◷
──────────────────────────────────────────────────────────────────────────────
```

- Classification chips colour-coded; excess-R as centered bar; matured vs open in separate
  sections (open never summarized as a scalar). Deployment (fill-rate) is the one panel with
  data from day one (N-independent). JargonTip tooltips on every stat. Top caveat strip
  carries §3.1/3.4/3.11. Decision overlay (`●` on group-acted rows) + relevance sub-view =
  Phase 3.

---

## 6. Phasing (revised)

- **Phase 1:** data bridge + dashboard. **Benchmark (market-excess) R as the headline**;
  EDGE/PORTFOLIO panels **N-gated** (so honestly "insufficient data" for weeks);
  DEPLOYMENT + per-candidate descriptive table live from day one; open as distribution only;
  gross-of-cost + regime + caveat strip; NO t-stats; limited pre-registered slices.
  Telemetry/exploratory only.
- **Phase 2:** sector/factor-neutral excess + random-universe baseline in the UI + cost
  haircut + deflated/robust (Newey-West) offline inference surfaced as "research-grade" stats.
- **Phase 3:** demoted A relevance lens (propensity-aware) + group-acted overlay.

---

## 7. Risks / open questions

- **R1 — the human IS the loop (top risk).** Mitigation = §0.1 exploratory reframe +
  versioned firebreak + limited live slicing; doctrine pinned here + ADR 0012 + monitor memo.
- **R2 — sparse for weeks.** N-gate means the EDGE panel shows "insufficient data" until
  `n_matured ≥ 30` (~weeks, 42-session hold). Honest, not a bug; near-term value = deployment
  + descriptive per-candidate table.
- **R3 — benchmark choice (now P1).** Market index v1 (SPY/IWM same-window); sector/factor +
  random-universe baseline v2. The choice is a judgment call — pre-register it.
- **R4 — ingest cadence coupling** (host venv vs Docker bind-mount/HOME) — pick in impl,
  mirror briefs-cache.
- **R5 — ladder protocol is a researcher DoF.** Version it (entry rule, TP/SL, 42-session
  stop frozen + documented); no retuning on the same sample; all edge is conditional on the
  ladder.
- **OQ1** two endpoints vs one envelope (lean two). **OQ2** compute summary at ingest vs API
  aggregate (lean API; but the N-gate + benchmark join may favor a precomputed meta row).

---

## 8. Pre-implementation gate

Adversarial review done (§9). Frontend + Django changes carry the mandatory zen pre-merge
pass. No >1h compute → no preaudit.

---

## 9. Adversarial review (Perplexity) — verdict + binding revisions

Perplexity **endorsed the direction** (B primary, A demoted, no closed loop) but did NOT
rubber-stamp — it raised 5 material objections, all accepted and folded into v2:

1. **"Telemetry only" is not enforceable** — human pattern-spotting on a live performance
   surface IS informal data-snooping. → reframed dashboard as exploratory + versioned
   firebreak + limited live slicing (§0.1, §3.6, R1). *Biggest correction.*
2. **Raw R in P1 is misleading / possibly worse than no dashboard** (long-only + beta +
   regime). → benchmark-excess R promoted to P1 headline; raw R de-emphasized (§3.1, §6).
3. **N-caveat insufficient** → hard N-gate ≥30 hide; per-name never headline (§3.2).
4. **Open_R mean is biased** → dropped entirely; open = descriptive distribution only (§3.3).
5. **Omissions** → added: gross-of-cost label (§3.4), no naive t-stats / overlap-aware
   inference (§3.5), distributional/quantiles (§3.7), regime stratification (§3.8),
   random-universe baseline (§3.9), propensity-aware A (§3.10), ladder-as-DoF versioning
   (R5), strengthened mechanical≠execution≠P&L disclaimer (§3.11).

Net effect: the P1 dashboard is intentionally **quantitatively conservative** (gated,
benchmark-relative, descriptive) — it ships the plumbing + honest deployment/per-candidate
views, and unlocks edge statistics only as N + benchmark-adjustment justify them. This is
the correct posture for a 14-failure, anti-overfitting program.

---

## 10. Phase-1 backend impl — one binding deviation from §4

§4 says "**Benchmark return per window must be computed at ingest**". The Phase-1
backend computes it **one stage earlier — in the pipeline**, not in the Django
ingest. Reason (a HARD constraint that overrides the literal §4 wording): the
Django `rebuild_*_cache` commands run in the **slim Django image**, which
deliberately does NOT install `alphalens_pipeline` (the 2026-06-01 prod incident:
a top-level `alphalens_pipeline` import broke `collectstatic` / the image build).
The benchmark leg needs a Polygon index fetch + the exchange calendar, both of
which live in `alphalens_pipeline`. So:

* **Pipeline** (`alphalens_pipeline/feedback/benchmark_excess.py`) computes
  `benchmark_window_return` (SPY raw close-to-close over the SAME arrival→exit
  window as `forward_return`, same arrival-VWAP reference anchor) and
  `market_excess_return = forward_return − benchmark_window_return`, and writes
  both columns onto the `population_ladders/{date}.parquet`. Wired into the
  nightly `backfill-shadow-returns` tail (reuses the 06:30 UTC timer, no new
  systemd unit), right after the population monitor sweep.
* **Django ingest** (`edge/ingest/parquet.py`) just READS the two columns —
  storing `None` for any older parquet that predates them, exactly the way the
  briefs ingest tolerates a missing column.

**Unit correctness (the §3.1 reason this is computed at the RETURN level, not R):**
`realized_r` is RISK-NORMALISED (a multiple of the per-share risk
`blended_entry − disaster_stop`), so `realized_r − benchmark_return` would be
dimensionally meaningless. The excess is taken at the raw-return level —
`forward_return` (the candidate's raw close-to-close window move, fill-independent,
already in the parquet) minus the benchmark's raw return over the IDENTICAL window.
`realized_r` stays in the model + API as the labelled gross / risk-normalised
protocol metric, never the headline.

**Limitation (documented, not fudged):** a row whose `[arrival, exit]` window is
not recoverable (no `brief_date`, exit before arrival) or whose benchmark fetch
returns no bars gets `None` for BOTH columns — never a substituted value. The
`market_excess_return` therefore populates only as the nightly pipeline run
reaches each row's matured window; until then the EDGE panel honestly reads
"insufficient" (per the N-gate). Random-universe / sector-neutral baselines stay
Phase 2 (§3.9).

**Shipped surfaces (Phase-1 backend):** `edge` Django app (`LadderOutcome` +
`DayMetaLadderOutcome` models + migration `0001_initial`), `rebuild_ladder_outcomes_cache`
command (migration-skew guard shared with briefs, `edge` added to `_GUARDED_APPS`),
`GET /v1/edge/summary` (N-gated, market-excess headline mean+median+10/50/90,
deployment block always-on, open as descriptive distribution) + `GET /v1/edge/outcomes`
(per-candidate rows, theme joined from the brief cache). CI coverage `--source=`,
pyright `include`, the Django Dockerfile COPY, and the image-smoke import list all
carry `edge` / `benchmark_excess`.
