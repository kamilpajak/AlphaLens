"""Prod settings: env-only secrets, strict ALLOWED_HOSTS, secure cookies.

CF Access JWT verification is mandatory in this environment — the
``CloudflareAccessMiddleware`` is added to the Django middleware chain so
that non-DRF views (admin, healthz) also see the authenticated user.
"""

# Django settings pattern: derived modules re-export every name from base so
# Django's lazy settings loader sees them at module scope. Star-import is
# canonical here; the explicit re-import below names the symbols this file
# actually references so static checkers don't flag them as undefined.
from .base import *  # noqa: F401,F403  # NOSONAR python:S2208
from .base import MIDDLEWARE, env

DEBUG = False
ALLOWED_HOSTS = env("ALLOWED_HOSTS")

# Insert CF middleware after AuthenticationMiddleware so request.user starts
# as AnonymousUser and gets upgraded to the CF-vouched principal.
_AUTH_IDX = MIDDLEWARE.index("django.contrib.auth.middleware.AuthenticationMiddleware")
MIDDLEWARE = [
    *MIDDLEWARE[: _AUTH_IDX + 1],
    "auth_cf.middleware.CloudflareAccessMiddleware",
    *MIDDLEWARE[_AUTH_IDX + 1 :],
]

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_HSTS_SECONDS = 60 * 60 * 24 * 365
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"
