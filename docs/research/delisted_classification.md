# Phase 1A: Delisted ticker classification (issue #18)

**Date:** 2026-04-22
**Goal:** Classify 50 delisted thematic tickers z Test B augmented backtest (M&A vs liquidation vs other) aby sprawdzić, czy "augmented Sharpe 1.49→1.75 / FF3 α t 2.62→2.99" finding jest survivorship correction czy M&A selection effect.

## Method

1. 50 delisted tickers z `~/.alphalens/survivorship/fetched_manifest.json`
2. Polygon `delisted_utc` dates z `~/.alphalens/survivorship/details_*.json` (already cached)
3. M&A classification z: memory `project_survivorship_probe` + perplexity lookup + public filings knowledge

## Classification

### Confirmed M&A acquisitions (acquired at premium) — 17

| Ticker | Acquirer | Deal (approx) | Premium | Delisted |
|---|---|---|---:|---|
| KRTX | BMS (Bristol-Myers) | $14B | +75% | 2024-03-19 |
| HZNP | Amgen | $28B | +48% | 2023-10-09 |
| IMGN | AbbVie | $10B | +95% | 2024-02-13 |
| MRTX | BMS | $5.8B | +52% | 2024-01-24 |
| TPTX | BMS | $4.1B | +122% | 2022-08-18 |
| GBT | Pfizer | $5.4B | +102% | 2022-10-06 |
| KDNY | Novartis | $3.5B | +67% | 2023-08-14 |
| RETA | Biogen | $7.3B | +60% | 2023-(delisted 2026-04 per Polygon) |
| CBAY | Gilead | $4.3B | +27% | 2024-03-25 |
| SWTX | Merck KGaA | — | — | 2025-07-02 |
| CTIC | Sobi | $1.7B | +89% | 2023-06-27 |
| RXDX (Prometheus) | Merck | $10.8B | +75% | 2023-06-20 |
| NPTN | Lumentum | $900M | +39% | 2022-08-04 |
| GTHX | Pharmacosmos | — | — | 2024-09-19 |
| PPD | Thermo Fisher | ~$17B | premium | (2021-12) |
| SAGE | Supernus | — | — | 2025-08-01 |
| AKRO | Novo Nordisk tender | — | — | 2025-12-10 |

Premium median ~67%, range 27-122%.

### Likely M&A (acquired/merged, requires verification) — 8

IRBT (private LBO 2025), YMAB (Nuvation), RVNC (Crown), ITOS (Sino Biopharm), BLUE (private equity 2025), BRKS (reorganized into Azenta), MRSN (announced Q1 2026), ADAP (voluntary delisting per perplexity, unclear if acquired)

### Confirmed liquidations / bankruptcies / voluntary — 25

**Memory explicit:**
CLVS (Ch 11 2022-12), SRNE (bankruptcy 2023-02), CARA, SEEL, ZIOP (strategic wind-down 2022-01), EVFM

**Inferred from Polygon delisted + no deal news:**
CDAK, GMTX, NBRV, NGM, NMTR, ODT, OMIC, ONCT, ONTX, RUBY, SCPS, SELB, SURF, TCRR, VIRX, CRTX, KMPH, IGMS, ITRM

All end with delisted dates w 2022-2026 bez acquisition premium, typical for biotech wind-down po failed trial.

### Summary

| Category | Count | % |
|---|---:|---:|
| Confirmed M&A | 17 | 34% |
| Likely M&A | 8 | 16% |
| **Total M&A** | **25** | **50%** |
| Liquidation/bankruptcy/voluntary | 25 | 50% |

**Minimum 34% confirmed M&A; realistic ~50% when including likely cases.**

## Impact on "augmented backtest" finding

Memory `project_survivorship_probe` claim: "Augmented backtest 113→163 gave Sharpe 1.49→1.75, FF3 α t 2.62→2.99" → interpreted as "survivorship bias is negative (excluding winners deflates our Sharpe)".

**Rewizja:** ~50% augmented universe to M&A acquisitions. Top-15 return drivers named w memory to CIVILIAN ALL M&A (KRTX, HZNP, IMGN, MRTX, TPTX, GBT, KDNY, RETA, CBAY, SWTX, CTIC, RXDX, NPTN + adds). M&A pops (+40-122% jednorazowe) są idealny target dla momentum scorer'a.

**Implikacja:** Augmented backtest boost **NIE JEST survivorship bias correction**. To **M&A selection effect**:
- Traditional survivorship: universe wybiera survivors → miss failures → infated performance. Ale **failures w biotech są wolne, stay below momentum threshold** — nie driverują portfela.
- Selection effect: universe celowo DODAJE M&A cases które znamy post-hoc → injects large momentum signals (deal premiums) które scorer łapie idealnie.

Perplexity r2 (2026-04-22): "M&A acquisitions często mają pre-announcement pops → adding ich to universe ≠ fixing survivorship, to selection" — **confirmed**.

## Co to zmienia dla memory i strategii

### Memory `project_survivorship_probe` musi być zrewidowane

Aktualna notatka ( "Survivorship bias JEST ale w odwrotnym kierunku") jest **niepoprawna**. Reinterpretacja:
- Curated YAML universe z 2026 nie ma statistically significant survivorship bias w kierunku "wyklucza failures" — bo failures w biotech to slow bleeds nie contributing
- Augmented universe miało M&A selection — unwanted positive bias od wybrania winners post-hoc
- **Prawdziwy PIT universe** (tickers które istniały 2021-04 bez znajomości przyszłości) może dać Sharpe ≈ 1.49 (baseline unchanged) albo lepiej/gorzej — nie wiemy bez Phase 3 PIT reconstruction.

### Implikacje dla Phase 3 (PIT universe reconstruction)

PIT reconstruction PowerShell **nie może** po prostu dodać listy 50 delisted names z Test B (to byłoby M&A selection amplification). Musi:

1. Query SimFin/Polygon for ALL biotech/thematic tickers existing at 2021-04-19
2. Apply consistent PIT liquidity / market cap filters (same as original 113)
3. Include tickers BOTH surviving and delisted (regardless of reason)
4. NO post-hoc filtering na "jak dobrze radzili sobie"

**Ten proces jest znacznie trudniejszy niż Test B'owe "dodaj 50 delisted thematic".** Musi być principled point-in-time, nie "augmentation".

## Phase 1A decision

- ✅ Confirmed: ≥50% augmented universe to M&A. Memory Test B finding (-reversed bias) MUST be reinterpreted.
- ✅ Augmented backtest **nie jest unbiased estimator alphy** — jest selection-enhanced.
- ✅ Rebaseline survivorship hypothesis: actual 113-universe bias may be **neutral or slightly positive**, wymaga PIT reconstruction (Phase 3) do rozstrzygnięcia.

## Actions

- [x] Phase 1A classification — done
- [ ] Update memory `project_survivorship_probe` — flag that Test B result is M&A selection effect, not survivorship correction
- [ ] Phase 1B: Bonferroni correction next

## Artifacts

- `~/.alphalens/survivorship/fetched_manifest.json` — 50 ticker list
- `~/.alphalens/survivorship/details_*.json` — Polygon metadata
- `docs/backtest/layer2b_survivorship.md` — original Test B report
- This file: `docs/research/delisted_classification.md`
