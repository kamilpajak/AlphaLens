# EDGE in-sample probes on the burnt discovery panel — 2026-09-03

**Status:** COMPLETE — kill evidence only; NO held-out look spent; NO registration.
**Panel:** discovery only (`brief_date <= 2026-07-05`, the ledger rule-3 freeze). Every
script carried a hard assertion on the date; the held-out window (>= 2026-07-06) was
read only outcome-blind (row counts, coverage, dropped share under a feature-only rule).
**Ledger:** `edge_hypothesis_budget_2026_07.md` §4 row 2026-09-03 (no charge; recorded
so the number of specifications examined on the burnt panel stays countable).
**Scripts:** session scratch (throwaway); the method is fully specified below.
**Data read:** VPS stores `~/.alphalens/{population_ladders,thematic_briefs,form4_parquet,grouped_daily_history}`, read 2026-09-03 (newest grouped session 2026-09-01).

## 1. Why this memo exists

Five in-sample probes were run in one session to answer "is there a relationship between
X and the /edge excess return of our candidates" for X in {O'Neil score, Buffett quality
score, expert spread, options data, opportunistic Form-4 insider buying}, then a
brainstorm on "how to get closer to an alpha signal" ended in a calibrated avoid-filter
that was deliberately NOT registered. In-sample probes on the burnt panel cost nothing
in the ledger (the panel cannot produce a discovery any more) but they do cost in the
quality of the single held-out shot (winner's curse, analyst anchoring). The price of
keeping them "free" is recording them. This memo is that record.

## 2. Common method

- Unit: ticker-episode (`ticker_episode_dedup`, chained 5-session collapse); clusters =
  brief days (probes 1-4) or arrival sessions (probe 5); cluster bootstrap B=4000.
- Outcome: `market_excess_return` (position-window /edge excess) for probes 1-4;
  `car_10` built exactly as the registered experts script (anchor = previous trading day
  of arrival, horizon = arrival + 9 sessions, split guard 0.55-1.8) for probe 5.
- Controls: ATR partial (`technical_atr_pct`), ATR+MA50 partial; ROIC partial where the
  feature is ROIC-shaped.
- Rows: `population_ladders` plannable (+terminal for the ladder outcome) joined to
  `thematic_briefs` on (brief_date, ticker); discovery = 414 plannable+terminal rows,
  40 brief-dates, 162 tickers; 204-205 episodes after dedup.

## 3. Results (all in-sample, all null or ATR-repackaging)

| Probe | N (episodes / clusters) | Row-level | Ticker-episode | ATR-partialled | Verdict |
|---|---|---|---|---|---|
| `oneil_score` | 114 / 21 | rho -0.18 (p .001) | -0.12 (p .16) | -0.15 (p .08) | inconclusive, NEGATIVE sign; +0.36 with MA50 distance, +0.28 with RSI (extension fade) |
| `buffett_quality_score` | 63 / 20 | +0.22 (p .004) | +0.05 (p .71) | -0.17 (p .26) | low-ATR repackaging (rho -0.51 with ATR, +0.75 with ROIC) |
| `buffett_roic_3y_avg` | 54 / 19 | +0.37 (p <.001) | +0.13 (p .22) | +0.06 (p .39) | the July "+0.46 headline" = pseudo-replication + ATR |
| `expert_spread` | 57 / 18 | -0.03 (p .73) | +0.05 (p .66) | +0.17 (p .12) | null; spread tracks `oneil_score` +0.72 (Buffett score sits near 0, median 10.7/100) |
| options (forward `options_*`) | 0 on discovery | — | — | — | first stamp is 2026-07-06: NOTHING on discovery; held-out is cluster 19 / #774 (no peek) |
| options (retro iVol pilot) | 148 / 26 | — | — | — | already COMPLETE-NULL 2026-07-09; not re-run ("no further retro spend") |
| opportunistic Form-4 (Cohen-Malloy, PIT recompute) | 202 / 38 | — | 8 buy episodes: mean -10.2% vs +1.5% (p .054) | diff -8.9 pp | structurally too sparse downstream (39 opportunistic vs 412 routine vs 193 unclassified insiders in the windows); the 8 flagged are one private-markets cluster (HLNE/STEP/HTGC, flat) + one meme cluster (GME/PLUG/EOSE, deep negative). Opportunistic SELL 21 episodes -7.6 pp (p .047, ATR-adjusted -2.7 pp). Eight variants examined; none survives any correction. |

Insider note kept for the record: candidates with good excess AND an insider purchase
(CRL, ICFI, WEX, NTLA, OSPN, GPRE, all TP_FULL) were dropped by the classifier as
ROUTINE/UNCLASSIFIED. In a catalyst-selected population the routine/opportunistic axis
does not separate outcomes. If the signal is ever revisited it belongs UPSTREAM as a
candidate SOURCE (where it scored alpha_t +2.71 on R2000), never as a candidate attribute.

## 4. The avoid-filter brainstorm and why it was not registered

Chosen frame (owner decision, this session): source stays thematic; "alpha" = better
ranking/filtering inside the list; primary metric = mean car_10 of the RETAINED cohort
>= 0; secondary = top-vs-bottom spread. A single-variant DROP rule without ATR (ATR
already has its registered September kill line; MA50-extension and press-gate both
survived the ATR partial in July, so the rule tests different information) was
calibrated on the burnt panel:

| DROP rule (discovery, 205 ep / 26 clusters) | dropped | retained car_10 | 95% CI | dropped car_10 |
|---|---|---|---|---|
| baseline | 0% | -1.4% | [-3.4, +0.7] | — |
| `technical_ma50_distance_pct` >= p80 (12.24) | 20% | +0.2% | [-1.4, +2.0] | -8.0% |
| press-gate passed | 30% | -0.1% | [-1.8, +1.6] | -4.4% |
| MA50 >= p80 OR press | 40% | +1.1% | [-0.4, +2.4] | -5.2% |
| MA50 >= p67 (5.57) OR press | 50% | +0.8% | [-0.8, +2.3] | -3.6% |
| MA50 >= p90 (23.41) OR press | 35% | +0.7% | [-0.9, +2.1] | -5.4% |

Sign-stable across the 2026-06-06 split (retained +1.9% early / +0.8% late). Eight
rules examined -> the best one's +1.1% is winner's-curse-inflated.

Outcome-blind held-out sizing (2026-09-03): 188 car_10-mature episodes / 31 arrival
clusters (brief dates to 2026-08-19); the rule drops 34%, retaining 125 episodes / 29
clusters; ~160 retained expected by 2026-09-29. With sd(car_10) ~ 0.19 the standard
error of the retained mean is ~1.8 pp, so a one-sided "retained > 0" test detects only a
true mean >= ~4.5 pp; at the in-sample +1.1% the clear probability is ~15%.

**Decision (owner, 2026-09-03): path 3 — do NOT register.** A look whose modal outcome
is "inconclusive" would charge the September denominator for nothing. Reading: on the
thematic list, subtraction lifts the retained cohort to ~0, not above it — "stops losing,
does not start winning". The filter `MA50 >= 12.24 OR press` is kept here as a
LESS-LOSS candidate to be judged alongside the September ATR kill line, WITHOUT its own
look; it never enters selection, ordering or display on in-sample evidence.

Consequence for the alpha question under the chosen frame: the remaining lever is the
POPULATION of the list (cluster 21, mechanical-rule-vs-LLM selection, already
registered, zero new charge), or a change of frame to a second candidate source.

## 4b. Earnings-window premium probe (Frazzini-Lamont) — not identifiable on this panel

Pre-committed specs (3): (1) an earnings release whose effective session falls inside
the car_10 window; (2) a release in the 10 sessions before arrival; (3) sessions-to-next
release vs car_10. Sources tried: the AV `EARNINGS` cache (502 tickers) covers only 4% of
candidate episodes (different universe); the PIT-stamped brief column
`next_earnings_date` (from 2026-06-04) covers 84% of discovery rows in range and 91% of
held-out episodes and is the usable instrument.

Finding (feature-only, no outcome joined for spec 1 — the in-sample cell is empty):
- Discovery (06-04..07-05): 2.8% of candidates had a release within 14 calendar days
  (median gap 46 days). June is the inter-season gap — the in-window flag has ~0
  positives in-sample, by calendar, not by effect.
- Held-out (outcome-blind): 57 of 188 mature episodes (30%) are in-window, but 52 of
  them sit in the three brief-weeks 2026-07-13..08-02 (Q2 season). The flag is a
  calendar-season proxy: in-window vs not is "late-July briefs vs others", i.e. the
  regime-confounded-with-time trap already documented in the options sweep.
- Power: two-group se ~3.0 pp at 57/131 (sd 0.19) detects only >= ~7.5 pp; the
  literature premium is ~1%/month. Within-week (regime-controlled) comparison has
  se ~4.9 pp. Neither can refute the hypothesis at this N.

Decision: NOT registered — a test that cannot refute has tested nothing; a positive
would be a July-regime artifact. The instrument (`next_earnings_date`, PIT) is already
stamped, so the look accrues for free. Revisit condition (issue, `waiting:data`): >= 150
in-window ticker-episodes spanning >= 3 earnings seasons (regime diversity), i.e. after
the Q3 and Q4 seasons mature (~2027-01). Specs 1-3 above stay the frozen family.

## 5. Hygiene rules confirmed this session (for future in-sample probes)

1. Every in-sample script asserts `brief_date <= 2026-07-05` on outcome rows; held-out
   reads are outcome-blind only (counts, coverage, feature-only dropped share).
2. Record the NUMBER of specifications examined per cluster in the ledger (this memo:
   O'Neil 4, Buffett 8, spread 4, insider 8, filter 8, earnings 3 (feature-only) — 35 specifications, all on the
   burnt panel), so winner's-curse magnitude stays countable.
3. Take only direction and kill/register decisions from in-sample; never a threshold,
   never a "win".
4. An in-sample result never reaches selection, ordering or the card.
