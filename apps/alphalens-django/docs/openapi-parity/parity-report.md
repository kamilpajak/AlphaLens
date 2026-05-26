# OpenAPI parity report — legacy FastAPI vs Django DRF

- legacy: `apps/alphalens-django/docs/openapi-parity/legacy.json`
- django: `apps/alphalens-django/docs/openapi-parity/django.json`

`/healthz` and `/readyz` are out of the briefs schema in Django (live in `core/views.py`); ignored.
Differences flagged **intentional** are greenfield decisions (F1 model design);
**breaking** would require either a Django fix or a coordinated frontend change.

## ✅ No paths missing in Django

## ⚠️  Per-endpoint differences

### `/v1/candidates/{date}/{ticker}`

**GET**

- Response fields missing in Django: `brief_full_md, gates_failed_str, gates_passed_str, gates_unknown_str, technicals_summary_str` **(intentional)**
- Response fields new in Django: `peer_cohort_level`

### `/v1/days/{date}/candidates`

**GET**

- Response fields missing in Django: `brief_full_md, gates_failed_str, gates_passed_str, gates_unknown_str, technicals_summary_str` **(intentional)**
- Response fields new in Django: `peer_cohort_level`

### `/v1/themes/{theme}/candidates`

**GET**

- Response fields missing in Django: `brief_full_md, gates_failed_str, gates_passed_str, gates_unknown_str, technicals_summary_str` **(intentional)**
- Response fields new in Django: `peer_cohort_level`

### `/v1/tickers/{ticker}/history`

**GET**

- Response fields missing in Django: `brief_full_md, gates_failed_str, gates_passed_str, gates_unknown_str, technicals_summary_str` **(intentional)**
- Response fields new in Django: `peer_cohort_level`
