"""Dev settings: DEBUG on, permissive CORS, console logging, auth disabled.

Local development typically runs behind ``manage.py runserver`` without a
Cloudflare tunnel, so ``CloudflareAccessAuthentication`` would 401 everything.
We override the default permission class to ``AllowAny`` here; the auth class
itself is still listed in ``DEFAULT_AUTHENTICATION_CLASSES`` so the test
suite can exercise it on demand via ``@override_settings``.
"""

# Django settings pattern: derived modules re-export every name from base so
# Django's lazy settings loader sees them at module scope. Star-import is
# canonical here; the explicit re-import below names the symbols this file
# actually references so static checkers don't flag them as undefined.
from .base import *  # noqa: F401,F403  # NOSONAR python:S2208
from .base import INSTALLED_APPS, MIDDLEWARE, REST_FRAMEWORK  # noqa: F401

DEBUG = True
ALLOWED_HOSTS = ["*"]
CORS_ALLOW_ALL_ORIGINS = True

REST_FRAMEWORK = {
    **REST_FRAMEWORK,
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.AllowAny"],
}
