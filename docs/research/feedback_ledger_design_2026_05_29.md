# Feedback ledger — design memo (v1, MVP slice)

**Status:** LOCKED (2026-05-29)
**Owner:** kamilpajak
**Scope:** Pierwszy konkret w kierunku "L3 weekly review" z poprzedniej dyskusji o docelowym kształcie narzędzia
**Estimated effort:** ~3-5 dni / 1 PR

## 0. Locked decisions (po doprecyzowaniu)

| # | Decyzja | Wynik |
|---|---------|-------|
| 1 | Action enum scope | 5 stanów: `interested` / `watching` / `dismissed` / `paper_traded` / `live_traded`. `watching` ukryty w UI pod `▾ more`. |
| 2 | Dismiss taxonomy | 2-poziomowa: 4 kategorie × 3 reasony = 9+other, per Perplexity + UX research. |
| 3 | Confidence subjective 1-5 | Opcjonalna, slider widoczny pod buttonami. Po 20 decyzjach review: jeśli zawsze NULL → usunąć w v2. |
| 4 | Group flag (`flagged_for_group_discussion`) | **Odroczone do v2**. `watching` jako proxy na MVP. |
| 5 | Uniqueness | Wariant A: `UNIQUE(brief_date, ticker, theme)`. NVDA × 2 tematy = 2 decyzje. |
| 6 | Market regime stamp | Tylko VIX bucket (low <15 / mid 15-25 / high >25). SPX trend + sector odroczone post-hoc. |
| 7 | position_size_usd + entry_price | Opcjonalne kolumny w schemacie, populated tylko dla `action='live_traded'`. |
| 8 | Cadence 4×/dzień | **Osobny PR po feedback ledger**. Re-entrancy pipeline'u to inna klasa testów. |

## 1. Why

