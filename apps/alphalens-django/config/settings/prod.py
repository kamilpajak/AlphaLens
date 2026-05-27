"""Prod settings: env-only secrets, strict ALLOWED_HOSTS, secure cookies.

CF Access JWT verification is mandatory in this environment — the
``CloudflareAccessMiddleware`` is added to the Django middleware chain so
that non-DRF views (admin, healthz) also see the authenticated user.
"""

# Django settings pattern: derived modules re-export every name from base so
# Django's lazy settings loader sees them at module scope. Star-import is
# canonical here; the explicit re-import below names the symbols this file
# actually references so static checkers don't flag them as undefined.
from django.core.exceptions import ImproperlyConfigured

from .base import *  # noqa: F401,F403  # NOSONAR python:S2208
from .base import MIDDLEWARE, SECRET_KEY, env

DEBUG = False
ALLOWED_HOSTS = env("ALLOWED_HOSTS")

# Fail-fast on a misconfigured prod container that started without supplying
# SECRET_KEY: base.py falls back to a known dev key, which would silently
# weaken cookie/CSRF/PRNG signing in production. Detect the sentinel here
# rather than removing the dev fallback from base.py (which would break the
# test suite that runs under config.settings.dev without a .env file).
# Exact-match the sentinel — operators sometimes pick keys with the
# ``dev-only-`` prefix to mark rotated emergency credentials, and we don't
# want to crash those legitimately set prod keys.
_DEV_SECRET_KEY_SENTINEL = "dev-only-insecure-do-not-use-in-prod"
# Also fail closed on an empty / falsy key: `"" == sentinel` is False, so a
# `SECRET_KEY=""` env would slip past an exact-match-only guard and boot with
# no signing key (Django catches it deeper, but fail here with a clear cause).
if not SECRET_KEY or SECRET_KEY == _DEV_SECRET_KEY_SENTINEL:
    raise ImproperlyConfigured(
        "SECRET_KEY must be set in the production environment "
        "(currently empty or using the base.py dev-only fallback)."
    )

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
