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

import datetime as dt
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Any

SETUPS_PATH = Path(__file__).parent / "config" / "pre_open_setups.json"


def _freeze(value: Any) -> Any:
    """Make a decoded JSON value read-only, all the way down.

    A ``MappingProxyType`` over the top level is not enough: it leaves the setup dicts and
    their ``entry_tiers`` / ``tp_tranches`` lists writable, so a caller reading the record
    directly could change a level in place and every later reader in that process would see
    the changed one. The record is evidence; it has to be unwritable.
    """
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    """The plain, writable mirror of a frozen value.

    The ladder replay reads the setup out of a candidate and the codecs expect ordinary
    dicts and lists, so the accessor hands back a copy in that shape rather than the
    read-only view.
    """
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return [_thaw(item) for item in value]
    return value


def _load() -> Mapping[dt.date, Mapping[str, Mapping[str, Any]]]:
    raw = json.loads(SETUPS_PATH.read_text(encoding="utf-8"))
    return MappingProxyType(
        {
            dt.date.fromisoformat(iso): MappingProxyType(
                {str(ticker).upper(): _freeze(setup) for ticker, setup in by_ticker.items()}
            )
            for iso, by_ticker in raw.items()
        }
    )


PRE_OPEN_SETUPS: Mapping[dt.date, Mapping[str, Mapping[str, Any]]] = _load()


def pre_open_setup(
    brief_date: dt.date,
    ticker: str,
    *,
    record: Mapping[dt.date, Mapping[str, Mapping[str, Any]]] | None = None,
) -> dict[str, Any] | None:
    """The frozen setup for ``ticker`` on ``brief_date``, or ``None`` if none was rebuilt.

    Returns a writable copy in ordinary containers, so what the caller does with it cannot
    reach the record and the ladder codecs get the shape they expect.

    ``record`` substitutes the committed one. It exists so a caller can ask what happens to
    a name the record cannot serve, and it goes through the same thawing path — reading a
    substitute by hand is how a caller ends up holding read-only views.
    """
    source = PRE_OPEN_SETUPS if record is None else record
    by_ticker = source.get(brief_date)
    if by_ticker is None:
        return None
    setup = by_ticker.get(ticker.upper())
    if setup is None:
        return None
    return _thaw(setup)
