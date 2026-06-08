# Entity-type-aware fundamental scoring — design memo

**Status: DRAFT**
**Date: 2026-06-08**
**Branch: `feature/entity-type-aware-scoring`**
**Scope: score financial-structure entities (BDCs, REITs, banks, insurers) on the metrics + peer cohorts their structure actually uses, instead of the industrial-only model that silently nulls them today.**

> **This is a DRAFT for the record, not a locked plan.** It exists so a future
> session has a concrete starting point. No code is committed against it. Before
> any implementation phase it needs the standard pre-compute adversarial review
> (zen + Perplexity on the locked memo) per the project's `>1h compute` rule.

---

## 1. Problem & evidence

The thematic tool surfaces LLM-mapped **beneficiary** tickers, which can be *any*
listed name — not just the curated SP1500 universe the fundamentals model was
built for. When a beneficiary is a **financial-structure entity**, the industrial
fundamental model produces a half-empty card.

**Live case (brief `2026-06-07`, candidate HTGC = Hercules Capital, a BDC):**

| field | value | why |
|---|---|---|
| `valuation_pe` | 8.6 | needs net income + price → present |
| `roe_pct` | 14.9% | needs net income + equity → present |
| `valuation_ps` | — | needs `revenue_ttm` → **None** |
| `valuation_ev_rev` | — | needs `revenue_ttm` → **None** |
| `valuation_ev_ebitda` | — | needs `ebitda_ttm` → **None** |
| `valuation_fcf_margin` | — | revenue is the denominator → **None** |
| `fcff_yield_pct` | — | BDC OCF is structurally negative (invests in portfolio) |
| `roic_pct` | — | needs `invested_capital` (PP&E) → **None** |
| `magic_formula_health_pass` | False | needs EV/EBITDA + ROIC |
| `insider_score_sector_percentile` | — | cohort is "thin" (see below) |

**Root cause (single):** the EDGAR companyfacts extractor reads industrial /
operating-company US-GAAP XBRL concept chains. BDCs file under the
investment-company taxonomy: top line is `InvestmentIncomeOperating` /
`InterestAndDividendIncomeOperating`, **not** `Revenues`; no `OperatingIncomeLoss`
→ no EBITDA; no PP&E → no invested capital. Every revenue/EV/EBITDA/ROIC-derived
metric resolves to None. Only net-income- and equity-based metrics (PE, ROE)
survive, because every filer reports `NetIncomeLoss` + `StockholdersEquity`.

The `REVENUE` chain that misses BDCs
(`alphalens_pipeline/data/fundamentals/concept_chains.py`):

```python
REVENUE = (
    "RevenueFromContractWithCustomerExcludingAssessedTax",  # ASC 606
    "Revenues",                                             # large-cap
    "SalesRevenueNet", "SalesRevenueGoodsNet",              # industrials
)
```

**The same root cause also produces the empty sector-percentiles** (a separate
symptom): HTGC is absent from the shipped `sic_index.parquet` (built only from
SP1500 PIT + delisted), so `get_sic("HTGC")` → None → cohort "thin" → all three
percentiles nulled (`scorer._build_candidate_row`, the issue #197 thin-cohort
contract). Two symptoms, one root: **the industrial model does not fit a BDC.**

This is **not a data-completeness bug** — HTGC's companyfacts are fetched
correctly (net income $332M, equity $2.23B, OCF −$434M all present). It is a
**taxonomy + cohort coverage gap**.

**Scale today:** rare — 3/190 candidate-rows (1.6%) over the last 14 briefs, all
HTGC. This memo is therefore a *future-readiness* track, not an urgent fix. The
honest "—" + thin badge is acceptable interim behavior. It becomes worth building
when BDCs/REITs/banks/insurers start entering briefs regularly.

### Non-goals

- Re-scoring completed factor paradigms (verdicts stand).
- A "scoreability gate" that drops financial names from briefs — rejected: a BDC
  beneficiary is a legitimate surface; dropping it hides a real name (money is
  entrusted to these briefs). We *augment* scoring, we do not *suppress* names.
