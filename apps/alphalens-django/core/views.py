"""Liveness + readiness probes, plus the Cloudflare Access re-auth trampoline.

`/healthz` — process is up. Never touches DB; safe for k8s livenessProbe.
`/readyz`  — process can serve traffic. Hits DB with a trivial `SELECT 1`.
`/auth/start` — re-auth landing for the SPA; see `auth_start` for the why.

All three are read-only by contract — `@require_GET` rejects POST/PUT/
DELETE/PATCH with a 405 instead of executing the handler.
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

from django.conf import settings
from django.db import OperationalError, connection
from django.http import (
    HttpRequest,
    HttpResponse,
    HttpResponseBadRequest,
    HttpResponseRedirect,
    JsonResponse,
)
from django.utils.http import url_has_allowed_host_and_scheme
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


def _allowed_return_hosts() -> set[str]:
    """SPA origins (host components) the trampoline is allowed to redirect to.

    Derived from `CORS_ALLOWED_ORIGINS` — those are the only browser origins
    we already trust for credentialed cross-origin requests, so they are
    also the only safe targets for a server-side redirect after re-auth.
    """
    hosts: set[str] = set()
    for origin in settings.CORS_ALLOWED_ORIGINS:
        netloc = urlparse(origin).netloc
        if netloc:
            hosts.add(netloc)
    return hosts


def _default_return_to() -> str:
    """Where to send the user if no `return_to` is provided.

    Falls back to the first configured SPA origin (CORS_ALLOWED_ORIGINS[0])
    so an operator hitting `/auth/start` directly lands on the SPA root
    instead of seeing the API JSON root. Empty list (dev) → root path "/".
    """
    if settings.CORS_ALLOWED_ORIGINS:
        return settings.CORS_ALLOWED_ORIGINS[0]
    return "/"


@require_GET
def auth_start(request: HttpRequest) -> HttpResponse:
    """Cloudflare Access re-authentication trampoline for the SPA.

    Flow per Cloudflare docs (developers.cloudflare.com/cloudflare-one/
    access-controls/applications/http-apps/authorization-cookie/): the
    `CF_Authorization` cookie for `api.alphalens.kamilpajak.pl` can only
    be set in an HTTP response served FROM that hostname — a redirect
    from `<team>.cloudflareaccess.com` directly back to the SPA origin
    leaves the API cookie unset. The SPA cannot recover by itself.

    This view exists so the SPA has a stable URL on the API origin to
    bounce through after its API cookie expires:

      1. SPA detects 401 on its credentialed API call.
      2. `+error.svelte` links to `/auth/start?return_to=<spa-url>` on
         the API origin.
      3. Cloudflare Access intercepts the request (no/expired
         CF_Authorization), runs the Google SSO flow, and lands the
         browser back on this view with a fresh CF_Authorization cookie
         set on api.* via the Set-Cookie header in the Access response.
      4. This view 302s the browser back to `return_to`, which is now
         a normal SPA load with a valid API cookie.

    Open-redirect guard via `url_has_allowed_host_and_scheme` (Django's
    canonical helper, used by `LoginView`): `return_to` must either be a
    same-origin path (starts with `/`) or carry a host from the
    `CORS_ALLOWED_ORIGINS` allowlist + use HTTPS.
    """
    return_to = request.GET.get("return_to") or _default_return_to()
    if not url_has_allowed_host_and_scheme(
        return_to,
        allowed_hosts=_allowed_return_hosts(),
        require_https=True,
    ):
        # `return_to` is user-controlled — never echo it verbatim into the
        # log stream (Sonar S5145 / CodeQL py/log-injection / CWE-117).
        # Truncate, then strip the CR + LF chars that would let an attacker
        # forge a second log entry. CodeQL's py/log-injection rule only
        # recognises explicit `.replace("\n", ...).replace("\r", ...)` as a
        # sanitizer — see its rule doc — so even though `unicode_escape`
        # would also work semantically, this form is what the analyzer
        # taint-tracks as cleaned. Emit length too so operators can spot
        # waves of long URLs without importing the attacker input.
        safe_head = (return_to or "")[:80].replace("\r", "").replace("\n", "")
        logger.warning(
            "auth_start: rejected invalid return_to (len=%d, head=%s)",
            len(return_to or ""),
            safe_head,
        )
        return HttpResponseBadRequest("invalid return_to")
    return HttpResponseRedirect(return_to)
