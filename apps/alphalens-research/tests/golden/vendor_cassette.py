"""VCR-style cassette player + recorder for NON-LLM vendor clients (Phase 3b).

The LLM golden replay (``replay_client.py``) keys on the full OpenRouter request
descriptor and re-wraps the recorded payload through the prod ``_wrap_response``.
The upstream thematic stages also hit HTTP vendors whose canonical clients
return plain structures (Polygon news → ``list[dict]``, SEC submissions →
``dict``, SEC document text → ``str``). This module is the equivalent VCR layer
for those: it records each ``(vendor, method, canonical-args) → payload`` pair
and replays it offline, fail-loud on a miss.

Design rules (mirror ``replay_client.py`` §10):

* **Key on the canonical request, not the raw call.** Each vendor method has an
  explicit canonicaliser (below) that pins exactly the args that change the
  response (Polygon: window + ticker + sort; SEC: cik / url) and drops the
  pagination knobs. Record and replay both go through the SAME canonicaliser so
  the key matches byte-for-byte.
* **Fail loud on a miss.** A changed request is a behaviour change to re-record
  deliberately (``record_golden_map.py``), never a silent live call.
* **No secrets.** The cassette stores only the canonical args + the response
  payload. The api-key / ``Authorization: Bearer`` / SEC ``User-Agent`` live on
  the wrapped real client and are never part of args or payload.

One ``VendorCassette`` instance duck-types as BOTH a ``PolygonClient`` and a
``SecEdgarClient`` (the method names don't collide), so a single object can be
injected wherever either client is expected.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class VendorCassetteMissError(KeyError):
    """Raised when a replay request has no recorded vendor cassette (fail-loud)."""


def _iso(value: Any) -> Any:
    """Canonicalise a datetime/date to a stable ISO string; pass through else."""
    if isinstance(value, dt.datetime | dt.date):
        return value.isoformat()
    return value


# --- per-method canonicalisers ------------------------------------------------
# Each pins exactly the request fields that change the response. Record + replay
# both call these, so the key is identical on both sides. Pagination knobs
# (max_items / max_pages) are intentionally excluded — they bound iteration, not
# the logical request, and are deterministic in the calling code anyway.


def _polygon_get_news_range_args(
    *,
    start: Any,
    end: Any,
    ticker: str | None = None,
    order: str = "desc",
    sort: str = "published_utc",
    limit: int = 1000,
    **_ignore: Any,
) -> dict[str, Any]:
    return {
        "start": _iso(start),
        "end": _iso(end),
        "ticker": ticker,
        "order": order,
        "sort": sort,
        "limit": limit,
    }


def _sec_cik_args(cik: str, **_ignore: Any) -> dict[str, Any]:
    return {"cik": str(cik)}


def _sec_get_text_args(url: str, *, encoding: str = "utf-8", **_ignore: Any) -> dict[str, Any]:
    return {"url": url}


def _sec_company_tickers_args(**_ignore: Any) -> dict[str, Any]:
    return {}


# (vendor, method) → (canonicaliser, payload_kind). payload_kind drives nothing
# at replay (the payload is returned as-is) but documents the shape + lets the
# recorder assert text vs json.
_CANON: dict[tuple[str, str], Any] = {
    ("polygon", "get_news_range"): _polygon_get_news_range_args,
    ("sec", "fetch_submissions"): _sec_cik_args,
    ("sec", "fetch_company_facts"): _sec_cik_args,
    ("sec", "get_text"): _sec_get_text_args,
    ("sec", "fetch_company_tickers"): _sec_company_tickers_args,
}

# Method → vendor (method names are unique across the two clients).
_METHOD_VENDOR: dict[str, str] = {
    "get_news_range": "polygon",
    "fetch_submissions": "sec",
    "fetch_company_facts": "sec",
    "get_text": "sec",
    "fetch_company_tickers": "sec",
}


def vendor_key(*, vendor: str, method: str, args: dict[str, Any]) -> str:
    """sha256 over the canonical JSON of (vendor, method, args)."""
    blob = json.dumps(
        {"vendor": vendor, "method": method, "args": args},
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _canon_args(method: str, args: tuple, kwargs: dict) -> tuple[str, dict[str, Any]]:
    vendor = _METHOD_VENDOR[method]
    canon = _CANON[(vendor, method)]
    return vendor, canon(*args, **kwargs)


class VendorCassette:
    """Offline player. Duck-types as both ``PolygonClient`` and ``SecEdgarClient``.

    Loads every ``*.json`` cassette under ``cassette_dir`` at construction and
    serves each recorded method from them. A miss raises
    :class:`VendorCassetteMissError` when ``fail_on_miss`` (the default).
    """

    def __init__(self, cassette_dir: Path | str, *, fail_on_miss: bool = True) -> None:
        self._dir = Path(cassette_dir)
        self._fail_on_miss = fail_on_miss
        self._cache: dict[str, dict[str, Any]] = {}
        for path in sorted(self._dir.glob("*.json")):
            record = json.loads(path.read_text())
            self._cache[record["key"]] = record

    def _serve(self, method: str, args: tuple, kwargs: dict) -> Any:
        vendor, canon_args = _canon_args(method, args, kwargs)
        key = vendor_key(vendor=vendor, method=method, args=canon_args)
        record = self._cache.get(key)
        if record is None:
            if self._fail_on_miss:
                raise VendorCassetteMissError(
                    f"no cassette for vendor={vendor!r} method={method!r} "
                    f"args={canon_args} key={key} in {self._dir} — re-record with "
                    "record_golden_map.py (a changed request is a behaviour change, "
                    "not a live-call fallback)"
                )
            logger.warning("vendor cassette miss (key=%s) — returning None", key)
            return None
        return record["payload"]

    # -- Polygon surface --
    def get_news_range(self, **kwargs: Any) -> list[dict[str, Any]]:
        return self._serve("get_news_range", (), kwargs)

    # -- SEC surface --
    def fetch_submissions(self, cik: str) -> dict[str, Any]:
        return self._serve("fetch_submissions", (cik,), {})

    def fetch_company_facts(self, cik: str) -> dict[str, Any]:
        return self._serve("fetch_company_facts", (cik,), {})

    def fetch_company_tickers(self) -> dict[str, Any]:
        return self._serve("fetch_company_tickers", (), {})

    def get_text(self, url: str, *, encoding: str = "utf-8") -> str:
        return self._serve("get_text", (url,), {"encoding": encoding})

    def __len__(self) -> int:
        return len(self._cache)


class RecordingVendor:
    """Wrap a real Polygon/SEC client and tee every recorded method to a cassette.

    Used once by ``record_golden_map.py`` against the live APIs. Each call is
    forwarded to the real client, then the (canonical-args → payload) pair is
    written to ``cassette_dir/{key}.json``. Stores the human-readable args
    alongside the key so a reviewer can see what was recorded.
    """

    def __init__(self, real_client: Any, cassette_dir: Path | str) -> None:
        self._real = real_client
        self._dir = Path(cassette_dir)
        self._dir.mkdir(parents=True, exist_ok=True)

    def _record(self, method: str, args: tuple, kwargs: dict) -> Any:
        vendor, canon_args = _canon_args(method, args, kwargs)
        payload = getattr(self._real, method)(*args, **kwargs)
        key = vendor_key(vendor=vendor, method=method, args=canon_args)
        payload_kind = "text" if isinstance(payload, str) else "json"
        record = {
            "key": key,
            "vendor": vendor,
            "method": method,
            "args": canon_args,
            "payload_kind": payload_kind,
            "payload": payload,
        }
        (self._dir / f"{key}.json").write_text(
            json.dumps(record, indent=2, sort_keys=True, ensure_ascii=False, default=str)
        )
        return payload

    def get_news_range(self, **kwargs: Any) -> list[dict[str, Any]]:
        return self._record("get_news_range", (), kwargs)

    def fetch_submissions(self, cik: str) -> dict[str, Any]:
        return self._record("fetch_submissions", (cik,), {})

    def fetch_company_facts(self, cik: str) -> dict[str, Any]:
        return self._record("fetch_company_facts", (cik,), {})

    def fetch_company_tickers(self) -> dict[str, Any]:
        return self._record("fetch_company_tickers", (), {})

    def get_text(self, url: str, *, encoding: str = "utf-8") -> str:
        return self._record("get_text", (url,), {"encoding": encoding})


__all__ = [
    "RecordingVendor",
    "VendorCassette",
    "VendorCassetteMissError",
    "vendor_key",
]