- Forcing financials onto industrial multiples (economically misleading — see
  the Perplexity adversarial note in §7).

---

## 2. Design overview — 4 layers

```
candidate ticker
   │
  [L1] entity_type resolver  →  {ORDINARY, BDC, REIT_EQUITY, REIT_MORTGAGE, BANK, INSURER}
   │
  [L2] per-type feature extraction  (entity-specific XBRL concept chains)
   │
  [L3] per-type peer cohort  (financial-aware universe, not SIC-on-SP1500)
   │
  [L4] per-type metric set → within-cohort percentile → common 0-100 axis
```

ORDINARY keeps the entire current path unchanged. The new branches activate only
when L1 classifies a special structure.

---

## 3. L1 — entity-type detection

Confidence hierarchy (regulatory election **>** XBRL signature **>** SIC), because
SIC classifies *business activity*, not the *regulatory election* that dictates
reporting.

| Type | Strongest marker | SIC backup | XBRL signature (fallback / corroboration) |
|---|---|---|---|
| **BDC** | **CIK prefix `814-`** (definitive; HTGC = `814-00702`) + Form N-54A election | none (BDCs have no dedicated SIC) | `InvestmentIncomeOperating` present **∧** `Revenues` absent |
| **REIT (equity)** | REIT election in 10-K | **6798** (679801) | `FundsFromOperations` / real-estate D&A; `RentalIncome` dominant |
| **REIT (mortgage)** | REIT election in 10-K | 6798 (67989901) | `InterestIncome` dominant (vs `RentalIncome`) |
| **Bank** | bank-holding-company designation | **6021 / 6022** | `NetInterestIncome`, tangible-equity tags |
| **Insurer** | — | 6311 (life) / 6331 (P&C) | `IncurredLosses`, `EarnedPremium` |

**Cheapest win for us: the `814-` CIK prefix.** We already carry issuer CIKs
everywhere (form4 parquet, companyfacts cache, `edgar-detect`). A BDC is a CIK
lookup away — no extra fetch, no guessing. HTGC classifies BDC immediately.

**Resolver contract (new module, e.g. `data/fundamentals/entity_type.py`):**

```python
def resolve_entity_type(*, ticker: str, cik: str, asof: date) -> EntityType:
    # 1. CIK prefix 814- → BDC  (definitive)
    # 2. SIC → REIT/BANK/INSURER bucket
    # 3. XBRL signature fallback (presence/absence of fingerprint concepts)
    # 4. default ORDINARY
```

Cache per (cik, asof) like the feature fetcher. Keep it pure + table-driven so
the SIC→type and prefix→type maps are unit-test fixtures. Note the **transition**
case (a fund electing BDC mid-year): election filing should win over a stale SIC.

---

## 4. L2 — per-type feature extraction (XBRL concept chains)

Extend `concept_chains.py` with per-type chains; `ev_fcff_features_as_of` branches
on `entity_type` and populates the type-appropriate fields instead of a blank
`revenue_ttm`.

### BDC (Phase 1 — the live HTGC case)
- **NAV** = `(TotalAssets − Liabilities − PreferredStockValue) / shares`
- **P/NAV** = `market_cap / NAV` (the cornerstone BDC multiple; premium/discount to NAV)
- **NII** (net investment income) = `InvestmentIncomeOperating − operating/interest expense`
- **NII yield** = `NII / market_cap`
- **asset-coverage ratio** = `TotalAssets / (Liabilities − PreferredStock)` (regulatory floor 200% / 150%)
- non-accrual rate, dividend coverage (cash vs total) — secondary quality signals

### REIT (Phase 2)
- **FFO** (NAREIT) = `NetIncomeLoss − GainLossOnSaleOfRealEstate + RealEstateDepreciationAmortization`
- **P/FFO**, **P/AFFO** (AFFO = FFO − recurring capex − straight-line-rent adjustment)
- **EBITDAre** = `NetIncomeLoss + InterestAndDebtExpense + IncomeTaxExpenseBenefit + RE_DepreciationAmortization`
- net debt / EBITDAre; NAV premium/discount

