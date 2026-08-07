# Analysis contract — the one page written before a comparison is run

Status: LOCKED 2026-08-07

## Why this exists

Between 2026-08-05 and 2026-08-07 the same mistake happened three times in a row:
a result was announced, someone challenged it, a cheap control was run, and the
result was withdrawn. Each control was obvious in hindsight and cost about as
much as the original run.

| # | Claim announced | What the control showed |
|---|---|---|
| 1 | "The beta=1 confound is closed as immaterial" — group means moved only 0.1-0.6 pp | The difference is `car_mm - car = (1 - beta) * spy_bhar`, a product of two terms. Median per-event effect was 0.96%, p90 3.5%. It cancels in the group mean only because the market was flat and betas sit on both sides of 1. |
| 2 | "The selected themes are syndication artefacts" — 5 articles, 1 distinct title | The daily news cache is capped at 200 rows/day while events run 200-376, so 38% of rows failed the join, and `nunique()` skips nulls. Against the full store: 6 articles / 6 distinct titles. No syndication. |
| 3 | "The value-chain prompt takes a barren theme-day from 0 to 9 in-bracket names" | The zero was the *selection criterion*, not a result. Re-running the unchanged prompt on the same days produced 4 hits. Paired outcome: 4 chain wins, 3 plain wins, 5 ties. |

The common shape is not a statistics error. It is **announcing before the control**,
and specifically **comparing one run against one run on cases chosen by their
outcome**.

This document is the fix: a one-page contract, written and committed *before* the
comparison runs. It is deliberately not a clinical-grade pre-registration. It is
sized for a solo pipeline running dozens of cheap LLM experiments.

## When it applies

Write a contract before any run whose output could change what ships, what is
believed, or what is written down as a finding.

It does **not** apply to exploration. Looking at cases, reading outputs, forming
hunches and poking at data are free and unconstrained. The contract marks the
boundary where exploration stops and a claim begins.

The two modes must be named out loud, because the failure above is exactly the
moment one silently becomes the other:

- **Stress test / case series** — outcome-selected cases, no contract needed, and
  **no claim may leave it**. Its only job is to generate a hypothesis.
- **Confirmation** — a fresh set chosen without reference to any arm's output,
  under a contract written first.

## The contract

Copy this block into `docs/research/<experiment>_contract_<DATE>.md`, fill it in,
and commit it before the confirmatory run starts. The commit timestamp is the
whole point — it is what makes the plan checkable after the fact.

```markdown
# <experiment name> — analysis contract
Written: <YYYY-MM-DD HH:MM TZ>, before any confirmatory run.

1.  QUESTION        What exact behaviour is being tested, in one sentence.
2.  UNIT            Theme-day / proposal / run / catalyst. One of them, named.
3.  SAMPLING        How cases are chosen. Then answer explicitly:
                    "Was any case selected because of an arm's output?" yes/no.
                    If yes, this is a stress test and cannot support a claim.
4.  ARMS            Verbatim text of every arm, and the single thing that differs.
5.  PRIMARY         One estimand. Not a list.
6.  SECONDARY       Everything else measured, listed now so it cannot be promoted
                    to primary after the fact.
7.  TEST            The test, chosen before the numbers exist.
8.  THRESHOLD       The difference that would matter operationally. A number.
9.  POWER           N, replicates per cell, and the smallest effect this design
                    can actually detect. See "Power before N" below.
10. FAILURE MODES   The distinct ways this can fail, listed separately:
                    refusal / zero output / all out of range / irrelevant /
                    duplicate. Do not collapse them into one denominator.
11. CONTROLS        Randomised arm order, fixed model + temperature + token
                    budget, blinded scoring where a judgement is involved.
12. STOPPING        No stopping because an early result looks interesting.
13. VERDICTS        What result earns "established", "suggestive",
                    "inconclusive", "no practically important effect".
14. ARCHIVE         Where prompts, raw outputs, model version and parsing code
                    are kept.
```

## Power before N, not after p

The step-3 control ran 12 paired theme-days and produced 7 discordant pairs. With
7 discordant pairs an exact two-sided sign test can only reject at 7-0, which has
probability `2 / 2^7 = 0.0156`; even 6-1 gives `p = 0.125`. So the **highest power
that test could reach was 1.56%**, no matter how large the true effect. Reporting
`p = 1.00` from it as "no effect" is not a weaker version of the original error —
it is the same error pointing the other way.

Only discordant pairs carry information in a paired binary test, so N must be
planned from the expected discordance, not the total. As a planning rule for a
two-sided test at 80% power:

```
N_pairs  ~=  4.57 / delta^2      delta = difference in per-unit hit probability
```

| delta | paired units needed |
|---:|---:|
| 0.10 | 457 |
| 0.20 | 114 |
| 0.30 | 51 |
| 0.40 | 29 |

Read the table before choosing the endpoint, not after. If the feasible N cannot
reach the threshold that matters, **the endpoint is wrong** — change what is
measured rather than run a study that cannot answer.

Where an endpoint is too weak, look for one whose power comes from *replicates*
instead of units. A refusal rate is the worked example: the same case can be run
20 times, so a dozen cases suffice, while a per-case binary outcome would need
hundreds.

## Denominators are part of the treatment

`hits / proposals` looks like a rate but is not a clean one when the arm changes
the proposal count — a refusing arm has a denominator of zero. Model it in two
parts instead: first the probability of refusal, then, conditional on not
refusing, the count.

Mixed models are the natural home for this and are also the easiest thing to
over-fit here. Conventional guidance puts reliable GLMM inference near 30-40
clusters; below that, prefer exact or permutation tests and treat any count model
as exploratory.

## The one rule that would have caught all three

> Never choose the cases after seeing which arm produced zero, a refusal, or an
> unusually high count.

If such a set is diagnostically useful, label it a stress test and run a fresh
confirmation set under a contract before any claim is made.

## Related

- `docs/research/paradigm_failures_postmortem.md` — the ledger discipline this
  mirrors on the strategy side.
- Repository convention: adversarial review before any run over 1h of compute.
  This contract is the cheap counterpart for runs that finish in minutes, where
  the cost of being wrong is a wrong belief rather than wasted compute.