Obecny pipeline produkuje brief → SPA pokazuje → użytkownik dyskutuje z grupą → decyzje znikają. Model **nie wie** czy działa:
- Nie ma sygnału "ten candidate był interesujący / odrzucony / paper-traded".
- Paper-trade ledger (PR #276/#279/#281/#282) jest oddzielnym strumieniem — wpis powstaje tylko jeśli świadomie wystartujesz plan; nie ma związku z candidate'em w briefie.
- Brak danych do per-signal-combo win-rate, calibration curve, dynamicznego re-weightingu `layer4_weighted_score`.

Bez **feedback ledger** L3 weekly review, personalizacja i learning loop są niemożliwe. To **rdzeń** wszystkiego co dalej w roadmap (Telegram bot ma wtedy gdzie czytać "user-marked-interested", historical analog ma na czym uczyć similarity).

## 2. What — schemat danych

Jedna tabela MVP: `decisions`. Implicit telemetry (czas patrzenia na card, kliki w evidence) — **odroczone** do v2; nie ma value przy dziennej liczbie candidates ~10-30.

```sql
CREATE TABLE decisions (
    id TEXT PRIMARY KEY,                    -- uuid4
    brief_date TEXT NOT NULL,               -- YYYY-MM-DD (dzień briefa)
    ticker TEXT NOT NULL,
    theme TEXT NOT NULL,                    -- normalised theme name
    surfaced_at TEXT NOT NULL,              -- ISO 8601 UTC (kiedy candidate trafił do briefa)
    action TEXT NOT NULL CHECK(
        action IN (
            'interested',    -- chcę kupić / na shortliście
            'watching',      -- obserwuję, jeszcze nie decyduję (kilka dni "trawienia")
            'dismissed',     -- odrzucone, z dismiss_category + dismiss_reason
            'paper_traded',  -- paper-trade plan utworzony (FK do paper_trade.db)
            'live_traded'    -- live position otwarta (poza paper)
        )
    ),
    action_at TEXT NOT NULL,                -- ISO 8601 UTC
    -- 2-poziomowy taksonomia dismiss reasonów (per Perplexity research +
    -- doc design memo). Level 1 = 4 kategorie wysokiego poziomu (Miller's
    -- law sweet spot dla UX), Level 2 = 3 konkretne powody per kategoria.
    -- UX: dropdown step 1 (4 opcje) → dropdown step 2 (3 opcje), max 4
    -- widzialne na krok. Each reason należy do dokładnie 1 kategorii.
    dismiss_category TEXT CHECK(            -- NULL unless action='dismissed'
        dismiss_category IS NULL OR dismiss_category IN (
            'thesis_setup',       -- sygnał lub setup nieatrakcyjny
            'risk_quality',       -- governance, regulatory, complexity
            'portfolio_style',    -- ekspozycja / liquidity / playbook fit
            'other'
        )
    ),
    dismiss_reason TEXT CHECK(              -- NULL unless action='dismissed'
        dismiss_reason IS NULL OR dismiss_reason IN (
            -- thesis_setup
            'wrong_theme',           -- model źle zmapował temat na ticker
            'too_expensive',         -- wycena za wysoka / upside już w cenie
            'bad_setup',             -- technicals / wrong timing / stale catalyst
            -- risk_quality
            'business_management',   -- governance, mgmt quality, accounting
            'risk_jurisdiction',     -- chińskie ADR, sanctions, restricted, ESG
            'dont_understand',       -- nie rozumiem tezy / za skomplikowane
            -- portfolio_style
            'already_have_exposure', -- już mam LUB strongly correlated z holdingiem
            'liquidity_too_low',     -- float / spread za cienki dla Ciebie
            'not_my_style',          -- momentum / short-term / poza Twoim playbookiem
            -- other
            'other'
        )
    ),
    dismiss_note TEXT,                      -- free-text, optional (REQUIRED gdy dismiss_reason='other')
    confidence_subjective INTEGER CHECK(    -- Twoja subiektywna ocena candidate'a, 1-5
        confidence_subjective IS NULL OR    -- OPTIONAL: suwak widoczny, ale nie wymagany
        confidence_subjective BETWEEN 1 AND 5
    ),                                      -- jeśli przez 20 decyzji zawsze pusty → usuwamy w v2
    paper_trade_plan_id TEXT,               -- FK do paper-trade ledger (NULL OK)
    -- Opcjonalne dla `action='live_traded'`; paper_traded korzysta z FK powyżej.
    -- Inne actions (interested/watching/dismissed) zostawiają NULL.
    position_size_usd REAL,
    entry_price REAL,
    market_regime_at_entry TEXT,            -- VIX bucket only w v1: 'low' / 'mid' / 'high'
    -- Wariant A: jeden decision per (brief × ticker × theme). NVDA tego samego dnia
    -- pod 2 tematami (AI infra + GPU shortage) = 2 osobne decyzje. Pozwala odrzucić
    -- jedną jako wrong_theme a drugą jako interested — wartość analityczna utrzymana.
    UNIQUE(brief_date, ticker, theme)
);

CREATE INDEX idx_decisions_brief_date ON decisions(brief_date);
CREATE INDEX idx_decisions_ticker ON decisions(ticker);
CREATE INDEX idx_decisions_action ON decisions(action);
```

**Outcome columns** (`outcome_pnl_pct`, `outcome_held_days`, `outcome_exit_reason`) — **NIE w v1**. Wypełni je v2 job który joinuje `decisions.paper_trade_plan_id` z paper-trade ledger po close'ie planu. v1 zostawia FK jako placeholder żeby nie migrować schemy później.

### 2.1 Dismiss taxonomy mapping (category → reason)

| Category | Reason | Kiedy używasz |
|----------|--------|---------------|
| **thesis_setup** | wrong_theme | Model źle zmapował temat na ticker (np. "AI infra" → producent klimatyzacji). |
| **thesis_setup** | too_expensive | Wycena za wysoka LUB upside już w cenie (consensus trade, no edge left). |
| **thesis_setup** | bad_setup | Technicals/timing — RSI ekstremalny, catalyst stale, breakout już za nami. |
| **risk_quality** | business_management | Governance / mgmt quality / accounting concerns — tail risk asymmetry. |
| **risk_quality** | risk_jurisdiction | Chińskie ADR, sanctioned, OFAC-restricted, ESG block, regulatory uncertainty. |
| **risk_quality** | dont_understand | Nie rozumiem tezy / supply chain logic nieczytelny / za skomplikowane. |
| **portfolio_style** | already_have_exposure | Już mam ten ticker LUB strongly correlated z istniejącym holdingiem. |
| **portfolio_style** | liquidity_too_low | Float / bid-ask spread za cienki na Twoją wielkość pozycji. |
| **portfolio_style** | not_my_style | Momentum / short-term / sektor poza Twoim playbookiem. |
| **other** | other | Catch-all. Wymaga `dismiss_note` (free-text). |

**Application-layer enforcement**: `(dismiss_category, dismiss_reason)` musi być valid pair z powyższej tabeli (Django serializer + pipeline `FeedbackStore.insert` validation). SQL CHECK constraints powyżej trzymają tylko union; pair-integrity enforce w Pythonie żeby uniknąć 9-row CHECK CASE-WHEN który jest nieczytelny.

**UX flow** (`FeedbackControls.svelte`):
1. User klika `[✕ Dismiss]` na karcie candidate'a
2. **Step 1** — pojawia się dropdown z 4 kategoriami: "Thesis & Setup", "Risk & Quality", "Portfolio & Style", "Other (free text)"
3. **Step 2** — po wybraniu kategorii (poza "Other") rozwija się drugi dropdown z 3 reasonami tej kategorii
4. **Other** — od razu pole free-text wymagane
5. Submit → `POST /v1/feedback/decisions` z `{action: "dismissed", dismiss_category, dismiss_reason, dismiss_note?}`
6. Po zapisie → button replaced przez "✓ Dismissed (label po polsku)" + mały `[undo]` link

**Po napełnieniu ledger'a** (~30+ decyzji): UX dodaje `order-by-frequency` na obu dropdownach (najczęściej używane na górze). Monitorujemy procent "other" — jeśli przekroczy 15% przez 30 decyzji, znak że taksonomia ma lukę → promote najczęstsze "other" do kategorii.

## 3. Where — storage + topologia

**SQLite** w `~/.alphalens/feedback.db`. Powody:
- Feedback to **user-authored, niewygenerowalne** dane. Postgres trzyma briefs cache który JEST regenerowalny z parquet → wymaga osobnej dyscypliny backup'u jeśliby tam mieszkał feedback.
- Tym samym lifecycle co `candidates.db` (Layer 1) i `paper_trade.db` (paper-trade ledger). Spójny pattern.
- Volume `~/.alphalens` już jest zmountowany do kontenera Django i pipeline — żadnej nowej infrastruktury.
- Backup: `rclone copy ~/.alphalens/feedback.db nextcloud:AlphaLens/backups/` — dodać do tygodniowego harmonogramu.

**Django** trzyma `feedback.db` jako **drugą bazę** (DATABASES['feedback']) z router'em który mapuje model `Decision` na `feedback` DB. Migrations jak normalnie via `python manage.py migrate --database=feedback`.

## 4. How — implementacja (3 commity w 1 PR)

### Commit 1 — pipeline-side primitives + tests
- `apps/alphalens-pipeline/alphalens_pipeline/feedback/__init__.py` z `__status__ = "ACTIVE"` + namespace docstring
- `feedback/store.py` — `FeedbackStore` class (open / insert / list_by_date / list_by_ticker / get)
- `feedback/schema.sql` — DDL jak wyżej, plus mała helper `ensure_schema(conn)`
- `feedback/regime.py` — `compute_market_regime_at(asof) -> str` (VIX bucket: low <15, mid 15-25, high >25)
- Testy `apps/alphalens-research/tests/test_feedback_store.py` — unittest, ephemeral SQLite per test
- Update `LAYERS_WITH_STATUS` w `apps/alphalens-research/tests/test_layer_status.py`

### Commit 2 — Django REST endpoint + tests
- Nowy app `apps/alphalens-django/feedback/` z models, serializers, views, urls
- DATABASES['feedback'] w `config/settings.py`, router w `feedback/db_router.py`
- 3 endpointy:
  - `POST /v1/feedback/decisions` (body: brief_date, ticker, theme, action, optional fields)
  - `GET /v1/feedback/decisions?brief_date=YYYY-MM-DD`
  - `DELETE /v1/feedback/decisions/<id>` (undo)
- pytest `feedback/tests/test_views.py` — happy path + idempotency (powtórny POST tej samej decyzji → 200 update, nie 201) + validation błędów
- Coverage wired w `apps/alphalens-django/coverage.xml` (już jest w CI, automatycznie)

### Commit 3 — SPA UI minimal cut
- Nowy `apps/web/src/lib/components/FeedbackControls.svelte`:
  - Dwa buttony: `[👁 Interested]` / `[✕ Dismiss]`
  - Click Dismiss → inline dropdown z 7 powodami → potwierdzenie
  - Po zapisie → "✓ Interested" / "✓ Dismissed (wrong_theme)" + mały undo link
  - Pesymistic UI: button click → API call → on success update local state; on fail → toast error
- Hook w `CandidateCard.svelte` — wstawić `<FeedbackControls candidate={c} briefDate={date} />` na dole karty
- Hydration: `+page.ts` dla `/brief/[date]` dociąga `GET /v1/feedback/decisions?brief_date=...` równolegle z candidates; merge w `+page.svelte`
- Playwright smoke: api-mock fixture z 1 candidate, klik Interested → assert button shows ✓; klik Dismiss → wybierz reason → assert state

## 5. Co celowo **nie** w v1

- **Implicit telemetry** (czas patrzenia, kliki w evidence) — nie ma value < 100 decyzji/miesiąc
- **Outcome join** (decyzja ↔ paper-trade PnL) — v2 background job
- **Re-weighting `layer4_weighted_score`** na podstawie historical hit-rate — wymaga ≥50 decyzji najpierw
- **Group-collaboration view** ("3 osób z grupy też dismissed") — single-user MVP, multi-user wymaga auth/tenancy
- **L3 weekly review SPA route** — osobny PR po napełnieniu ledger'a
- **Privacy / consent toggle** — solo project, Twoje dane

## 6. Test plan

- Pipeline tests (unittest): 8-10 cases — open new DB, insert, idempotency (unique key), list by date, list by ticker, get by id, market_regime computed correctly, schema migration is idempotent
- Django tests (pytest): 6-8 cases — POST happy, POST validation (bad action / bad reason / missing fields), GET by date, DELETE, idempotency on duplicate POST, regime stamped on insert
- Playwright smoke: 2 cases — Interested click flow, Dismiss-with-reason flow; runs hermetic via api-mock fixture

## 7. Adversarial review checklist (przed mergem)

- [ ] zen codereview deepseek-v4-pro thinking=high — focus: schema migration safety, unique-key races, FK to paper-trade integrity, SQLite locking under concurrent Django + pipeline writes
- [ ] zen codereview check #2: Django DB router edge cases (cross-DB queries, fixtures)
- [ ] Manual sanity: insert decision via API, restart Django, GET — survives
- [ ] CI green na wszystkich 9 checks

## 8. Risks + mitigations

| Ryzyko | Mitigation |
|--------|-----------|
| SQLite write-lock collisions (Django + pipeline backend writing równolegle) | WAL mode na otwarciu, retry-with-backoff w `FeedbackStore.insert`. Pipeline write rzadki (zero w MVP, tylko paper-trade reconciler później). |
| FK do paper_trade.db (osobna baza) — brak referential integrity | Trzymamy plan_id jako TEXT, nie real FK. Walidacja "czy plan istnieje" jako join-on-read w v2 outcome job. |
| Cofnięcie decyzji nadpisuje data (lost history) | DELETE w v1 to soft hide tylko z perspektywy UI; v2 doda kolumnę `deleted_at` zamiast hard delete. Idempotent POST = upsert, ostatni write wygrywa. |
| Backup discipline | Dodać `~/.alphalens/feedback.db` do tygodniowego rclone copy do Nextcloud po pierwszym mergu. |
| Schema migration na produkcji (Django migrations + raw SQLite) | Pierwsza wersja: `ensure_schema()` w runtime przy starcie, idempotent. Future schema changes: standard Django migrations + `--database=feedback`. |

## 9. Definition of Done

1. PR z 3 commitami zielony na CI (research + django + web + sonar + CodeQL).
2. Zen pre-merge review zaadresowany jako dodatkowy commit (jeśli findings).
3. Deploy na VPS (`docker compose pull && up -d` dla Django; pipeline image rebuild jeśli zmiana — w MVP NIE).
4. Smoke manual: brief 2026-05-28 → klik Interested na 1 candidate → reload SPA → przycisk wciąż ✓.
5. Memory `project_feedback_ledger_v1_shipped_<date>.md` + update MEMORY.md.
