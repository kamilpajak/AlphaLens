"""Dev settings: DEBUG on, permissive CORS, console logging, auth disabled.

Local development typically runs behind ``manage.py runserver`` without a
Cloudflare tunnel, so ``CloudflareAccessAuthentication`` would 401 everything.
We override the default permission class to ``AllowAny`` here; the auth class
itself is still listed in ``DEFAULT_AUTHENTICATION_CLASSES`` so the test
suite can exercise it on demand via ``@override_settings``.
"""

from .base import *  # noqa: F401,F403
from .base import INSTALLED_APPS, MIDDLEWARE, REST_FRAMEWORK  # noqa: F401

DEBUG = True
ALLOWED_HOSTS = ["*"]
CORS_ALLOW_ALL_ORIGINS = True

REST_FRAMEWORK = {
    **REST_FRAMEWORK,
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.AllowAny"],
}
