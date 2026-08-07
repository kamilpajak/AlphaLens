# Saxo LIVE Price Feed (INC-2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the live-market exit engine a real-time Saxo LIVE price with an honest freshness gate, so that no stale or unverifiable price can ever trigger a market order.

**Architecture:** Three components with one responsibility each. A `SaxoMarketDataClient` (the only place LIVE URLs exist, outside the `brokers/` package because the SIM-only rail forbids LIVE URL strings there) owns HTTP: session capability, instrument resolution, subscription lifecycle. A `SaxoPriceStream` owns one long-lived WebSocket thread: connect, merge deltas, hold a per-uic quote cache, reconnect. A `SaxoLivePriceFeed` adapter implements the existing `PriceFeed` Protocol by reading that cache through a freshness gate. The `PricePoint` contract is reshaped so an unknown quote age is expressible as `None` and is auto-vetoed.

**Tech Stack:** Python 3.13, `unittest` (NOT pytest), `requests`, `websockets` 16, `broker_contract` dependency-free leaf, `alphalens_pipeline`.

## Global Constraints

- **Tests are `unittest.TestCase` subclasses.** pytest-style bare functions are silently skipped by this repo's CI discovery. Never write them.
- **TDD, always.** Red → green → refactor, even for two-line changes. Write the failing test first and run it to see it fail.
- **English only** in code, comments, docstrings and identifiers. Math notation is fine.
- **No backward compatibility.** Solo project, zero external users. Rename and delete in the same commit; never add aliases or shims.
- **Conventional Commits** (`type(scope): description`). Never mention AI assistance in a commit message.
- **The SIM-only rail (ADR 0014) is untouchable.** No LIVE URL string may appear anywhere under `apps/alphalens-pipeline/alphalens_pipeline/brokers/`. `SaxoClient` and `SaxoAuthClient` are not modified by this plan.
- **Every doubt returns `None`.** No code path in this feature may guess, synthesize, or extrapolate a price.
- **Test command** (from repo root): `.venv/bin/python -m unittest discover -s apps/alphalens-research/tests -t apps/alphalens-research -k <pattern> -v`
- **Full suite** (from repo root): `.venv/bin/python -m unittest discover -s apps/alphalens-research/tests -t apps/alphalens-research`
- **Env var names:** `SAXO_LIVE_APP_KEY`, `SAXO_LIVE_APP_SECRET`, `SAXO_LIVE_AUTH_REDIRECT_URL`, `SAXO_LIVE_TOKEN_STORE_PATH`, `ALPHALENS_SAXO_LIVE_PRICES`.
- **LIVE endpoints** (constants, never env-configurable): auth `https://live.logonvalidation.net`, REST `https://gateway.saxobank.com/openapi`, WS `wss://live-streaming.saxobank.com/oapi/streaming/ws/connect`.
- **Measured facts this plan relies on:** the OAuth code exchange and the subscription POST both answer **HTTP 201**, the capability PATCH answers **202**, the `RefreshRate` floor is **1000 ms**, price messages are **deltas** (absent field = unchanged), and event lag is **0.1–1.4 s**.

---

## File Structure

| File | Responsibility |
|---|---|
| `apps/alphalens-broker-contract/broker_contract/price_feed.py` | MODIFY. `PricePoint` (two-sided, optional event time) + `PriceFeed` Protocol + the pure `is_fresh` predicate. Dependency-free. |
| `apps/alphalens-pipeline/alphalens_pipeline/brokers/automanager/live_exit_engine.py` | MODIFY (1 line). Feed the planner `point.bid`. |
| `apps/alphalens-pipeline/alphalens_pipeline/brokers/automanager/yfinance_price_feed.py` | MODIFY. New contract, `event_time=None`, structurally vetoed. |
| `apps/alphalens-pipeline/alphalens_pipeline/data/alt_data/saxo_marketdata_client.py` | CREATE. LIVE HTTP: capabilities, instruments, subscriptions. Only file with LIVE REST URLs. |
| `apps/alphalens-pipeline/alphalens_pipeline/data/alt_data/saxo_marketdata_auth.py` | CREATE. LIVE OAuth: authorize URL, code exchange, refresh, token store. Only file with the LIVE auth URL. |
| `apps/alphalens-pipeline/alphalens_pipeline/data/alt_data/saxo_price_stream.py` | CREATE. WebSocket thread, delta merge, quote cache, reconnect, reclaim. Only file with the LIVE WS URL. |
| `apps/alphalens-pipeline/alphalens_pipeline/brokers/automanager/saxo_live_price_feed.py` | CREATE. `PriceFeed` adapter over the cache. Contains no URLs. |
| `apps/alphalens-pipeline/alphalens_pipeline/brokers/automanager/control_loop.py` | MODIFY. Feed factory selects Saxo behind `ALPHALENS_SAXO_LIVE_PRICES`. |
| `apps/alphalens-research/tests/...` | Unit tests per component + one opt-in live probe. |
| `apps/alphalens-research/tests/test_no_raw_saxo_http.py` | MODIFY. Register the two new canonical LIVE HTTP surfaces. |

---

### Task 1: Reshape the `PricePoint` contract

**Files:**
- Modify: `apps/alphalens-broker-contract/broker_contract/price_feed.py`
- Modify: `apps/alphalens-pipeline/alphalens_pipeline/brokers/automanager/live_exit_engine.py:170`
- Modify: `apps/alphalens-pipeline/alphalens_pipeline/brokers/automanager/yfinance_price_feed.py`
- Modify: `apps/alphalens-research/tests/brokers/test_price_feed.py`
- Modify: `apps/alphalens-research/tests/brokers/automanager/test_run_live_exits.py:22`
- Modify: `apps/alphalens-research/tests/brokers/automanager/acceptance/world.py:67`
- Modify: `apps/alphalens-research/tests/brokers/automanager/test_yfinance_price_feed.py`
- Modify: `apps/alphalens-research/tests/live/test_yfinance_price_feed_live.py`

**Interfaces:**
- Produces: `PricePoint(uic: int, bid: float, ask: float, event_time: datetime | None, received_at: datetime, source: str)`; `is_fresh(point: PricePoint, *, now: datetime, max_age_s: float = 3.0, max_relative_spread: float = 0.02) -> bool`; `PriceFeed.latest(uic: int) -> PricePoint | None`. Every later task consumes these exact names.
- The old fields `price` and `asof` cease to exist.

- [ ] **Step 1: Write the failing contract test**

Replace the whole body of `apps/alphalens-research/tests/brokers/test_price_feed.py`:

```python
from __future__ import annotations

import datetime as dt
import unittest

from broker_contract.price_feed import PriceFeed, PricePoint, is_fresh

_NOW = dt.datetime(2026, 8, 7, 14, 0, 0, tzinfo=dt.UTC)


def _point(**over) -> PricePoint:
    base = dict(
        uic=211,
        bid=314.01,
        ask=314.04,
        event_time=_NOW - dt.timedelta(seconds=1),
        received_at=_NOW,
        source="saxo-live-l1",
    )
    base.update(over)
    return PricePoint(**base)


class TestPricePoint(unittest.TestCase):
    def test_carries_both_sides_and_is_frozen(self):
        p = _point()
        self.assertEqual(p.bid, 314.01)
        self.assertEqual(p.ask, 314.04)
        with self.assertRaises(Exception):
            p.bid = 1.0

    def test_has_no_fabricated_single_price(self):
        """`price`/`asof` are GONE: a single number with a synthesized stamp is
        exactly the false-freshness bug this contract change removes."""
        p = _point()
        self.assertFalse(hasattr(p, "price"))
        self.assertFalse(hasattr(p, "asof"))


class TestIsFresh(unittest.TestCase):
    def test_fresh_quote_passes(self):
        self.assertTrue(is_fresh(_point(), now=_NOW))

    def test_unknown_event_time_is_vetoed(self):
        """A source that publishes no tick time can never be fresh. This is the
        structural ban on stamping fetch time as quote time."""
        self.assertFalse(is_fresh(_point(event_time=None), now=_NOW))

    def test_too_old_is_vetoed(self):
        old = _point(event_time=_NOW - dt.timedelta(seconds=3.5))
        self.assertFalse(is_fresh(old, now=_NOW))

    def test_boundary_age_passes(self):
        edge = _point(event_time=_NOW - dt.timedelta(seconds=3.0))
        self.assertTrue(is_fresh(edge, now=_NOW))

    def test_future_event_time_is_vetoed(self):
        """Clock skew must not read as extra freshness."""
        future = _point(event_time=_NOW + dt.timedelta(seconds=5))
        self.assertFalse(is_fresh(future, now=_NOW))

    def test_crossed_market_is_vetoed(self):
        self.assertFalse(is_fresh(_point(bid=314.10, ask=314.00), now=_NOW))

    def test_non_positive_or_non_finite_is_vetoed(self):
        self.assertFalse(is_fresh(_point(bid=0.0), now=_NOW))
        self.assertFalse(is_fresh(_point(ask=float("nan")), now=_NOW))
        self.assertFalse(is_fresh(_point(ask=float("inf")), now=_NOW))

    def test_absurd_relative_spread_is_vetoed(self):
        wide = _point(bid=100.0, ask=103.0)  # 3% > the 2% ceiling
        self.assertFalse(is_fresh(wide, now=_NOW))

    def test_normal_spread_passes(self):
        ok = _point(bid=100.0, ask=100.5)  # 0.5%
        self.assertTrue(is_fresh(ok, now=_NOW))


class TestPriceFeedProtocol(unittest.TestCase):
    def test_protocol_runtime_checkable(self):
        class _F:
            def latest(self, uic):
                return None

        self.assertIsInstance(_F(), PriceFeed)

        class _N:
            pass

        self.assertNotIsInstance(_N(), PriceFeed)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it and watch it fail**

```bash
.venv/bin/python -m unittest discover -s apps/alphalens-research/tests -t apps/alphalens-research -k test_price_feed -v
```

Expected: `ImportError: cannot import name 'is_fresh'`.

- [ ] **Step 3: Rewrite the contract**

Replace `apps/alphalens-broker-contract/broker_contract/price_feed.py` entirely:

```python
"""Broker-agnostic live price feed — the trigger source for live-market E/TP.

Dependency-free leaf. ``latest(uic)`` returns ``None`` when there is no
TRUSTWORTHY price (disconnect, staleness, halt, unknown age) — the engine treats
``None`` as "do not fire" (the stream-health veto).

``event_time`` is deliberately optional. A source that publishes no tick
timestamp reports ``None``, and :func:`is_fresh` vetoes it. Stamping local fetch
time into ``event_time`` is therefore not merely discouraged — the honest
alternative is expressible, so the dishonest one has no excuse. ``received_at``
records local arrival for diagnostics and MUST NEVER be used to compute age.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

DEFAULT_MAX_AGE_S = 3.0
"""Roughly twice the worst event lag measured on the Saxo LIVE stream (1.4 s),
and still detects a dead 1 Hz push within seconds."""

DEFAULT_MAX_RELATIVE_SPREAD = 0.02
"""(ask-bid)/mid ceiling. Liquid US names measured 0.003-0.03%, so 2% catches a
broken quote without vetoing a normal one. Relative because this project has no
per-instrument spread table and inventing one is not worth the upkeep."""


@dataclass(frozen=True)
class PricePoint:
    uic: int
    bid: float
    ask: float
    event_time: dt.datetime | None  # UTC, from the PROVIDER; None = not published
    received_at: dt.datetime  # UTC, local arrival — diagnostics only
    source: str  # e.g. "saxo-live-l1", "yfinance-last"

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0


def is_fresh(
    point: PricePoint,
    *,
    now: dt.datetime,
    max_age_s: float = DEFAULT_MAX_AGE_S,
    max_relative_spread: float = DEFAULT_MAX_RELATIVE_SPREAD,
) -> bool:
    """Pure predicate: may this quote drive an order decision?

    Vetoes an unknown or future ``event_time`` (clock skew must not read as
    freshness), a non-finite or non-positive side, a crossed market, and an
    absurd relative spread.
    """
    if point.event_time is None:
        return False
    if not (math.isfinite(point.bid) and math.isfinite(point.ask)):
        return False
    if point.bid <= 0.0 or point.ask <= 0.0:
        return False
    if point.bid > point.ask:
        return False
    age = (now - point.event_time).total_seconds()
    if age < 0.0 or age > max_age_s:
        return False
    mid = point.mid
    if mid <= 0.0:
        return False
    return (point.ask - point.bid) / mid <= max_relative_spread


@runtime_checkable
class PriceFeed(Protocol):
    def latest(self, uic: int) -> PricePoint | None: ...
```

- [ ] **Step 4: Run the contract test — expect PASS**

```bash
.venv/bin/python -m unittest discover -s apps/alphalens-research/tests -t apps/alphalens-research -k test_price_feed -v
```

- [ ] **Step 5: Update the engine to use the bid**

In `live_exit_engine.py`, inside `run_live_exits`, change the planner call:

```python
        exits = plan_tranche_exits(
            price=point.bid,  # selling a long: the executable side is the BID
            tp_tranches=m.tp_tranches,
            reference_qty=m.reference_qty,
            owned=live.quantity,
            already_fired=m.already_fired,
        )
```

`plan_tranche_exits` itself stays an unchanged pure scalar function.

- [ ] **Step 6: Update `YfinancePriceFeed` to the new contract**

In `yfinance_price_feed.py`, replace the `return` at the end of `latest` and drop the `clock` usage for `asof`:

```python
        now = self._clock()
        return PricePoint(
            uic=uic,
            bid=float(price),
            ask=float(price),
            # fast_info.last_price publishes NO tick timestamp. Reporting None
            # (rather than stamping `now`) makes this feed structurally unable
            # to pass is_fresh, which is the correct outcome: it is a last trade
            # of unverifiable age, not an executable quote.
            event_time=None,
            received_at=now,
            source="yfinance-last",
        )
```

Update the class docstring: state that it is UNWIRED, that `event_time` is
always `None`, and that `is_fresh` therefore always vetoes it.

- [ ] **Step 7: Update the three remaining fake-feed construction sites**

`tests/brokers/automanager/test_run_live_exits.py:22` and
`tests/brokers/automanager/acceptance/world.py:67` build `PricePoint(uic=..., price=..., asof=...)`.
Change each to the new shape, keeping the same numeric value on both sides so
existing expectations hold:

```python
PricePoint(
    uic=uic,
    bid=px,
    ask=px,
    event_time=dt.datetime(2026, 8, 5, tzinfo=dt.UTC),
    received_at=dt.datetime(2026, 8, 5, tzinfo=dt.UTC),
    source="test",
)
```

In `tests/brokers/automanager/test_yfinance_price_feed.py` and
`tests/live/test_yfinance_price_feed_live.py`, replace assertions on
`.price`/`.asof` with `.bid`/`.ask` and add, in the unit test:

```python
    def test_event_time_is_none_so_the_point_can_never_be_fresh(self):
        feed = YfinancePriceFeed(resolve_ticker={211: "AAPL"}.get, yf_client=_FakeYf(314.0))
        point = feed.latest(211)
        self.assertIsNotNone(point)
        self.assertIsNone(point.event_time)
        self.assertFalse(is_fresh(point, now=dt.datetime.now(dt.UTC)))
```

(Import `is_fresh` from `broker_contract.price_feed`; `_FakeYf` is the fake
already used in that file — reuse it, do not invent a new one.)

- [ ] **Step 8: Run the full suite — everything green**

```bash
.venv/bin/python -m unittest discover -s apps/alphalens-research/tests -t apps/alphalens-research
```

- [ ] **Step 9: Commit**

```bash
git add apps/alphalens-broker-contract apps/alphalens-pipeline apps/alphalens-research
git commit -s -m "feat(broker-contract): two-sided PricePoint with optional event time

A quote now carries bid and ask, the provider's event time (None when the
source publishes none) and local receipt time separately. is_fresh vetoes an
unknown or future event time, a crossed or non-finite quote and an absurd
relative spread, so a source without tick timestamps cannot drive an order.
The long-exit planner now reads the bid."
```

---

### Task 2: LIVE OAuth + token store

**Files:**
- Create: `apps/alphalens-pipeline/alphalens_pipeline/data/alt_data/saxo_marketdata_auth.py`
- Test: `apps/alphalens-research/tests/data/test_saxo_marketdata_auth.py`

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces: `LiveAuthConfig.from_env() -> LiveAuthConfig` (fields `app_key`, `app_secret`, `redirect_url`, `store_path`); `build_authorize_url(cfg, state) -> str`; `exchange_code(cfg, code) -> dict`; `refresh(cfg, refresh_token) -> dict`; `LiveTokenProvider(cfg)` with `.access_token() -> str` (refreshes when within `_REFRESH_MARGIN_S` of expiry) and `.force_refresh() -> str`. Tasks 3–6 consume `LiveTokenProvider`.

- [ ] **Step 1: Write the failing test**

Create `apps/alphalens-research/tests/data/test_saxo_marketdata_auth.py`:

```python
from __future__ import annotations

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from alphalens_pipeline.data.alt_data.saxo_marketdata_auth import (
    LIVE_AUTH_BASE_URL,
    LiveAuthConfig,
    LiveTokenProvider,
    build_authorize_url,
    exchange_code,
)


def _cfg(tmp: Path) -> LiveAuthConfig:
    return LiveAuthConfig(
        app_key="key123",
        app_secret="secret456",
        redirect_url="http://localhost:8765/callback",
        store_path=tmp / "token_store.json",
    )


class _Resp:
    def __init__(self, status: int, payload: dict):
        self.status_code = status
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class TestAuthorizeUrl(unittest.TestCase):
    def test_targets_the_live_host_and_carries_state(self):
        with tempfile.TemporaryDirectory() as d:
            url = build_authorize_url(_cfg(Path(d)), state="st8")
        self.assertTrue(url.startswith(f"{LIVE_AUTH_BASE_URL}/authorize?"))
        self.assertIn("client_id=key123", url)
        self.assertIn("state=st8", url)
        self.assertIn("response_type=code", url)


class TestExchangeCode(unittest.TestCase):
    def test_accepts_http_201(self):
        """Saxo answers the code exchange with 201, not 200. A `== 200` check
        reads success as failure."""
        payload = {"access_token": "at", "refresh_token": "rt", "expires_in": 1200}
        with tempfile.TemporaryDirectory() as d, mock.patch(
            "alphalens_pipeline.data.alt_data.saxo_marketdata_auth.requests.post",
            return_value=_Resp(201, payload),
        ):
            got = exchange_code(_cfg(Path(d)), code="abc")
        self.assertEqual(got["access_token"], "at")

    def test_raises_without_leaking_the_body(self):
        """On failure the body may still contain a token; only the code and the
        error description may surface."""
        payload = {"error": "invalid_grant", "error_description": "bad code", "access_token": "SECRET"}
        with tempfile.TemporaryDirectory() as d, mock.patch(
            "alphalens_pipeline.data.alt_data.saxo_marketdata_auth.requests.post",
            return_value=_Resp(400, payload),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                exchange_code(_cfg(Path(d)), code="abc")
        self.assertNotIn("SECRET", str(ctx.exception))
        self.assertIn("bad code", str(ctx.exception))


class TestLiveTokenProvider(unittest.TestCase):
    def test_returns_stored_token_while_valid(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = _cfg(Path(d))
            expiry = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=900)
            cfg.store_path.write_text(
                json.dumps({"access_token": "live-tok", "refresh_token": "rt", "expires_at": expiry.isoformat()})
            )
            self.assertEqual(LiveTokenProvider(cfg).access_token(), "live-tok")

    def test_refreshes_when_close_to_expiry_and_persists_rotation(self):
        """The refresh token is single-use; the replacement must land on disk or
        the session is lost."""
        with tempfile.TemporaryDirectory() as d:
            cfg = _cfg(Path(d))
            expiry = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=30)
            cfg.store_path.write_text(
                json.dumps({"access_token": "old", "refresh_token": "rt-old", "expires_at": expiry.isoformat()})
            )
            payload = {"access_token": "new", "refresh_token": "rt-new", "expires_in": 1200}
            with mock.patch(
                "alphalens_pipeline.data.alt_data.saxo_marketdata_auth.requests.post",
                return_value=_Resp(201, payload),
            ):
                self.assertEqual(LiveTokenProvider(cfg).access_token(), "new")
            on_disk = json.loads(cfg.store_path.read_text())
            self.assertEqual(on_disk["refresh_token"], "rt-new")

    def test_store_is_written_0600(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = _cfg(Path(d))
            payload = {"access_token": "at", "refresh_token": "rt", "expires_in": 1200}
            with mock.patch(
                "alphalens_pipeline.data.alt_data.saxo_marketdata_auth.requests.post",
                return_value=_Resp(201, payload),
            ):
                exchange_code(cfg, code="abc")
            self.assertEqual(cfg.store_path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it and watch it fail**

```bash
.venv/bin/python -m unittest discover -s apps/alphalens-research/tests -t apps/alphalens-research -k test_saxo_marketdata_auth -v
```

Expected: `ModuleNotFoundError: ... saxo_marketdata_auth`.

- [ ] **Step 3: Implement the module**

Create `apps/alphalens-pipeline/alphalens_pipeline/data/alt_data/saxo_marketdata_auth.py`:

```python
"""Saxo LIVE OAuth for MARKET DATA ONLY (app `bracket-keeper`).

