"""Periodic literature review — monthly deep + weekly RSS scan via Perplexity.

Output:
- Monthly: ``docs/research/literature_review/YYYY-MM.md`` + Telegram digest.
- Weekly:  ``docs/research/literature_review/weekly/YYYY-Www.md`` + Telegram digest.

Driven by the VPS systemd units in ``deploy/systemd/`` (``alphalens-literature-scan-{weekly,monthly}``);
manual invocation via ``alphalens literature monthly|weekly``.
"""

from typing import Literal

__all__ = ["__status__"]

__status__: Literal["ACTIVE", "CLOSED", "RESEARCH_ONLY", "ARCHIVED"] = "ACTIVE"
