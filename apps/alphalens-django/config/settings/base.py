# pyright: reportArgumentType=false, reportCallIssue=false
"""Base Django settings shared by dev and prod.

Env-driven via django-environ. No secrets here — `.env` (dev) or process env
(prod) supplies SECRET_KEY, DATABASE_URL, CORS_ALLOWED_ORIGINS, etc.
"""

from __future__ import annotations

from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env(
    DEBUG=(bool, False),
    ALLOWED_HOSTS=(list, ["localhost", "127.0.0.1"]),
    CORS_ALLOWED_ORIGINS=(list, []),
    CORS_ALLOWED_ORIGIN_REGEXES=(list, []),
    CORS_ALLOW_CREDENTIALS=(bool, False),
)
environ.Env.read_env(BASE_DIR / ".env")

SECRET_KEY = env("SECRET_KEY", default="dev-only-insecure-do-not-use-in-prod")
DEBUG = env("DEBUG")
ALLOWED_HOSTS = env("ALLOWED_HOSTS")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "drf_spectacular",
    "corsheaders",
    "briefs.apps.BriefsConfig",
    "edge.apps.EdgeConfig",
    "auth_cf.apps.AuthCfConfig",
    "core.apps.CoreConfig",
    "market.apps.MarketConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

DATABASES = {"default": env.db("DATABASE_URL", default="sqlite:///" + str(BASE_DIR / "db.sqlite3"))}

# Server-side VIX regime cache. Written out-of-band by
# `alphalens cache refresh-vix` (FRED VIXCLS) during the daily thematic build.
# No Django consumer reads it after the Track-A user-action feedback ledger was
# removed; the setting is kept so the cache path stays a single documented
# constant shared with the pipeline-side ``alphalens_feedback.regime`` default
# and the Prometheus VIX-cache staleness alert. Same host ``~/.alphalens`` mount
# + container HOME trap as ALPHALENS_BRIEFS_DIR.
ALPHALENS_VIX_CACHE = env(
    "ALPHALENS_VIX_CACHE",
    default=str(Path.home() / ".alphalens" / "macro" / "vix_regime_cache.json"),
)

# Population-ladder outcome parquet store (edge dashboard cache, memo §4). The
# pipeline writes one parquet per brief date here; rebuild_ladder_outcomes_cache
# mirrors them into Postgres. Same host ``~/.alphalens`` mount + container HOME
# trap as ALPHALENS_BRIEFS_DIR. The ingest reads this via its own module-level
# default (so argparse picks it up); the env name is documented here for parity.
ALPHALENS_LADDER_OUTCOMES_DIR = env(
    "ALPHALENS_LADDER_OUTCOMES_DIR",
    default=str(Path.home() / ".alphalens" / "population_ladders"),
)

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

CORS_ALLOWED_ORIGINS = env("CORS_ALLOWED_ORIGINS")
CORS_ALLOWED_ORIGIN_REGEXES = env("CORS_ALLOWED_ORIGIN_REGEXES")
CORS_ALLOW_CREDENTIALS = env("CORS_ALLOW_CREDENTIALS")

REST_FRAMEWORK = {
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.LimitOffsetPagination",
    "PAGE_SIZE": 50,
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "auth_cf.authentication.CloudflareAccessAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
}

# In-memory cache by default. Production swaps to django-redis via DATABASES-style URL.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "alphalens-default",
    },
}

SPECTACULAR_SETTINGS = {
    "TITLE": "AlphaLens Briefs API",
    "DESCRIPTION": (
        "Read/write HTTP access to thematic briefs produced by the AlphaLens daily pipeline."
    ),
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {"format": "{asctime} {levelname} {name} {message}", "style": "{"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "verbose"},
    },
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        "django.db.backends": {"level": "WARNING"},
    },
}