Deliberately separate from ``brokers/saxo/oauth.py``: that module carries the
SIM-only rail (ADR 0014) and must keep refusing LIVE hosts. This one is the only
file in the tree holding the LIVE auth host, and it never places an order.

The token store is a SEPARATE file from the SIM store. The refresh token is
single-use and rotates on every refresh, so exactly one process may hold a given
store; two holders invalidate each other.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import urllib.parse
from base64 import b64encode
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

LIVE_AUTH_BASE_URL = "https://live.logonvalidation.net"

_APP_KEY_ENV = "SAXO_LIVE_APP_KEY"
_APP_SECRET_ENV = "SAXO_LIVE_APP_SECRET"
_REDIRECT_ENV = "SAXO_LIVE_AUTH_REDIRECT_URL"
_STORE_ENV = "SAXO_LIVE_TOKEN_STORE_PATH"

_DEFAULT_STORE = Path.home() / ".alphalens" / "saxo_auth_live" / "token_store.json"
_REFRESH_MARGIN_S = 120.0
_TIMEOUT_S = 30.0


@dataclass(frozen=True)
class LiveAuthConfig:
    app_key: str
    app_secret: str
    redirect_url: str
    store_path: Path

    @classmethod
    def from_env(cls) -> LiveAuthConfig:
        missing = [k for k in (_APP_KEY_ENV, _APP_SECRET_ENV, _REDIRECT_ENV) if not os.environ.get(k)]
        if missing:
            raise RuntimeError(f"missing LIVE market-data env: {', '.join(missing)}")
        store = os.environ.get(_STORE_ENV)
        return cls(
            app_key=os.environ[_APP_KEY_ENV],
            app_secret=os.environ[_APP_SECRET_ENV],
            redirect_url=os.environ[_REDIRECT_ENV],
            store_path=Path(store) if store else _DEFAULT_STORE,
        )


def _basic(cfg: LiveAuthConfig) -> str:
    return "Basic " + b64encode(f"{cfg.app_key}:{cfg.app_secret}".encode()).decode()


def build_authorize_url(cfg: LiveAuthConfig, state: str) -> str:
    query = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": cfg.app_key,
            "state": state,
            "redirect_uri": cfg.redirect_url,
        }
    )
    return f"{LIVE_AUTH_BASE_URL}/authorize?{query}"


def _post_token(cfg: LiveAuthConfig, data: dict[str, str]) -> dict[str, Any]:
    resp = requests.post(
        f"{LIVE_AUTH_BASE_URL}/token",
        headers={"Authorization": _basic(cfg)},
        data=data,
        timeout=_TIMEOUT_S,
    )
    # Saxo answers 201 Created on success. NEVER echo the body: it carries the
    # bearer token itself.
    if not (200 <= resp.status_code < 300):
        try:
            payload = resp.json()
            detail = payload.get("error_description") or payload.get("error") or "(no error field)"
        except ValueError:
            detail = "(non-JSON body, redacted)"
        raise RuntimeError(f"Saxo LIVE token endpoint failed: HTTP {resp.status_code} - {detail}")
    bundle = resp.json()
    _save(cfg, bundle)
    return bundle


def exchange_code(cfg: LiveAuthConfig, *, code: str) -> dict[str, Any]:
    return _post_token(
        cfg,
        {"grant_type": "authorization_code", "code": code, "redirect_uri": cfg.redirect_url},
    )


def refresh(cfg: LiveAuthConfig, *, refresh_token: str) -> dict[str, Any]:
    return _post_token(
        cfg,
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "redirect_uri": cfg.redirect_url,
        },
    )


def _save(cfg: LiveAuthConfig, bundle: dict[str, Any]) -> None:
    expires_at = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=int(bundle.get("expires_in", 1200)))
    cfg.store_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.store_path.write_text(
        json.dumps(
            {
                "access_token": bundle["access_token"],
                "refresh_token": bundle["refresh_token"],
                "expires_at": expires_at.isoformat(),
            },
            indent=2,
        )
    )
    cfg.store_path.chmod(0o600)


class LiveTokenProvider:
    """Reads the store, refreshing when the access token is near expiry."""

    def __init__(self, cfg: LiveAuthConfig):
        self._cfg = cfg

    def _load(self) -> dict[str, Any]:
        if not self._cfg.store_path.is_file():
            raise RuntimeError(
                f"no LIVE token store at {self._cfg.store_path} - run the market-data auth bootstrap"
            )
        return json.loads(self._cfg.store_path.read_text())

    def access_token(self) -> str:
        state = self._load()
        expires_at = dt.datetime.fromisoformat(state["expires_at"])
        remaining = (expires_at - dt.datetime.now(dt.UTC)).total_seconds()
        if remaining > _REFRESH_MARGIN_S:
            return state["access_token"]
        return refresh(self._cfg, refresh_token=state["refresh_token"])["access_token"]

    def force_refresh(self) -> str:
        state = self._load()
        return refresh(self._cfg, refresh_token=state["refresh_token"])["access_token"]
