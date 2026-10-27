# Research lab — editing `alphalens_research/**`

Nested file: it loads only once Claude reads, writes or edits a file under
`apps/alphalens-research/`, and it is NOT re-injected after `/compact`. Rules that must hold
everywhere (dependency direction, English-only, TDD, where new code goes) stay in the root
`CLAUDE.md`.

## Lifecycle status markers

Every package under the eight research-side **layer roots** declares, in its own `__init__.py`:

```python
__status__: Literal["ACTIVE", "CLOSED", "RESEARCH_ONLY", "ARCHIVED"] = "..."
```

The eight roots are `screeners/`, `gates/`, `backtest/`, `overlays/`, `attribution/`,
`preaudit/`, `diagnostics/`, `retrospective_audit/`.

- Adding a sub-package under any of those eight without `__status__` turns the gate red on the
  commit it lands. There is no registry to update — discovery is an `rglob` over `__init__.py`.
- `NAMESPACE_ONLY_ALLOWLIST` is the only exemption and holds exactly ONE entry today:
  `alphalens_research.screeners`. Add to it only for a genuine namespace package that holds
  sub-packages but no layer of its own.
- Outside the eight roots a status is OPTIONAL but VALIDATED when present. `discover_lane/` and
  `eval/` declare one voluntarily; declaring one is the cheap option, since a wrong VALUE is red
  either way.
- A `__status__` assigned in a MODULE instead of an `__init__.py` is INVISIBLE to the gate — it
  walks `__init__.py` only. Three modules under `diagnostics/` (`exit_policy_analysis.py`,
  `exit_policy_missingness.py`, `exit_policy_replay.py`) carry one and are never checked. Put the
  marker in the package.
- Pipeline-side packages are validated when they declare a status and are never REQUIRED to. The
  other four uv workspace members — `apps/alphalens-broker-contract`, `apps/alphalens-feedback`,
  `apps/intent-replay`, `apps/alphalens-django` — are not scanned at all, so a missing marker
  there is invisible rather than red.

## CLOSED / ARCHIVED needs evidence, not just the word

A package whose `__status__` is `CLOSED` or `ARCHIVED` must also declare:

- `__closed_reason__` — a non-empty string.
- `__closed_evidence__` — a dict whose keys are EXACTLY these seven: `carhart_4f_hac`,
  `sanity_checks_4gate`, `walk_forward_oos`, `multiple_testing_correction`, `cost_drag`,
  `bootstrap_ci`, `survivorship_pit`. A missing key and an extra key both fail. Each value is
  either a repo-relative path ending `.md` that must EXIST on disk, or a string starting
  `"N/A: "` or `"UNTESTED: "` followed by at least 3 characters of justification.
- `__closed_date__` is convention only — no test reads it. Write it anyway.

What each of the seven gates measures, and its acceptance threshold:
[`docs/research/kill_verdict_checklist.md`](../../docs/research/kill_verdict_checklist.md).

## Run the gate

```bash
cd apps/alphalens-research && ../../.venv/bin/python -m unittest tests.test_layer_status -v
```

`test_discovery_is_non_empty` is a positive control with two floors: more than 10 required
packages, and strictly MORE status-declaring packages than required ones. It guards against
discovery breaking, not against a single deleted marker: measured 2026-10-27, 19 packages are
required and 40 declare a status, 21 of them research-side, so even deleting EVERY pipeline-side
marker leaves the second floor satisfied. 5 tests green.
