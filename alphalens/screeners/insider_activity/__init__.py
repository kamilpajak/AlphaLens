"""Insider-activity screeners — Form-4 informed-trader-flow signals.

Pre-registered as ``insider_form4_opportunistic_2026_05_05`` in signal class
``insider_form4_opportunistic_2026_05_05`` (see
``docs/research/preregistration/params_insider_form4_opportunistic_2026_05_05.json``).

Distinct from archived ``alphalens.archive.screeners.insider`` (cluster
detection scorer, CLOSED 2026-04-24, Carhart αt=2.14 IS → 0.68 OOS overfit).
This package implements the Cohen-Malloy-Pomorski 2012 (JFE) routine vs
opportunistic insider classification — orthogonal mechanism, faithful paper
replication per p. 1786 Section III.A.
"""

from typing import Literal

__status__: Literal["ACTIVE", "CLOSED", "RESEARCH_ONLY", "ARCHIVED"] = "RESEARCH_ONLY"
