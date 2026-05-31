"""URL config for the ``/v1/market/*`` endpoints.

Currently a single status endpoint; the namespace is reserved so future
calendar / session-detail / trading-day-arithmetic endpoints can land
without re-organising the URL graph.
"""

from __future__ import annotations

from django.urls import path

from market.views import MarketStatusView

urlpatterns = [
    path("v1/market/status", MarketStatusView.as_view(), name="market-status"),
]
