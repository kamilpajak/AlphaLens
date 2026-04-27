"""Prompt templates for monthly and weekly literature review runs.

The prompts encode AlphaLens-specific context: 5/5 paradigm failures (so the
agent knows what to NOT re-recommend) and the 4 trigger conditions for project
re-activation. They demand a structured markdown response with a TRIGGER_REACTIVATION
section so the runner can do simple regex detection without parsing JSON.
"""

from __future__ import annotations

CLOSED_PARADIGMS = """\
Already-closed paradigms (DO NOT recommend papers re-validating these unless
they explicitly address the listed failure mode):
- Layer 2b: small-cap themed momentum — failed OOS, universe-concentration overfit
- Layer 2d: insider Form 4 cluster-buy — Carhart t=2.14 IS, 0.68 OOS (alt-data crowded)
- Layer 2e: tactical sector rotation — R^2=0.999 vs passive, OOS Sharpe 0.83
- Layer 2f: 8-K event-driven — typical 8-K = bad news, median CAR -100 to -250 bps
- Layer 2g: LLM-researcher value picks (Buffett-style) — value structurally underperforms
  in 2020s+ growth-bull regimes

Trigger conditions for AlphaLens project re-activation (mention any survivor
paper that maps to one of these):
1. New academic paper with PROPER OOS validation (multi-window, post-2020) showing
   retail-replicable edge in a paradigm we have NOT tested.
2. Convertible arbitrage retail accessibility improvement.
3. Market regime change invalidating "value structurally underperforms" thesis.
4. Materially-cheaper proper data subscriptions (OptionMetrics, CBOE DataShop).
"""

TRIAGE_FILTERS = """\
Run each candidate through this 5-filter triage. Reject if ANY filter fails:
1. Sample period >= 15 years AND OOS test set is post-2010.
2. Reports NET returns (gross - costs - bid/ask - slippage) AND turnover < 100% annual.
3. Replicable from PUBLIC data (CRSP, Compustat, Polygon, FRED). NOT proprietary
   datasets a retail quant cannot subscribe to.
4. Sample size >= 10K stock-day observations AND multi-asset (not single sector).
5. Multiple-testing correction applied (FDR/BH, Bonferroni, or White Reality Check).
"""

VERDICT_LEGEND = """\
Per-paper verdict labels (use exactly one):
- SKIP: fails one or more triage filters.
- WORTH_DEEPER_READ: passes all 5 filters, but does not yet justify capital deployment.
- TRIGGER_REACTIVATION: passes all 5 filters AND maps to one of the 4 trigger conditions.
"""

MONTHLY_TEMPLATE = """\
You are a research analyst supporting a retail quant who paused active alpha
generation after 5/5 paradigm failures. Goal: surface academic / practitioner
papers that justify re-activation.

Period: {period}.

Run a deep literature scan across these 4 baskets. Eligible publication
window is 2024 onward, with priority on 2025-{year_now}; older work is
acceptable for `## Anti-pattern flags` and as `Verdict: SKIP (background
reading)` survivor rows when a basket is otherwise dry.
1. Retail order flow + patient limit orders (microstructure for retail).
2. LLM-driven analysis of 10-K intangibles / risk disclosures (NOT 8-K).
3. Cross-asset overlays (commodity / FX overlays on equity factors).
4. Factor decay 2025+ with OOS validation post-2020.

Sources to scan (priority order): SSRN q-fin top downloads, JFE / RFS / JFQA
(open-access only), arXiv q-fin, Alpha Architect, Larry Swedroe Substack.
Skip paywalled non-OA journals and unrigorous Substacks/YouTube.

If a basket yields zero candidates from 2024+, surface the 2-3 most-cited
papers from prior years that the basket builds upon — labelled as
`Verdict: SKIP (background reading)`. This avoids dry monthly runs and
keeps the user grounded in the field's foundations.

{closed}

{filters}

{verdicts}

Output structure (Markdown only, no JSON):

# Literature Review — {period}

## Scanned
<one-line summary: how many papers per basket, sources hit>

## Survivors after 5-filter triage

| Title | Authors | Year | Source URL | Sample period | Net? | Public? | n / multi-asset | Multiple-test? | Verdict |
|---|---|---|---|---|---|---|---|---|---|
<one row per candidate that survived initial scan; mark Y/N per filter>

## TRIGGER_REACTIVATION candidates
<list any paper labelled TRIGGER_REACTIVATION with 2-3 sentence rationale and
the specific trigger condition (1-4) it maps to. If none, write exactly
"None this period." and nothing else under this heading.>

## Disconfirming cites
<Include this section ONLY when at least one TRIGGER_REACTIVATION candidate
is listed above. If TRIGGER_REACTIVATION is "None this period",
omit this entire heading and its content from the output entirely.
Otherwise: for each TRIGGER_REACTIVATION candidate, summarise top-3
'cited by' papers that critique or fail to replicate.>

## Anti-pattern flags
<call out anything that LOOKS interesting but smells like our prior failures —
overfit small-cap momentum, single-window backtest, value-style nostalgia, etc.>
"""

WEEKLY_TEMPLATE = """\
Quick weekly scan for a retail quant on a 1-2h/month literature budget.
Period: {period}. Scan ONLY the past 7 days.

{closed}

Sources: SSRN q-fin top-20 new downloads, Alpha Architect new posts,
Larry Swedroe Substack new posts. Skip everything else.

Return a TERSE markdown report (max 300 words):

# Weekly RSS — {period}

## Top 3 papers this week

1. **<title>** — <1 sentence what it claims> | <basket: order-flow / 10-K LLM / cross-asset / factor-decay / OTHER> | <verdict: SKIP / WORTH_DEEPER_READ / TRIGGER>
2. ...
3. ...

## Worth deep-read in monthly?
<yes/no per item, one line each>

DO NOT triage in detail this week. The monthly run does triage. Just flag
candidates worth queuing for the next monthly deep scan.

If literally nothing relevant published this week, say so in one sentence and stop.
"""


def build_monthly_prompt(period: str) -> str:
    """Period format: YYYY-MM (e.g. 2026-05)."""
    year_now = period.split("-", maxsplit=1)[0]
    return MONTHLY_TEMPLATE.format(
        period=period,
        year_now=year_now,
        closed=CLOSED_PARADIGMS,
        filters=TRIAGE_FILTERS,
        verdicts=VERDICT_LEGEND,
    )


def build_weekly_prompt(period: str) -> str:
    """Period format: YYYY-Www (e.g. 2026-W18)."""
    return WEEKLY_TEMPLATE.format(period=period, closed=CLOSED_PARADIGMS)