```

- [ ] **Step 4: Run the test — expect PASS**

```bash
.venv/bin/python -m unittest discover -s apps/alphalens-research/tests -t apps/alphalens-research -k test_saxo_marketdata_auth -v
```

- [ ] **Step 5: Commit**

```bash
git add apps/alphalens-pipeline apps/alphalens-research
git commit -s -m "feat(data): Saxo LIVE market-data OAuth with rotating token store

Separate from the SIM-only broker OAuth surface, which must keep refusing LIVE
hosts. Accepts the HTTP 201 the code exchange really returns, never echoes a
response body that could carry the bearer token, and persists the rotated
refresh token 0600 so a single holder keeps the session alive."
```

---

### Task 3: `SaxoMarketDataClient` — capabilities, instruments, subscriptions

**Files:**
- Create: `apps/alphalens-pipeline/alphalens_pipeline/data/alt_data/saxo_marketdata_client.py`
- Test: `apps/alphalens-research/tests/data/test_saxo_marketdata_client.py`

**Interfaces:**
- Consumes: `LiveTokenProvider` (Task 2).
- Produces: `SaxoMarketDataClient(token_provider, session=None)` with `get_capabilities() -> dict`, `elevate_session() -> bool`, `resolve_uic(ticker) -> int | None`, `create_price_subscription(context_id, reference_id, uics, refresh_rate_ms=1000) -> dict`, `delete_price_subscription(context_id, reference_id) -> None`. Tasks 4–6 consume these.

- [ ] **Step 1: Write the failing test**

Create `apps/alphalens-research/tests/data/test_saxo_marketdata_client.py`:

```python
from __future__ import annotations

import unittest
from unittest import mock

from alphalens_pipeline.data.alt_data.saxo_marketdata_client import (
    LIVE_API_BASE_URL,
    SaxoMarketDataClient,
)


