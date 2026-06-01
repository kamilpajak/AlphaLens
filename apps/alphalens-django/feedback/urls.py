"""URL config for the feedback REST endpoints."""

from __future__ import annotations

from django.urls import path

from feedback.views import DecisionDetailView, DecisionsView, TaxonomyView

urlpatterns = [
    path("v1/feedback/decisions", DecisionsView.as_view(), name="feedback-decisions"),
    path(
        "v1/feedback/decisions/<str:decision_id>",
        DecisionDetailView.as_view(),
        name="feedback-decision-detail",
    ),
    path("v1/feedback/taxonomy", TaxonomyView.as_view(), name="feedback-taxonomy"),
]