### Bank (Phase 3)
- **P/TBV**, TBV = `TotalAssets − Liabilities − GoodwillAndIntangibleAssets`
- **ROTCE** = `NICommon / avg(TangibleCommonEquity)`
- NIM, efficiency ratio, P/E

### Insurer (Phase 3)
- **combined ratio** = `(IncurredLosses + UnderwritingExpenses) / EarnedPremium`
- P/BV, ROE

The feature dict gains type-tagged fields (`nav_per_share`, `nii_ttm`,
`ffo_ttm`, `tbv`, …). The existing industrial fields stay None for these types —
the UI renders the type-appropriate block, not the industrial one.

---

## 5. L3 — per-type peer cohorts

SIC-on-SP1500 fallback is useless here. Build financial-aware cohorts:

- **BDC** = its own cohort (split internally- vs externally-managed — different
  fee structures + insider-alignment, see §7).
- **REIT** = split equity vs mortgage **first**, then property sub-sector
  (residential / retail / industrial / office / data-center / healthcare).
- **Bank** = by asset size (the $10B community-bank threshold) + business model.
- **Insurer** = by line of business (P&C / life / health / reinsurance).

**Cohort-size rule:** ≥15-20 peers for a credible percentile (optimum 25-30).
Fallback chain: expand within sub-sector → adjacent sub-sector → broad type →
**thin** (keep today's badge — the honest no-percentile state). This reuses the
existing thin-cohort UX contract; only the cohort *source* changes per type.

Cohort universe source is an open question (§8) — a curated financial-entity
list (the BDC universe is ~50 names; equity REITs a few hundred) maintained like
`sic_index.parquet`, refreshed monthly.

---

## 6. L4 — cross-type percentile integration

**Key property that makes this clean:** our `composite_sector_percentile` is
*already* a within-cohort percentile on a 0-100 axis. So:

- each type computes its own **cheapness composite** on its own metrics
  (BDC: P/NAV + NII-yield; REIT: P/FFO + P/AFFO; industrial: EV/EBITDA + FCFF-yield),
- the output is a 0-100 percentile that is **comparable across types by
  construction** — a BDC at the 80th percentile of BDC cheapness sits on the same
  axis as an industrial at the 80th percentile of industrial cheapness.

We keep the output contract (one 0-100 number per signal); we change only *what
feeds* the percentile per type. An optional refinement (per Perplexity) is a
percentile→z-score step within each metric category to equalize distribution
shape across types — treat as a later nicety, not a Phase-1 requirement.

Insider-buying signal: stays type-agnostic in mechanics but see §7 for the
externally-managed-BDC interpretation caveat.

---

## 7. Pitfalls (must design around these)

- **BDC NAV is a quarterly fair-value mark** — it lags and is itself an estimate,
  not a market price. P/NAV inherits that staleness; flag the as-of date.
- **REIT FFO has company-specific adjustments** — raw NAREIT FFO ≠ the figure a
  REIT reports. Decide whether to compute NAREIT-canonical FFO from XBRL (stable,
  comparable) or trust the reported figure (matches the company, not comparable).
  Lean canonical-from-XBRL for cohort comparability.
- **Mortgage vs equity REITs need different metrics** — never mix them in one
  cohort; the equity-vs-mortgage split in L1/L3 is load-bearing.
- **Externally-managed BDC insider buying means something different** — the buyer
  is often the external manager, not a company insider. Our opportunistic-Form-4
  signal may misread it. Consider down-weighting or annotating insider signal for
  externally-managed BDCs.
- **EDGAR tag availability/lag** for these concepts is patchier than for
  industrial revenue — the extractor must degrade gracefully (per-field None,
  never crash the row), exactly as the industrial path already does.

---

## 8. Phased rollout

| Phase | Scope | Why this order |
|---|---|---|
| **0** | `entity_type` resolver (CIK `814-` + SIC + XBRL signature) + UI tag | cheapest; alone lets a card say "BDC — industrial multiples N/A" instead of bare "—" |
| **1** | **BDC-only** scoring: BDC concept chains + `bdc_metrics.py` (P/NAV, NII-yield) + BDC cohort | HTGC is the live, recurring case; `814-` detection is certain; only ~4 XBRL tags |
| **2** | REIT (equity vs mortgage, FFO/AFFO, property sub-sector cohorts) | next most common beneficiary structure |
| **3** | bank + insurer | rare in briefs today (0 in last 14) |

Each phase = own PR, TDD red→green, zen `deepseek/deepseek-v4-pro` review, its
own short follow-up memo. **Recommendation: build Phase 0 + Phase 1 (BDC) first;
defer 2-3 until those types actually appear in briefs.**

---

## 9. Open questions

1. **Cohort universe source** — curated financial-entity lists (BDC ~50, equity
   REIT few-hundred) maintained alongside `sic_index.parquet`, or pull dynamically
   from EDGAR by SIC/CIK-prefix at build time? (Leaning: a `build_financial_index.py`
   sibling, monthly refresh, same pattern as the SIC index.)
2. **NAREIT-canonical FFO vs reported FFO** — comparability vs fidelity (§7).
3. **Insider signal for externally-managed BDCs** — down-weight, annotate, or
   leave as-is with a UI caveat?
4. **UI** — does the card render a *different* fundamentals block per type, or the
   same grid with type-appropriate rows swapped in? (Affects `apps/web` +
   the brief generator's fact rendering.)
5. **Does Track G (compound-catalyst, subject-anchored) need the same
   entity-awareness**, or is this isolated to Layer-4 fundamentals?

---

## 10. Code touch-points (for the implementer)

- `alphalens_pipeline/data/fundamentals/concept_chains.py` — add per-type chains
- `alphalens_pipeline/data/fundamentals/entity_type.py` — **new** resolver
- `alphalens_pipeline/data/store/edgar_fundamentals.py` —
  `ev_fcff_features_as_of` branches on entity_type
- `alphalens_pipeline/thematic/screening/valuation_signal.py` (+ new
  `bdc_metrics.py` / `reit_metrics.py` / …) — per-type metric computation
- `alphalens_pipeline/thematic/screening/sector_peers.py` /
  `sic_index.py` — financial-aware cohort source
- `alphalens_pipeline/thematic/screening/scorer.py` — dispatch on entity_type;
  keep the `composite_sector_percentile` output contract
- `apps/web` brief card + brief generator fact rendering — per-type block

---

## 11. References

- Diagnosis session 2026-06-08 (this repo) — HTGC empty-fundamentals + thin-cohort
  trace; `concept_chains.REVENUE` gap; `sic_index.parquet` SP1500-only coverage.
- Perplexity `sonar deep research` (2026-06-08, high effort) — entity detection
  (CIK `814-`, SIC 6798/6021/6022, N-54A, XBRL fingerprints), per-type metrics +
  US-GAAP XBRL tags, cohort construction (15-20 peer floor), cross-type
  percentile→z-score normalization, pitfalls. Full transcript archived in session.
- Perplexity `sonar-reasoning-pro` (2026-06-08) — adversarial review rejecting the
  blunt "broaden SIC index to all EDGAR" option: cohort contamination by
  micro-caps/shells/SPACs; BDCs/REITs need specialized metrics (NII/NAV,
  FFO/AFFO), not generic corporate multiples. This memo's per-type approach is the
  curated answer that review pointed to.
- NAREIT FFO/AFFO + EBITDAre definitions; SEC BDC data sets (XBRL investment
  schedules); Investment Company Act §55-65 (BDC election, asset-coverage).
- Related project context: issue #197 (thin-cohort UX contract), the SP1500-PIT
  universe doctrine (`CLAUDE.md` "True PIT universe"), ADR 0007 (layer
  architecture — this is a Layer-4 scoring extension).
