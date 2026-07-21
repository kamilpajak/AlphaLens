"""Saxo auto-manager (SIM-first exit-management engine) — control-loop seams.

Net-new live-infra layer (ADR 0011) wiring the shipped placement + reconcile
primitives into an always-on polling daemon. Design:
docs/research/saxo_automanager_mvp_design_2026_07_21.md. Each module is a thin
single-responsibility seam; the loop holds no durable in-memory state (status
is recomputed each tick by the read-only reconcile engine). __status__ is not
REQUIRED (brokers/ is not a research LAYER_ROOT) but we stamp ACTIVE to match
the sibling brokers/__init__.py house style (needs no __closed_* fields).

ADR 0013 inheritance (via brokers): R2 (no execution output feeds T2
SELECTION), R3 (placement carries execution_config_version), T8 (live fills
never pool with broker-free replays).
"""

from typing import Literal

__status__: Literal["ACTIVE", "CLOSED", "RESEARCH_ONLY", "ARCHIVED"] = "ACTIVE"
