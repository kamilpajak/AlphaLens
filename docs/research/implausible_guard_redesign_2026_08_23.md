# Design — teach the implausible-move guard to ask, not guess

Status: ACCEPTED 2026-08-23 (owner approved the §7 decision: lookup + cross-check, `SPLIT_INVALIDATED` terminal, parked rows resolve)
Date: 2026-08-23
Issue: #1090
Related: `docs/research/mcap_bracket_cost_contract_2026_08_22.md` (the measurement
this defect starves), `docs/research/theme_shadow_arm_contract_2026_08_23.md`

## 1. The defect, measured

`IMPLAUSIBLE_RETURN_THRESHOLD = 0.60` (`feedback/bar_window.py:35`, consumed by
`population_ladder_monitor._outcome_is_implausible`) rejects any replayed
outcome whose |forward return| exceeds 60%, on the assumption that a move that
large spans an unadjusted split. The row is not stamped; the prior row is
carried, so the position never terminates.

Every rejection in the last 21 days of production, checked against Polygon's
corporate-actions reference:

| ticker | rejected return | splits / dividends in window | verdict |
|---|---:|---|---|
| MRNA | +142% | none | **false positive** — real move (volume 4.3M → 199M, $34.7bn turnover on 2026-08-19; a reverse split cuts volume, it does not multiply it 46-fold) |
| CRSR | +61.6% | none | **false positive** — real move, barely over the threshold |
| MQ | +342% | **4:1 reverse split, executed 2026-07-01** | **true positive** — the replay window crosses the split, so raw-bar arithmetic is genuinely meaningless |

The guard is not wrong so much as **blind**: it cannot distinguish the case it
exists for from the case it destroys. One reference lookup distinguishes all
three correctly. Positive controls exist in quantity — ten real splits executed
in August alone (SMTK 50:1, MGN 40:1, XCH 20:1, ...), precisely the small-cap
population this project targets.

