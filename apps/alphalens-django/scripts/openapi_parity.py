"""OpenAPI parity diff: legacy FastAPI vs new Django DRF.

Compares the two ``openapi.json`` blobs and emits a structured report
distinguishing breaking changes (frontend won't work) from cosmetic ones
(field renames in $ref names, x-extension drift, etc).

Path-name aliasing: DRF's ``DefaultRouter`` injects ``{id}`` as the lookup
parameter name regardless of the resource. We normalise this to the legacy
name (``{date}``, ``{theme}``, ``{ticker}``) before diffing — the URL bytes
sent over the wire are identical, only the OpenAPI parameter name differs.

Usage::

    python scripts/openapi_parity.py \\
        --legacy  docs/openapi-parity/legacy.json \\
        --django  docs/openapi-parity/django.json \\
        --report  docs/openapi-parity/parity-report.md
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

# Map Django path → legacy path (parameter rename). Empty after F5 fix —
# kept as a hook for future router-driven renames.
PATH_ALIASES: dict[str, str] = {}

# Endpoints that exist legacy-side but are intentionally moved or dropped.
# /healthz + /readyz live in core/, not under /v1, so they don't show up in
# drf-spectacular's briefs schema. Both still exist as plain Django views.
LEGACY_ONLY_OK = {"/healthz", "/readyz"}

# Response fields intentionally dropped in F1 — denormalised legacy SQLite
# joins that the greenfield Django model replaces with computed serializer
# output (or in frontend code). Diff entries that consist only of these
# names are classified as ``intentional``, not ``breaking``.
INTENTIONAL_DROPS = frozenset(
    {
        "gates_passed_str",
        "gates_failed_str",
        "gates_unknown_str",
        "technicals_summary_str",
        # Retired 2026-05-26: the rendered markdown blob duplicated the
        # structured brief_* fields already served + shown in the UI. The
        # per-candidate brief is now consumed as structured columns only.
        "brief_full_md",
        # Retired 2026-05-27: the thin trade-management fields (single
        # position size, flat -25% stop, hardcoded 8w/4w exits, entry note)
        # were replaced by the deterministic brief_trade_setup ladder
        # (entry/TP tiers + structural stop). The new JSON field is an
        # additive, non-breaking response extension.
        "brief_entry_price_note",
        "brief_position_pct",
        "brief_time_exit_weeks",
        "brief_time_exit_on_catalyst_failure_weeks",
        "brief_disaster_stop_pct",
    }
)


def _normalise(spec: dict, *, is_django: bool) -> dict:
    """Apply aliases and drop irrelevant paths so diffing is fair."""
    paths = {}
    for path, item in spec.get("paths", {}).items():
        canonical = PATH_ALIASES.get(path, path) if is_django else path
        if canonical in LEGACY_ONLY_OK and not is_django:
            continue
        paths[canonical] = item
    return paths


def _query_params(operation: dict) -> dict[str, dict]:
    """Index ``parameters[in=query]`` by name → param dict."""
    out: dict[str, dict] = {}
    for p in operation.get("parameters", []):
        if p.get("in") == "query":
            out[p["name"]] = p
    return out


def _path_params(operation: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for p in operation.get("parameters", []):
        if p.get("in") == "path":
            out[p["name"]] = p
    return out


def _resp_schema(operation: dict) -> dict:
    resp_200 = operation.get("responses", {}).get("200", {})
    content = resp_200.get("content", {}).get("application/json", {})
    return content.get("schema") or {}


def _resolve_ref(spec: dict, schema: dict) -> dict:
    """Follow one level of ``$ref`` into ``components.schemas``."""
    ref = schema.get("$ref")
    if not ref:
        return schema
    name = ref.rsplit("/", 1)[-1]
    return spec.get("components", {}).get("schemas", {}).get(name, {})


def _response_fields(spec: dict, operation: dict) -> set[str]:
    """Surface-level field names of the 200 response body.

    For envelope responses (``{data, meta}``), recurse one level to expose
    ``data`` item fields.
    """
    schema = _resolve_ref(spec, _resp_schema(operation))
    if not schema:
        return set()
    props = schema.get("properties") or {}
    if set(props) == {"data", "meta"}:
        data_schema = _resolve_ref(spec, props["data"])
        # data is array → resolve items
        if data_schema.get("type") == "array":
            item_schema = _resolve_ref(spec, data_schema.get("items", {}))
            return set((item_schema.get("properties") or {}).keys())
        return set((data_schema.get("properties") or {}).keys())
    return set(props.keys())


def diff(legacy: dict, django: dict) -> dict:
    """Compute the structured diff. Returns a dict ready for report rendering."""
    legacy_paths = _normalise(legacy, is_django=False)
    django_paths = _normalise(django, is_django=True)

    report: dict[str, Any] = {
        "missing_in_django": sorted(set(legacy_paths) - set(django_paths)),
        "extra_in_django": sorted(set(django_paths) - set(legacy_paths)),
        "per_endpoint": defaultdict(dict),
    }

    common = sorted(set(legacy_paths) & set(django_paths))
    for path in common:
        legacy_methods = legacy_paths[path]
        django_methods = django_paths[path]
        for method in sorted(set(legacy_methods) | set(django_methods)):
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            legacy_op = legacy_methods.get(method)
            django_op = django_methods.get(method)
            if legacy_op is None:
                report["per_endpoint"][path][method] = {"only_in_django": True}
                continue
            if django_op is None:
                report["per_endpoint"][path][method] = {"only_in_legacy": True}
                continue

            lq = _query_params(legacy_op)
            dq = _query_params(django_op)
            lp = _path_params(legacy_op)
            dp = _path_params(django_op)

            lf = _response_fields(legacy, legacy_op)
            df = _response_fields(django, django_op)

            diff_entry: dict[str, Any] = {}
            if set(lq) != set(dq):
                diff_entry["query_params"] = {
                    "missing": sorted(set(lq) - set(dq)),
                    "extra": sorted(set(dq) - set(lq)),
                }
            if set(lp) != set(dp):
                diff_entry["path_params"] = {
                    "missing": sorted(set(lp) - set(dp)),
                    "extra": sorted(set(dp) - set(lp)),
                }
            if lf and df and lf != df:
                resp_missing = sorted(lf - df)
                resp_extra = sorted(df - lf)
                # Extras are additive — JSON-by-convention clients ignore
                # unknown fields, so a new field in Django can never break
                # an older legacy consumer. Only the ``missing`` side gates
                # the classification (and only when it strays outside the
                # documented INTENTIONAL_DROPS set).
                intentional = set(resp_missing).issubset(INTENTIONAL_DROPS)
                diff_entry["response_fields"] = {
                    "missing": resp_missing,
                    "extra": resp_extra,
                    "classification": "intentional" if intentional else "breaking",
                }

            if diff_entry:
                report["per_endpoint"][path][method] = diff_entry

    report["per_endpoint"] = {k: v for k, v in report["per_endpoint"].items() if v}
    return report


def render_report(report: dict, *, legacy_path: Path, django_path: Path) -> str:
    """Render the diff dict as a markdown report."""
    lines: list[str] = [
        "# OpenAPI parity report — legacy FastAPI vs Django DRF",
        "",
        f"- legacy: `{legacy_path}`",
        f"- django: `{django_path}`",
        "",
        "`/healthz` and `/readyz` are out of the briefs schema in Django (live in `core/views.py`); ignored.",
        "Differences flagged **intentional** are greenfield decisions (F1 model design); ",
        "**breaking** would require either a Django fix or a coordinated frontend change.",
        "",
    ]

    if report["missing_in_django"]:
        lines += [
            "## ❌ Missing in Django",
            "",
            *(f"- `{p}`" for p in report["missing_in_django"]),
            "",
        ]
    else:
        lines += ["## ✅ No paths missing in Django", ""]

    if report["extra_in_django"]:
        lines += [
            "## ➕ Extra in Django (not in legacy)",
            "",
            *(f"- `{p}`" for p in report["extra_in_django"]),
            "",
        ]

    if not report["per_endpoint"]:
        lines += ["## ✅ All endpoints match", ""]
    else:
        lines += ["## ⚠️  Per-endpoint differences", ""]
        for path, methods in sorted(report["per_endpoint"].items()):
            lines.append(f"### `{path}`")
            lines.append("")
            for method, delta in sorted(methods.items()):
                lines.append(f"**{method.upper()}**")
                lines.append("")
                if delta.get("only_in_django"):
                    lines.append("- Only in Django (legacy lacks this method).")
                elif delta.get("only_in_legacy"):
                    lines.append("- Only in legacy (Django lacks this method). **BREAKING.**")
                else:
                    qp = delta.get("query_params")
                    if qp:
                        if qp["missing"]:
                            lines.append(
                                f"- Query params missing in Django: `{', '.join(qp['missing'])}` **(breaking)**"
                            )
                        if qp["extra"]:
                            lines.append(
                                f"- Query params new in Django: `{', '.join(qp['extra'])}`"
                            )
                    pp = delta.get("path_params")
                    if pp:
                        if pp["missing"]:
                            lines.append(
                                f"- Path params renamed away: `{', '.join(pp['missing'])}` **(OpenAPI breaking, URL stable)**"
                            )
                        if pp["extra"]:
                            lines.append(f"- Path params new in Django: `{', '.join(pp['extra'])}`")
                    rf = delta.get("response_fields")
                    if rf:
                        tag = (
                            "intentional"
                            if rf.get("classification") == "intentional"
                            else "breaking"
                        )
                        if rf["missing"]:
                            lines.append(
                                f"- Response fields missing in Django: `{', '.join(rf['missing'])}` **({tag})**"
                            )
                        if rf["extra"]:
                            lines.append(
                                f"- Response fields new in Django: `{', '.join(rf['extra'])}`"
                            )
                lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--legacy", type=Path, required=True)
    parser.add_argument("--django", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Non-zero exit if any breaking differences remain.",
    )
    args = parser.parse_args(argv)

    legacy = json.loads(args.legacy.read_text())
    django = json.loads(args.django.read_text())
    report = diff(legacy, django)
    args.report.write_text(render_report(report, legacy_path=args.legacy, django_path=args.django))

    def _is_breaking(method_diff: dict) -> bool:
        if method_diff.get("only_in_legacy"):
            return True
        qp = method_diff.get("query_params") or {}
        if qp.get("missing"):
            return True
        rf = method_diff.get("response_fields") or {}
        if rf.get("classification") == "breaking":
            return True
        return False

    breaking = bool(report["missing_in_django"]) or any(
        any(_is_breaking(m) for m in methods.values())
        for methods in report["per_endpoint"].values()
    )
    print(f"report → {args.report}")
    print(f"missing_in_django: {len(report['missing_in_django'])}")
    print(f"extra_in_django:   {len(report['extra_in_django'])}")
    print(f"diff endpoints:    {len(report['per_endpoint'])}")
    print(f"breaking:          {breaking}")
    return 1 if (args.strict and breaking) else 0


if __name__ == "__main__":
    sys.exit(main())
