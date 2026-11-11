# Design Memo: Successor System for the Proposal Pipeline

**Status:** DRAFT — not lockable until §6 (news vendor) is decided. Decisions D-2 (storage licence), D-4 (repo topology) and D-7 (private item stream) were closed by the owner on 2026-11-11. Amended the same day with §2.1b, five requirements that came out of measurement after the first draft. Sections 1.9 and 1.10 were added the same evening and they move the headline numbers: the treated count is 6,529 rather than 4,715, and measured power at the registered floor is 10.9% rather than 27.4%. No compute over one hour is launched from this memo.
**Author:** quant research (solo)
**Type:** system requirements + high-level architecture + transition plan
**Origin:** two days of measurement (2026-11-10, 2026-11-11) on the production stores turned up seven defects in the current pipeline whose common cause is architectural, not local. Each is individually fixable; the shape that allowed all seven is not.
**Related:** `apps/newsvault` (PR #1881, first slice shipped), issue #1882, `paradigm_failures_postmortem.md` (the reusable-infrastructure inventory this memo inherits from).
**Doctrine anchors:** run it before believing it; a number is a claim published with its construction; recount a key N by a second path; pre-registration freezes the text, not the population; no backtested strategy earns capital.

---

## 0. TL;DR and framing

Build a successor **in parallel**, in Rust, and stop adding features to the current pipeline while
keeping it fully maintained. Do **not** rebuild the broker rail. Register a **parity gate** with a
date, because a parallel build without one becomes two systems forever.

Three asymmetries decide this, and they are the whole argument:

1. **The pipeline's output is unvalidated.** No backtested strategy holds a standing PASS and
   nothing it produces earns capital. Incrementalism would be protecting a generator of proposals
   whose quality is unmeasured.
2. **The broker rail is validated — by real fills on a real account.** It is the one part with
   something to lose, so it is the one part that is never rewritten.
3. **The archive has to fill anyway.** The study this pipeline feeds needs 246 sessions and has
   about 125 (§1.8). Building during that wait costs no calendar time. A paid news archive may
   collapse the wait entirely (§6), which is the single highest-value unknown in this memo.

What this memo does **not** do: choose a vendor, resolve the storage-licence question, specify the
service contracts, or set the parity-gate date. Those are §8, listed as open rather than written
around.

---

## 1. What the current pipeline measured wrong

Every row is a measurement with its construction and date, not a code review opinion. Nine of the
ten surfaced within two days, during one study.

### 1.1 The archive is shaped by another consumer's budget

`thematic_news/<date>.parquet` keeps 200 items per day because 200 is the LLM budget for briefs,
and `alphalens thematic ingest --force` (`deploy/docker/run_thematic_day.sh:44`) overwrites it on
every run. Counted on the production lake 2026-11-11 by reading every `run=*.parquet` under
`thematic_news_lake/session_date=<d>/` and taking distinct `id`:

| date | capped view | distinct in lake | truncation |
|---|---|---|---|
| 2026-07-01 | 200 | 287 | 30% |
| 2026-07-15 | 200 | 560 | 64% |
| 2026-08-12 | 200 | 678 | 70% |

The truncation rate moves day to day, so a count taken from the view carries a day-varying bias in
the treatment variable. The selection rule is "newest clusters first", which is a sensible rule for
a brief and a wrong one for counting.

### 1.2 A one-sided point-in-time filter leaks

Measured 2026-11-11 on 69 Perplexity retrievals: filtering on publication date alone returned
documents not visible at the claimed instant in **60.2%** of probes. Adding
`last_updated_before_filter` alongside `search_after_date_filter` closed it on **0 of 69**, at the
cost of 39% yield and a median document age of 2 days. The defect was not a wrong comparison; it
was asking only one of the two questions that exist.

### 1.3 The view postdates our own knowledge

The current view holds exactly one distinct `ingested_at`, equal to the **last** ingest run's
stamp. Verified to the millisecond on four days 2026-11-11 by parsing `run=<stamp>Z` filenames and
comparing against the view's column:

| session_date | runs | first sighting | view's stamp | gap |
|---|---|---|---|---|
| 2026-07-01 | 6 | 2026-07-02 00:34:21.486Z | 2026-07-02 20:33:21.503Z | 19 h 59 m |
| 2026-08-12 | 6 | 2026-08-13 00:33:40.394Z | 2026-08-13 20:33:40.261Z | 19 h 59 m |
| 2026-09-02 | 6 | 2026-09-03 00:32:41.566Z | 2026-09-03 20:30:42.067Z | 19 h 58 m |
| 2026-11-09 | 3 | 2026-11-10 04:30:25.071Z | 2026-11-10 12:31:23.681Z | 8 h 00 m |

Observation time should be the earliest instant we could have known an item. Taking the last run
moves our knowledge forward by up to 20 hours.

### 1.4 No run lands before the close it is attributed to

Across the 24 run files in partitions 2026-07-01..04, **0 of 24** executed at or before 16:00 ET of
their own session; the first lands 20:30–20:35 ET. Measured 2026-11-11 by converting each run
stamp to America/New_York.

Meanwhile **59.7%** of items (5,204 of 8,711 over 50 files in 2026-06-29..09-02) carry a
publication timestamp before 16:00 ET of the same day, with **zero** missing timestamps. So the
news was public during the session and we learned of it that evening. This is a property of the
cadence, not of the pipeline's logic, and it is why §4 exists.

Cadence history, read from the lake 2026-11-11: 6 runs/day at 00,04,08,12,16,20 UTC from
2026-06-10 to 2026-09-15; 3 runs/day at 00,04,08 from 2026-09-16; 3 runs/day at 04,08,12 from
2026-09-29, matching the deployed user timer `OnCalendar=*-*-* 04,08,12:30:00 UTC`. Individual days
carry 7–9 runs where a manual run overlapped the timer. Note that
`news_ingest.py` still carries a comment asserting 6 builds/day; the comment is stale.

### 1.5 A swallowed network error deletes companies silently

`httpx.ReadError` / `ConnectError` escaped the retry loop in the map builder, so a ticker got no
output file. The vote rule requires all three extraction draws, so **37 of 1,672** companies
(2.2%) could not carry an edge and sat in the control arm as structurally eligible. Counted
2026-11-11 as the union of per-draw absences; recovered for $0.2142, which added 209 pairs
(5,756 → 5,965) and 18 edge-carrying companies (747 → 765).

### 1.6 A missing input became an empty result

`/usr/share/dict/words` is absent on the VPS (the directory exists and is empty), and the code fell
back to an empty set. The dictionary-word audit therefore reported **0** cores on a host that had
no dictionary — indistinguishable from a clean result. After copying the file (235,976 entries),
the real figures are **108** single-token dictionary cores producing **485 treated rows (10.3%)**,
the largest being `Target`.

### 1.7 Units without types, identity without content

Two defects with one cause. A standard error in units of sd was compared against one in units of
return, printing an apparent **0.105×** where the true inflation is **2.61×**. Separately,
typed-pair filenames used `abs(hash(name))`, and Python randomises string hashing per process, so
`f.exists()` could never skip across runs and a resume would have rewritten all 5,756 pairs.

### 1.8 Rows are not the sample size

The analysis panel was `len(universe) * len(sessions)` — `count_cases.py:104` — giving exactly
1,672 × 47 = **78,584**. Recounted 2026-11-11 against the actual presence of a close on `d` and on
`d+1`: **78,460**. The error is 124 rows; the method is wrong regardless of its size.

Worse, the power figure that cleared the study's only binding gate was
`sqrt(1/4172 + 1/74412) = 0.0159101` fed into the iid two-sample formula. Measured 2026-11-11 by
injecting a floor-sized effect into the frozen treatment vector and running the registered
permutation procedure (24,000 reference draws, no fixed points, seed 20261111):

| quantity | analytic | measured |
|---|---|---|
| se of the statistic | 6.28 bps | **16.39 bps** |
| inflation over iid | 1.00× | **2.61×** |
| power at the 0.05 sd floor (= 20.1 bps), α = 0.05 | 88.2% | **27.4%** |

The exchangeable unit is the **session** — 47 of them — not the row. Cross-sectional width does not
help: reaching 80% power at that floor needs 246 sessions.

### 1.9 Both of those figures were themselves computed on the truncated view

Found the same day, after §1.8 was written. The counting script read
`~/.alphalens/thematic_news/<date>.parquet` — the **capped 200-item view** — not the append-only
lake. So the measurement that diagnosed the cap as an architectural defect was itself taken through
the cap. Recounted over the complete lake with three corrections, each attributed separately:

| configuration | treated company-sessions | vs original |
|---|---|---|
| original: capped view, HTML tags left in, 4,000-char truncation | 4,715 | — |
| complete lake, tags stripped, truncation kept | 5,857 | +24.2% |
| complete lake, tags stripped, **no truncation** | 6,662 | +41.3% |
| complete lake, tags stripped, no truncation, **18 cores sieved** | **6,529** | **+38.4%** |

Attribution: reading the lake instead of the view **+24.2%**, lifting the 4,000-character cap
**+13.7%**, sieving the cores **−2.0%**. The largest cause was the one not being looked for. The cap
alone discarded 50.5% of all core occurrences, against a median EDGAR press-release body of 50,622
characters.

**And the corrected panel has *less* power, not more.** Re-measured by the same registered
procedure (24,000 draws, no fixed points, seed 20261111):

| quantity | panel of §1.8 (4,715 rows) | corrected panel (6,529 rows) |
|---|---|---|
| se of the statistic | 16.39 bps | **24.74 bps** |
| inflation over the iid formula | 2.61× | **4.50×** |
| power at the 0.05 sd floor, α = 0.05 | 27.4% | **10.9%** |
| power at the 0.10 sd floor | 64.3% | 39.1% |

The mechanism is concentration, measured rather than inferred:

| treated rows per session | §1.8 panel | corrected |
|---|---|---|
| median | 91 | 125 |
| minimum | 49 | **24** |
| maximum | 179 | **356** |
| maximum / median | 1.97 | **2.85** |

The six largest sessions are 2026-07-30, 07-31, 08-04, 08-05, 08-06 and 08-07 — one contiguous
earnings-season block, where EDGAR releases are long and numerous. So lifting the cap did not add
rows evenly; it added them banked into a fortnight whose sessions share one market environment.
Nominally there are still 47 exchangeable units; effectively there are fewer.

**The 4,000-character cap had been acting as an accidental variance stabiliser.** It bounded what any
one long document could contribute and so flattened that spike. Removing it is correct — half the
signal was being discarded — but it exposes a concentration the cap was hiding, which is exactly
the §4.4 guard the first adversarial review called the only live one and found absent from the
power section. It is now not merely live but binding.

### 1.10 Entity precision of the matcher, on our own corpus

Sixty occurrences, seeded uniform sample (seed 20261111), hand-labelled:

| label | | share | Wilson 95% |
|---|---|---|---|
| entity-correct | 36/60 | **60.0%** | [47.4; 71.4] |
| wrong entity | 17/60 | 28.3% | [18.5; 40.8] |
| residual boilerplate | 7/60 | 11.7% | [5.8; 22.2] |

Against 50.0% ± 6.5 pp measured on general news pages, the difference is +10.0 pp with a standard
error of 9.1 pp — **1.10 SE, indistinguishable**. Our corpus is not measurably cleaner.

But the diagnosis differs, and it is the actionable part: **16 of the 17 wrong-entity errors come
from the CORE being wrong, not from an ambiguous match.** Generic name components (`Therapeutics`
matching "Relay Therapeutics"), financial terms (`Enterprise` matching "Enterprise Value"), table
words (`Institutions`), government programmes (`Medicare`, `Medicaid`) and one bankruptcy term
(`Actions`). Exactly one error was a genuine matching ambiguity: `Target` inside "Talos Target
Coulomb Interest". Removing the bad cores would put precision at 36/44 = 81.8%, and 36/37 = 97.3%
among non-boilerplate occurrences.

One failure of my own belongs in the record. The sieve initially removed `Premier`, which is
**Premier Inc. (PINC)**, a real company and a real counterparty of ANIP. It was restored. `Popular`
stays removed because the same company also reports "Banco Popular", which is multi-token and
survives. This is the irreducible class — a single-token name of a real company that is also an
ordinary word — and no word list resolves it; only corroboration in context does.

---

## 2. Requirements

### 2.1 Correctness — each one answers a row of §1

| # | Requirement | Answers |
|---|---|---|
| R1 | The archive is complete and append-only. Any cap is a derived **view**, computable from the archive; the reverse is not required to work. | §1.1 |
| R2 | Valid time and observation time are **distinct types** that cannot be compared with each other. The single visibility query requires both; the half-predicates are not reachable from outside their module. | §1.2 |
| R3 | When several runs carry one item, observation time is the **earliest**. | §1.3 |
| R4 | Every store carries a **coverage ledger**: one row per (source, target date, attempt) with filter parameters, status, item count, cost, fetch time and a hash of the result. | §1.1, §1.4 |
| R5 | A failed network operation must be handled for the program to compile. Dropped records are counted and reported, never silent. | §1.5 |
| R6 | A missing input aborts with an error. An empty fallback is forbidden. | §1.6 |
| R7 | Units are types; artefact identity is a content hash, never a per-process function. | §1.7 |
| R8 | Row presence is counted, never multiplied. A panel reports its number of **exchangeable units**, not only its row count. | §1.8 |
| R9 | Power is obtained from the same randomization procedure that will compute the result, reported with its Monte-Carlo error. The iid formula does not enter a registered document. | §1.8 |
| R10 | Every item carries a **provenance** label distinguishing a live ingest from a vendor-archive backfill. A panel mixing them discloses the mix. | §6 |
| R11 | A backfilled item's observation time derives from a **measured lag** carrying the probe id, probe count and leak count. There is no constructor that lets it claim same-day visibility. | §1.2 |
| R12 | No service holds credentials for, or a path to, the pick queue. | §7 |

R1, R2, R3, R5, R6, R7 and R11 are implemented and under test in `apps/newsvault` as of PR #1881;
R2's "cannot be compared" is pinned by a `compile_fail` doc test.

### 2.1b Added after the first draft, 2026-11-11

Five more, each from something measured after section 2.1 was written. R13 and R14 come from
reading a private repository that consumes this project's own brief API; R15 to R17 come from
probing Common Crawl's CC-NEWS against our own archive.

| # | Requirement | What forces it |
|---|---|---|
| R13 | **Five time columns per observation**, not two: when we asked, when the answer arrived, when the source itself observed it, the latest fact the payload covers, and when we committed it. A database-assigned sequence number is the **only** ordering authority; wall clocks are evidence, never order. | R2 and R3 carry two instants, which is not enough. A fetch issued at 10:00 that commits at 10:01:10 carries data through 10:00, while a fetch issued at 10:01 has already committed data through 10:01 — so ordering by commit time hands back the older data and calls it current. Verified in `src/profiles/catalog.py:67-79` of that repository. |
| R14 | **An attempt is recorded even when it yields nothing.** `EMPTY`, `FAILED` and `COLLAPSED` are distinct outcomes, filed separately from values, so absence-of-fact never reads as failure-to-ask. A payload that shrinks to a fraction of what the store holds is a degraded fetch, not a quieter world. | 2026-07-11 holds 59 items against a median of 181 and a maximum of 321, with nothing recording whether that was a Saturday or an outage. This sharpens R4 from "a ledger exists" to "the ledger distinguishes the four outcomes". |
| R15 | **A disputed publication timestamp is marked, never corrected.** Where two sources carry the same story with dates far apart, the newer one's date is flagged as unknown and the earlier one carried alongside; neither is moved. | Aggregators re-stamp old articles, and 79.6% of CC-NEWS records yield a publication date only by scraping page metadata of varying quality (measured 2026-11-11 on 4,000 records). A disputed date is an **unknown** age, never an old one: silently re-dating it would hide a genuinely new item behind an old twin. |
| R16 | **The coverage ledger records poll events, not only content changes.** Content-hash deduplication is correct for storage and wrong for the ledger. | Deduplicating observations by content hash — which R1 wants, to avoid re-stamping unchanged data as current — destroys the ability to reconstruct when we asked and received the same answer. That reconstruction is exactly what R4 exists for, so the two must be recorded separately. |
| R17 | **Entity matching must survive a broad corpus.** A core list that works on a pre-filtered feed is not evidence that it works on general news. Matching runs over the extracted article body, never over full page HTML. | Measured 2026-11-11 on one CC-NEWS file: single-token cores produced 5,017 hits against 299 from multi-token ones, and 60.4% of the top-60 hits were social-platform and big-tech names appearing in page boilerplate rather than in article text. Separately, our own map carries geographic names as cores — `Los Angeles`, `South Korea`, `Saudi Arabia`, `Australia`, `Qatar` are 5 of the 588 ever-salient cores and account for 43 of 2,258 core-session salience events, or **1.9%**; adding `Premier`, `Popular` and `Industrial` takes it to roughly 3%. Note that `Target`, `Block`, `Circle` and `Reddit` are real companies in the map that are also ordinary words, so a stoplist cannot fix this. |
| R18 | **`derive` must be able to produce two panels under two rule versions over one snapshot and diff them.** A rule change that cannot be diffed cannot be reviewed. | §1.9 is the argument. A single net figure of +38.4% would have hidden that **+24.2%** of it came from reading the capped view — the cause nobody was looking for and the largest of the three. Separating the effects was not a nicety; it was the only way that defect surfaced. The gazetteers make the same point from the other side: three sets hand-written inside a throwaway script, and when one entry was wrong there was no version, no test and no record. Rules belong in a hashed file with a `rule_version` stamped into every output, so a wrong entry is a diff and `Premier` becomes a test case. |

### 2.2 Non-functional — stated here because they are currently UNSET

These are requirements in the sense that their absence is a defect. None has a number yet, and
each needs one before the memo can lock.

| # | Question | Why it bites |
|---|---|---|
| N1 | **Backup and recovery of the archive.** Where is the second copy, how is it verified, what is the recovery time? | The design treats the archive as irreplaceable and as the project's most valuable asset, and says nothing about surviving a disk. On 2026-11-11 the development machine reached 100% disk and tooling stopped; the warning was free this time. |
| N2 | **Freshness target.** How stale may the newest item be before it is an incident? | Event-driven ingest changes the alert from "job did not start" to "age of newest item" and "backlog depth". Without a number there is nothing to alert on. |
| N3 | **Monthly cost ceiling**, split between vendor and model. | `modelgate`'s cap is the only real guard; a cap with no number is a good intention. |
| N4 | **Retention.** How long is raw article text kept, and is that compatible with D-2? | Changes the storage design, not just a setting. |
| N5 | **Derive runtime budget.** How long may a full panel rebuild take? | Decides whether reproducing a past panel is routine or an expedition. |

---

## 3. High-level architecture

Five deployables over eleven bounded contexts. Each deployable has exactly one reason to be
separate; a context without such a reason stays inside one.

| Service | Contexts | The one reason it is separate | Never |
|---|---|---|---|
| `vault` | newsvault, filingvault, marketvault | Runs on a clock and talks to vendors; its failure must not take derivation down. Owns the session calendar, including half days and DST, in one place. Also carries the private item stream of D-7, as a derived surface. | Deletes, overwrites, or truncates to another consumer's budget. Publishes its internal event shape to anyone. |
| `derive` | matcher, signals, panel | Pure function of (snapshot hash, rule version); CPU-bound and reproducible bit-for-bit. Most of §1 lived here. | Calls a vendor, reads a clock, or keeps state between runs. |
| `modelgate` | modelgate, extractor | The only component that spends money per call and the only one that can answer differently to the same input. | Returns a number, a date or a threshold. Receives a market-cap bracket condition in a prompt. |
| `propose` | scorer, briefer | Decides ordering and the text a human reads; a weighting change must not force a store rebuild. | Produces an order, an executable position size, or anything the broker rail would accept. |
| `ledger` | ledger | Sees outcomes and prices, which nothing upstream may see early. The reverse edge is forbidden one-way. | Writes to any service above it. |

Statistics stay in Python. The boundary is an Arrow file plus a manifest, not a network call;
rewriting `numpy`/`scipy` would be cost without benefit. Under D-4 this tier is **written fresh and
kept small** — permutation inference over our own panel is a few hundred lines — rather than ported
from the 9,602 lines that analysed closed paradigms. It lives in the same repository as `derive`
on purpose: the panel-to-statistics handover is where §1.8's error happened, and a test can only
span it from inside one tree.

**Splitting finer is rejected for now.** Eleven contexts are drawn so the seams exist later. For a
one-person system each extra deployable is another deploy, another log, another contract version
and another place something goes quiet — and this repo already records a case where an edge between
components *removed* a standing signal instead of raising one.

---

## 4. Event-driven ingest, and the four rules that keep it reproducible

§1.4 is the motivation: a clock-driven ingest structurally cannot deliver same-session news.

The vendor under consideration offers no webhook; its `minuteStreamArticles` endpoint returns
articles added to its index in the last minute, with a `minutesAgo` parameter for catch-up
(checked 2026-11-11; not confirmed against official docs, see §6). So **we** produce the events: a
one-minute poll, then an event emitted after the write.

Event-driven must not become incremental state. The four rules:

1. **An event triggers computation; it never accumulates it.** `derive` stays a pure function over
   a **closed** window; the event decides only *when* to run it. Incremental salience would make
   the result depend on arrival order, which destroys R8/R9 — the invariant §1.8 cost the most to
   learn.
2. **Write, then emit, in one transaction.** The archive is the source of truth; the event is a
   notification that it grew. A lost event then costs latency, not data.
3. **The hourly sweep stays**, as a backstop. Event-driven *plus* periodic, not instead of. A
   silently dead consumer looks exactly like a quiet news day — the failure §1.4's ledger gap
   already produced once.
4. **Salience is a function of a cutoff, not of a day:** `salient(core, T)`. The rolling threshold
   computed from a partial day differs from the same threshold at the close, so a company can be
   salient at 10:00 and not at 16:00. Without an explicit `T` the treatment value does not exist,
   and a result could be moved after the fact by choosing an hour.

**Transport: a Postgres outbox with `LISTEN`/`NOTIFY`.** Postgres already runs in production. The
outbox in the same transaction as the write gives rule 2 atomically, and the outbox table *is* the
replay log. Kafka solves a throughput problem we do not have: 300–700 items/day is one event every
two to five minutes.

**Two consequences to price, not assume away.** Per-item model calls would be 300–700/day instead
of one batch, so windowing and debounce live *inside* `modelgate` with the cost cap doing real
work (N3). And lower latency manufactures the argument "we must act now", which is exactly the
pressure that talks a human into automating a pick — so §7's wall matters more under this design,
not less.

---

## 5. Transition

### 5.1 Parallel build, with a forcing function

This is the same work as a staged migration with the irreversible step removed — and with it, the
only thing that forced completion. The replacement is a **parity gate**: the successor must
reproduce the current system's outputs on frozen data, to the number, before anything on the old
side is switched off.

Demonstrated on 2026-11-11 over the **whole archive**, not a sample. Two implementations written
independently from the same stated rules — one in Rust, one in pandas — were run over all 156 lake
partitions and compared on five fields per day, including a sha256 digest of the sorted item-id set:

| | |
|---|---|
| days | 156 (2026-06-04 … 2026-11-10) |
| raw rows | 243,226 |
| distinct items after the first-sighting rule | 62,507 |
| unusable rows | 0 |
| valid-time conflicts | 301 |
| **days agreeing on every field, digest included** | **156 / 156** |

Zero disagreements, so the rules of §2.1 are unambiguous rather than merely written down. That is
the shape every stage needs — characterisation tests at the command-line boundary, not ported unit
tests.

One caution from doing it: the first comparison reported a mismatch on all 156 days because the
digest was computed once through the shell (`sort` collates by locale, `shasum` counts a trailing
newline) and once in Python (code-point order, no trailing newline). The counts agreed throughout.
An instrument that introduces its own difference will report a defect that does not exist, so the
two sides must share one digest function, not two implementations of one.

**The frozen outputs must be captured while the old system still runs.** After it is switched off
there is nothing to compare against and the gate stops existing.

### 5.2 "No new features" cannot mean "no maintenance"

18 units run on their own. Classified 2026-11-11 from `systemctl --user list-timers` and
`list-units` on the production host:

| group | units | freeze? |
|---|---|---|
| data collection (news, prices, Form 4, EDGAR, bracket cost) | 5 | **no** — it is the successor's fuel |
| money rail (two broker daemons, price reader, two Saxo token refreshes) | 5 | **no** — real orders |
| observability (unit drift, Grafana, Prometheus rules) | 3 | **no** — without it a failure is silent |
| measurement and literature (shadow returns, /edge mirror, literature scans, issue wake) | 5 | yes |

13 of 18 keep running and keep receiving fixes. The Saxo token refresh is the quietest way to break
this: on 2026-10-23 the upstream app registration failed and the token endpoint answers identically
for every cause, so diagnosis requires a different endpoint. A frozen repo with a broken token
refresh is a stopped money rail.

### 5.3 The cost that is not code

193 test files and **77,540 lines** touch `thematic`, the part being rebuilt; separately
**107,838** lines touch the broker rail (counted 2026-11-11 by `grep -l` over
`apps/alphalens-research/tests/test_*.py`; the two sets overlap, so they do not sum to the suite's
277,978 lines). Those tests encode years of edge cases found by production incidents and do not
port. §5.1's characterisation approach is the mitigation; it is partial, and saying otherwise would
be false.

**What does carry over**, inherited deliberately rather than rewritten: the bitemporal schema; the
append-only lake; the one-door money path with hand-written picks; the methodology gates; the
`__status__` / `__closed_reason__` lifecycle discipline; the drift check that detects and never
applies; per-vendor canonical clients with an AST gate; the CLI-as-versioned-API conventions;
and `paradigm_failures_postmortem.md` (650 lines, eleven closed paradigms, with its own
reusable-infrastructure section).

**What does NOT carry over, decided 2026-11-11 (D-4):** the research memos. Of 354 files in
`docs/research`, 125 carry a `Status:` line at all — 77 `LOCKED`, 31 `DRAFT`, 17
`CLOSED`/`REJECTED`/`SUPERSEDED` — and the remaining two hundred-odd are one-off reports and audit
verdicts. They stay here as an archive. Exactly **two** files move, 813 lines in total:
`methodology-gates.md` (163) because its data-vendor point-in-time gate fires on the very next
decision this project takes, and `paradigm_failures_postmortem.md` (650) because without it the
successor is free to repeat eleven failures that are already paid for. The 18 ADRs (1,931 lines)
are optional and mostly describe code that is being left behind.

One consequence to state plainly: **this memo is itself in the archive.** The document founding the
successor would otherwise live in the repository being walked away from. So it moves too, as the new
repository's first file, while this copy stays here as the record of the decision.

---

## 6. News vendor — open, with the shortlist measured

Two jobs with different requirements, which is why no single vendor wins:

| requirement | live stream | historical backfill |
|---|---|---|
| observation time | ours; we know when we fetched | must come from the vendor or a measured lag |
| "indexed before T" filter | irrelevant | **critical** |
| archive depth | irrelevant | **critical** |
| archive stability | irrelevant; we hold our own snapshot | **critical** |
| push delivery | matters; it is the whole latency | irrelevant |
| article body | required | required |

Checked against official pages on 2026-11-11:

| | NewsAPI.ai | NewsCatcher | Webz.io |
|---|---|---|---|
| archive from | **2014** | 2019, Enterprise only (listed plans: 30 days) | 2008 (vendor claim) |
| both timestamps exposed | yes (`dateTime`, `dateTimePub`) | yes (`published_date`, `parse_date`) | yes (`published`, `crawled`) |
| **filter on index time** | **no — `dateStart`/`dateEnd` are documented as publication; sort `date` is publication** | **yes — `by_parse_date=true`** | documented, unverified |
| push | no; one-minute polling | webhook advertised, unverified | unverified |
| full body | yes | **not stated on any tier**; free tier lists metadata, summary and snippet | yes |
| archive stability | documentation silent | documentation silent | documentation silent |
| price | $90/mo for 5,000 tokens; archive search 5 tokens per searched year; 100 articles per search; overage $0.015/token | custom quote for the 2019 archive | ~$70/mo at 15,000 results; archive quoted separately |

Backfill cost from the published NewsAPI.ai pricing at our measured volume (§1.1: 287–678 distinct
items/day), one year of history: **$82–$192**. At 10,000 articles/day the same year is $2,738. The
difference is not in the price list; it is in how broadly we query — and the query must **not** be
narrowed by a bracket condition, for the same reason a market-cap constraint never goes into a
prompt. Query broadly, filter locally.

### 6b. Common Crawl's CC-NEWS, probed and rejected

Measured 2026-11-11 against our own archive rather than against its specification.

| measure | value |
|---|---|
| our 2026-07-01 items found in CC-NEWS | 7 of 287 = **2.4%** |
| plausible ceiling for the full day | ~19 of 287 = 6.6% |
| our 17 hosts that CC-NEWS crawls at all | **2** |
| tier-1 US financial outlets in 72,000 records across 12 crawl windows | **0** of 16 |
| volume, 2026-01..10 as published | **4.19 TB** |

All 7 hits are GlobeNewswire releases the pipeline already receives from EDGAR and Polygon. The
cause is structural rather than qualitative: our own corpus is 36% `sec.gov`, 7% wire services, and
the rest largely `fool.com`, `wired.com`, `techcrunch.com`, `investing.com`, `ft.com`,
`marketwatch.com` and `seekingalpha.com` — publishers CC-NEWS does not crawl. Three earlier
objections of mine were wrong and are recorded as such: the 02:24 UTC sampling hour was **not** the
cause (the same hosts are absent at 12:17, 17:59 and 22:02), publication dates **are** recoverable
for 79.6% of records, and streaming needs no disk (6.5 MB peak while reading 5.57 GB).

What it alone offers, verified: free and anonymous access, raw HTML rather than vendor-extracted
text, and a structurally point-in-time archive — the crawl window is in the filename and the twelve
files of a day tile it contiguously, so a historical read cannot leak later content. What kills it
is that **there is no index**: the CDX server lists 129 collections and none is CC-NEWS, so a URL
cannot be looked up and a range request cannot be aimed, and every byte must be read.

**An index timestamp is not an immutable archive.** A filter on first-seen time cannot reconstruct
an earlier article *body* if only the latest body survives upstream. This is why the archive is a
snapshot we hold, not a query we re-run, and why R4 includes a result hash.

---

## 7. What is never rebuilt

The broker rail. Two daemons run continuously, one of them placing real-money orders; the arming
door takes hand-written documents carrying `"source": "manual"`, and no automatic brief-to-intent
producer exists — it was removed on 2026-09-24.

R12 makes this structural rather than a policy: successor services hold no credentials for the
queue, know no path to it, and an AST gate forbids order-placing calls in their roots. Circumventing
it would require changes in two repositories plus removing a gate — a deliberate act, not an
accident.

The same rule in the other direction: no backward edge from `ledger`. A service that sees outcomes
must not influence the service that produces proposals, or selection happens under the result.

---

## 8. Open decisions — the owner's, not registered here

| # | Decision | What hangs on it |
|---|---|---|
| D-1 | **Which news vendor**, and whether one or two (live vs archive). | §6. Blocked on the probe below. |
| D-2 | ~~**Storage licence.**~~ **CLOSED 2026-11-11 by the owner:** not treated as a constraint for a private, single-user research archive. The archive holds full article text. Recorded because the alternative it displaced — storing hashes, spans and quotations instead — would have made re-matching after a rule change impossible, so the decision bought that capability. | — |
| D-3 | **The parity-gate date.** | Named in §5.1 as the cure for "parallel forever" and deliberately left unset here, which is the same defect one level up. |
| D-4 | ~~**Repo topology.**~~ **CLOSED 2026-11-11 by the owner: three repositories.** This repository keeps the front-end, the thin API, the five data-collection units and — as an **archive** — the dead lab and its memos. A second holds the broker rail and the backtester, never rewritten. The successor is a third, polyglot, but its Python is a **fresh** statistics tier of roughly one to two thousand lines rather than the 9,602 lines of `diagnostics` / `eval` / `preaudit` / `retrospective_audit` / `gates`, which served closed paradigms and stay behind. | Buys the one gate that today's worst error needed: the panel artefact and the power routine live in one repository, so a test can require that the panel declares its number of exchangeable units and that the power routine refuses to run without it. Across a repository boundary that gate cannot be written, and the 88.2%-versus-27.4% error of §1.8 is precisely what it catches. Inherits one known hazard: a polyglot CI needs path filters, and in this project the `.github/` filters must be mirrored into the post-deploy check under a different pathspec syntax — a coupling that has already caused a failure once, so it belongs in the gate list rather than being discovered later. |
| D-5 | **Brief handover criterion.** "Then the old pipeline can stop" is not testable as written. | Without a number, the transition has no end. |
| D-6 | **Does the pipeline ever need to propose on the session it observes?** | If yes, `vault` needs a run before 16:00 ET and the partial-day problem of §4 rule 4 becomes load-bearing. If no, today's cadence suffices and §4 buys only honesty, not tradability. |
| D-7 | ~~**A published item stream?**~~ **CLOSED 2026-11-11 by the owner: build the WebSocket, keep it private for now.** It is a **derived surface** on `vault`, never a window onto the outbox: it publishes versioned *items*, not internal events, so a change to `newsvault`'s row shape cannot break a consumer. Each message carries both timestamps and a monotonic sequence number, so a consumer decides its own point-in-time cut and can order by our sequence instead of trusting clocks. A heartbeat is mandatory, because silence must be distinguishable from a dead stream — 2026-07-11 held 59 items against a median of 181 and nothing said which it was. No delivery guarantee and no latency promise: the archive remains the source of truth (§4 rule 2) and reconciliation is an HTTP read, while a latency promise would need N2, which has no number. | **"Private" is a property of who holds credentials, not of whether a contract exists.** The payload shape and the heartbeat go in from the start even while nobody else connects, because retrofitting them after a consumer depends on the stream is a breaking change in someone else's system. The tripwire: the schema freezes at the **first external connection**, not at a later decision. Note also that this is the **third** consumer of one store — briefs want 200 items, the study wants completeness, a stream wants latency — and §1.1 is the record of what happened the last time one store served two consumers with different needs. |
| D-8 | **Does salience count occurrences or documents?** Today `n(c, x)` counts **occurrences** of a core, so one 650,000-character EDGAR exhibit naming Honeywell forty times counts the same as forty separate articles about Honeywell. The candidate rule counts **items** mentioning the core instead: "how many news items named this counterparty today", not "how many times was it written". | Unmeasured, and it is the last hypothesis that could reverse §1.9's power collapse, because long-document inflation is the plausible source of the earnings-season concentration. It is a `matcher` rule change, so R18 governs it: the two panels must be produced and diffed rather than swapped. Not a free choice either way — occurrence counting is defensible and document counting is defensible, so whichever is chosen must be chosen before its power is known. |

Open decisions N1–N5 of §2.2 are the owner's too, but they are numbers to set rather than branches
to pick.

---

## 9. Gates

- **Gate 3, data-vendor PIT validation** (`methodology-gates.md`): triggers when a vendor enters a
  pre-registration. This memo registers no study, so the gate is not yet live. The probe in §10 is
  a superset on volume, stability, body text and index lag, and it does **not** replace the
  five-anchor two-source triangulation the gate requires. That must run before any vendor's data
  reaches a pre-registration.
- **Gate 4, Little's Law**: applies to position weighting for an event-driven daily-rebalance
  strategy. This pipeline is event-driven but has no weighting and no rebalance, so it does not
  apply. Recorded so a later reader does not read the omission as an oversight.
- **Adversarial review before compute**: owed before the backfill spend, not before this draft.

---

## 10. Build order

| # | Deliverable | Unblocks | State |
|---|---|---|---|
| 1 | Point-in-time core and lake reader | Complete days instead of truncated ones | done (PR #1881) |
| 2 | **Vendor probe on three days, two vendors** | Volume and therefore cost of a year of history ($82 vs $2,738); archive stability under a repeated query; whether a body is returned; the distribution of publication-to-index lag, which is the input to R11 | one day of work; most urgent |
| 3 | Frozen outputs of the current system over several dozen days | The parity gate begins to exist. Without it, steps 6 and 7 are a bet | cheapest, most important |
| 4 | Event-driven ingest with outbox, coverage ledger and hourly sweep | First sighting drops from 4.5 h after the close to about a minute after publication | after step 2 |
| 4b | Private item stream over WebSocket (D-7), on top of step 4's outbox | A second consumer, which is the only real test of whether the item contract is a contract | after step 4; schema and heartbeat designed in from the start |
| 5 | Backfill to 246 sessions, if step 2 came out cheap | Removes six months of waiting | depends on step 2 |
| 6 | `derive` reproducing the frozen outputs to the number | Evidence the successor can do what the current system does | designed |
| 7 | `modelgate`, then `propose` taking over brief production | Only now may the old pipeline stop | requires step 6 |

---

## 11. What this memo does not establish

- **No service contracts.** API surfaces, error codes and the outbox event payload are unspecified.
  "High-level architecture" one level down is missing.
- **No numbers for N1–N5**, including the backup for an archive the design calls irreplaceable. N5 has one rough datum: a full panel rebuild over 87 lake partitions completed in minutes rather than hours, timed by wall clock only, so the order is measured and the number is not. N4 (retention) is now partly answered by D-2: the archive holds full text, so the question is how long and on what medium, not whether.
- **No architecture gates.** 22 tests currently enforce repo-wide rules (layer imports, per-vendor
  clients, language) from one place; a successor repo starts with none, and a gate nobody replaced
  is a rule that quietly stopped applying. They must be written at the start, not later.
- **Vendor facts are vendor claims.** Pricing, archive depth and token costs in §6 were read off
  the vendors' own pages on 2026-11-11 and are not our measurement. The backfill cost is computed
  from those claims and our measured volume; our actual query volume is unknown until step 2.
- **The characterisation approach recovers some, not all,** of the 77,540 lines of behavioural
  knowledge in the `thematic` test corpus.
