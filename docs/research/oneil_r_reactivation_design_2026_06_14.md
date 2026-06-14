# O'Neil "R" (relative strength) re-activation — design memo

**Status:** DRAFT 2026-06-14 (proposal — not scheduled; needs user GO/NO-GO). Follows
[`oneil_expert_design_2026_06_13.md`](oneil_expert_design_2026_06_13.md), which shipped
O'Neil v1 as **N + L + C/A** and **deferred R** (relative strength) for three independent
data-feasibility reasons. This memo is the plan to lift that deferral by building a
persistent market-wide daily-close history, and notes that the new approach also dissolves
2 of the 3 original blockers.

## 1. Why R was deferred (recap)
R/RS is a **cross-sectional** signal: it needs the trailing returns of the WHOLE market
(~8000 names) to percentile-rank one candidate's return. The PR-7 feasibility probe tried
to source it from the population-monitor grouped-daily disk cache and found three fatal
problems:

1. **The cache is empty / forward-only.** It is written on-demand only for dates a brief
   needs, and grows forward from the brief date — a ~252-session lookback snapshot for an
   arbitrary `asof` essentially never exists → RS would be ~100% `None`.
2. **No `n-sessions-before(asof)` calendar helper** to even locate the lookback date.
3. **Broken split math + survivorship-biased universe** — the proposed fixed-band split
   guard nulled every legitimate 1-year leader, and the "universe" was the survivorship
   intersection of two cached snapshots, not a true market.

In contrast, **N** ships because it needs only ONE ticker's own 252-day price window, which
the screening scorer already fetches and caches per candidate. The asymmetry was **data
availability at retail scale**, never signal quality (the 52-week-high literature —
George & Hwang 2004 — actually validates N strongly).

## 2. The proposal: one-time backfill + free-tier forward top-up
The root need is a **persistent, market-wide, split-adjusted daily-close history** (~1–2y,
full US equity universe). Once it exists:
- **Compute RS** = a disk read (percentile of the candidate's trailing return vs the
  universe at `asof`) — zero in-pass API calls.
- **Maintain it** = ONE grouped-daily call per new trading session, which the free Polygon
  tier (5 req/min) handles trivially (the monitor already does forward grouped-daily fetches).

This is exactly re-activation condition #1 from the v1 memo ("a periodic grouped-daily
prefetch job + ≥1y history").

### Data sourcing — grouped-daily PER DATE, `adjusted=true`
Polygon's grouped-daily aggregates endpoint
(`/v2/aggs/grouped/locale/us/market/stocks/{date}`) returns the **whole market in ONE call
per date**. So ~252–504 calls = 1–2 years of full-market history.

- **Likely free, one-time:** ~252–504 dates ÷ 5 req/min ≈ **1–2 hours for $0** against the
  existing free `POLYGON_API_KEY` (Polygon free retains ~2y history). Must run in a quiet
  window so it does not starve the live population monitor's quota. **VERIFY** free-tier
  grouped-daily historical depth + rate caps before assuming $0.
- **Safe / faster path:** one month of **Polygon Stocks Starter (~$29)**, or its **Flat
  Files** (bulk S3 daily-bar dumps) for a single clean pull, then cancel. One-time ~$30.

### Why this dissolves 2 of the 3 original blockers (for free)
1. **Survivorship bias → gone.** Backfill **per-date (grouped-daily)**, NOT per-current-ticker.
   A past date's grouped snapshot contains the names that traded THAT day — including ones
   later delisted — so **each date is a PIT-correct universe by construction**. (Pulling
   today's tickers backward would be biased; grouped-daily-per-date is not.)
2. **Broken split math → gone.** Pull `adjusted=true` (split + dividend adjusted) for the
   backfill → clean returns, no raw-close jumps. The fixed-band / MAD-z-score split detector
   becomes unnecessary. (The monitor keeps its own `adjusted=false` cache for intraday touch
   detection — this is a SEPARATE store; see §4.)
3. **Missing calendar helper → still to build** (trivial once the store is dated — §4).

## 3. RS-approx computation (once the store exists)
For a candidate present at `asof`:
```
ret = close[asof] / close[asof − N_SESSIONS] − 1          # N_SESSIONS ≈ 252 (12 months)
rs_percentile = percentile_rank(ret, over = universe)      # 0–100
```
- **Universe** = all tickers present in BOTH the `asof` and the `asof − N_SESSIONS` snapshots
  (a natural PIT intersection: names delisted by `asof`, or that IPO'd after the lookback,
  are correctly excluded). The reference-universe choice (whole market vs mcap bracket vs a
  fixed index) stays an **unvalidated hand-chosen constant** — having the data enables the
  choice but does not validate it.
- **Approximation note:** IBD's RS Rating weights recent quarters more (most-recent quarter
  double-weighted). v1 RS-approx uses a plain trailing-252-session total return percentile —
  document it as an approximation; a weighted blend is a later refinement.