class _Resp:
    def __init__(self, status: int, payload=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.text = str(self._payload)

    def json(self):
        return self._payload


class _Session:
    """Records calls so the test asserts on URL and body, not on transport."""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.calls = []

    def _next(self, method, url, **kw):
        self.calls.append((method, url, kw))
        return self._responses.pop(0)

    def get(self, url, **kw):
        return self._next("GET", url, **kw)

    def post(self, url, **kw):
        return self._next("POST", url, **kw)

    def patch(self, url, **kw):
        return self._next("PATCH", url, **kw)

    def delete(self, url, **kw):
        return self._next("DELETE", url, **kw)


class _Tokens:
    def access_token(self):
        return "tok"


def _client(session):
    return SaxoMarketDataClient(token_provider=_Tokens(), session=session)


class TestElevateSession(unittest.TestCase):
    def test_patches_trade_level_and_reports_success_on_202(self):
        s = _Session(_Resp(202))
        self.assertTrue(_client(s).elevate_session())
        method, url, kw = s.calls[0]
        self.assertEqual(method, "PATCH")
        self.assertEqual(url, f"{LIVE_API_BASE_URL}/root/v1/sessions/capabilities")
        self.assertEqual(kw["json"], {"TradeLevel": "FullTradingAndChat"})

    def test_reports_failure_without_raising(self):
        """A failed elevation must degrade to 'not elevated', never crash the
        daemon tick."""
        self.assertFalse(_client(_Session(_Resp(403))).elevate_session())


class TestResolveUic(unittest.TestCase):
    def test_picks_the_exact_symbol_match(self):
        payload = {"Data": [
            {"Symbol": "AAPLX:xnas", "Identifier": 999},
            {"Symbol": "AAPL:xnas", "Identifier": 211},
        ]}
        self.assertEqual(_client(_Session(_Resp(200, payload))).resolve_uic("AAPL"), 211)

    def test_returns_none_when_no_exact_match(self):
        payload = {"Data": [{"Symbol": "AAPLX:xnas", "Identifier": 999}]}
        self.assertIsNone(_client(_Session(_Resp(200, payload))).resolve_uic("AAPL"))


class TestPriceSubscription(unittest.TestCase):
    def test_create_accepts_201_and_sends_the_measured_body(self):
        snapshot = {"RefreshRate": 1000, "Snapshot": {"Data": []}}
        s = _Session(_Resp(201, snapshot))
        got = _client(s).create_price_subscription(
            context_id="ctx", reference_id="px", uics=[211, 1249]
        )
        self.assertEqual(got["RefreshRate"], 1000)
        method, url, kw = s.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(url, f"{LIVE_API_BASE_URL}/trade/v1/infoprices/subscriptions")
        body = kw["json"]
        self.assertEqual(body["ContextId"], "ctx")
        self.assertEqual(body["ReferenceId"], "px")
        self.assertEqual(body["Format"], "application/json")
        self.assertEqual(body["Arguments"]["Uics"], "211,1249")
        self.assertEqual(body["Arguments"]["AssetType"], "Stock")

    def test_delete_is_quiet_on_404(self):
        """Deleting an already-gone subscription is not an error."""
        _client(_Session(_Resp(404))).delete_price_subscription("ctx", "px")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it and watch it fail**

```bash
.venv/bin/python -m unittest discover -s apps/alphalens-research/tests -t apps/alphalens-research -k test_saxo_marketdata_client -v
```

Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement the client**

Create `apps/alphalens-pipeline/alphalens_pipeline/data/alt_data/saxo_marketdata_client.py`:

```python
"""Canonical Saxo LIVE MARKET-DATA HTTP client (read-only + session capability).

Lives OUTSIDE ``brokers/`` on purpose: the SIM-only rail (ADR 0014) fails red if
a LIVE URL string appears anywhere in that package. This client never places,
amends or cancels an order; the LIVE app's trading permission stays unused.

All requests go through an injected ``requests.Session`` so this file has no
module-level raw HTTP call.
"""

from __future__ import annotations

import logging
from typing import Any

import requests

from alphalens_pipeline.data.alt_data.saxo_marketdata_auth import LiveTokenProvider

logger = logging.getLogger(__name__)

LIVE_API_BASE_URL = "https://gateway.saxobank.com/openapi"

_TIMEOUT_S = 30.0
_ELEVATED_TRADE_LEVEL = "FullTradingAndChat"
# Saxo clamps anything lower to 1000 ms (probed 2026-08-07: 0/100/500 all
# came back assigned 1000), so asking for less is noise.
_MIN_REFRESH_RATE_MS = 1000


class SaxoMarketDataClient:
    def __init__(
        self,
        *,
        token_provider: LiveTokenProvider,
        session: requests.Session | None = None,
    ):
        self._tokens = token_provider
        self._session = session or requests.Session()

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._tokens.access_token()}"}

    # ----- session capability -----

    def get_capabilities(self) -> dict[str, Any]:
        resp = self._session.get(
            f"{LIVE_API_BASE_URL}/root/v1/sessions/capabilities",
            headers=self._headers(),
            timeout=_TIMEOUT_S,
        )
        return resp.json() if resp.status_code == 200 else {}

    def elevate_session(self) -> bool:
        """PATCH the session to the elevated trade level (202 on success).

        A default OAuth session is ``OrdersOnly``, which SILENTLY serves
        15-minute-delayed prices. Failure is reported, never raised: a
        non-elevated session simply means every quote carries the delayed flag
        and the freshness gate vetoes it.
        """
        resp = self._session.patch(
            f"{LIVE_API_BASE_URL}/root/v1/sessions/capabilities",
            headers={**self._headers(), "Content-Type": "application/json"},
            json={"TradeLevel": _ELEVATED_TRADE_LEVEL},
            timeout=_TIMEOUT_S,
        )
        if 200 <= resp.status_code < 300:
            return True
        logger.warning("Saxo LIVE session elevation failed: HTTP %s", resp.status_code)
        return False

    # ----- reference data -----

    def resolve_uic(self, ticker: str) -> int | None:
        """Ticker -> LIVE uic. Never assume the SIM uic is the LIVE uic."""
        resp = self._session.get(
            f"{LIVE_API_BASE_URL}/ref/v1/instruments",
            headers=self._headers(),
            params={"Keywords": ticker, "AssetTypes": "Stock"},
            timeout=_TIMEOUT_S,
        )
        if resp.status_code != 200:
            return None
        wanted = f"{ticker.upper()}:"
        for row in resp.json().get("Data", []):
            symbol = str(row.get("Symbol", "")).upper()
            if symbol.startswith(wanted) and symbol.split(":")[0] == ticker.upper():
                return int(row["Identifier"])
        return None

    # ----- subscriptions -----

    def create_price_subscription(
        self,
        *,
        context_id: str,
        reference_id: str,
        uics: list[int],
        refresh_rate_ms: int = _MIN_REFRESH_RATE_MS,
    ) -> dict[str, Any]:
        resp = self._session.post(
            f"{LIVE_API_BASE_URL}/trade/v1/infoprices/subscriptions",
            headers={**self._headers(), "Content-Type": "application/json"},
            json={
                "ContextId": context_id,
                "ReferenceId": reference_id,
                "RefreshRate": max(refresh_rate_ms, _MIN_REFRESH_RATE_MS),
                "Format": "application/json",
                "Arguments": {
                    "AssetType": "Stock",
                    "Uics": ",".join(str(u) for u in uics),
                    "FieldGroups": ["Quote", "PriceInfo", "DisplayAndFormat"],
                },
            },
            timeout=_TIMEOUT_S,
        )
        if not (200 <= resp.status_code < 300):
            raise RuntimeError(f"price subscription failed: HTTP {resp.status_code}")
        return resp.json()

    def delete_price_subscription(self, context_id: str, reference_id: str) -> None:
        """Idempotent teardown: an already-gone subscription is not an error."""
        self._session.delete(
            f"{LIVE_API_BASE_URL}/trade/v1/infoprices/subscriptions/{context_id}/{reference_id}",
            headers=self._headers(),
            timeout=_TIMEOUT_S,
        )
```

- [ ] **Step 4: Run the test — expect PASS**

```bash
.venv/bin/python -m unittest discover -s apps/alphalens-research/tests -t apps/alphalens-research -k test_saxo_marketdata_client -v
```

- [ ] **Step 5: Commit**

```bash
git add apps/alphalens-pipeline apps/alphalens-research
git commit -s -m "feat(data): Saxo LIVE market-data client (capabilities, instruments, subscriptions)

Read-only HTTP surface outside the brokers package, because the SIM-only rail
forbids LIVE URL strings there. Elevates the session (a default OAuth session
is OrdersOnly and silently serves 15-minute-delayed prices), resolves ticker to
LIVE uic rather than trusting the SIM uic, and manages price subscriptions."
```

---

### Task 4: `SaxoPriceStream` — quote cache with delta merging

**Files:**
- Create: `apps/alphalens-pipeline/alphalens_pipeline/data/alt_data/saxo_price_stream.py`
- Test: `apps/alphalens-research/tests/data/test_saxo_price_stream.py`

**Interfaces:**
- Consumes: `SaxoMarketDataClient` (Task 3), `parse_stream_frames` from `alphalens_pipeline.brokers.saxo.streaming` (pure function, reused unchanged — it decoded real LIVE frames in the 2026-08-07 probe).
- Produces: `Quote(uic, bid, ask, event_time, delayed_by_minutes, received_at)`; `QuoteCache` with `apply(payload_row, *, received_at)` and `get(uic) -> Quote | None`; `SaxoPriceStream(client, ...)` with `.ensure_subscribed(uics)`, `.get(uic) -> Quote | None`, `.start()`, `.stop()`. Tasks 5–6 consume `Quote` and `SaxoPriceStream.get`.

**Note on scope:** this task builds the CACHE and the delta semantics, which are
pure and fully testable. The socket loop is thin glue around
`parse_stream_frames` and the existing reconnect policy; its correctness is
covered by the live probe in Task 8, not by mocking a WebSocket.

- [ ] **Step 1: Write the failing test**

Create `apps/alphalens-research/tests/data/test_saxo_price_stream.py`:

```python
from __future__ import annotations

import datetime as dt
import unittest

from alphalens_pipeline.data.alt_data.saxo_price_stream import QuoteCache

_T0 = dt.datetime(2026, 8, 7, 13, 48, 0, tzinfo=dt.UTC)


def _row(**over) -> dict:
    row = {
        "Uic": 211,
        "LastUpdated": "2026-08-07T13:47:59Z",
        "Quote": {"Bid": 314.01, "Ask": 314.04, "DelayedByMinutes": 0},
    }
    row.update(over)
    return row


class TestQuoteCache(unittest.TestCase):
    def test_snapshot_row_is_stored(self):
        c = QuoteCache()
        c.apply(_row(), received_at=_T0)
        q = c.get(211)
        self.assertEqual((q.bid, q.ask), (314.01, 314.04))
        self.assertEqual(q.delayed_by_minutes, 0)
        self.assertEqual(q.event_time, dt.datetime(2026, 8, 7, 13, 47, 59, tzinfo=dt.UTC))

    def test_delta_with_one_side_keeps_the_other(self):
        """THE delta rule. Saxo omits unchanged fields; treating an absent Ask as
        'no ask' would blank half the quote and produce a None mid."""
        c = QuoteCache()
        c.apply(_row(), received_at=_T0)
        c.apply(
            {"Uic": 211, "LastUpdated": "2026-08-07T13:48:01Z", "Quote": {"Bid": 314.10}},
            received_at=_T0 + dt.timedelta(seconds=2),
        )
        q = c.get(211)
        self.assertEqual(q.bid, 314.10)
        self.assertEqual(q.ask, 314.04)  # preserved

    def test_delta_without_a_quote_block_still_advances_event_time(self):
        c = QuoteCache()
        c.apply(_row(), received_at=_T0)
        c.apply(
            {"Uic": 211, "LastUpdated": "2026-08-07T13:48:05Z"},
            received_at=_T0 + dt.timedelta(seconds=5),
        )
        q = c.get(211)
        self.assertEqual(q.event_time, dt.datetime(2026, 8, 7, 13, 48, 5, tzinfo=dt.UTC))
        self.assertEqual(q.bid, 314.01)

    def test_delayed_flag_is_carried_and_updatable(self):
        """Session demotion arrives as a flag change on an otherwise healthy
        quote - the ONLY signal that prices went 15 minutes stale."""
        c = QuoteCache()
        c.apply(_row(), received_at=_T0)
        c.apply(
            {"Uic": 211, "LastUpdated": "2026-08-07T13:48:02Z", "Quote": {"DelayedByMinutes": 15}},
            received_at=_T0 + dt.timedelta(seconds=2),
        )
        self.assertEqual(c.get(211).delayed_by_minutes, 15)

    def test_out_of_order_event_time_is_dropped(self):
        c = QuoteCache()
        c.apply(_row(), received_at=_T0)
        c.apply(
            {"Uic": 211, "LastUpdated": "2026-08-07T13:47:50Z", "Quote": {"Bid": 1.0}},
            received_at=_T0 + dt.timedelta(seconds=3),
        )
        self.assertEqual(c.get(211).bid, 314.01)  # regression ignored

    def test_unknown_uic_returns_none(self):
        self.assertIsNone(QuoteCache().get(999))

    def test_row_without_uic_is_ignored(self):
        c = QuoteCache()
        c.apply({"LastUpdated": "2026-08-07T13:48:00Z", "Quote": {"Bid": 1.0}}, received_at=_T0)
        self.assertIsNone(c.get(211))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it and watch it fail**

```bash
.venv/bin/python -m unittest discover -s apps/alphalens-research/tests -t apps/alphalens-research -k test_saxo_price_stream -v
```

- [ ] **Step 3: Implement the cache and the stream**

Create `apps/alphalens-pipeline/alphalens_pipeline/data/alt_data/saxo_price_stream.py`. Start with the pure part:

```python
"""Saxo LIVE price stream: one long-lived WebSocket thread + a quote cache.

Price subscriptions stream DELTAS - an unchanged field is OMITTED. This differs
from the positions/orders stream, whose reader deliberately never merges and
re-reads full REST state instead. There is no cheap full re-read for a quote, so
this cache MUST merge: a message carrying only a Bid must leave the Ask intact.

The socket loop only decodes and applies. Every decision about whether a cached
quote may drive an order lives in the feed adapter's freshness gate.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
from dataclasses import dataclass, replace
from typing import Any

logger = logging.getLogger(__name__)

LIVE_STREAM_URL = "wss://live-streaming.saxobank.com/oapi/streaming/ws/connect"


def _parse_utc(raw: object) -> dt.datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass(frozen=True)
class Quote:
    uic: int
    bid: float | None
    ask: float | None
    event_time: dt.datetime | None
    delayed_by_minutes: int | None
    received_at: dt.datetime


class QuoteCache:
    """Thread-safe per-uic quote state with delta merging."""

    def __init__(self) -> None:
        self._quotes: dict[int, Quote] = {}
        self._lock = threading.Lock()

    def apply(self, row: dict[str, Any], *, received_at: dt.datetime) -> None:
        raw_uic = row.get("Uic")
        if raw_uic is None:
            return
        uic = int(raw_uic)
        event_time = _parse_utc(row.get("LastUpdated"))
        quote_block = row.get("Quote") or {}
        with self._lock:
            prev = self._quotes.get(uic)
            if prev is not None and prev.event_time and event_time and event_time < prev.event_time:
                return  # sequence regression: an older quote never overwrites a newer one
            merged = Quote(
                uic=uic,
                bid=quote_block.get("Bid", prev.bid if prev else None),
                ask=quote_block.get("Ask", prev.ask if prev else None),
                event_time=event_time or (prev.event_time if prev else None),
                delayed_by_minutes=quote_block.get(
                    "DelayedByMinutes", prev.delayed_by_minutes if prev else None
                ),
                received_at=received_at,
            )
            self._quotes[uic] = merged

    def get(self, uic: int) -> Quote | None:
        with self._lock:
            return self._quotes.get(uic)

    def forget(self, uic: int) -> None:
        with self._lock:
            self._quotes.pop(uic, None)
```

Then add `SaxoPriceStream` in the same file: it owns a `QuoteCache`, a
`SaxoMarketDataClient`, a `contextId`, the subscribed uic set, and a daemon
thread running the socket loop. Reuse `parse_stream_frames` from
`alphalens_pipeline.brokers.saxo.streaming` to decode frames, `json.loads` each
non-control payload, and call `cache.apply(row, received_at=now)` for every row.
Reuse the existing reconnect tuning constants (`max_consecutive_failures=6`,
backoff 1 s → 30 s ceiling). `ensure_subscribed(uics)` diffs the requested set
against the subscribed set, and recreates the subscription when it changed.
`stop()` deletes the subscription and joins the thread.

- [ ] **Step 4: Run the test — expect PASS**

```bash
.venv/bin/python -m unittest discover -s apps/alphalens-research/tests -t apps/alphalens-research -k test_saxo_price_stream -v
```

- [ ] **Step 5: Commit**

```bash
git add apps/alphalens-pipeline apps/alphalens-research
git commit -s -m "feat(data): Saxo LIVE price stream with delta-merging quote cache

Price subscriptions omit unchanged fields, so a message carrying only a Bid
must leave the Ask intact - unlike the positions stream, which never merges and
re-reads REST instead. Drops sequence regressions and carries the delayed flag,
which is the only signal that a demoted session went 15 minutes stale."
```

---

### Task 5: `SaxoLivePriceFeed` — the adapter and its gate

**Files:**
- Create: `apps/alphalens-pipeline/alphalens_pipeline/brokers/automanager/saxo_live_price_feed.py`
- Test: `apps/alphalens-research/tests/brokers/automanager/test_saxo_live_price_feed.py`

**Interfaces:**
- Consumes: `Quote`, `SaxoPriceStream` (Task 4), `PricePoint`, `is_fresh` (Task 1).
- Produces: `SaxoLivePriceFeed(stream, resolve_live_uic, clock=None)` implementing `latest(uic) -> PricePoint | None`. Task 7 constructs it.

**This file must contain no LIVE URL string** — it lives under `brokers/` and
the rail test scans that package. It imports the stream; it never names a host.

- [ ] **Step 1: Write the failing test**

Create `apps/alphalens-research/tests/brokers/automanager/test_saxo_live_price_feed.py`:

```python
from __future__ import annotations

import datetime as dt
import unittest

from alphalens_pipeline.brokers.automanager.saxo_live_price_feed import SaxoLivePriceFeed
from alphalens_pipeline.data.alt_data.saxo_price_stream import Quote

_NOW = dt.datetime(2026, 8, 7, 13, 48, 0, tzinfo=dt.UTC)


class _Stream:
    def __init__(self, quote: Quote | None):
        self._quote = quote
        self.subscribed: list[int] = []

    def ensure_subscribed(self, uics):
        self.subscribed = list(uics)

    def get(self, uic):
        return self._quote if self._quote and self._quote.uic == uic else None


def _quote(**over) -> Quote:
    base = dict(
        uic=211,
        bid=314.01,
        ask=314.04,
        event_time=_NOW - dt.timedelta(seconds=1),
        delayed_by_minutes=0,
        received_at=_NOW,
    )
    base.update(over)
    return Quote(**base)


def _feed(quote, *, sim_to_live=None):
    mapping = sim_to_live if sim_to_live is not None else {211: 211}
    return SaxoLivePriceFeed(
        stream=_Stream(quote),
        resolve_live_uic=mapping.get,
        clock=lambda: _NOW,
    )


class TestSaxoLivePriceFeed(unittest.TestCase):
    def test_fresh_quote_becomes_a_pricepoint(self):
        p = _feed(_quote()).latest(211)
        self.assertEqual((p.bid, p.ask), (314.01, 314.04))
        self.assertEqual(p.source, "saxo-live-l1")
        self.assertEqual(p.event_time, _NOW - dt.timedelta(seconds=1))

    def test_delayed_quote_is_vetoed_even_though_it_looks_healthy(self):
        """Session demotion: prices keep arriving and keep moving, 15 minutes
        old. Age alone would not catch it because LastUpdated also lags."""
        self.assertIsNone(_feed(_quote(delayed_by_minutes=15)).latest(211))

    def test_stale_quote_is_vetoed(self):
        stale = _quote(event_time=_NOW - dt.timedelta(seconds=10))
        self.assertIsNone(_feed(stale).latest(211))

    def test_missing_side_is_vetoed(self):
        self.assertIsNone(_feed(_quote(bid=None)).latest(211))

    def test_unknown_quote_is_vetoed(self):
        self.assertIsNone(_feed(None).latest(211))

    def test_unmapped_uic_is_vetoed(self):
        self.assertIsNone(_feed(_quote(), sim_to_live={}).latest(211))

    def test_returned_point_keeps_the_caller_uic_not_the_live_uic(self):
        """The engine keys everything by the uic it asked for; handing back a
        LIVE uic would silently mismatch the managed position."""
        stream = _Stream(_quote(uic=9999))
        feed = SaxoLivePriceFeed(
            stream=stream, resolve_live_uic={211: 9999}.get, clock=lambda: _NOW
        )
        self.assertEqual(feed.latest(211).uic, 211)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it and watch it fail**

```bash
.venv/bin/python -m unittest discover -s apps/alphalens-research/tests -t apps/alphalens-research -k test_saxo_live_price_feed -v
```

- [ ] **Step 3: Implement the adapter**

Create `apps/alphalens-pipeline/alphalens_pipeline/brokers/automanager/saxo_live_price_feed.py`:

```python
"""PriceFeed adapter over the Saxo LIVE quote stream.

Deliberately holds NO URL: it lives under ``brokers/``, where the SIM-only rail
(ADR 0014) fails red on any LIVE host string. Hosts live in the data-side stream
and client modules; this adapter only reads their cache.

Everything ambiguous returns ``None``. There is no path here that guesses.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable

from broker_contract.price_feed import PricePoint, is_fresh

from alphalens_pipeline.data.alt_data.saxo_price_stream import SaxoPriceStream

SOURCE = "saxo-live-l1"


class SaxoLivePriceFeed:
    """A structural ``PriceFeed`` reading the live quote cache.

    ``resolve_live_uic`` maps the caller's uic (which comes from the SIM broker's
    positions) to the LIVE uic the stream is keyed by. The two are NOT assumed
    equal: subscribing to the wrong instrument would be a silent catastrophe.
    """

    def __init__(
        self,
        *,
        stream: SaxoPriceStream,
        resolve_live_uic: Callable[[int], int | None],
        clock: Callable[[], dt.datetime] | None = None,
    ) -> None:
        self._stream = stream
        self._resolve_live_uic = resolve_live_uic
        self._clock = clock or (lambda: dt.datetime.now(dt.UTC))

    def latest(self, uic: int) -> PricePoint | None:
        live_uic = self._resolve_live_uic(uic)
        if live_uic is None:
            return None
        quote = self._stream.get(live_uic)
        if quote is None or quote.bid is None or quote.ask is None:
            return None
        # Its OWN condition, not folded into age: a demoted session keeps
        # delivering plausible, moving, 15-minute-old quotes with no error.
        if quote.delayed_by_minutes != 0:
            return None
        point = PricePoint(
            uic=uic,  # the CALLER's uic — the engine keys its state by it
            bid=float(quote.bid),
            ask=float(quote.ask),
            event_time=quote.event_time,
            received_at=quote.received_at,
            source=SOURCE,
        )
        return point if is_fresh(point, now=self._clock()) else None
```

- [ ] **Step 4: Run the test — expect PASS**

```bash
.venv/bin/python -m unittest discover -s apps/alphalens-research/tests -t apps/alphalens-research -k test_saxo_live_price_feed -v
```

- [ ] **Step 5: Run the SIM rail test to prove nothing leaked**

```bash
.venv/bin/python -m unittest discover -s apps/alphalens-research/tests -t apps/alphalens-research -k saxo_sim_only_rail -v
```

Expected: PASS. If it fails, a LIVE URL string reached the `brokers/` package —
move it into the data-side module rather than weakening the test.

- [ ] **Step 6: Commit**

```bash
git add apps/alphalens-pipeline apps/alphalens-research
git commit -s -m "feat(brokers): Saxo LIVE PriceFeed adapter with an honest freshness gate

Reads the live quote cache and returns a PricePoint only when the quote is
undelayed, two-sided, sane and under three seconds old. The delayed flag is
checked on its own because a demoted session keeps delivering plausible,
moving, 15-minute-old prices with no error at all. Holds no URL, so the
SIM-only rail stays intact."
```

---

### Task 6: Rate-limited session reclaim

**Files:**
- Create: `apps/alphalens-pipeline/alphalens_pipeline/data/alt_data/session_reclaim.py`
- Test: `apps/alphalens-research/tests/data/test_session_reclaim.py`
- Modify: `apps/alphalens-pipeline/alphalens_pipeline/data/alt_data/saxo_price_stream.py`

**Interfaces:**
- Consumes: `SaxoMarketDataClient.elevate_session` (Task 3).
- Produces: `ReclaimLimiter(max_per_hour=4, clock=None)` with `.try_reclaim(elevate: Callable[[], bool]) -> str` returning one of `"reclaimed"`, `"failed"`, `"budget-exhausted"`.

- [ ] **Step 1: Write the failing test**

Create `apps/alphalens-research/tests/data/test_session_reclaim.py`:

```python
from __future__ import annotations

import datetime as dt
import unittest

from alphalens_pipeline.data.alt_data.session_reclaim import ReclaimLimiter


class _Clock:
    def __init__(self):
        self.now = dt.datetime(2026, 8, 7, 12, 0, 0, tzinfo=dt.UTC)

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += dt.timedelta(**kw)


class TestReclaimLimiter(unittest.TestCase):
    def test_reclaims_while_budget_remains(self):
        clock = _Clock()
        lim = ReclaimLimiter(max_per_hour=4, clock=clock)
        for _ in range(4):
            self.assertEqual(lim.try_reclaim(lambda: True), "reclaimed")

    def test_stops_after_the_budget_is_spent(self):
        """The human wins if they keep pressing resume: the daemon must not
        ping-pong with the platform forever."""
        clock = _Clock()
        lim = ReclaimLimiter(max_per_hour=2, clock=clock)
        lim.try_reclaim(lambda: True)
        lim.try_reclaim(lambda: True)
        self.assertEqual(lim.try_reclaim(lambda: True), "budget-exhausted")

    def test_budget_refills_on_a_rolling_hour(self):
        clock = _Clock()
        lim = ReclaimLimiter(max_per_hour=1, clock=clock)
        lim.try_reclaim(lambda: True)
        self.assertEqual(lim.try_reclaim(lambda: True), "budget-exhausted")
        clock.advance(minutes=61)
        self.assertEqual(lim.try_reclaim(lambda: True), "reclaimed")

    def test_a_failed_elevation_still_spends_budget(self):
        """Otherwise a broken elevation retries without limit."""
        clock = _Clock()
        lim = ReclaimLimiter(max_per_hour=1, clock=clock)
        self.assertEqual(lim.try_reclaim(lambda: False), "failed")
        self.assertEqual(lim.try_reclaim(lambda: True), "budget-exhausted")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it and watch it fail**

```bash
.venv/bin/python -m unittest discover -s apps/alphalens-research/tests -t apps/alphalens-research -k test_session_reclaim -v
```

- [ ] **Step 3: Implement the limiter**

Create `apps/alphalens-pipeline/alphalens_pipeline/data/alt_data/session_reclaim.py`:

```python
"""Rate-limited reclaim of the elevated Saxo LIVE session.

Only ONE session per user may hold the elevated capability (verified
empirically 2026-08-07: the operator logging into SaxoTraderGO dropped the API
session to OrdersOnly and its prices to 15 minutes old). SaxoTraderGO shows the
loser a banner with a resume button, so a reclaim never leaves the operator
confused - but that button means an unlimited reclaim would ping-pong forever.

The budget makes the outcome fair: the unattended daemon wins by default, and an
operator who keeps pressing resume wins by persistence.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections import deque
from collections.abc import Callable

logger = logging.getLogger(__name__)

DEFAULT_MAX_PER_HOUR = 4
_WINDOW = dt.timedelta(hours=1)


class ReclaimLimiter:
    def __init__(
        self,
        *,
        max_per_hour: int = DEFAULT_MAX_PER_HOUR,
        clock: Callable[[], dt.datetime] | None = None,
    ) -> None:
        self._max = max_per_hour
        self._clock = clock or (lambda: dt.datetime.now(dt.UTC))
        self._attempts: deque[dt.datetime] = deque()

    def try_reclaim(self, elevate: Callable[[], bool]) -> str:
        now = self._clock()
        while self._attempts and now - self._attempts[0] > _WINDOW:
            self._attempts.popleft()
        if len(self._attempts) >= self._max:
            return "budget-exhausted"
        # Recorded BEFORE the outcome: a failing elevation must consume budget
        # too, or a broken session retries without limit.
        self._attempts.append(now)
        if elevate():
            logger.info("Saxo LIVE session reclaimed (%s/%s this hour)", len(self._attempts), self._max)
            return "reclaimed"
        logger.warning("Saxo LIVE session reclaim attempt failed")
        return "failed"
```

- [ ] **Step 4: Run the test — expect PASS**

```bash
.venv/bin/python -m unittest discover -s apps/alphalens-research/tests -t apps/alphalens-research -k test_session_reclaim -v
```

- [ ] **Step 5: Wire it into the stream**

In `saxo_price_stream.py`, hold a `ReclaimLimiter`. After applying a batch of
rows, if any cached quote reports `delayed_by_minutes` greater than 0, call
`limiter.try_reclaim(client.elevate_session)` and log the outcome once per
transition (not per message). On `"budget-exhausted"` log a warning and leave
the quotes delayed — the feed gate vetoes them anyway.

- [ ] **Step 6: Commit**

```bash
git add apps/alphalens-pipeline apps/alphalens-research
git commit -s -m "feat(data): rate-limited reclaim of the elevated Saxo LIVE session

The unattended daemon takes real-time back when the operator's platform login
demotes it, at most four times per rolling hour. Past the budget it backs off
and stays vetoed, so an operator who keeps pressing resume keeps the data."
```

---

### Task 7: Wire the feed into the daemon behind a flag

**Files:**
- Modify: `apps/alphalens-pipeline/alphalens_pipeline/brokers/automanager/control_loop.py:543-596`
- Test: `apps/alphalens-research/tests/brokers/automanager/test_live_exits_feed_selection.py`

**Interfaces:**
- Consumes: `SaxoLivePriceFeed` (Task 5).
- Produces: `_saxo_live_prices_enabled() -> bool` reading `ALPHALENS_SAXO_LIVE_PRICES`; `_default_live_exits_feed_factory(uic_to_ticker)` returning a `SaxoLivePriceFeed` when the flag is on and a vetoing `_NullPriceFeed` when it is off.

- [ ] **Step 1: Write the failing test**

Create `apps/alphalens-research/tests/brokers/automanager/test_live_exits_feed_selection.py`:

```python
from __future__ import annotations

import unittest
from unittest import mock

from alphalens_pipeline.brokers.automanager.control_loop import (
    _default_live_exits_feed_factory,
    _saxo_live_prices_enabled,
)


class TestFeedSelection(unittest.TestCase):
    def test_flag_defaults_off(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertFalse(_saxo_live_prices_enabled())

    def test_flag_on_only_for_exactly_one(self):
        for value, expected in (("1", True), ("0", False), ("true", False), ("", False)):
            with mock.patch.dict("os.environ", {"ALPHALENS_SAXO_LIVE_PRICES": value}, clear=True):
                self.assertEqual(_saxo_live_prices_enabled(), expected, value)

    def test_factory_returns_a_vetoing_feed_when_the_flag_is_off(self):
        """Off means no prices at all - never a silent fall back to yfinance."""
        with mock.patch.dict("os.environ", {}, clear=True):
            feed = _default_live_exits_feed_factory({211: "AAPL"})
        self.assertIsNone(feed.latest(211))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it and watch it fail**

```bash
.venv/bin/python -m unittest discover -s apps/alphalens-research/tests -t apps/alphalens-research -k test_live_exits_feed_selection -v
```

- [ ] **Step 3: Replace the factory in `control_loop.py`**

```python
_SAXO_LIVE_PRICES_ENV = "ALPHALENS_SAXO_LIVE_PRICES"


def _saxo_live_prices_enabled() -> bool:
    return os.environ.get(_SAXO_LIVE_PRICES_ENV) == "1"


class _NullPriceFeed:
    """Vetoes everything. The OFF state of the Saxo feed is 'no prices', never a
    quiet downgrade to a weaker source (see the INC-2 design memo)."""

    def latest(self, uic: int):
        return None


def _default_live_exits_feed_factory(
    uic_to_instrument: Mapping[int, tuple[str, str]],
) -> PriceFeed:
    """The production price feed: Saxo LIVE streaming, or nothing.

    yfinance is NOT a fallback here. It remains in the tree, unwired, and its
    PricePoint carries no event time so the freshness gate would veto it anyway.
    """
    if not _saxo_live_prices_enabled():
        return _NullPriceFeed()
    from alphalens_pipeline.brokers.automanager.saxo_live_price_feed import SaxoLivePriceFeed
    from alphalens_pipeline.data.alt_data.saxo_price_stream import get_shared_price_stream

    stream = get_shared_price_stream()
    live_uics = {
        sim_uic: stream.live_uic_for(ticker, exchange_mic=mic)
        for sim_uic, (ticker, mic) in uic_to_instrument.items()
    }
    stream.ensure_subscribed([u for u in live_uics.values() if u is not None])
    return SaxoLivePriceFeed(stream=stream, resolve_live_uic=live_uics.get)
```

Add `get_shared_price_stream()` to `saxo_price_stream.py`: a module-level
singleton, started on first call, so the WebSocket outlives the per-tick factory
call. Add `live_uic_for(ticker, *, exchange_mic)` to the stream: cached
(ticker, venue) → LIVE uic through `SaxoMarketDataClient.resolve_uic`.

**The venue is load-bearing, not decoration.** `resolve_uic` matches on the
(ticker, venue) pair and returns `None` when the pair is still ambiguous,
because a ticker listed on several venues would otherwise resolve to an
arbitrary instrument and feed a real, fresh, healthy-looking price for the WRONG
company into an order decision. No freshness gate catches that. This mirrors the
SIM adapter, which refuses an ambiguous resolution rather than taking row zero.

Change the existing map construction in `_run_live_exits_pass` accordingly — it
currently drops the venue:

```python
    # uic -> (ticker, venue) off the live positions just read. The venue must
    # survive: resolving a LIVE instrument by bare ticker is ambiguous for
    # cross-listed names.
    uic_to_instrument = {
        uic: (pos.instrument.ticker, pos.instrument.exchange_mic)
        for pos in long_positions
        if (uic := _position_uic(pos)) is not None
    }
```

- [ ] **Step 4: Run the test and the full suite**

```bash
.venv/bin/python -m unittest discover -s apps/alphalens-research/tests -t apps/alphalens-research -k test_live_exits_feed_selection -v
.venv/bin/python -m unittest discover -s apps/alphalens-research/tests -t apps/alphalens-research
```

- [ ] **Step 5: Commit**

```bash
git add apps/alphalens-pipeline apps/alphalens-research
git commit -s -m "feat(brokers): select the Saxo LIVE price feed behind ALPHALENS_SAXO_LIVE_PRICES

Default off, and off means a feed that vetoes every uic rather than a quiet
downgrade to yfinance. The WebSocket lives in a shared stream that outlives the
per-tick factory call, which only reconciles the subscription set."
```

---

### Task 8: Live probe, rail registration, runbook

**Files:**
- Create: `apps/alphalens-research/tests/live/test_saxo_marketdata_live.py`
- Modify: `apps/alphalens-research/tests/test_no_raw_saxo_http.py`
- Modify: `deploy/systemd/README.md`
- Modify: `.env.example`

- [x] **Step 1: Register the new canonical HTTP surfaces — ALREADY DONE in Task 3 (commit a5392542).**

Deferring this to Task 8 was a sequencing defect: the first LIVE HTTP file lands
in Task 2, so `tests/test_no_raw_saxo_http.py` went red at commit `2cf2fa03` and
stayed red, which would have made "full suite green" a meaningless signal for six
tasks. It was pulled forward into Task 3's fix round 1, together with two tests
that pin the exemption itself so the allowlist cannot rot to a no-op. **Skip this
step; verify it is green and move to Step 2.** Kept below for the record:

In `test_no_raw_saxo_http.py`, extend `CANONICAL_CLIENT_RELS` and explain why:

```python
CANONICAL_CLIENT_RELS = (
    "apps/alphalens-pipeline/alphalens_pipeline/brokers/saxo/client.py",
    "apps/alphalens-pipeline/alphalens_pipeline/brokers/saxo/oauth.py",
    # LIVE market-data surfaces (INC-2). Registered EXPLICITLY rather than
    # slipping past the check behind an injected session: they are read-only,
    # never place an order, and live outside brokers/ because the SIM-only rail
    # forbids LIVE URL strings in that package.
    "apps/alphalens-pipeline/alphalens_pipeline/data/alt_data/saxo_marketdata_auth.py",
    "apps/alphalens-pipeline/alphalens_pipeline/data/alt_data/saxo_marketdata_client.py",
)
```

- [ ] **Step 2: Run the enforcement tests**

```bash
.venv/bin/python -m unittest discover -s apps/alphalens-research/tests -t apps/alphalens-research -k no_raw_saxo -v
.venv/bin/python -m unittest discover -s apps/alphalens-research/tests -t apps/alphalens-research -k saxo_sim_only_rail -v
```

Both must pass.

- [ ] **Step 3: Write the opt-in live probe**

Create `apps/alphalens-research/tests/live/test_saxo_marketdata_live.py`, following
the existing `tests/live` convention exactly (`skipUnless` on its own env flag,
`run_probes`, transient vs permanent classification, SHAPE only — never values):

```python
"""Live Saxo LIVE market-data probe — opt-in via SAXO_MARKETDATA_LIVE_TEST=1.

Shape-only, NEVER values. Needs the SAXO_LIVE_* env and a bootstrapped LIVE
token store. Asserts: the session can be elevated, AAPL resolves to a uic, and a
price subscription returns a snapshot whose quote is undelayed. A closed market
is TRANSIENT (inconclusive), not a shape break.

WARNING: elevating the session takes real-time data away from any SaxoTraderGO
session the operator has open, and from the production daemon if it is running.

    SAXO_MARKETDATA_LIVE_TEST=1 .venv/bin/python -m unittest tests.live.test_saxo_marketdata_live -v
"""
```

Assert only: `elevate_session()` is True; `resolve_uic("AAPL")` is a positive
int; the subscription snapshot contains at least one row with a `Quote` block;
`DelayedByMinutes == 0`. Delete the subscription in `finally`.

- [ ] **Step 4: Run it once against the real service**

```bash
SAXO_MARKETDATA_LIVE_TEST=1 .venv/bin/python -m unittest discover -s apps/alphalens-research/tests -t apps/alphalens-research -k saxo_marketdata_live -v
```

Expected during XNYS hours: PASS. Confirm it SKIPS without the flag:

```bash
.venv/bin/python -m unittest discover -s apps/alphalens-research/tests -t apps/alphalens-research -k saxo_marketdata_live -v
```

- [ ] **Step 5: Document the operator runbook**

In `deploy/systemd/README.md` add a "Saxo LIVE market data" section covering:
the `SAXO_LIVE_*` env in `/etc/alphalens/env`; the OAuth bootstrap performed
**on the VPS** over an SSH tunnel to `localhost:8765` (the token store is never
copied — the rotating refresh token permits exactly one holder); its own refresh
cadence, separate from the SIM `alphalens-saxo-refresh` timer; and the rule that
exactly one LIVE session may hold the elevated capability, so any Mac-side probe
must be stopped before the daemon runs.

In `.env.example`, add the `SAXO_LIVE_*` block with empty values and the same
warnings already written into the operator's local `.env`.

- [ ] **Step 6: Commit**

```bash
git add apps/alphalens-research deploy .env.example
git commit -s -m "test(live): opt-in Saxo LIVE market-data probe + rail registration

Registers the two LIVE market-data surfaces as canonical Saxo HTTP explicitly
rather than letting them slip past the shadow-client check. Adds a shape-only
live probe behind its own flag, so a green hermetic suite can no longer hide a
dead feed, plus the operator runbook for the single-holder session rule."
```

---

## Self-Review

**Spec coverage.** §1 problem → Task 1 (contract) + Task 2/3 (replacement source). §2 measured facts → encoded as constants and test assertions in Tasks 1–4 (201/202 status codes, 1000 ms floor, deltas, 3 s gate). §4 no-fallback → Task 7 `_NullPriceFeed`; yfinance kept-but-unwired → Task 1 Step 6. §5 architecture, three components → Tasks 3, 4, 5, with placement enforced by Task 5 Step 5 and Task 8 Step 1. §6 gate → Task 1 `is_fresh` + Task 5 delayed-flag check. §7 failure behaviour → Task 4 (reconnect, sequence regression), Task 5 (every veto path), Task 6 (demotion). §8 testing → every task's tests plus Task 8's probe. §9 rollout → Task 7's flag and Task 8's runbook. §11 reclaim decision → Task 6.

**Known deliberate gap.** The WebSocket socket loop itself (Task 4, Step 3
second half) is described rather than given line by line. Mocking a WebSocket
would test the mock; the loop is thin glue over `parse_stream_frames`, which is
already tested and was verified against real LIVE frames on 2026-08-07. Its real
gate is the Task 8 live probe. The implementer should reuse
`SaxoStreamingClient`'s reconnect structure rather than inventing one.

**Type consistency.** `PricePoint(uic, bid, ask, event_time, received_at, source)`
and `is_fresh(point, *, now, max_age_s, max_relative_spread)` are used with
identical names in Tasks 1, 5 and 7. `Quote(uic, bid, ask, event_time,
delayed_by_minutes, received_at)` is identical in Tasks 4 and 5.
`elevate_session()` is the same name in Tasks 3, 6 and 8. `ensure_subscribed`,
`get`, `live_uic_for` are consistent between Tasks 4, 5 and 7.
