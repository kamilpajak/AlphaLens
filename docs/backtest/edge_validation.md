# EDGE validation vs. Polygon ticks — BLOCKED

**Status**: Niezakończona — Polygon **Stocks Starter** nie obejmuje endpoint'u `v3/trades`. Każde zapytanie zwraca `403 NOT_AUTHORIZED "You are not entitled to this data"`. Tick-level access wymaga Advanced ($199/mies).

## Co zweryfikowane empirycznie (2026-04-20)

1. `scripts/pull_tick_sample.py` odpalony dla 113 tickerów × 5 dni → 100% failów `403`. Zero parquetów w `~/.alphalens/tick_samples/`.
2. Wszystkie płaszczyzny rynkowe dały ten sam błąd (mega/large/mid/small-cap) — to nie jest per-ticker entitlement, tylko tier-wide blok.
3. Starter dalej poprawnie zwraca `v2/aggs/*` (adjusted i raw OHLC) — patrz `edge_raw_vs_adjusted.md`.

## Konsekwencje dla walidacji EDGE

Bez danych tick-level nie możemy:
- Policzyć time-weighted rzeczywistego bid-ask spreadu jako ground truth.
- Zmierzyć per-size-bucket bias EDGE.
- Dostroić calibration factor dla small-capów (które jak podejrzewaliśmy mogą być najmocniej obciążone, bo EDGE myli volatility z spreadem).

## Dostępne alternatywy

1. **Upgrade Polygon Advanced** (~$199/mies) — jedyne źródło tickowe na naszej infrastrukturze. Uzasadnione jeśli walidacja EDGE + block trade monitor (issue #2) wchodzą do production.

2. **Odsunąć walidację w czasie** i przyjąć EDGE z literatury — paper Ardia-Guidotti-Kroencke ma benchmarki na CRSP tickach, ale nie dla konkretnie naszego universum thematic small-caps. Ryzyko: EDGE nadal będzie systematycznie off dla QUBT/IONQ-klasa.

3. **Free/darmowe alternatywy tickowe** — FINRA ATS Transparency daje tygodniowe dark-pool vol per ticker (nie spread). IEX Cloud TOPS giveaway działa tylko na IEX liquidity. `yfinance` daily quotes nie mają spreadu. Żadne z tych nie zastępuje OPRA/SIP ticków.

4. **Przyjąć empirycznie konserwatywny fallback** — w `PerTickerCostModel` ustawić `min_spread_bps` wyższy (np. 20 bps dla large-caps, 50 bps dla small-caps) zamiast globalnego 5 bps. Nie jest to walidacja — to inflacja progu bez empirycznego backing'u.

## Rekomendacja

Odroczyć decyzję o Advanced upgrade ($199/mies) do momentu gdy:
- (a) `scripts/regression_vs_flat_model.py` pokaże że per-ticker model **materialnie** zmienia wnioski Layer 2b na 5-letnim oknie (jeśli Δ Sharpe < 0.3, nie warto płacić za walidację).
- **lub**
- (b) issue #2 (block trade monitor) wchodzi do aktywnej implementacji i ticki są i tak potrzebne.

Tymczasem: **zostawić per-ticker model w trybie "flat" jako domyślnym dla produkcji**. Per-ticker dostępny przez `--cost-model per_ticker` dla eksploracji, ale bez uzasadnienia że EDGE estymuje poprawnie na naszym universum. Wszystkie wyniki z per-ticker model'u do czasu walidacji trzeba traktować jako **wskazania jakościowe** ("drag wydaje się bardzo wysoki na small-capach"), nie punktowe szacunki.

## Następne kroki

- [ ] Zaczekać na wynik `regression_vs_flat_model.py` (leci w tle).
- [ ] Jeśli Δ Sharpe < 0.3: nie upgradować, zamknąć temat z per-ticker jako opcjonalną diagnostyką.
- [ ] Jeśli Δ Sharpe ≥ 0.3: decyzja Polygon Advanced albo szukanie tańszego źródła tick data (QuantConnect LEAN ma wbudowaną historię tickową na QuantConnect'owej chmurze; IB TWS ma tick history dla retail kont).