- **Sparsity:** `None` when the candidate lacks either endpoint (recent IPO, gap), or the
  required historical snapshot is absent (early in the store's life). Tri-state, like every
  other O'Neil term.

## 4. Implementation plan (≈ 1 PR of infrastructure)
1. **Persistent dated store** — `~/.alphalens/grouped_daily_history/<date>.parquet`
   (`adjusted=true`), DISTINCT from the monitor's `~/.alphalens/population_ladders/grouped/`
   (`adjusted=false`). One canonical Polygon client (the existing `PolygonClient`), no shadow
   client.
2. **One-time backfill script** — `apps/alphalens-research/scripts/backfill_grouped_daily_history.py`
   (idempotent, resumable, rate-throttled; runs on the VPS like the other backfills).
3. **`n-sessions-before(asof)` calendar helper** — add to the exchange-parametrized calendar
   (`alphalens_pipeline/.../calendar.py`); trivial against the dated store (count available
   sessions). Closes blocker #2.
4. **Daily top-up job** — fold one grouped-daily(`adjusted=true`) append into the existing
   nightly population-monitor job, or a small new systemd unit + `AlphalensJobStale` alert.
5. **Un-defer the O'Neil R term** — re-add `oneil_rs_approx_pct` (+ a sparsity flag) to
   `ONEIL_COLUMNS` and `comparison.py`, reading the store via an injected reader (no in-pass
   Polygon). Re-weight `compute_oneil_score` to a 4-term basket (N + R + L + earnings) — the
   new weights are again **unvalidated module constants**.
6. **Config-version bumps (log-now discipline):**
   - O'Neil's score formula changes (R term added, re-weighted) → a new O'Neil score config
     token (or fold into the existing per-expert provenance).
   - Because `oneil_score` feeds `expert_spread`, the panel corpus changes meaning → **bump
     `PANEL_CONFIG_VERSION`** (e.g. `panel-v2-...`). Rows under v1 (no R) and v2 (with R) are
     NOT poolable in the deferred Expert×EDGE study — the analyst groups by config_version.
7. **Django + SPA:** no schema change — `oneil_rs_approx_pct` rides the existing
   `expert_assessments.oneil` blob (add to `_EXPERT_COLUMNS["oneil"]` + the float coerce set +
   the frozen drift-guard pin, in lockstep). The drawer's O'Neil section gains one readout +
   the headline/score reflect the 4-term basket. The card chip is unchanged.

## 5. Open risks / honest limits
- **Still display-only until Expert×EDGE.** Even with perfect, PIT-correct, split-clean RS
  data, whether RS (and its weight) predicts our EDGE outcomes is **unvalidated**. This memo
  moves R from "cannot be computed" to "can be computed correctly and PIT" — NOT to "proven
  to work". The log-now discipline (record raw, validate at N≥30 ~2026-09+) is unchanged.
- **Reference-universe + lookback + weighting are unvalidated constants** pinned by
  config_version.
- **Top-up reliability** — a missed daily append leaves a gap that breaks `asof − 252`
  lookups for ~1 year; the job needs the same staleness alerting as the other backfills.
- **Free-tier assumptions** — verify grouped-daily historical depth + rate caps on the free
  tier before relying on the $0 path; the ~$30 Starter/Flat-Files path is the safe fallback.
- **Storage** — ~2y × ~8000 × OHLCV ≈ tens–hundreds of MB parquet; trivial on the VPS.
- **Literature ≠ oracle** (project doctrine) — George & Hwang validates the 52-week-high
  anchor (our N) and the broad RS literature (Jegadeesh-Titman) validates trailing-return
  momentum (our R), but neither is treated as an informative prior for our novel
  retail-scale single-name composite.

## 6. Recommendation
**Technically clean, cheap (likely $0, ~$30 worst case), and it dissolves 2 of the 3
original deferral reasons for free** (survivorship via grouped-daily-per-date; split math via
`adjusted=true`). The remaining work is ~1 PR of infrastructure (persistent store + backfill
script + calendar helper + top-up job + the O'Neil R-term wiring + config_version bumps).

**Decision: needs user GO/NO-GO.** Not scheduled. If GO, the natural sequence is:
backfill script → store + calendar helper → daily top-up + alert → un-defer the R term
(behind a `panel-v2` config_version) → log-now until the deferred Expert×EDGE study.
