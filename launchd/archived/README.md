# Archived launchd jobs

Pliki w tym katalogu to plisty strategii które były rozważane lub działały produkcyjnie, ale NIE są obecnie deployowane. Kod źródłowy w `alphalens/` pozostaje — infrastruktura jest reużywana — ale te konkretne joby NIE ładują się do launchd. Pełny postmortem każdej strategii: `docs/research/5_paradigm_failures_postmortem.md`.

## `com.alphalens.watchdog.themed.plist` (Layer 2b — themed momentum)

**Status**: CLOSED 2026-04-22 (PR #18 audit)

**Strategia**: daily 22:00 CET scan curated YAML universe (quantum/AI/semis/nuclear/crypto), pluggable scorer (`MomentumScorer` lub `EarlyStageScorer`), top-5 do queue + Telegram report.

**Przyczyna**: pipeline-wide bias audit pokazał (a) momentum overfit (train Sharpe 1.71 → OOS 0.82, FF3 α t-stat collapsed), (b) early-stage gross α realne ale realistic execution cost ~100% ann eats signal na microcap rebalance daily. Patrz `docs/research/layer2b_audit_final.md` + ADR 0005.

**Reactivation gate**: nowy paper z proper OOS validation lub regime change na małym/mid-cap momentum. Kod `alphalens/screeners/themed/` zostaje (status ACTIVE w infra znaczeniu, CLOSED w deployment znaczeniu — patrz `__status__` w `__init__.py`).

## `com.alphalens.insider.screen.plist` (Layer 2d — Form 4 cluster-buy)

**Status**: CLOSED 2026-04-24 (KILL)

**Strategia**: daily 22:00 CET Form 4 cluster-buy scan over IWM current constituents → top-20 dry-run report do `~/.alphalens/insider/daily_{date}.md` (feed dla GATE 1 firing-rate check).

**Przyczyna**: in-sample weekly Carhart t=2.14 (marginal PAPER_TRACK) ale OOS weekly Carhart t=0.68 → classic overfit, ten sam pattern co Layer 2b momentum. Patrz `project_pivot_alt_data.md` w memory.

**Reactivation gate**: broader universe (3000+ stocks) + quarterly rebalance + walk-forward OOS protocol pre-commit. Infra (rebalance_stride, 5xx retry, prewarm, autocorr Sharpe) jest reusable.

## `com.alphalens.watchdog.lean.plist` + `alphalens-lean` (Layer 2c)

**Status**: ARCHIVED 2026-04-19

**Strategia**: Lean-based broad Russell screener (rule-based technical scorer × 782 curated small/mid-cap universe), QuantConnect Lean w Docker.

**Przyczyna archiwum**: 5-letni rigorous backtest ujawnił **brak genuine alpha**:
- Sharpe net +0.246 (vs próg deploy 0.3)
- FF3 alpha t-stat +0.14 (brak statystycznej istotności)
- Mean IC −0.0015 (słabo ujemny w pełnym cyklu)
- Regime breakdown: strategia traci pieniądze w bull markets (−9% annual)

Szczegóły: `project_mvp1_backtest_findings.md` (memory) + `docs/backtest/mvp1_5year.md`.

**Wartość pozostała**:
- Kod `alphalens/screeners/lean/` jest reużywany przez `backtest --scorer lean` + BacktestEngine, diagnostics, weighting, theme analysis. Nie usuwać.
- `SCREENERS["lean"]` zostaje w `alphalens/registry.py` — nie powoduje harm.
- Dane historyczne (`~/.alphalens/lean/data/`) są źródłem OHLCV dla pozostałych backtestów — nie usuwać.

**Reactivation gate**: re-backtest na nowszym oknie, deploy tylko jeśli aktualne metryki spełniają Sharpe > 0.5 net AND FF3 α_t > 2. Wymaga Docker Desktop + `POLYGON_API_KEY`.
