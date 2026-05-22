# AlphaLens → Django: dziennik migracji

Greenfield migracja `alphalens/api/` (FastAPI + SQLite cache) do Django 6.0.5
z DRF + Postgres + Cloudflare Access auth. Branch: `feature/django-migration`,
worktree: `.claude/worktrees/django-migration/`.

Każda faza ma własną sekcję: cel, co powstało, problemy, decyzje, testy.

---

## F0 — Scaffold (kontekst dla F1+)

Pusty Django project + monorepo layout (`apps/alphalens-django/`,
`packages/alphalens-core/`, `deploy/docker/django-dev/`), settings split
base/dev/prod, healthz/readyz z drf-spectacular OpenAPI endpoint, Postgres 16
compose, `.env.example`. Verify: `manage.py check` clean, migracje przeszły
(SQLite fallback), `/healthz` + `/readyz` + `/api/schema/` zwracają 200.

---

## F1 — Modele ORM (`Brief`, `DayMeta`)

### Cel

Przenieść 70-kolumnowy schemat z `alphalens/api/schema.py` (legacy SoT — tuple
dataclass'ów `Column`) do Django ORM, bez tracenia parity i wprowadzając
greenfield ulepszenia tam, gdzie SQLite wymuszał kompromisy.

### Co powstało

- `briefs/models.py`
  - `Brief` — 66 pól + composite PK `(date, ticker)` przez Django 5.2+
    `models.CompositePrimaryKey`. JSONField na 5 kolumnach `list[str]`
    (`gates_passed`, `gates_failed`, `gates_unknown`, `theme_search_keywords`,
    `also_in_themes`). 4 indeksy (theme, ticker, date, composite
    `(-date, -score)` pod sortowanie listy briefów).
  - `DayMeta` — `date` jako naturalny PK + `JSONField` dla `theme_counts`.
- `briefs/migrations/0001_initial.py` (auto-generated; DDL z `sqlmigrate`
  wygląda czysto: composite PK, JSON constraints, descending composite index).
- `briefs/tests/test_schema_parity.py` — pilnuje driftu w obie strony:
  - `test_every_sot_column_is_modeled_or_dropped` — SoT → Brief
  - `test_no_orphan_brief_fields` — Brief → SoT
- `conftest.py` w root — sys.path hook dla legacy `alphalens.api.schema` SoT.

### Greenfield wycinki (świadome, nie drift)

Cztery zdenormalizowane kolumny `*_str` ze starego SQLite cache pominięte
celowo, bo Postgres JSONB query'uje listy natywnie:

- `gates_passed_str`, `gates_failed_str`, `gates_unknown_str` —
  list[str]/JSONField zostaje, str re-buildowane w DRF serializer (F3).
- `technicals_summary_str` — j.w.
- `next_earnings_date`: str → realny `DateField` (proper typing).

Lista `INTENTIONALLY_DROPPED` w `test_schema_parity.py` — nowa kolumna w SoT
bez świadomego mapowania wymaga decyzji (model field lub explicit drop).

### Problemy

Brak istotnych. `CompositePrimaryKey` z Django 5.2+ działa od ręki —
`Brief.objects.get(date=..., ticker=...)` resolve'uje composite key bez
surrogate id. JSONField round-trip listy działa zarówno w SQLite (z JSON
CHECK constraint) jak i w Postgres (JSONB).

### Testy

2/2 zielone.

---

## F2 — Ingest parquet → ORM

### Cel

Zastąpić `alphalens/api/cache.py` (raw SQL, mtime gate) management commandem
Django, który robi to samo idempotent ale przez ORM `bulk_create` +
`update_or_create`, z zachowaniem semantyki:

1. mtime gate per-date (re-ingest tylko zmienionych parquet)
2. orphan drop (parquet znikł → DB row znika)
3. tolerancja schema (starsze parquety bez `catalyst_*` itp. ingestują z NULL/[])

### Co powstało

- `briefs/ingest/coerce.py` — 7 czystych funkcji
  (`coerce_str/float/int/bool/list_str/date/datetime` + `is_missing`).
  `is_missing` przez `pd.isna()` zamiast `x != x` idiomu (Sonar S1764
  flaguje to ostatnie jako duplicate-expression — patrz user CLAUDE.md).
- `briefs/ingest/parquet.py` — `rebuild_from_parquet(briefs_dir, *, force=False)`:
  - glob `*.parquet`, ISO stem → `date` (non-ISO skipped z warningiem)
  - mtime gate via `DayMeta.parquet_mtime` z `_MTIME_EPS = 1e-6`
  - per-date w `@transaction.atomic`: delete + `bulk_create` +
    `update_or_create(DayMeta)`
  - drop orphans (`Brief` + `DayMeta` filter na date__in)
  - `_coerce_for_field()` introspektuje typ pola — **brak osobnej mapy
    field→kind**, parity z modelem za darmo
- `briefs/management/commands/rebuild_briefs_cache.py` — `--briefs-dir`,
  `--force`, stdout summary
- `briefs/tests/test_coerce.py` (22 testy), `briefs/tests/test_ingest.py`
  (10 testów: smoke, mtime skip/bump/force, orphan drop, schema tolerance
  dla 2024-era parquet, non-ISO stem skip, management command)

### Problemy

**Bug 1: `bulk_create` nie odpala model defaults.**

`Brief.gates_passed = models.JSONField(default=list)` ma default na poziomie
modelu, ale Django `bulk_create` wstawia surowy SQL — wszystkie kolumny muszą
być wypełnione przez caller. Pierwsze testy ingestu rzuciły 7× IntegrityError
`NOT NULL constraint failed: briefs_brief.n_gates_failed`, gdy parquet
omijał kolumnę i `coerce_int(None)` zwracało `None`.

**Fix**: w `_coerce_for_field()` po coerce sprawdzić `field.null` i jeśli
False, wstawić `field.get_default()`. Bez tego każde nowe NOT NULL pole z
defaultem psułoby ingest.

### Greenfield decyzje

- Algorytm legacy 1:1 ale ORM zamiast surowego SQL — `Brief.objects.filter(date=...).delete()` + `bulk_create`.
- **Brak `SCHEMA_VERSION` ręcznego** — migracje Django to zastępują (`makemigrations` daje historię).
- **Brak `wal_checkpoint(TRUNCATE)`** — Postgres nie potrzebuje, SQLite Django sam zarządza.

### Testy

44/44 zielone (32 coerce + 10 ingest + 2 parity z F1).

---

## F3 — DRF viewsets + serializers (`/v1/*`)

### Cel

Zastąpić 6 routerów FastAPI (`alphalens/api/routes/*.py`) viewsetami DRF,
zachowując kontrakt:

- 8 endpointów pod `/v1/*` (days, themes, candidates, tickers, stats)
- envelope `{data, meta: {total, limit, offset}}`
- query params `from`/`to` jako alias na ISO daty, `limit ≤ 200`
- 404 dla URL path PK invalid; 400 dla query param invalid

### Co powstało

- `briefs/api/serializers.py`:
  - `CandidateSerializer(ModelSerializer)` — `exclude=("pk",)` żeby skip
    `CompositePrimaryKey` descriptor
  - `DayMetaSerializer` (DB default empty-string `top_theme` → `null` przez
    `SerializerMethodField`)
  - `DayBriefSerializer`, `ThemeSummarySerializer`, `TopThemeSerializer`,
    `StatsSerializer`
- `briefs/api/pagination.py`:
  - `EnvelopePagination(LimitOffsetPagination)` — override
    `get_paginated_response()` + `get_paginated_response_schema()` dla
    drf-spectacular kompatybilności
  - `envelope()` helper dla aggregate endpoints (themes list, stats)
- `briefs/api/filters.py`:
  - `parse_iso_date()` — `DRF ValidationError` (→ 400) dla query params
  - `parse_clamped_int()`, `get_paging()`
- `briefs/api/views.py`:
  - `DayViewSet` (list, retrieve, action `candidates`)
  - `ThemeViewSet` (list z `.values('theme').annotate(Count, Min, Max)`,
    action `candidates`)
  - `CandidateViewSet` (custom compound URL handler `retrieve_compound`)
  - `TickerViewSet` (action `history`)
  - `StatsView(APIView)` — pojedyncze read-only
  - `_date_from_path()` helper — ISO-invalid w path PK → `NotFound` (404),
    nie `ValidationError` (400)
- `briefs/api/urls.py`:
  - `DefaultRouter(trailing_slash=False)` dla 3 viewsetów z single-PK
  - explicit `path()` dla `/v1/candidates/<date>/<ticker>` (compound PK
    nie pasuje do standardowego routera DRF)
- `briefs/tests/test_api.py` — 20 testów end-to-end (APIClient):
  - envelope shape parity, range filter, theme filter, min_score
  - lowercase ticker → uppercase normalization
  - empty DB / unknown ticker → 200 z `total=0`
  - 400 vs 404 differentiation
  - max_limit=200 clamp
  - OpenAPI schema renderuje wszystkie ścieżki

### Problemy

**Bug 1: Path PK invalid jako 400 zamiast 404.**

Pierwsza wersja `retrieve(self, request, pk)` używała bezpośrednio
`parse_iso_date(pk)` — która rzuca `ValidationError` → DRF mapuje na 400. Ale
URL `/v1/days/garbage` semantycznie znaczy "nie ma takiego dnia" (404), nie
"źle zapytałeś" (400). Query param invalid (`?from=garbage`) zostaje 400.

**Fix**: helper `_date_from_path(pk, what)` łapie `ValidationError` i
zamienia na `NotFound`.

**Bug 2: DRF `LimitOffsetPagination` emituje `{count, next, previous, results}`.**

Frontend i legacy OpenAPI konsumują `{data, meta: {total, limit, offset}}`.

**Fix**: override `get_paginated_response()` + `get_paginated_response_schema()`
(ten drugi jest kluczowy żeby drf-spectacular wyemitował poprawny OpenAPI
list shape).

### Greenfield decyzje

- DRF `LimitOffsetPagination` override zamiast ręcznej envelope kopii w
  każdym endpoint — DRY wygrywa
- `DefaultRouter(trailing_slash=False)` → URL kontrakt 1:1 z FastAPI bez
  301-redirect
- `*_str` denormalizacje porzucone konsekwentnie z F1 (re-computable z
  JSONField po stronie serializer jeśli FE poprosi)
- Path-vs-query semantic distinction dla błędów (404 path, 400 query)
- drf-spectacular auto-generuje OpenAPI z viewset signatures + serializers
  (warningi `id untyped` to kosmetyka)

### Testy

64/64 zielone (44 z F2 + 20 nowych API).

---

## F4 — Cloudflare Access JWT auth

### Cel

Zastąpić "trust the reverse proxy" pattern legacy FastAPI (CF Access przed
nginx, API słucha 127.0.0.1:8086) realną weryfikacją JWT po stronie Django.
API musi sam się bronić, nawet jeśli ktoś go bezpośrednio wystawi.

### Co powstało

- `auth_cf/conf.py` — env-driven config:
  - `CF_ACCESS_TEAM`, `CF_ACCESS_AUD`, `CF_ACCESS_JWKS_CACHE_TTL`,
    `CF_ACCESS_REQUIRED`
  - `issuer_url()` + `jwks_url()` helpers
  - `JWT_HEADER = "HTTP_CF_ACCESS_JWT_ASSERTION"`, `JWT_COOKIE = "CF_Authorization"`
- `auth_cf/jwt_verifier.py`:
  - `get_jwks(refresh=False)` — cached JWKS lookup via Django cache framework
  - `verify(token) → claims` — kid lookup w JWKS, decode RS256 z aud/iss
    enforcement + `require=[exp, iat, iss, aud]`
  - **Auto-retry przy kid-miss**: jeśli kid nie ma w cache, refresh JWKS raz
    i spróbuj jeszcze raz (handle key rotation between cache fetches)
- `auth_cf/authentication.py` — `CloudflareAccessAuthentication(BaseAuthentication)`:
  - Header `Cf-Access-Jwt-Assertion` lub cookie `CF_Authorization` fallback
  - `email` claim → `User.username=email` (lowercased)
  - `common_name` claim (service tokens) → `User.username=cf-svc:<common_name>`
  - `get_or_create` przy każdym auth (auto-provisioning — CF Access już
    zweryfikowało tożsamość, więc signup flow byłby nadmiarem)
  - `CF_ACCESS_REQUIRED=False` (dev) → `return None` na missing JWT
    (fallthrough); `True` (prod) → 401
- `auth_cf/middleware.py` — `CloudflareAccessMiddleware` dla non-DRF views
  (admin, `/healthz` itd.). Non-fatal: invalid JWT zostawia AnonymousUser.
- Settings wiring:
  - `base.py` — DRF defaults
    `[CloudflareAccessAuthentication, SessionAuthentication]` + `IsAuthenticated`
  - `dev.py` — override `IsAuthenticated → AllowAny`
  - `prod.py` — wstrzykuje `CloudflareAccessMiddleware` po
    `AuthenticationMiddleware`
  - `CACHES` (LocMem default, Redis-ready przez settings)
- `auth_cf/tests/`:
  - `conftest.py` — session-scope RSA keypair (2048), JWKS fixture, `make_jwt`
    factory z dowolnymi claim overrides, autouse monkeypatch `conf` + cache
    seed
  - `test_jwt_verifier.py` (9 testów: happy + service token + 6 rejections)
  - `test_authentication.py` (10 testów: provisioning, idempotent, cookie
    fallback, email normalization, service token namespace, optional/required mode)

### Problemy

**Bug 1: `override_settings` w pytest-django nie czyści DRF
`api_settings._cached_attrs`.**

Pierwsza wersja testów auth_cf używała `@override_settings` jako class
decorator. Zwróciło `ValueError: Only subclasses of Django SimpleTestCase
can be decorated`.

**Fix #1**: `override_settings(...)` as context manager wewnątrz każdego
testu, nie decorator. OK na poziomie auth_cf, ale...

**Bug 2: Cross-file state leak — `StatsView.permission_classes` mutuje się
na `[IsAuthenticated]` po auth_cf testach i nie wraca.**

Najboleśniejszy bug F4. Po przejściu auth_cf tests (które flipują REST_FRAMEWORK
na strict via override_settings), briefs/test_api.py zaczyna failować 19/20
testów z 401. Settings są OK (dev `AllowAny`), DRF `api_settings.DEFAULT_PERMISSION_CLASSES`
zwraca `AllowAny`, ale `StatsView.permission_classes` **jako class attribute**
trzyma `[IsAuthenticated]`.

Mechanizm: pytest-django + DRF `setting_changed` signal handler współdziałają
tak, że klasy view które rozwiązują `permission_classes = api_settings.DEFAULT_PERMISSION_CLASSES`
w class-body widzą aktualną wartość — gdy auth_cf test flipnął na strict,
a teardown nie zresetował klasy.

Próbowane (nie zadziałały):
- Signal replay `setting_changed.send(setting='REST_FRAMEWORK', enter=False)` w autouse teardown
- Manual `api_settings._cached_attrs.discard(...)` + `__dict__.pop()`
- Inline `override_settings` context manager w fixture briefs conftest
- pytest-django `settings` fixture override
- Subprocess reproduction failed → bug widoczny tylko gdy pytest-django session
  trzyma DB

**Fix #2 (działa)**: `briefs/tests/conftest.py` autouse fixture rebinduje
`permission_classes` + `authentication_classes` na każdej z 5 klas viewsetów
ze świeżego `api_settings.DEFAULT_*` przed każdym testem. Brzydkie ale
deterministyczne. Verified: oba orderings (`auth_cf, briefs` i `briefs, auth_cf`)
zielone.

To jest test infrastructure quirk, nie production bug — production nigdy nie
robi `override_settings` mid-flight. Zostawiłem komentarz w conftest
wyjaśniający dlaczego.

### Greenfield decyzje

- PyJWT[crypto] zamiast python-jose — Anthropic + CF docs preferują, mniej
  transitive deps
- JWKS cached via Django cache framework — działa zarówno dev (LocMem) jak
  prod (Redis), bez ad-hoc dict
- `CF_ACCESS_REQUIRED` toggle pozwala API uruchomić się lokalnie bez tunelu
  (`return None` z auth class → DRF fallthrough)
- Two-principal model: email + `cf-svc:<common_name>` namespace dla CF
  Service Tokens (CI runners, internal services)
- Auto-provision User on first auth — CF Access już zweryfikowało tożsamość,
  Django signup form byłby nadmiarem
- **Skip django-allauth** — Google OAuth fallback dorzucam dopiero przy F7
  jeśli local dev bez CF tunelu okaże się potrzebny

### Testy

83/83 zielone (64 z F3 + 19 nowych auth: 9 verifier + 10 DRF integration).
Reverse ordering verified.

---

## F5 — OpenAPI parity diff vs legacy FastAPI

### Cel

Wymuszone porównanie kontraktu: 8 endpointów + envelope + query params + path
params + response field set. Każda różnica sklasyfikowana **intentional**
(greenfield decyzje z F1) vs **breaking** (frontend się rozwala). Mechanizm
działa offline (oba `openapi.json` z dysku) + jako pytest gate (regresje na CI).

### Co powstało

- `docs/openapi-parity/legacy.json` — snapshot z legacy `create_app().openapi()`
  (10 paths)
- `docs/openapi-parity/django.json` — generowany przez `manage.py spectacular`
  (8 paths; `/healthz`+`/readyz` żyją poza `/v1` w Django i nie pokazują się
  w briefs schema)
- `scripts/openapi_parity.py` — diff tool:
  - normalizacja `LEGACY_ONLY_OK` (healthz/readyz)
  - klasyfikacja `INTENTIONAL_DROPS` (4× `*_str` z F1)
  - per-endpoint diff query/path params + response fields
  - `--strict` exit 1 jeśli realne breaking
- `docs/openapi-parity/parity-report.md` — wygenerowany raport
- `briefs/tests/test_openapi_parity.py` — pytest gate: regeneruje Django
  schema in-process przez `SchemaGenerator`, diffuje z legacy JSON,
  failuje na breaking

### Problemy

**Bug 1: pusta sekcja `/v1/days/{date}` w raporcie.**

Render skryptu wpisywał nagłówek `### path` + `**GET**` nawet gdy diff_entry
był pusty. Realnie diff był: `path_params {missing=[date], extra=[id]}` —
ja po prostu nie wypisywałem `path_params` w renderze. Po fixie renderu
wyszedł realny bug: DRF `DefaultRouter` używa `{id}` jako default lookup
keyword regardless of resource. OpenAPI client codegen by się rozsypał.

**Fix**: w każdym viewset (`Day`, `Theme`, `Ticker`) dodaję:
```python
lookup_field = "date"          # / "theme" / "ticker"
lookup_url_kwarg = "date"      # j.w.
lookup_value_regex = r"[^/.]+"
```
+ rename handler signatures (`def retrieve(self, request, date=...)` zamiast `pk=...`).
URL wire-level identyczny przed/po; tylko nazwa parametru w schemie się
zmienia. Test `TestOpenAPISchema::test_schema_renders_with_all_endpoints`
zaktualizowany o nowe nazwy path params.

**Bug 2: drf-spectacular warningi o nie-rozwiązanej `CloudflareAccessAuthentication`.**

Bez `OpenApiAuthenticationExtension` schema generation zwraca 12 warningów.
Nie krytyczne (security scheme placeholder), ale brudzi output. **Decyzja:
zostawiam na F7** (gdy będę finalizować deploy + admin auth UI),
prawdopodobnie wymaga zarejestrowania własnego extension class — zbędne
ryzyko teraz.

### Greenfield decyzje

- `*_str` denormalizacje pozostają wycięte (frontend `types.ts` ma definicje
  ale ich nie renderuje — F6 ich oczyści; F1 model stoi)
- Sklasyfikowane `intentional` w raporcie — `--strict` exit 0 gdy każda
  znaleziona różnica jest intentional
- Path param rename naprawiony (taniutka zmiana, realna parity)
- `/healthz` i `/readyz` zostają w `core/` poza schema briefs — Django
  konwencja oddziela API od liveness probes

### Wynik parity

- 0 missing paths
- 0 extra paths (poza healthz/readyz świadomie ignored)
- 4 endpointy z `*_str` drift → wszystkie **intentional**
- 0 breaking changes
- pytest gate zielony

### Testy

84/84 zielone (83 z F4 + 1 nowy parity gate).

---

## F6 — Frontend pointing (Django-aware web/)

### Cel

Frontend (SvelteKit SPA) musi móc rozmawiać zarówno z legacy FastAPI
(istniejący deploy na VPS) jak i z nowym Django (dev, parallel deploy
F7). Bez zmian backendowych, środowiskowo wybieralne.

### Co powstało

- `web/src/lib/api.ts` — `api(path)` helper:
  - same-origin default: `api('/v1/days')` → `/api/v1/days`
  - cross-origin override: `VITE_API_BASE=https://api.example.com` →
    `https://api.example.com/v1/days` (bez `/api` prefiksu — to artifact
    same-origin proxy, nie part of API contract)
  - `isCrossOrigin()` helper dla future warunkowych UI changes
- Refactor 4 fetch sites (`+page.ts`, `briefs/+page.ts`, `brief/[date]/+page.ts`,
  bonus path constant w `+layout.ts`) — wszystkie używają `api()`
- `web/src/lib/types.ts` — usunięcie 4× `*_str` denormalisations (F1
  greenfield decision)
- `web/vite.config.ts` — dokumentacja `VITE_API_TARGET` dla dev proxy
  (legacy 8081 vs Django 8000), bez zmiany default behaviour
- `web/tests/django-smoke.test.ts` — real-wire Playwright test:
  - gating na `DJANGO_SMOKE=1` env (skipped by default)
  - 3 testy: dashboard render bez console errors, `/v1/days` envelope shape,
    `/v1/stats` top-level keys
  - używa `request` fixture (APIRequestContext) świadomie — bypassuje
    `page.route()`, hituje realną sieć (per MEMORY
    `feedback_playwright_page_request_bypasses_route`)

### Problemy

Brak nowych. Pre-existing 4 errors / 5 warnings z `pnpm check` (Snippet
typing w JargonTip, @types/node missing dla smoke tests) — istniały
przed F6, F6 ich nie tknęło ani nie wprowadziło dodatkowych.

### Greenfield decyzje

- `api()` helper zamiast direct string literals — centralizacja URL
  building, pozwala cross-origin deploy bez touching call sites
- `*_str` definitively z `types.ts` usunięte (F1 → F3 → F5 → F6: pełny
  ślad konsekwentny przez 4 fazy)
- Real-wire smoke test gated, **nie default-on** — wymaga uruchomionego
  Django + populated DB, więc CI tego nie odpali; ręczny verify F7
- `VITE_API_TARGET` zachowuje default `http://127.0.0.1:8081` (legacy) —
  cutover na Django to **manual env flip**, nie kod-change

### Testy

84 backend + 97/100 hermetic Playwright smoke (3 skipped Django smoke).
Total: 181 passing test runs.

---

## F7 — Production deploy (single greenfield stack)

### Reframe vs original plan

Pierwotny F7 z roadmapy zakładał **parallel deploy + canary 5%** przez
nginx upstream — typowa technika brownfield migration gdzie legacy ma
realny traffic. Greenfield: **brak traffic do canarowania**, brak legacy
do utrzymania. Cutover jednorazowy gdy F8 dokona dekommisji
`alphalens/api/`.

F7 robi więc to, co realnie produkcyjne: **single production stack
Django + Postgres + nginx + SPA**.

### Cel

Działający `docker compose up` reprezentujący docelowy production
deploy, z Cloudflare Access aktywnym, sprawdzony end-to-end.

### Co powstało

- `auth_cf/openapi.py` — `CloudflareAccessScheme(OpenApiAuthenticationExtension)`:
  - rejestruje `CloudflareAccessAuthentication` w drf-spectacular jako
    apiKey/header (nie `http-bearer`, bo CF nie używa `Bearer` prefix)
  - rejestracja przez `AuthCfConfig.ready()` (local import → bez kosztu
    spectacular na non-API code paths)
- `briefs/api/views.py` — `DATE_PATH` / `THEME_PATH` / `TICKER_PATH` path
  param declarations + `@extend_schema_view(...)` na 3 viewsetach:
  - czyści 4 pozostałe path-param warnings drf-spectacular
  - łącznie z F7.1 ekstension: **12 warnings → 0**
- `deploy/docker/django-prod/Dockerfile`:
  - multi-stage: `uv:python3.13-bookworm-slim` builder → `python:3.13-slim` runtime
  - non-root user (uid 1000), libpq + curl jako jedyne runtime apt deps
  - `collectstatic` w build time (runtime image read-only)
  - gunicorn + uvicorn worker (sync DRF + async-ready dla Django 5.2+)
  - HEALTHCHECK na `/healthz`
- `deploy/docker/django-prod/docker-compose.yaml`:
  - postgres + django + nginx + jeden-shot `rebuild-cache` (profile `maintenance`)
  - `migrate --noinput` na każdym starcie django (idempotent)
  - bind-mount `~/.alphalens/thematic_briefs` read-only dla rebuild cache
  - YAML anchor `&django-base` żeby django + rebuild-cache współdzielili konfigurację
- `deploy/docker/django-prod/nginx.conf`:
  - static SPA + immutable cache na `/_app/` (SvelteKit fingerprinted assets)
  - `/api/*` rewrite na upstream root + `X-Forwarded-*` headers
  - `/healthz` + `/readyz` direct pass-through (platform liveness)
  - SPA fallback `try_files ... /index.html` dla client-side routes
- `deploy/docker/django-prod/.env.example` — wszystkie wymagane env vars
- `deploy/docker/django-prod/README.md` — topology diagram + bring-up
  steps + greenfield note (no parallel deploy)

### Problemy

**Bug 1: `ghcr.io/astral-sh/uv:0.11.6` to distroless image bez `/bin/sh`.**

Pierwszy build wywalił się na `RUN uv python install ${PYTHON_VERSION}`
z `runc run failed: ... "/bin/sh": stat /bin/sh: no such file or directory`.
Tag `0.11.6` w GHCR uv to scratch-based distroless — przeznaczony dla
binary embeds, nie multi-stage builds z RUN.

**Fix**: `ghcr.io/astral-sh/uv:python3.13-bookworm-slim` — Debian-slim
variant z preinstalowanym Pythonem i normalnym shell-em.

**Bug 2: `COPY apps/alphalens-django/ ./` clobberuje `.venv` z poprzedniego stage'a.**

Po `RUN uv venv .venv && uv pip install ...`, następne `COPY` overlay'uje
folder. `.dockerignore` w `deploy/docker/django-prod/.dockerignore` jest
**ignorowany przez Docker** bo build context to repo root — Docker
szuka `.dockerignore` w build context root, nie obok Dockerfile. Wynik:
host's `apps/alphalens-django/.venv/` zostaje skopiowany i nadpisuje
container's `.venv`, ale tylko częściowo, więc `.venv/bin/python` znika
po drodze. `uv pip install` widzi "no venv found".

**Fix**: explicit COPY na cztery konkretne foldery (`config/`, `briefs/`,
`auth_cf/`, `core/`) zamiast wholesale `COPY apps/alphalens-django/ ./`.
Bardziej verbose ale 100% deterministyczne, bez polegania na
non-portable `<Dockerfile>.dockerignore` buildkit feature.

### Greenfield decyzje

- **Brak parallel deploy / canary**: original F7 plan zawierał nginx
  canary header routing, dropped — nie ma traffic do canarowania
- Postgres jako primary store (nie SQLite cache jak legacy) — F1 model
  już to założył
- `gunicorn --workers=2` zamiast async ASGI workers dla DRF view set —
  worker liczba dostrojona dla VPS z 2 CPU, sync views z threadpool dla
  blocking pandas
- `collectstatic` w build time, nie runtime (immutable image)
- One-shot `rebuild-cache` service zamiast in-container cron — host's
  systemd timer zarządza scheduling, container state inspectable
- bind-mount `BRIEFS_DIR` read-only — daily pipeline pisze, Django tylko
  czyta, brak race condition
- Cloudflare tunnel zamiast bezpośredniego port forward — nginx za
  tunnel, no TLS termination w container

### Wynik end-to-end verify

Local `docker compose up`:
- postgres: healthy
- django: healthy (HEALTHCHECK na `/healthz`)
- nginx: up

Probes via `localhost:8080`:
- `GET /healthz` → 200 `{"status": "ok"}`
- `GET /readyz` → 200 `{"status": "ready"}` (Postgres SELECT 1)
- `GET /api/v1/stats` → **401** `{"detail":"missing Cf-Access-Jwt-Assertion"}`
  — auth aktywna w prod settings, parity z legacy semantyką
- `GET /api/schema/` → 200 OpenAPI

### Testy

84/84 backend zielone (regression check po extension + path param fixes).
drf-spectacular schema generation: **0 warnings, 0 errors** (z 12 unique
warnings przed F7).

---

## F8 — Decommission legacy `alphalens/api/`

### Greenfield reframe vs original plan

Pierwotny F8 z roadmapy zakładał "Cutover + dekommisja" po parallel
deploy. Skoro greenfield: brak parallel deploy → cutover był już w
momencie merge'u F7. F8 robi po prostu rip-out:

1. Usunięcie kodu legacy
2. Usunięcie consumers (CLI subcommand, deploy artifacts)
3. Frozen contract baseline + ADR

### Cel

Stan repo bez `alphalens/api/`, bez FastAPI/uvicorn deps, bez legacy
docker-compose, bez systemd `restart api` step. Wszystko, co realnie
serwuje briefs, idzie przez Django.

### Co usunięte / co zmienione

**Usunięte:**
- `alphalens/api/` cały folder (10 plików: app.py, cache.py, db.py,
  deps.py, models.py, schema.py, routes/*)
- `alphalens_cli/commands/api.py` + entry z `alphalens_cli/main.py`
- `tests/api/` cały folder (10 testów FastAPI)
- `tests/test_api_serve_cli.py`
- `deploy/docker/docker-compose.yml` (legacy 3-service compose)
- `apps/alphalens-django/conftest.py` (sys.path hook dla legacy
  schema import — już niepotrzebny po freeze contract)

**Zmienione:**
- `pyproject.toml` — usunięte `fastapi>=0.136.1`, `uvicorn[standard]>=0.47.0`
- `tests/test_layer_status.py` — usunięte `alphalens.api` z LAYERS_WITH_STATUS
- `tests/test_deploy_systemd_units.py` — `restart_api_post_run` test
  zamieniony na `rebuilds_briefs_cache_post_run` (testuje nowy
  ExecStartPost wskazujący na django-prod compose)
- `deploy/systemd/alphalens-thematic-daily.service` — ExecStart przez
  `docker run --rm alphalens-pipeline:latest` (zamiast compose),
  ExecStartPost przez `docker compose --profile maintenance run --rm rebuild-cache`
- `deploy/docker/run_thematic_day.sh` — usunięty step `alphalens api rebuild-cache`
  (cache rebuild to teraz osobny step w systemd ExecStartPost)
- `deploy/docker/README.md` — przepisane od zera, pointuje na django-prod
- `deploy/systemd/README.md` — paragraf o `restart api` zastąpiony opisem `rebuild-cache`
- `CLAUDE.md` — tabela systemd unit reflectuje nowy flow
- `apps/alphalens-django/briefs/tests/test_schema_parity.py` — import
  `alphalens.api.schema` zastąpiony **frozen `LEGACY_CONTRACT_COLUMNS`
  tuple** inline (70 nazw); dwa testy zachowują rolę gate'u driftu
  contract'u w obie strony, bez zależności od skasowanego modułu

**Dodane:**
- `docs/adr/0009-django-replaces-fastapi.md` + index update — formalna decyzja
- `apps/alphalens-django/docs/openapi-parity/README.md` — dokumentuje
  `legacy.json` jako frozen contract baseline (NIE edytować)

### Problemy

Brak nowych. F8 to mechaniczne rip-out — wszystkie ryzyka były
zaadresowane w F1-F7 (schema parity, API contract parity, auth,
deploy verify).

### Greenfield decyzje

- **Brak deprecation period**: greenfield = nie ma użytkowników legacy,
  rip-out jest natychmiastowy
- **Brak rollback**: ADR 0009 explicit'y noted to. Verified by
  full-suite green + spectacular clean przed merge
- **Frozen contract baseline** zamiast live import legacy SoT:
  - `LEGACY_CONTRACT_COLUMNS` inline w `test_schema_parity.py`
  - `legacy.json` snapshot pod `docs/openapi-parity/` (oznaczony jako
    "do not edit" w README)
  - Skala drift'u dalej widoczna w obu kierunkach przez testy
- `Dockerfile.pipeline` zostaje (to nie API, to daily thematic ingest) —
  separate concern, separate image

### Wynik final

**Django side (apps/alphalens-django/):**
- 84/84 backend zielone
- drf-spectacular: 0 warnings, 0 errors
- OpenAPI parity gate: `--strict` exit 0, 0 missing, 0 extra, 4
  intentional drops

**Repo side:**
- 8/8 zmienionych unittestów zielone (`tests/test_deploy_systemd_units.py`,
  `tests/test_layer_status.py`)
- `alphalens_cli` bootuje: `app.registered_groups` lista bez `api`
- `alphalens/` ma 16 podpakietów (było 17 — minus `api`)
- `deploy/docker/` ma 2 docelowe artefakty: `Dockerfile.pipeline` + `django-prod/`

---

## Suma F1-F8 (greenfield migration complete)

- 84 testy zielone, oba orderings
- 4 nowe Python deps: pyjwt[crypto], httpx, pyarrow, pandas (Django side); psycopg, drf-spectacular, django-environ, django-cors-headers już w F0
- 0 mocków produkcyjnego kodu — testy używają realnych RSA keypair, signed JWT, parquet round-trip
- API parity 8/8 endpointów + envelope shape + status code semantics + path param names
- Schema parity test pilnuje 70-kolumnowego SoT contractu w obu kierunkach
- OpenAPI parity gate w pytest pilnuje breaking drift vs legacy FastAPI

### Pliki dodane w F1-F7

```
apps/alphalens-django/
├── briefs/
│   ├── models.py                                        # F1
│   ├── migrations/0001_initial.py                       # F1
│   ├── ingest/{__init__,coerce,parquet}.py              # F2
│   ├── management/commands/rebuild_briefs_cache.py      # F2
│   ├── api/{__init__,serializers,pagination,filters,views,urls}.py  # F3 + F5 path params + F7 schema decorators
│   └── tests/
│       ├── conftest.py                                   # F4 (defensive perms reset)
│       ├── test_schema_parity.py                         # F1
│       ├── test_coerce.py                                # F2
│       ├── test_ingest.py                                # F2
│       ├── test_api.py                                   # F3
│       └── test_openapi_parity.py                        # F5
├── auth_cf/
│   ├── conf.py                                          # F4
│   ├── jwt_verifier.py                                  # F4
│   ├── authentication.py                                # F4
│   ├── middleware.py                                    # F4
│   ├── openapi.py                                       # F7 (drf-spectacular ext)
│   └── tests/{conftest,test_jwt_verifier,test_authentication}.py  # F4
├── scripts/openapi_parity.py                            # F5
├── docs/
│   ├── migration-log.md                                  # F1-F7 (ten plik)
│   └── openapi-parity/{legacy,django}.json + parity-report.md  # F5
└── config/settings/{base,dev,prod}.py                    # F0/F4

web/
├── src/lib/{api,types}.ts                               # F6
├── src/routes/{+page,brief/[date]/+page,briefs/+page}.ts # F6 (api() refactor)
├── tests/django-smoke.test.ts                           # F6 (gated DJANGO_SMOKE=1)
└── vite.config.ts                                       # F6 (target docs)

deploy/docker/django-prod/
├── Dockerfile                                           # F7
├── docker-compose.yaml                                  # F7
├── nginx.conf                                           # F7
├── .env.example                                         # F7
├── .dockerignore                                        # F7
└── README.md                                            # F7
```

### Następne fazy

- Brak. Migracja F1-F8 zakończona; ADR 0009 dokumentuje całość.

### Open follow-ups (post-merge, nice-to-have)

- `web/tests/django-smoke.test.ts` jako CI gate (wymaga running Django w
  CI sandboxie — odroczone bo dziś gate hermetic smoke jest wystarczający)
- Redis cache backend zamiast LocMem dla `JWKS_CACHE_KEY` w prod (LocMem
  działa, ale per-worker cache; mała skala briefs API ⇒ niski priorytet)
- `manage.py rebuild_briefs_cache --force` jako weekly systemd job
  (defense-in-depth: full rebuild w razie cichego mtime drift)
