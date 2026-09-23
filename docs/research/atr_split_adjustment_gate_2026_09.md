# The #1227 split-adjustment gate — can a corporate action reach the estimand?

**Status:** COMPLETE 2026-09-23 — **the gate passes, and its stated premise was wrong.**
No corporate action reaches a held-out label window. The premise it was written from — ATR
built from raw unadjusted bars against a split-adjusted label — is false in both legs, and
the real exposure runs the opposite way, on the label side. §6 holds the verdict.
**Closes** the last unrun item in
[`a20_overlap_inference_2026_09.md`](a20_overlap_inference_2026_09.md) §7.
**Registers nothing. Reads no held-out outcome.** Held-out reads are the same allowlist the
preflight fixed — ticker, dates, status — plus public corporate-action records and price
bars. No label value is read, printed or stored.

---

## 1. What the gate was written to catch

The #1227 checklist carried one unrun item, phrased as a fact:

> ATR is built from raw unadjusted bars, the label from split-adjusted ones; the label has a
> `split_guard`, ATR has none.

If true, a split inside the ATR lookback would put a fabricated volatility reading into the
signal, and the one irreversible look would be run on it. The item had never been measured.

## 2. The two sides, as the code actually builds them

| | source | adjustment | shape |
|---|---|---|---|
| ATR | `yfinance.Ticker(T).history(auto_adjust=False)`, cached one parquet per `(ticker, asof)` | split-adjusted, dividend-unadjusted | ONE fetch, re-adjusted end to end at fetch time |
| label `sel_ar_20` | Polygon grouped-daily, `adjusted=True`, one parquet per session, **never re-fetched** | split-adjusted, dividend-unadjusted | a PATCHWORK, each file adjusted as of its own fetch |

Both legs of the premise fail. The two sides agree on adjustment policy; what differs is
*shape*, and the danger sits with the label, not with ATR.

### 2.1 `auto_adjust=False` does not mean split-unadjusted

Yahoo's own quote payload is already split-adjusted. `auto_adjust` applies the further
`Adj Close / Close` ratio, which on Yahoo daily data carries the dividend and capital-gain
leg. So the code comment in `yfinance_client.daily_ohlcv` calling these "raw prices —
split/div adjustments applied at analysis time" is correct about dividends and wrong about
splits.

This is a **provider convention, not a contract**. yfinance ships a `repair` feature
precisely for the case where Yahoo fails to adjust a split. It is a fact to re-measure, not
a law to lean on, and §7 says what would reopen it.

### 2.2 The label store is a patchwork, and that is where a split survives

`backfill_grouped_daily_history.py` skips any session already on disk. So a split leaves
exactly one unadjusted step — but only if the two neighbouring files were fetched on
opposite sides of it. Of the 508 session files, **439 were bulk-written on 2026-06-14** and
share one adjustment epoch; only 69 come from the daily topup era. Among the 243 tickers in
the panel, exactly **one** split falls in that era: MQ, 1-for-4 reverse, ex-date 2026-07-01.

## 3. Three discriminations, and how each one fails quietly

Each arm of this gate is written against its own failure mode. The module is
`scripts/ml/split_adjustment_gate.py`; the reasoning is pinned in
`tests/test_split_adjustment_gate.py`.

**"Was there a split?" answered from a vendor list inherits that vendor's completeness.**
So the decisive arm does not ask a vendor at all. A split the patchwork failed to adjust
appears in the store and **not** in a uniformly re-adjusted source; a real market move
appears in both. `classify_step` is that test, and a missing reference returns `unchecked`
rather than collapsing into "clean".

**"Does a split reach ATR?" answered from the final ATR number has almost no power.**
Wilder's ATR is an EWM at `alpha = 1/14`. A bar 190 sessions back — the actual distance from
LCID's split to its episode — carries a weight of `7.5e-7`. A cross-sectional percentile of
the end-of-window ATR therefore cannot see a spike that has decayed, and an early draft of
this work used exactly that check and mistook it for evidence. The question has to be asked
on the **true range of the split bar itself**, which is also the only place a high/low-only
discrepancy could show.

**"Does the guard see it?" is arithmetic, and the arithmetic has a hole.** §5.

## 4. What was measured

### 4.1 The ATR side is clean, on the quantity ATR actually consumes

24 cached fetches span a known split of their own ticker, across 5 tickers and both
directions. For each, the true range of the split bar, built from `high`, `low` and the
previous close:

| ticker | split ratio | measured TR% of close | ticker median TR% | a split-unadjusted bar would read | ratio |
|---|---|---|---|---|---|
| LCID | 0.1 (1-for-10 reverse) | 13.82 | 5.54 | 90.0 | 7x smaller |
| MQ | 0.25 (1-for-4 reverse) | 6.20 | 3.28 | 75.0 | 12x |
| PEGA | 2.0 (2-for-1) | 4.28 | 3.51 | 100.0 | 23x |
| PIPR | 4.0 (4-for-1) | 5.13 | 2.89 | 300.0 | 58x |
| RGR | 0.374 | 2.24 | 2.64 | 62.6 | 28x |

