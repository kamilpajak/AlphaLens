# OpenAPI parity report — legacy FastAPI vs Django DRF

- legacy: `docs/openapi-parity/legacy.json`
- django: `docs/openapi-parity/django.json`

`/healthz` and `/readyz` are out of the briefs schema in Django (live in `core/views.py`); ignored.
Differences flagged **intentional** are greenfield decisions (F1 model design);
**breaking** would require either a Django fix or a coordinated frontend change.

## ✅ No paths missing in Django

## ➕ Extra in Django (not in legacy)

- `/v1/edge/chart/{brief_date}/{ticker}`
- `/v1/edge/outcomes`
- `/v1/edge/summary`
- `/v1/market/status`

## ⚠️  Per-endpoint differences

### `/v1/candidates/{date}/{ticker}`

**GET**

- Response fields missing in Django: `brief_disaster_stop_pct, brief_entry_price_note, brief_full_md, brief_position_pct, brief_time_exit_on_catalyst_failure_weeks, brief_time_exit_weeks, gates_failed_str, gates_passed_str, gates_unknown_str, technicals_summary_str` **(intentional)**
- Response fields new in Django: `brief_template_facts, brief_template_id, brief_trade_setup, expert_assessments, gate_verdict_json, peer_cohort_level`

### `/v1/days/{date}/candidates`

**GET**

- Response fields missing in Django: `brief_disaster_stop_pct, brief_entry_price_note, brief_full_md, brief_position_pct, brief_time_exit_on_catalyst_failure_weeks, brief_time_exit_weeks, gates_failed_str, gates_passed_str, gates_unknown_str, technicals_summary_str` **(intentional)**
- Response fields new in Django: `brief_template_facts, brief_template_id, brief_trade_setup, expert_assessments, gate_verdict_json, peer_cohort_level`

### `/v1/themes/{theme}/candidates`

**GET**

- Response fields missing in Django: `brief_disaster_stop_pct, brief_entry_price_note, brief_full_md, brief_position_pct, brief_time_exit_on_catalyst_failure_weeks, brief_time_exit_weeks, gates_failed_str, gates_passed_str, gates_unknown_str, technicals_summary_str` **(intentional)**
- Response fields new in Django: `brief_template_facts, brief_template_id, brief_trade_setup, expert_assessments, gate_verdict_json, peer_cohort_level`

### `/v1/tickers/{ticker}/history`

**GET**

- Response fields missing in Django: `brief_disaster_stop_pct, brief_entry_price_note, brief_full_md, brief_position_pct, brief_time_exit_on_catalyst_failure_weeks, brief_time_exit_weeks, gates_failed_str, gates_passed_str, gates_unknown_str, technicals_summary_str` **(intentional)**
- Response fields new in Django: `brief_template_facts, brief_template_id, brief_trade_setup, expert_assessments, gate_verdict_json, peer_cohort_level`