Cost of the blindness: `/edge` silently discards its largest movers (2 of 3
recent rejections were real), and in the bracket-cost measurement (#1087) 9 of
413 rows — all MRNA, the DISCARDED arm, the arm under test — are parked
indefinitely. ±60% over a 42-session hold is an ordinary move for biotech.

## 2. Mechanism

Keep the 0.60 threshold — but demote it from a verdict to a **trigger**. On a
trip, ask the source of record instead of guessing:

```
|forward_return| > 0.60
  └─ lookup: Polygon v3/reference/splits  + v3/reference/dividends
     for the ticker over [arrival_session − 3d, horizon_end + 1d]
       ├─ nothing found            → the move is real → ACCEPT the outcome
       ├─ split / special dividend → SPLIT_INVALIDATED (terminal, see §3)
       └─ lookup failed            → carry prior (today's behaviour), counted
```

Properties:

* **Lookups are rare and cacheable.** The trigger fired 3 times in 21 days
  across the whole population; each trip costs two reference calls, and the
  answer for a (ticker, window) is immutable — cache it forever on disk next to
  the bar cache. Free-tier budget impact: negligible.
* **Fail-closed.** A lookup failure preserves today's conservative behaviour
  rather than stamping a possibly-corrupt value. It is counted, not silent.
* **The dividends arm** matters because a large special cash dividend gaps raw
  prices exactly like a split; the original guard comment names both. "Large" =
  cash_amount > 10% of the window's entry anchor; ordinary quarterly dividends
  never trip the 0.60 trigger in the first place.
* **What does not change:** the `_SPLIT_SCREEN_THRESHOLD = 0.18` daily screen
  (it only forces a resolve — harmless), the adjusted=false bar doctrine, and
  every already-terminal row (frozen by design, never re-replayed).
* **Rejected: comparing the two grouped stores** (adjusted vs raw). The
  adjusted store is an append-only chain of daily snapshots, so each file's
  own-day close is in that day's basis and consecutive files show the split jump
  in BOTH stores. The comparison cannot detect anything. This dead end is
  recorded so nobody re-derives it.

## 3. The MQ class gets a name

A window that crosses a real split can never resolve meaningfully: the ladder
levels were set on pre-split prices. Today such rows are carried night after
night until the brief date falls out of the 75-day lookback — paying fetches the
whole way and looking identical to "still ongoing".

New terminal classification **`SPLIT_INVALIDATED`**: terminal (stops the
re-fetch spend), `realized_r` null (excluded from R aggregates, like the
`NO_FILL` convention Amendment 2 of the bracket contract records), counted in
classification mixes. Django's `ladder_classification` is a free `CharField`
with no choices, so ingest and `/edge` tolerate the new string; the SPA's status
mapping gets a label in the same PR.

## 4. Countable, not just logged

Today a rejection is one journal line. The nightly job will emit:

```
alphalens_feedback_guard_total{disposition="accepted_real"}
alphalens_feedback_guard_total{disposition="split_invalidated"}
alphalens_feedback_guard_total{disposition="lookup_failed"}
```

plus an alert on sustained `lookup_failed` (the fail-closed arm silently
reverting to the old blindness is exactly the state that must not be quiet).

## 5. What deploying this does to published numbers — the real answer

The framing in #1090 ("changing it moves figures already read") turns out to be
mostly wrong, and pleasantly so:

* **No terminal row ever changes.** Terminal rows are frozen and never
  re-replayed. Every figure already stamped stays byte-identical.
* **Parked rows are ongoing rows.** After the fix they resolve through the
  normal nightly evolution — the same transition every open position makes.
  Aggregates GAIN rows that were always supposed to be there; nothing is
  rewritten. That is bias removal, not history revision.
* The honest disclosure is therefore one sentence, not a migration: */edge*
  medians will move when the suppressed tail enters, and the deploy-day note
  records the before/after row counts so the step is attributable.
* MQ-class rows become `SPLIT_INVALIDATED` — which they de facto already are,
  minus the nightly fetch spend and the pretence of being alive.

Consequently the three options in #1090 collapse into one behaviour: **fix
everywhere, once**. There is no fix-forward/fix-history fork because history is
frozen by construction.

No `ladder_config_version` bump: for any accepted row the stamped values are
exactly what an unguarded replay would always have produced — the guard never
altered values, only suppressed rows. The counter metric plus this memo's date
provide the provenance. (The alternative — bumping the version — would split the
pooled population over a change that cannot alter any pooled value.)

## 6. Test plan (for the implementation PR)

* Lookup port injected; fixtures are the three REAL cases above (MRNA, CRSR,
  MQ), not invented shapes — the session's standing lesson.
* Dispositions: nothing-found → accepted; split-in-window → `SPLIT_INVALIDATED`;
  lookup exception → carry + counted. Cache hit does not re-call.
* A dividend below the size floor does not invalidate; one above it does.
* `_TERMINAL_SET` extension; the store round-trips the new string; Django ingest
  accepts it; the metric emits all three labels (zeros included).
* Mutation check on the disposition switch — each arm's deletion must go red.
* Live smoke on the VPS against the real parked rows before merge: expected
  outcome is MRNA/CRSR resolving and MQ going `SPLIT_INVALIDATED`.

## 7. The one decision that remains

§5 removes the migration fork, so what is left to approve is the mechanism
itself:

**accept / reject: replace the guess with a corporate-actions lookup, name the
MQ class terminal, and let the currently-parked rows resolve.**

If accepted, implementation is one PR on the monitor + a small SPA label, with
the zen pre-merge gate as usual. If rejected, the alternative worth naming is
"leave it and record the bias" — defensible only if ±60% tail outcomes are
deliberately out of scope for every current and future measurement, which the
bracket-cost contract already contradicts.

---

## Amendment 1 — adversarial review (Perplexity, 2026-08-23), adjudicated

The design above was submitted for adversarial review before implementation.
Three findings are accepted and change the design; three are adjudicated
against, with reasons; the rest are recorded as declared limitations.

### Accepted 1 — "no action found ⇒ accept" is logically unsafe (§2 revised)

The reviewer is right: a missing corporate-action record does not validate the
raw-bar arithmetic. Bad vendor bars, halt-reopen gaps, ticker changes, and
merger consideration all pass a splits+dividends lookup and would be stamped as
real. MRNA's 46× volume was evidence, not proof — a corrupted aggregation can
carry large reported volume too.

Revised disposition tree:

```
|forward_return| > 0.60
  └─ corporate-actions lookup (splits + dividends, window ±3d/+1d)
       ├─ action found   → SPLIT_INVALIDATED (terminal)
       ├─ lookup failed  → carry, counted (disposition=lookup_failed)
       └─ none found     → INDEPENDENT-VENDOR cross-check:
            window return recomputed from yfinance ADJUSTED daily closes
            (canonical yfinance client; different vendor, different basis)
              ├─ agrees within 10pp   → accept (disposition=extreme_validated)
              ├─ disagrees            → carry, counted (disposition=data_quality)
              └─ no data (delisted /
                 renamed / halted)    → carry, counted (disposition=data_quality)
```

The cross-check is local-cheap (one yfinance daily-history call, already
quota-tracked by the canonical client) and it also catches most of the
identity-lineage failures the reviewer listed: a renamed or merged ticker has
no coherent yfinance series and lands in data_quality rather than being
accepted. Only `extreme_validated` enters the aggregates automatically.

### Accepted 2 — the no-version-bump argument was wrong (§5 revised)

"Population membership is part of the measurement instrument" — conceded.
Reproducibility requires stamping the rule change even when no stamped value
moves, and the original argument was also too strong on its own terms: a parked
row is path-dependent state, and un-parking it changes downstream aggregation
even though no individual stamped value is rewritten.

Implementation compromise, to avoid fragmenting the pooled population over a
change that alters membership but no per-row value: a **provenance column**
(`guard_disposition`, plus a `guard_config_version` string) on every row the
trigger ever touched — NOT a bump of `ladder_config_version`, which is a
pooling key. The deploy-day note records before/after population counts. Any
future read can therefore split by regime; no read is forced to.

### Accepted 3 — the dividend threshold used the wrong denominator (§2 revised)

Materiality is relative to the pre-ex-date close, not the trade's entry price.
Trivial fix: the pre-ex close comes from our own raw grouped store. The 10%
figure stays as a triage level only — the reviewer's own position ("can remain
as an alert threshold").

Cache policy also tightened per review: a FOUND action is cached forever
(immutable once executed); a NONE-FOUND answer is cached 14 days, because
corporate-action records are corrected and appended late.

### Adjudicated against 1 — mechanical ladder adjustment across the split

The reviewer's preferred treatment (LEAN-style: scale quantity, levels, stops
by the split factor and let the position run) is standard for backtest engines
simulating an economically maintained position. Rejected HERE, for a reason
specific to this system: the replay measures what the SHIPPED ladder would have
done at a real broker, and real brokers **cancel resting orders on corporate
actions** — the Saxo rail this project actually trades on does. A mechanically
adjusted resting ladder models an order state that would not have existed.
`SPLIT_INVALIDATED` is closer to broker reality than adjustment is. Recorded as
possible future work for the already-filled-position case (~1 ticker/month at
the measured rate), not as part of this change.

### Adjudicated against 2 — CUSIP/FIGI security master

Correct at institutional scale; out of proportion here. The universe is brief
candidates from the last 75 days with tickers current at brief time, and the
independent-vendor cross-check fails loudly (data_quality) on exactly the
lineage breaks a security master would catch. Declared limitation, not a
component.

### Adjudicated against 3 — full sensitivity-report battery per read

The reviewer proposes five parallel population variants per read. At the
measured event rate (one true split per month in-population) the strata would
be empty theatre. The counts ARE reported (classification mixes + the counter
metric); a sensitivity battery becomes worth building when
`split_invalidated`'s count stops being ~1.

### Declared limitations (from the review, kept visible)

* **Selection effect of exclusion**: reverse splits correlate with distress, so
  excluded windows are a non-random slice. Unchanged from today — these rows
  are already suppressed; the change makes the exclusion terminal, counted and
  visible instead of silent and eternal.
* **Look-ahead in the lookup**: the reference is queried as of today, not as of
  the replay date. This is retrospective telemetry, not a tradable signal;
  declared, not fixed.
* **Intraday split timing**: irrelevant while the disposition is quarantine;
  becomes a real problem only if mechanical adjustment is ever built.
