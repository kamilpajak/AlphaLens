# Layer 2b Theme Refresh Runbook

Procedura kwartalnego przeglądu uniwersum Layer 2b (`alphalens/momentum_screener/universe.yaml`).

**Cadence**: Co kwartał (styczeń, kwiecień, lipiec, październik), najlepiej po earnings season żeby widzieć aktualne fundamentals.

**Narzędzia**:
- `alphalens watchdog momentum-status --days 90` — ostatnie 3 miesiące produkcji
- Backtest engine (`alphalens watchdog backtest`) — walidacja na 5-letnim oknie
- Theme validation script `/tmp/theme_validation.py` (wzorcowy — kopiuj do daty refresh'u)

---

## Proces refresh (~2-4h)

### 1. Pre-refresh audit (30 min)

```bash
alphalens watchdog momentum-status --days 90
```

Zapisz metryki do journala:
- Średnie HHI (jeśli > 0.60, sygnał konsolidacji tematycznej)
- Dni alert >70% (jeśli > 20%, jeden theme dominuje za często)
- Staleness: nazwy w top-5 ≥30 dni (czy scorer zablokował się na kilku favorites?)
- Top-5 turnover average (jeśli < 15%, portfolio jest too stable)

### 2. Walidacja istniejących tematów (45 min)

Dla każdego theme w current YAML, backtestuj osobno na 5-letnim oknie:

```python
# /tmp/check_existing_themes.py
from alphalens.momentum_screener.universe import load_universe, flatten_universe
from alphalens.backtest.engine import BacktestEngine
# ... (wzorzec z /tmp/theme_validation.py)
```

Akceptuj theme jeśli Sharpe > 1.0, IC t-stat > 1.5, FF3 α_t > 1.0 (same kryteria co dla nowych).

**Jeśli theme oblewa**: nie usuwaj natychmiast — flag do "watch" i check ponownie za 30 dni. Trzy consecutive failed refreshes = sunset.

### 3. Add/remove candidates (45 min)

**Add (dodanie nazw do istniejącego theme)**:
- Search Russell 2000 dla new IPOs/spinoffs w ostatnich 6 mo pasujących do theme definition
- Dla każdego kandydata: data coverage > 95% (min 200 dni bars), sector-consistent, no pending delist
- Nie dodawać > 5 nazw per theme per refresh (smooth evolution)

**Remove (usunięcie nazw z YAML)**:
- Delisted (natural attrition)
- Moved to MegaCap → Russell 1000 top-200 (strategy nie targetuje mega caps)
- No data for > 30 dni (broken ticker, moved to OTC)

### 4. Nowe tematy (30 min — opcjonalne)

Jeśli pre-refresh pokazał że 1 theme dominuje > 30% dni, rozważ dodanie kolejnego tematu dla dywersyfikacji.

Kryteria acceptance nowego theme (z Perplexity):
- ≥ 25 pub traded spółek pasujących do theme
- Uzasadniony secular thesis (regulacje, technologia, demografia)
- 3-year EPS growth > 15% (jeśli fundamentals dostępne)
- Momentum persistence IC > 0.05 w 3-year window

Uruchom `/tmp/theme_validation.py` ze swoimi 3 kryteriami. Accept tylko jeśli wszystkie przeszły.

**Lekcje z expansion 2026-04-19**:
- ✅ **semis** passed (Sharpe 1.00, IC t-stat 3.67) → DODANE
- ❌ **nuclear** failed (Sharpe 0.62) — Max DD -59% zbyt duży. Lesson: czysty nuclear zbyt correlated z uranium prices
- ❌ **crypto_mining** failed (Sharpe 0.92, IC t-stat 0.83) — Max DD -79%. Lesson: BTC macro dominates → scorer bez predictive power

### 5. Post-refresh backtest validation (30 min)

Po zmianach w YAML:

```bash
alphalens watchdog backtest \
  --start 2021-04-19 --end 2026-04-17 \
  --cost-profile moderate \
  --report docs/backtest/post_refresh_$(date +%Y%m).md \
  --diagnose
```

**Sanity checks**:
- Net Sharpe nie spadł > 10% vs poprzedni backtest
- FF3 α_t wciąż > 2.0
- Theme HHI spadł (dywersyfikacja się poprawiła) lub utrzymał (jeśli dodałeś tylko names)

**Jeśli edge spadł > 15%**: rollback zmian YAML (git revert) i review co poszło nie tak.

### 6. Commit + deployment (15 min)

```bash
git add alphalens/momentum_screener/universe.yaml
git commit -m "refactor(momentum): Q1 2026 universe refresh — add X, remove Y"
```

Launchd job odczyta nowy YAML przy kolejnym runie (22:00 CET) automatycznie — nie trzeba restartować.

---

## Theme sunset rules

Sunset (usunięcie całego theme) to poważniejsza decyzja. Kryteria:

1. **Fundamental thesis disproven** — secular trend został zweryfikowany empirycznie jako wrong (np. "quantum computing commercially viable by 2025" — clearly nie spełniło się). Niesprawiedliwość przyszłych oczekiwań.

2. **3 kolejne kwartały failed refresh validation** — theme nie osiąga Sharpe > 1.0 w żadnym z 3 refreshów.

3. **Collapsed universe** — liczba pub-traded companies pasujących do theme spadła poniżej 15 (natural attrition).

4. **Regulatory/macro killer** — np. ban on crypto mining w USA, nuclear plant phase-out policy, etc.

**NIE sunset tematu tylko dlatego że recent performance jest słaby** — momentum jest cyklic, 6-12mo drawdown w jednym temacie to norma (zobacz: ARKK w 2022). Sunset dopiero gdy thesis się zdemolowała.

---

## Antiwzorce (czego unikać)

- **Hindsight ticker selection** — nie wybieraj tylko tych names które ostatnio wystrzeliły. Patrz na thesis coherence, nie recent returns.
- **Theme proliferation** — więcej niż 6-8 tematów to oznaka braku discipline. Każdy nowy theme musi bić statistical threshold, nie "może będzie działać".
- **YAML churn** — zmienianie uniwersum częściej niż kwartalnie → chasing noise, przestaniesz wiedzieć co faktycznie działa. Zapisz każdy refresh jako commit, żebyś mógł prześledzić drift.
- **Ignoring momentum-status alerts** — jeśli HHI > 0.7 przez 2 tygodnie, nie czekaj do kwartalnego refresh. Dodaj 1 nowy theme ad-hoc (ale waliduj normalnie).
