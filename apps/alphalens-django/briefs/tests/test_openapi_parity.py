"""Wire the OpenAPI parity diff into pytest as a regression gate.

If a future change to ``briefs.api`` introduces a *breaking* schema
divergence from the legacy FastAPI contract (path drop, query-param
removal, response field removal that isn't in ``INTENTIONAL_DROPS``),
this test fails. Cosmetic / intentional diffs do not fail.

The test regenerates Django's schema in-process via drf-spectacular's
``SchemaGenerator``; the legacy snapshot lives on disk in
``docs/openapi-parity/legacy.json`` (committed alongside this code) and
gets refreshed manually when the FastAPI side changes.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from drf_spectacular.generators import SchemaGenerator

DOCS_DIR = Path(__file__).resolve().parents[2] / "docs" / "openapi-parity"
SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"


@pytest.fixture(scope="module")
def parity():
    sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        import openapi_parity
    finally:
        sys.path.pop(0)
    return openapi_parity


@pytest.mark.django_db
def test_no_breaking_openapi_drift(parity):
    legacy_path = DOCS_DIR / "legacy.json"
    if not legacy_path.exists():
        pytest.skip(f"legacy snapshot missing: {legacy_path}")

    legacy = json.loads(legacy_path.read_text())
    django = SchemaGenerator().get_schema(request=None, public=True)

    report = parity.diff(legacy, django)

    # Replicate the script's breaking classifier.
    def is_breaking(method_diff: dict) -> bool:
        if method_diff.get("only_in_legacy"):
            return True
        qp = method_diff.get("query_params") or {}
        if qp.get("missing"):
            return True
        rf = method_diff.get("response_fields") or {}
        return rf.get("classification") == "breaking"

    breaking_endpoints = {
        path: {m: d for m, d in methods.items() if is_breaking(d)}
        for path, methods in report["per_endpoint"].items()
    }
    breaking_endpoints = {p: v for p, v in breaking_endpoints.items() if v}

    assert not report["missing_in_django"], (
        f"Legacy paths missing in Django: {report['missing_in_django']}"
    )
    assert not breaking_endpoints, (
        f"Breaking OpenAPI drift detected. Either fix Django to match, or add "
        f"the dropped name to scripts/openapi_parity.INTENTIONAL_DROPS with "
        f"justification.\nDiff: {json.dumps(breaking_endpoints, indent=2)}"
    )


# The committed Django schema snapshot the parity diff above consumes.
DJANGO_SNAPSHOT = DOCS_DIR / "django.json"


def _normalise(schema: dict) -> dict:
    """JSON round-trip so OrderedDict / tuple / date types compare structurally."""
    return json.loads(json.dumps(schema, default=str, sort_keys=True))


def _drift_summary(live: dict, committed: dict) -> str:
    """Compact hint at WHAT drifted: added / removed schema components + paths."""
    lc = set((live.get("components") or {}).get("schemas") or {})
    cc = set((committed.get("components") or {}).get("schemas") or {})
    lp = set(live.get("paths") or {})
    cp = set(committed.get("paths") or {})
    parts = []
    if lc - cc:
        parts.append(f"+schemas {sorted(lc - cc)}")
    if cc - lc:
        parts.append(f"-schemas {sorted(cc - lc)}")
    if lp - cp:
        parts.append(f"+paths {sorted(lp - cp)}")
    if cp - lp:
        parts.append(f"-paths {sorted(cp - lp)}")
    return "; ".join(parts) or "a field/type within an existing schema changed"


@pytest.mark.django_db
def test_django_snapshot_is_fresh():
    """``docs/openapi-parity/django.json`` must match the LIVE drf-spectacular schema.

    The snapshot feeds the parity diff above and documents the API contract, but
    nothing regenerated it automatically — so it silently drifted as serializers
    changed across PRs (caught + corrected in #720). This gate fails when it goes
    stale. Compared STRUCTURALLY (parsed + key-sorted) so formatting / key order
    never cause a false failure.
    """
    assert DJANGO_SNAPSHOT.exists(), f"snapshot missing: {DJANGO_SNAPSHOT}"
    live = _normalise(SchemaGenerator().get_schema(request=None, public=True))
    committed = _normalise(json.loads(DJANGO_SNAPSHOT.read_text()))
    assert live == committed, (
        "docs/openapi-parity/django.json is STALE (drifted from the live schema). "
        "Regenerate: `python manage.py spectacular --format openapi-json "
        "--file docs/openapi-parity/django.json` and commit.\nDrift: "
        + _drift_summary(live, committed)
    )
