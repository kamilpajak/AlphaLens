# Archived launchd jobs

Pliki w tym katalogu reprezentują strategie/joby które były rozważane ale NIE są deployowane produkcyjnie. Kod źródłowy w `alphalens/` pozostaje — infrastruktura może być reużywana — ale te konkretne konfiguracje NIE ładują się do launchd.

## `com.alphalens.watchdog.lean.plist` + `alphalens-lean`

**Status**: ARCHIVED 2026-04-19

**Strategia**: Layer 2c MVP1 — Lean-based broad Russell screener (rule-based technical scorer × 782 curated small/mid-cap universe).

**Przyczyna archiwum**: 5-letni rigorous backtest ujawnił **brak genuine alpha**:
- Sharpe net +0.246 (vs próg deploy 0.3)
- FF3 alpha t-stat +0.14 (brak statystycznej istotności)
- Mean IC −0.0015 (słabo ujemny w pełnym cyklu)
- Regime breakdown: strategia traci pieniądze w bull markets (−9% annual)

Szczegóły w `~/.claude/projects/-Users-jacoren-Developer-Personal-AlphaLens/memory/project_mvp1_backtest_findings.md` oraz `docs/backtest/mvp1_5year.md`.

**Wartość pozostała**:
- Kod `alphalens/screeners/lean/` jest reużywany przez `backtest --scorer lean` + BacktestEngine, diagnostics, weighting, theme analysis (wszystko w `alphalens/backtest/` submodule). Nie usuwać.
- `SCREENERS["lean"]` zostaje w `alphalens/registry.py` — nie powoduje harm.
- Dane historyczne (`~/.alphalens/lean/data/`) są źródłem dla Layer 2b backtestów — nie usuwać.

**Gdyby wskrzesić**: `cp` plików z powrotem do `launchd/` + `launchctl load`. Ale przed tym — re-backtest na nowszym oknie i tylko jeśli aktualne metryki spełniają progi deploy (Sharpe > 0.5 net AND FF3 α_t > 2).
