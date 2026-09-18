"""The trade setup of a recovered pre-open name that no brief ever stored.

``pre_open_brief`` holds the names the brief carried at the arrival open on 14 as-of dates
(#1494). A name on that list which a later run dropped is in no brief parquet, so it has no
trade setup, and the ladder replay cannot price a name without one.

This module serves the setup that was rebuilt for those names, once, by
``apps/alphalens-research/scripts/build_pre_open_setups.py``: the production builder over
the frame the pipeline itself cached that day, with ``order_ttl_days`` taken from the
date's own stored setups rather than today's default. The record is committed rather than
rebuilt on each run for three reasons — the geometry then cannot move after an outcome has
been seen, it does not depend on a cache that may be pruned, and it reads in a diff.

That freezing puts a re-added name on the same footing as its date-mates, whose setups are
likewise frozen in the brief parquet and would not necessarily be reproduced by today's
builder either. Each entry carries the ``builder_config_version`` it was built with, so a
later divergence is visible instead of silent.

The reconstruction was checked against every name whose true setup is known: it reproduces
all 61 of the recovered names that a brief still stores, and 866 further names on the dates
that are not being rebuilt, exactly on the close, the ATR and every entry tier. See
``docs/research/pre_open_brief_recovery_2026_09_18.md``.

A date or a ticker this record does not carry answers ``None``, which means "nothing was
rebuilt here" — the caller keeps whatever the brief already says.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any

SETUPS_PATH = Path(__file__).parent / "config" / "pre_open_setups.json"


def _load() -> Mapping[dt.date, Mapping[str, Mapping[str, Any]]]:
    raw = json.loads(SETUPS_PATH.read_text(encoding="utf-8"))
    return MappingProxyType(
        {
            dt.date.fromisoformat(iso): MappingProxyType(
                {str(ticker).upper(): setup for ticker, setup in by_ticker.items()}
            )
            for iso, by_ticker in raw.items()
        }
    )


PRE_OPEN_SETUPS: Mapping[dt.date, Mapping[str, Mapping[str, Any]]] = _load()


def pre_open_setup(brief_date: dt.date, ticker: str) -> dict[str, Any] | None:
    """The frozen setup for ``ticker`` on ``brief_date``, or ``None`` if none was rebuilt.

    Returns a deep copy: the ladder replay reads the setup out of a mutable candidate, and
    the record behind it must stay the record.
    """
    by_ticker = PRE_OPEN_SETUPS.get(brief_date)
    if by_ticker is None:
        return None
    setup = by_ticker.get(ticker.upper())
    if setup is None:
        return None
    return copy.deepcopy(dict(setup))
