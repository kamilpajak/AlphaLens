"""Liveness + readiness probes.

`/healthz` — process is up. Never touches DB; safe for k8s livenessProbe.
`/readyz`  — process can serve traffic. Hits DB with a trivial `SELECT 1`.

Both are read-only by contract — `@require_GET` rejects POST/PUT/DELETE/
PATCH with a 405 instead of executing the handler.
"""

from __future__ import annotations

import logging

from django.db import OperationalError, connection
from django.http import HttpRequest, JsonResponse
from django.views.decorators.http import require_GET

logger = logging.getLogger(__name__)


@require_GET
def healthz(_: HttpRequest) -> JsonResponse:
    return JsonResponse({"status": "ok"})


@require_GET
def readyz(_: HttpRequest) -> JsonResponse:
    try:
        with connection.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
    except OperationalError:
        # Don't leak DB connection details (host, port, error class) into
        # the public probe body — log the full exception for operators
        # and return a generic "degraded" so external monitors can act
        # without seeing internals (CodeQL py/stack-trace-exposure).
        logger.exception("readyz: database probe failed")
        return JsonResponse({"status": "degraded"}, status=503)
    return JsonResponse({"status": "ready"})
