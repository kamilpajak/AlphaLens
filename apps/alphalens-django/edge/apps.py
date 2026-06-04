"""App config for the market-behavior edge dashboard cache."""

from __future__ import annotations

from django.apps import AppConfig


class EdgeConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "edge"
