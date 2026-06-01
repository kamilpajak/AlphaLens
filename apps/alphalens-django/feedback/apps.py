"""Django app config for the feedback REST endpoints.

Thin Django layer over ``alphalens_pipeline.feedback`` — views delegate
straight to ``FeedbackStore`` rather than going through a Django model.
Rationale: the schema authority is the pipeline-side store (DDL declared
idempotently in ``store._SCHEMA_DDL``); duplicating it as a Django model
with ``managed=False`` would introduce silent drift risk. The endpoints
need only request validation + dispatch, both of which DRF serializers
+ APIViews give us without the ORM.
"""

from __future__ import annotations

from django.apps import AppConfig


class FeedbackConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "feedback"
