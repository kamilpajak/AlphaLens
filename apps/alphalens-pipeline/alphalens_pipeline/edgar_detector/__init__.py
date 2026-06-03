"""Layer 1 SEC EDGAR detector (live on VPS systemd)."""

from typing import Literal

from .types import Event, FormType

__all__ = ["Event", "FormType"]

__status__: Literal["ACTIVE", "CLOSED", "RESEARCH_ONLY", "ARCHIVED"] = "ACTIVE"
