# Layer 2d GATE 1 — single-day empirical reading (2026-04-22)

**Decision:** YELLOW → proceed Phase 2.5 constrained. User-override of R9's 5-day-aggregation rec; session cost > 5-day wall-clock delay per explicit preference.

## Data (2026-04-22, first live IWM scan)

**1930 tickers scanned**, exact R5 signal spec (≥3 distinct officers+directors in 30-day trailing window, code P open-market, exclude 10b5-1 plans ≥90d old).

**16 active clusters** detected. Top 5 by insider count:

| # | Ticker | Insiders | $ aggregate | Sector |
|---:|---|---:|---:|---|
| 1 | YORW | 7 | $44K | Utility (York Water) |
| 2 | MPB | 6 | $27K | Regional bank (Mid Penn) |
| 3 | ZBIO | 6 | $14M | Biotech |
| 4 | KLTR | 6 | $187K | Tech (Kaltura) |
| 5 | MLYS | 5 | $44M | Biotech (Mineralys) |

Top 3 by $ volume:
- WVE $218M (Wave Life Sciences — biotech)
- SVC $50M (Service Properties Trust REIT)
- MLYS $44M (Mineralys Therapeutics — biotech)

**Biotech skew:** WVE, MLYS, ZBIO, LENZ, ALT — consistent z Jagolinzer 2009 ("biotech insiders have genuine information advantages tied to clinical trial knowledge") literature.

## R9 statistical caveat (accepted, documented)

Perplexity R9 (2026-04-22) flagged critical distinction: **snapshot prevalence ≠ monthly firing rate**. Single daily reading of 16 could represent:

- True monthly flow ≈ 16 (if 0% persistence) — upper YELLOW
- True monthly flow ≈ 4 (if 75% persistence) — RED/YELLOW boundary

Range spans entire YELLOW zone. No confidence interval derivable from n=1.

R9 recommended **5-day flow-correction protocol**: run daily, count NEW-ticker appearances, convert to new-cluster-per-month flow rate. Apply thresholds to flow.

## User decision (2026-04-22)

> "5 dni czekania to większa strata niż kilka sesji"

Explicit session-cost > wall-clock preference. Accept risk of RED misclassified as YELLOW (false-positive Phase 2.5 investment). Justification:

1. **Phase 2.5 artifacts are reusable** even if Layer 2d dies — SEC XBRL client + yfinance cache + PIT universe builder are general-purpose alt-data infra for any future strategy.
2. **Wall-clock 5 days = calendar week idle.** Session work building toward validation infrastructure is productive regardless of gate outcome.
3. **Phase 3a plist continues collecting data** in parallel with Phase 2.5 work — 5-day aggregation happens for free during 2.5 build sessions.

## Mitigation — maintain R9 protocol passively

Phase 3a plist (`com.alphalens.insider.screen.plist`) odpala codziennie 22:00 CET and writes markdown report to `~/.alphalens/insider/daily_{date}.md`. Over the ~2 wall-clock weeks of Phase 2.5 build, we'll accumulate 5-10 daily snapshots for free. Before Phase 3b capital-deploy decision (post-validation), apply R9 flow-correction protocol to validate snapshot-vs-flow interpretation. If flow turns out to be <5/mo, Phase 3b conclusions re-examined against underpowered-signal caveat.

## Phase 2.5 constraints inherited from GATE YELLOW

Per plan file §GATE 1 decision rules: YELLOW → "Phase 3b uses top_n=15-20 (not 30); document underpowered."

## Artifacts

- `~/.alphalens/insider/daily_2026-04-22.md` — raw 16-cluster report
- `~/.alphalens/insider/scan_2026-04-22.log` — INFO-level pipeline progress
- `~/.alphalens/insider/scan_2026-04-22.crashed.log`, `.crash2.log` — pre-fix diagnostic crashes (timezone date, disclaimer ticker — both fixed)
- `alphalens/alt_data/data/iwm_current.yaml` — 1930 cleaned tickers (1933 pre-fix, 3 garbage purged)

## Bugs uncovered during live smoke (both fixed + committed)

1. **Form 4 timezone-date crash** — `date.fromisoformat('2026-04-09-05:00')` → ValueError → uncaught → scan dead. Fix: `_parse_iso_date` takes first 10 chars + converts to Form4ParseError (commit `92a5d6f`).
2. **iShares CSV footer disclaimer as "ticker"** — 4210-char BlackRock disclaimer line parsed as ticker → `OSError File name too long` on cache write. Fix: strict ticker regex in parser (commit `20eb788`).

Both caught because we did live integration smoke — unit tests alone missed both.
