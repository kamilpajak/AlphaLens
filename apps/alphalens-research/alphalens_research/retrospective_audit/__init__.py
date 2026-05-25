"""Retrospective replication audit support — PIT universe loaders for one-shot audits.

Hosts ``universe_loaders`` (U1/U2/U3 variants) used by retrospective experiment
scripts under ``apps/alphalens-research/scripts/`` to rebuild PIT universes
from iVol cache + SEC XBRL shares-outstanding facts. Inventory parquet at
``~/.alphalens/ivolatility_smd_inventory.parquet`` is the fast asof index;
rebuild with ``scripts/build_ivol_inventory.py`` before any audit.

Status RESEARCH_ONLY: callers are retrospective scripts only — no live consumer
in pipeline / launchd / CLI. Module survives package retirement of
``alphalens_research.paper_trade`` (retired 2026-05-25) because the loaders
are not paper-trade-specific in concept and remain useful for any future
retrospective on the same iVol cache.
"""

from typing import Literal

__status__: Literal["ACTIVE", "CLOSED", "RESEARCH_ONLY", "ARCHIVED"] = "RESEARCH_ONLY"
