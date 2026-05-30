"""Django app config for the ``/v1/market/*`` endpoints (PR-C, epic #295)."""

from django.apps import AppConfig


class MarketConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "market"
