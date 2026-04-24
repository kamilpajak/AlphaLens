# Post-fix comparison + perplexity second review (issue #18)

**Date:** 2026-04-22
**Previous docs:** `fundamental_gate_postfix_comparison.md` (numbers), perplexity first review z 2026-04-21 (wskazała biasy), perplexity second review (ten raport, analiza post-fix)

## Post-fix numbers — confirmed

Look-ahead bias fix pogorszył gate'a we wszystkich 4 wariantach:

| Run | Pre-fix Sharpe → Post | Pre-fix FF3 α t → Post | Pre-fix IC t → Post |
|---|---|---|---|
| momentum 5d | 0.763 → 0.676 (−11%) | 1.20 → 1.01 | 5.19 → 4.82 |
| momentum 60d | 0.763 → 0.668 (−13%) | 1.20 → 1.03 | 2.56 → 3.24 |
| early-stage 5d | 0.981 → 0.932 (−5%) | 1.60 → 1.54 | 3.76 → 3.41 |
| early-stage 60d | 1.010 → 0.959 (−5%) | 1.60 → 1.53 | 2.24 → 3.13 |

## Perplexity second review — kluczowe punkty

### Akceptuję (ważne)

1. **IC/Sharpe divergence nie jest w pełni wyjaśniona moją hipotezą.** Rosnące IC z padającą Sharpe/α może być also delayed-fundamentals effect w post-fix (Publish Date filter przesuwa quarterly signals ~45 dni do przodu, zmienia timing correlations), nie tylko "equal footing vs market". **Do weryfikacji:** rolling correlation pre vs post-fix, żeby sprawdzić czy IC structure zmieniła się w sposób inny niż tylko "level shift".

2. **Early-stage α t=1.53 post-fix = statystyczne zero, nie borderline.** Dwustronna significance wymaga t > 1.96 (5%). t=1.53 to jednostronny 10% sig level. W 5y sample na 4F HAC z daily rebalance = nie deploy bez out-of-sample validation.

