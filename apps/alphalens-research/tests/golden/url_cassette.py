"""URL-keyed cassettes for the two ingest sources with no vendor client (Phase 3b).

``VendorCassette`` (``vendor_cassette.py``) keys on ``(vendor, method, args)`` and
fits the canonical Polygon / SEC clients. The GDELT and RSS sources have no such
client — they hit a module-level helper keyed purely on the URL:

* GDELT  → ``gdelt._http_get_json(url, ...)`` returns a JSON dict.
* RSS    → ``rss._parse_feed(url)`` returns a feedparser result (attribute-access
  ``.entries`` / ``.bozo`` / per-entry ``.link/.title/.summary/
  .published_parsed/.updated_parsed``).

Bolting these into ``VendorCassette``'s method registry would force fake method
names + a URL-only canonicaliser that contradicts its "pin the request fields"
doctrine, so they get their own tiny URL-keyed players here. Same discipline:
fail-loud on a miss, no secrets (these endpoints are keyless), record + replay
key identically on the (deterministic) URL.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

logger = logging.getLogger(__name__)


class UrlCassetteMissError(KeyError):
    """Raised when a replay URL has no recorded cassette (fail-loud)."""


# --- GDELT: url -> JSON dict --------------------------------------------------


class UrlJsonCassette:
    """Offline replacement for ``gdelt._http_get_json``; serves frozen JSON by URL."""

    def __init__(self, store_path: Path | str, *, fail_on_miss: bool = True) -> None:
        self._fail_on_miss = fail_on_miss
        path = Path(store_path)
        self._store: dict[str, Any] = json.loads(path.read_text()) if path.exists() else {}

    def __call__(self, url: str, **_kwargs: Any) -> dict[str, Any]:
        if url in self._store:
            return self._store[url]
        if self._fail_on_miss:
            raise UrlCassetteMissError(
                f"no GDELT cassette for url={url!r} — re-record with "
                "record_golden_ingest.py (a changed query is a behaviour change, "
                "not a live-call fallback)"
            )
        logger.warning("GDELT url cassette miss (%s) — returning empty", url)
        return {}


class RecordingUrlJson:
    """Wrap the real ``_http_get_json`` and tee each (url → JSON) to a single store."""

    def __init__(self, real_fn: Any, store_path: Path | str) -> None:
        self._real = real_fn
        self._path = Path(store_path)
        self._store: dict[str, Any] = {}

    def __call__(self, url: str, **kwargs: Any) -> dict[str, Any]:
        resp = self._real(url, **kwargs)
        self._store[url] = resp
        # One-shot recorder: rewrites the whole store each call — fine at the
        # handful of buckets a capture queries.
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self._store, indent=2, sort_keys=True, ensure_ascii=False))
        return resp


# --- RSS: url -> frozen feedparser result -------------------------------------


def _struct_to_list(value: time.struct_time | None) -> list[int] | None:
    return list(value) if value is not None else None


def _list_to_struct(value: list[int] | None) -> time.struct_time | None:
    # rss._entry_timestamp uses only parsed[:6] + forces tzinfo=UTC, so the
    # 9-int round-trip is exact for the ingest path.
    return time.struct_time(tuple(value)) if value else None


def _freeze_feed(result: Any) -> dict[str, Any]:
    entries = []
    for e in getattr(result, "entries", []) or []:
        entries.append(
            {
                "link": getattr(e, "link", None),
                "title": getattr(e, "title", None),
                "summary": getattr(e, "summary", None),
                "published_parsed": _struct_to_list(getattr(e, "published_parsed", None)),
                "updated_parsed": _struct_to_list(getattr(e, "updated_parsed", None)),
            }
        )
    return {"bozo": int(getattr(result, "bozo", 0) or 0), "entries": entries}


def _rehydrate_feed(record: dict[str, Any]) -> SimpleNamespace:
    entries = [
        SimpleNamespace(
            link=e.get("link"),
            title=e.get("title"),
            summary=e.get("summary"),
            published_parsed=_list_to_struct(e.get("published_parsed")),
            updated_parsed=_list_to_struct(e.get("updated_parsed")),
        )
        for e in record.get("entries", [])
    ]
    # bozo_exception is only logged by rss.fetch_feed, never inspected for
    # content, so dropping it (replay sets None) is intentional + safe. If a
    # future consumer reads it, re-record to capture it.
    return SimpleNamespace(bozo=record.get("bozo", 0), bozo_exception=None, entries=entries)


class FeedCassette:
    """Offline replacement for ``rss._parse_feed``; rebuilds a feedparser-like object."""

    def __init__(self, store_path: Path | str, *, fail_on_miss: bool = True) -> None:
        self._fail_on_miss = fail_on_miss
        path = Path(store_path)
        self._store: dict[str, Any] = json.loads(path.read_text()) if path.exists() else {}

    def __call__(self, url: str) -> SimpleNamespace:
        record = self._store.get(url)
        if record is None:
            if self._fail_on_miss:
                raise UrlCassetteMissError(
                    f"no RSS cassette for url={url!r} — re-record with record_golden_ingest.py"
                )
            logger.warning("RSS feed cassette miss (%s) — returning empty", url)
            return SimpleNamespace(bozo=1, bozo_exception=None, entries=[])
        return _rehydrate_feed(record)


class RecordingFeed:
    """Wrap the real ``_parse_feed`` and tee each (url → frozen entries) to a store."""

    def __init__(self, real_fn: Any, store_path: Path | str) -> None:
        self._real = real_fn
        self._path = Path(store_path)
        self._store: dict[str, Any] = {}

    def __call__(self, url: str) -> Any:
        result = self._real(url)
        self._store[url] = _freeze_feed(result)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self._store, indent=2, sort_keys=True, ensure_ascii=False))
        return result


__all__ = [
    "FeedCassette",
    "RecordingFeed",
    "RecordingUrlJson",
    "UrlCassetteMissError",
    "UrlJsonCassette",
]
