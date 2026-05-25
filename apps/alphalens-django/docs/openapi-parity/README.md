# OpenAPI parity snapshots

This directory holds two `openapi.json` blobs and one generated diff
report. After F8 the legacy FastAPI code is gone — `legacy.json`
remains as a **frozen contract baseline** that the pytest gate
(`briefs/tests/test_openapi_parity.py`) diffs Django's live schema
against on every CI run.

| File | Origin | Refresh policy |
|------|--------|----------------|
| `legacy.json` | snapshot of legacy `alphalens.api.app.create_app().openapi()` taken at F5 | **frozen** — never edit. If a deliberate contract change is justified (new field, new endpoint), update both this file and the `INTENTIONAL_DROPS` set in `scripts/openapi_parity.py` together |
| `django.json` | `manage.py spectacular --format openapi-json --file docs/openapi-parity/django.json` | regenerable; refresh whenever Django views change |
| `parity-report.md` | `scripts/openapi_parity.py` output | regenerable |

## Verifying parity

```bash
.venv/bin/python manage.py spectacular --format openapi-json --file docs/openapi-parity/django.json
.venv/bin/python scripts/openapi_parity.py \
    --legacy docs/openapi-parity/legacy.json \
    --django docs/openapi-parity/django.json \
    --report docs/openapi-parity/parity-report.md \
    --strict
```

`--strict` exits 1 if any **breaking** drift is found (legacy field
disappeared and isn't in `INTENTIONAL_DROPS`). The same checks run
in-process under `briefs/tests/test_openapi_parity.py`.
