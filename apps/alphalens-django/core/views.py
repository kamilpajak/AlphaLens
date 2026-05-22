"""Liveness + readiness probes.

`/healthz` — process is up. Never touches DB; safe for k8s livenessProbe.
`/readyz`  — process can serve traffic. Hits DB with a trivial `SELECT 1`.
"""

from __future__ import annotations

from django.db import OperationalError, connection
from django.http import HttpRequest, JsonResponse


def healthz(_: HttpRequest) -> JsonResponse:
    return JsonResponse({"status": "ok"})


def readyz(_: HttpRequest) -> JsonResponse:
    try:
        with connection.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
    except OperationalError as exc:
        return JsonResponse({"status": "degraded", "db": str(exc)}, status=503)
    return JsonResponse({"status": "ready"})
