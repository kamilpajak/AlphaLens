# `alphalens-django` — API

Scope: `apps/alphalens-django/**`. This file loads only once Claude reads, writes or edits a file
here, and it is NOT re-injected after `/compact`.

## A docstring here is published API schema

- drf-spectacular renders a view or serializer docstring, and any `@extend_schema(description=...)`
  text, into the OpenAPI `description`. Measured against the committed snapshot on 2026-10-14:
  13 operations carry such a description.
- Editing one makes `docs/openapi-parity/django.json` stale. Regenerate it IN THE SAME COMMIT:
  `cd apps/alphalens-django && ../../.venv/bin/python manage.py spectacular --format openapi-json --file docs/openapi-parity/django.json`
- No formatting fix-up is needed afterwards. `briefs/tests/test_openapi_parity.py::test_django_snapshot_is_fresh`
  compares the live schema and the snapshot STRUCTURALLY — both sides pass through
  `json.loads(json.dumps(..., default=str, sort_keys=True))` (`_normalise`, line 78-80, used at
  112-113) — so key order, indentation and a missing trailing newline can never fail it. Only a
  real schema difference does.
- Second gate in the same file: `test_no_breaking_openapi_drift` diffs the live schema against
  `docs/openapi-parity/legacy.json` (the frozen FastAPI contract) and fails on a dropped path, a
  removed query param, or a response-field removal classified as breaking. When a removal is
  intended, add the name to `INTENTIONAL_DROPS` in `scripts/openapi_parity.py:42` with a
  justification.
- Run both: `just test-django`, or just the gate with
  `cd apps/alphalens-django && uv run pytest briefs/tests/test_openapi_parity.py`.
- Write docstrings that do not enumerate values the pipeline owns (for example marker kinds) —
  they go stale on every pipeline change.

## `/v1/market/status`

- `market/views.py` hard-codes `exchange = market_calendar.DEFAULT_EXCHANGE` (line 112) and reads
  exactly one query param, `as_of` (line 100; `YYYY-MM-DD`, anything else returns 400).
- Its published description already promises "the requested venue (defaulting to XNYS)", yet no
  `exchange` param exists — the committed snapshot lists `as_of` only (checked 2026-10-14).
  Serving another MIC is a change to this view, not to the calendar: `market/calendar.py` already
  takes `exchange` on every helper, with `DEFAULT_EXCHANGE = "XNYS"` at line 38.