3. **Multiple testing correction.** W tym projekcie (#14 + #15 + #17 + #18) przeprowadziłem ~16 hipotez faktorowych + IC. Bez Bonferroni: 56% false positive rate. Z Bonferroni α_adj = 0.003 (ekwiwalent t > 2.97 dla 2-sided):
   - **Nic w gate'u nie przeżywa** (najwyższe α t = 1.54)
   - **Baseline momentum α t=2.62 też marginally survives** (p≈0.009 vs threshold 0.003 → nie przeżywa)
   - **Baseline momentum Carhart t=2.62 jest zbyt optymistyczna** przy strict correction

4. **Delisted names POPRAWIAJĄ performance** = smoking gun requires classification:
   - M&A vs bankruptcy vs other — ma diametralnie różne implikacje
   - M&A acquisitions często mają pre-announcement pops → adding them to universe ≠ fixing survivorship, to selection
   - Jeśli większość moich removed tickers to M&A, to "augmented backtest" nie koryguje biasa, tylko zmienia selection
   - **Action:** klasyfikować AKRO, VERV, LAZR + innych delisted przed PIT reconstruction

5. **Pipeline ma więcej problemów niż 2 zidentyfikowane.** Konkretne flagi:
   - **Universe membership PIT** — czy zmiany `universe.yaml` są logged per-date? Moja data pipeline nie ma point-in-time universe membership. Np. IONQ dodany w 2026; kiedy był "tematyczny" to backtest widział go od 2021 — effectively adding a ticker retrospectively.
   - **Microcap liquidity** — bid-ask spread + market impact nie modeled. 75/100/150 bps cost scenarios to coarse proxy, nie realistic execution.
   - **Index reconstitution** — zero handlingu IPO dates w universe (kiedy ticker trafił do my curated list vs kiedy IPO'd).
   - **FF/UMD data vintage** — Ken French library może być re-estimated, używanie current vintage = look-ahead subtle.
   - **Daily rebalance commission** — nie explicit w cost model.

### Odrzucam (słabsze argumenty)

- **"Solo dev = za mało oversight"** — prawda, ale to nie jest technical point, to governance. W scope projektu (research, no external users) acceptable.
- **"10+ lat backtestu"** — fair ideal ale SimFin free tier to 5 lat. Pokryte przez walk-forward zamiast extension.
- **Niektóre sugestie powtarzają już znane** (transaction cost modeling, factor model cross-validation) — mamy generic 75/100/150 bps drag w reports, wiedzą że jest coarse.

## Rewizja konkluzji

### Co się nie zmieniło

- **Fundamental-gate family close verdict trzyma się.** Post-fix momentum gate α t=1.01-1.03, early-stage 1.53-1.54 — nic poniżej Bonferroni-corrected threshold t=2.97. Decyzja o closed family jest robust.
- **Baseline numbers unaffected przez fix** (baseline nie używa SimFin).

### Co jest osłabione

- **Layer 2b "validated alpha" baseline status.** Jeśli apply Bonferroni na cały research program (#12, #13, #14, #15, #17, #18), baseline momentum α t=2.62 może nie survive strict correction. Memory `project_mvp1_backtest_findings` mówi "Sharpe 1.71 net, FF3 α 34.6%, IC t-stat 0.62" — ten 0.62 IC jest łatwo pod threshold. Full re-assessment wymaga formal multiple-hypothesis framework.

- **"Live-deployed themed screener z validated alpha" claim** — wymaga explicit OOS test przed dalszą promocją. Memory pokazuje live deployment 2026-04-20 (wczoraj); stan dopiero 2 dni in paper/live. Akceptable dopóki nie capital deploy.

### Nowe to-dos (priority order)

1. **Klasyfikuj delisted names** — M&A vs bankruptcy vs other. Jeśli M&A dominuje, "augmented backtest" memory finding (Sharpe 1.49→1.75) jest selection effect, nie survivorship correction. Quick — 30 min z finnhub/Polygon delisted endpoint.

2. **Bonferroni/FDR correction na cały research program.** Formally list all hypothesis tests conducted 2026-04 i adjusts α. Może force re-evaluation of "validated" status baseline.

3. **Walk-forward split 2021-04 → 2024-12 train, 2025-01 → 2026-04 test.** Baseline + gate. Czy gate's post-fix marginal performance zmienia się OOS? Czy baseline α t=2.62 holds OOS?

4. **Universe membership PIT log.** Dla każdego ticker w `universe.yaml`: kiedy został dodany do repo? Git log już daje signal. Rekonstrukcja uniwersum as-of each backtest date.

5. **Microcap liquidity audit.** Median ADV + bid-ask spread dla 113 tickers at 2021-04. Jeśli znaczna część < $1M ADV → transaction cost model wymaga per-ticker, nie global scenarios.

## Meta-lekcje metodologiczne

Perplexity twice rescued me from premature closure / false-positive conclusions:
- **Round 1 (2026-04-21)**: regime bias + constant Sharpe red flag → Phase 3B PASS → Phase 3B.1 reversal (Sharpe mirage) → close family
- **Round 2 (2026-04-22)**: post-fix reviews show "close family" trzyma się, ale baseline validated alpha może sam być Bonferroni-fail, delisted-names smoking gun, universe PIT nierozwiązane

**Generalizacje:**

1. **Sharpe regime-split na low-sample = mirage generator.** Confirmed.
2. **Report Date vs Publish Date = baseline look-ahead w każdym fundamental data store.** Audit wszystkich future data sources przed użyciem.
3. **"Delisted improve performance" = flag do classify, nie automatic survivorship correction.** M&A ≠ bankruptcy w swoich effect.
4. **Multiple testing discipline musi być part of research protocol.** Solo dev + unlimited backtesting = implicit p-hacking bez pre-registration.
5. **Baseline "validated alpha" też wymaga verification pod strict correction.** Fakt że jeden scorer działa w 5y in-sample nie wystarczy bez OOS + Bonferroni.

## Actions dla #18

- [x] Fix SimFin Publish Date — DONE + tested
- [x] Re-run 4 gated backtests post-fix — DONE, close family verdict strengthened
- [x] Perplexity second review — DONE
- [ ] Klasyfikacja delisted names (M&A vs bankruptcy)
- [ ] Bonferroni correction na cały 2026-04 research program
- [ ] Walk-forward split baseline + gate (2021-04 → 2024-12 train, 2025-01 → 2026-04 test)
- [ ] Universe PIT reconstruction
- [ ] Microcap liquidity audit