The expectation column is `100 * |1 - r|` and is **strongly asymmetric**: a forward 4-for-1
reads about 300% because the denominator shrinks, a 1-for-10 reverse only about 90%. An
earlier draft of this memo quoted one multiple for every split and overstated the reverse
cases tenfold. Not one of the 24 bars is within a factor of seven of its unadjusted value.

Corroboration on the vendor's own behaviour: comparing every pair of cached fetches of the
same ticker over their overlapping dates gives 1185 identical pairs, 0 non-uniform, and one
uniform rescaling — MQ, where **open, high, low and close all scale by exactly 4.000000 and
volume by exactly 0.250000** across all 197 overlapping sessions. The inverse volume scaling
is what rules out a currency or unit correction, which would not touch volume.

### 4.2 No corporate action reaches a label window

Census over three windows, on the arrival calendar, with a ±3 day tolerance because the
store's discontinuity can precede the vendor ex-date by a session:

| window | burnt (197 episodes) | held out (419 episodes) |
|---|---|---|
| label, 20 sessions | **0** | **0** |
| beta pre-window, 251 sessions | 5 events / 5 episodes | 4 events / 3 episodes |
| ATR lookback, ~400 days | 5 events / 5 episodes | 5 events / 4 episodes |

Every beta-window event is outside the guard band, so `estimate_pre_window` drops the
offending return — 2 of 251 observations for those three held-out episodes. The tolerance
surfaces exactly one edge case, MQ's burnt episode, whose window ends 2026-06-29 against a
store step on 2026-06-30; its window closes were read directly and are all on the pre-split
scale.

### 4.3 The vendor-independent arm agrees

Scanning the label's own closes for steps matching a small-split template — the ones the
guard cannot see — flags 9 steps inside held-out label windows. Cross-sourced against
yfinance on the same dates, **all 9 agree to four decimals and carry volume ratios of 1.0 to
9.7**, rising rather than scaling inversely. All 9 are earnings moves; they cluster between
2026-07-27 and 2026-08-07, which is the Q2 reporting window.

The one confirmed artefact in the whole store is MQ: **Polygon 3.8852 against yfinance
0.9713** on the same two dates, volume ×1.22. It is real, it is the patchwork, and it lands
in no label window.

## 5. The guard has a hole, and it is empty by luck

`selection_label._out_of_bounds` fails a row when the close step leaves `(0.55, 1.8)`. So a
split whose ratio lies in **[0.556, 1.818]** is invisible to it — to the label status and to
the beta-window drop alike. That covers 3-for-2 (step 0.667), 4-for-3 (0.75) and 5-for-4
(0.8). Published US samples put 3-for-2 at roughly a fifth of all splits, so this is a
material class, not a corner.

On this panel the band is **empty**: every observed split has a step of 10.0, 3.885, 2.674,
0.5 or 0.25. That is a property of these 243 tickers over these 15 months, not of the guard.

A second, tighter margin: a plain 2-for-1 leaves a step of 0.5 against a 0.55 bound, so a
split day on which the stock also rallies **10%** stops being visible. 2-for-1 is the single
most common split there is.

## 6. Verdict

**The gate passes.** Nothing on this axis blocks the #1227 confirmation. No adjustment
artefact reaches a held-out label window, by two independent methods, one of which does not
depend on any split record being complete.

**The premise it was written from is withdrawn.** ATR is not built from split-unadjusted
bars. The memo that carried the item should be read with §2 substituted for it.

## 7. What is not settled, and what would reopen this

- **The blind band is not closed.** A 3-for-2 in a future window would pass unseen and become
  a fabricated −33% return. Empty today is not fixed. Filed separately; this memo does not
  change production behaviour.
- **The 2-for-1 margin is 10%.** A split day with a double-digit rally defeats the guard.
- **The guard's only three firings to date are false positives.** All three are MYGN, all on
  the step 5.370 → 2.860 of 2026-07-31, a real −47% day that yfinance reports identically.
  MYGN's only recorded splits are 2000 and 2009. The guard cost three episodes and has never
  yet caught a split.
- **Yahoo's back-adjustment is a convention, not a guarantee.** If yfinance's `repair` notes
  ever apply to a panel ticker — a split present in the data with preceding prices not
  adjusted — §4.1 is reopened. The runnable check is the true-range test, not the comment.
- **The ex-date offset is one session.** Any future census must keep the tolerance; matching
  on the ex-date alone has a blind spot at both window edges.
- **Dividends are unadjusted on both sides**, which is consistent, but Polygon's dividend
  behaviour was read from its docstring and not measured. It does not affect this verdict,
  because a dividend cannot produce a scale discontinuity of the size at issue.

## 8. Review trail

- **Perplexity** corrected the mechanism behind §2.1: the split adjustment happens in Yahoo's
  payload before yfinance sees it, rather than yfinance overriding `auto_adjust`. It supplied
  the split-frequency figures behind §5 and demanded the volume control in §4.1, which was
  then run and passed.
- **zen `deepseek/deepseek-v4-pro`** found the two real holes: that the ATR arm had tested
  closes only when ATR eats high and low, and that the end-of-window percentile check had no
  power against a decayed spike. Both were closed by running §4.1 rather than by argument;
  the second is the reason §4.1 exists in its present form. Its remaining points — a
  single-vendor split record, and the beta pre-window — were already answered by §4.3 and
  §4.2 respectively, the latter missing from the brief it was given rather than from the work.
