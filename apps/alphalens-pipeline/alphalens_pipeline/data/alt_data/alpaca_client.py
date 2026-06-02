"""Canonical Alpaca paper-trading client wrapper.

Single source of truth for every Alpaca call in the project. Paper sandbox is
structurally enforced — :class:`AlpacaClient` constructs the underlying
``alpaca.trading.client.TradingClient`` with ``paper=True`` hardcoded, and the
``ALPACA_API_BASE_URL`` env var (if set) MUST point at the paper endpoint.

The project doctrine `capital_deploy_clause` keeps real capital off the table;
this client makes accidental live submission structurally impossible rather
than relying on a runtime check that could be bypassed.

What this client centralises:
- The ``alpaca-py`` SDK import boundary. The SDK is loaded once per process;
  one actionable "install alpaca-py" error message lives here.
- API-key + secret resolution from ``ALPACA_API_KEY`` + ``ALPACA_API_SECRET``
  via :meth:`from_env`.
- Underlying ``TradingClient`` construction with ``paper=True`` locked.
- Primitive order ops (limit / stop / market) wrapping the SDK's
  ``OrderRequest`` dataclasses, so callers don't need to import
  ``alpaca.trading.requests`` directly. Order orchestration (entry ladder,
  TP tranches, time-stop) lives one level up in the ``paper/`` module.

What this client does NOT do:
- Order lifecycle orchestration. The planner + reconciler in
  ``alphalens_pipeline.paper`` own the entry/exit choreography.
- Historical market data. Bars + quotes go through a separate wrapper if and
  when needed; YAGNI for Phase A (we only need fills + positions, both of
  which come from ``TradingClient``).
"""

from __future__ import annotations

import logging
import os
import threading
from types import SimpleNamespace
from typing import Any

logger = logging.getLogger(__name__)

API_KEY_ENV = "ALPACA_API_KEY"
SECRET_ENV = "ALPACA_API_SECRET"
BASE_URL_ENV = "ALPACA_API_BASE_URL"

# Test-account env vars — read by ``from_env(profile="test")`` so the
# PR 3 reconciler + submitter can run live smoke tests against a sandbox
# isolated from the main paper account's persistent state. Both accounts
# are paper-only (the ``paper=True`` hardcoding + URL guard apply to both).
TEST_API_KEY_ENV = "ALPACA_TEST_API_KEY"
TEST_SECRET_ENV = "ALPACA_TEST_API_SECRET"

# Allowed profile names. ``main`` reads ALPACA_API_KEY/SECRET (production
# paper account for real candidate planning); ``test`` reads
# ALPACA_TEST_API_KEY/SECRET (dev sandbox). Both still route to
# paper-api.alpaca.markets — the profile only switches credentials.
_VALID_PROFILES = frozenset({"main", "test"})

PAPER_BASE_URL = "https://paper-api.alpaca.markets"
# Anything in this set is accepted as "obviously paper"; everything else is
# rejected at construction time. Trailing slash + ``/v2`` variants are
# enumerated explicitly so a copy-paste from the Alpaca dashboard (which
# displays the URL as ``https://paper-api.alpaca.markets/v2``) doesn't trip
# the guard. The SDK appends the API version itself; both forms route to
# the same paper sandbox endpoint.
_PAPER_URL_VARIANTS = frozenset(
    {
        PAPER_BASE_URL,
        f"{PAPER_BASE_URL}/",
        f"{PAPER_BASE_URL}/v2",
        f"{PAPER_BASE_URL}/v2/",
        "paper-api.alpaca.markets",
    }
)

__all__ = [
    "API_KEY_ENV",
    "BASE_URL_ENV",
    "PAPER_BASE_URL",
    "SECRET_ENV",
    "TEST_API_KEY_ENV",
    "TEST_SECRET_ENV",
    "AlpacaClient",
    "AlpacaClientError",
    "_reset_default_client_for_tests",
    "_reset_sdk_cache_for_tests",
    "get_default_alpaca_client",
]


class AlpacaClientError(RuntimeError):
    """Non-transient Alpaca wrapper failure (config, missing SDK, paper-guard)."""


def _round_to_alpaca_tick(price: float) -> float:
    """Round a price to the minimum tick Alpaca accepts for equity orders.

    Alpaca rejects sub-penny precision on equity limit / stop prices with
    ``APIError 42210000 — sub-penny increment does not fulfill minimum
    pricing criteria``. The minimum tick is:

    - ``price >= $1.00`` → $0.01 (penny)
    - ``price <  $1.00`` → $0.0001 (Reg NMS sub-dollar tick size for
      low-priced shares; sometimes called "sub-penny" in vendor docs but
      strictly it is the tick allowed on sub-dollar instruments).

    Applied centrally inside :class:`AlpacaClient` so every callsite
    (planner-driven entry tiers, exit_manager TPs / SLs, time-stop
    market orders that route through other paths) benefits without each
    callsite having to remember the rule. The deterministic
    ``brief_trade_setup`` ladder produces full float precision (e.g.
    ``69.22318381700624``); rounding here turns it into ``69.22``.

    Note: ``round`` in Python 3 uses banker's rounding ("round half to
    even"). For Alpaca that's fine — the half-cent rounding direction
    isn't material for paper observation and any drift is bounded to
    half a tick.
    """
    if price >= 1.0:
        return round(price, 2)
    return round(price, 4)


# Module-level lazy SDK handle. Populated on first AlpacaClient construction
# via _load_alpaca_sdk(); cleared between tests by _reset_sdk_cache_for_tests.
_SDK: SimpleNamespace | None = None


def _load_alpaca_sdk() -> SimpleNamespace:
    """Import the alpaca-py SDK lazily; cache after first success.

    Raises :class:`AlpacaClientError` with an actionable message if the SDK is
    not installed. The message and import path live HERE so future call sites
    share one error path.
    """
    global _SDK  # noqa: PLW0603 — documented lazy cache
    if _SDK is not None:
        return _SDK
    try:
        from alpaca.trading.client import TradingClient
        from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
        from alpaca.trading.requests import (
            GetOrdersRequest,
            LimitOrderRequest,
            MarketOrderRequest,
            StopLossRequest,
            StopOrderRequest,
            TakeProfitRequest,
        )
    except ImportError as exc:
        raise AlpacaClientError(
            "alpaca-py SDK not installed. `uv add alpaca-py` (already in "
            "apps/alphalens-pipeline/pyproject.toml — run `uv sync`)."
        ) from exc
    _SDK = SimpleNamespace(
        TradingClient=TradingClient,
        LimitOrderRequest=LimitOrderRequest,
        MarketOrderRequest=MarketOrderRequest,
        StopOrderRequest=StopOrderRequest,
        TakeProfitRequest=TakeProfitRequest,
        StopLossRequest=StopLossRequest,
        GetOrdersRequest=GetOrdersRequest,
        OrderSide=OrderSide,
        OrderClass=OrderClass,
        TimeInForce=TimeInForce,
    )
    return _SDK


def _validate_paper_base_url(value: str | None) -> None:
    """Reject anything that is not the paper endpoint.

    Called at construction time so the failure surfaces immediately — not on
    the first order submission, by which point the operator has already moved
    on to a different task and may not notice a 401-from-live-with-paper-keys.
    """
    if value is None:
        return  # SDK default routes to paper when paper=True
    normalised = value.strip()
    if normalised not in _PAPER_URL_VARIANTS:
        raise AlpacaClientError(
            f"{BASE_URL_ENV}={normalised!r} is not a paper endpoint. "
            f"AlpacaClient refuses to construct against a non-paper URL "
            f"(doctrine: capital_deploy_clause). Set to {PAPER_BASE_URL!r} "
            "or unset the var to use the SDK default."
        )


def _tif(time_in_force: str, sdk: SimpleNamespace) -> Any:
    """Map our short string (``"gtc"`` / ``"day"``) to the SDK enum.

    The SDK enum has more values (IOC / FOK / OPG / CLS) — Phase A uses GTC
    for entries (cancel via TTL in reconciler) and DAY for time-stop exits.
    Extend the mapping when a new TIF is actually needed; rejecting unknown
    strings up-front catches typos before they reach the SDK.
    """
    key = time_in_force.lower()
    if key == "gtc":
        return sdk.TimeInForce.GTC
    if key == "day":
        return sdk.TimeInForce.DAY
    raise ValueError(
        f"unsupported time_in_force={time_in_force!r}; AlpacaClient currently "
        "accepts only 'gtc' or 'day'"
    )


def _side(side: str, sdk: SimpleNamespace) -> Any:
    key = side.lower()
    if key == "buy":
        return sdk.OrderSide.BUY
    if key == "sell":
        return sdk.OrderSide.SELL
    raise ValueError(f"unsupported side={side!r}; expected 'buy' or 'sell'")


class AlpacaClient:
    """Thin wrapper around ``alpaca.trading.client.TradingClient``.

    Hardcoded ``paper=True``. State: the underlying SDK handle + the
    instantiated TradingClient. No retry/throttle — Alpaca paper's rate
    limits are generous enough that adapter-level orchestration handles it
    when it actually starts biting.
    """

    def __init__(self, api_key: str, secret_key: str):
        if not api_key:
            raise ValueError(f"Alpaca requires a non-empty API key (env {API_KEY_ENV})")
        if not secret_key:
            raise ValueError(f"Alpaca requires a non-empty secret (env {SECRET_ENV})")
        _validate_paper_base_url(os.environ.get(BASE_URL_ENV))
        sdk = _load_alpaca_sdk()
        self._sdk = sdk
        # paper=True is the structural guarantee — no constructor arg to flip
        # it, no env override path. The base-URL guard above prevents the
        # other side of the same footgun.
        self._trading = sdk.TradingClient(api_key, secret_key, paper=True)

    @classmethod
    def from_env(cls, *, profile: str = "main") -> AlpacaClient:
        """Build a client reading credentials per profile.

        Profiles:
        - ``"main"`` (default): ``ALPACA_API_KEY`` + ``ALPACA_API_SECRET`` —
          production paper account for real candidate planning.
        - ``"test"``: ``ALPACA_TEST_API_KEY`` + ``ALPACA_TEST_API_SECRET`` —
          dev sandbox for live smoke tests of the submitter + reconciler
          without polluting the main account's persistent state.

        Both profiles still route to ``paper-api.alpaca.markets`` — the
        ``paper=True`` hardcoding + URL guard apply uniformly. The profile
        only switches credentials.
        """
        if profile not in _VALID_PROFILES:
            raise ValueError(
                f"profile={profile!r} not in {sorted(_VALID_PROFILES)}; "
                "AlpacaClient only supports 'main' or 'test'"
            )
        if profile == "test":
            key_env, secret_env = TEST_API_KEY_ENV, TEST_SECRET_ENV
        else:
            key_env, secret_env = API_KEY_ENV, SECRET_ENV
        api_key = os.environ.get(key_env)
        secret = os.environ.get(secret_env)
        if not api_key:
            raise ValueError(f"{key_env} environment variable is not set.")
        if not secret:
            raise ValueError(f"{secret_env} environment variable is not set.")
        return cls(api_key=api_key, secret_key=secret)

    @property
    def trading_client(self) -> Any:
        """Underlying SDK ``TradingClient``. Escape hatch for ops not yet
        wrapped (e.g. ``close_position``, asset metadata)."""
        return self._trading

    # ----- account / portfolio reads -----

    def get_account(self) -> Any:
        """Return the ``TradeAccount`` (equity, buying_power, cash)."""
        return self._trading.get_account()

    def get_position(self, symbol: str) -> Any | None:
        """Return the open position for ``symbol`` or ``None`` if flat.

        The SDK signals "no position" via 404 / a message containing
        ``does not exist`` / ``position not found``. The wrapper converts
        ONLY that case to ``None``; other failures (timeouts, 401, 500s)
        re-raise so the caller can decide whether to retry or alert. A
        catch-all swallow would mask network outages as "no position",
        which the planner would then interpret as "safe to plan a fresh
        position" — exactly wrong during an Alpaca-side incident.
        """
        try:
            return self._trading.get_open_position(symbol)
        except Exception as exc:
            status_code = getattr(exc, "status_code", None)
            message = str(exc).lower()
            if status_code == 404 or "does not exist" in message or "position not found" in message:
                logger.debug("get_position(%s) returned no position: %s", symbol, exc)
                return None
            # ``logger.exception`` attaches the active traceback automatically;
            # we're inside the ``except`` block so this captures ``exc``.
            logger.exception("get_position(%s) failed (not a missing-position error)", symbol)
            raise

    def get_all_positions(self) -> list[Any]:
        return list(self._trading.get_all_positions())

    def get_order(self, order_id: str) -> Any:
        return self._trading.get_order_by_id(order_id)

    def get_orders(self, *, status: str | None = None) -> list[Any]:
        """List orders, optionally filtered by status ('open', 'closed', 'all').

        Required by the reconciler to enumerate pending entries + open
        TP/SL legs without falling back to the ``trading_client`` escape
        hatch. The SDK's GetOrdersRequest is wrapped here so callers
        don't need to import ``alpaca.trading.requests`` directly. When
        ``status`` is ``None`` the SDK default applies (which today
        returns open orders only — Alpaca's convention).
        """
        if status is None:
            return list(self._trading.get_orders())
        # GetOrdersRequest in the SDK accepts a QueryOrderStatus enum;
        # passing the bare string works for status='open'/'closed'/'all'
        # via the underlying validator. Routed through the cached SDK
        # handle so the no-raw-http enforcement test stays happy.
        return list(self._trading.get_orders(filter=self._sdk.GetOrdersRequest(status=status)))

    # ----- order submission primitives -----

    def submit_limit_order(
        self,
        *,
        symbol: str,
        qty: int | float,
        limit_price: float,
        side: str = "buy",
        time_in_force: str = "gtc",
    ) -> Any:
        """Submit a simple limit order (used for entry tiers + TP exits).

        ``side="buy"`` for entry-ladder tiers; ``side="sell"`` for TP-tranche
        exits. The stop-loss leg is submitted separately via
        :meth:`submit_stop_order` because ``brief_trade_setup`` ships a
        multi-tranche TP ladder (Alpaca BRACKET supports a single TP +
        single SL); the reconciler manually orchestrates "if any TP
        fills, cancel-and-resubmit stop for reduced qty; if stop fires,
        cancel remaining TPs". When a candidate has exactly one TP
        tranche, :meth:`submit_bracket_order` is the simpler one-call
        path with automatic OCO semantics.
        """
        req = self._sdk.LimitOrderRequest(
            symbol=symbol,
            qty=qty,
            side=_side(side, self._sdk),
            time_in_force=_tif(time_in_force, self._sdk),
            limit_price=_round_to_alpaca_tick(limit_price),
        )
        return self._trading.submit_order(order_data=req)

    def submit_stop_order(
        self,
        *,
        symbol: str,
        qty: int | float,
        stop_price: float,
        side: str = "sell",
        time_in_force: str = "gtc",
    ) -> Any:
        """Submit a plain stop order (used for disaster_stop exit)."""
        req = self._sdk.StopOrderRequest(
            symbol=symbol,
            qty=qty,
            side=_side(side, self._sdk),
            time_in_force=_tif(time_in_force, self._sdk),
            stop_price=_round_to_alpaca_tick(stop_price),
        )
        return self._trading.submit_order(order_data=req)

    def submit_market_order(
        self,
        *,
        symbol: str,
        qty: int | float,
        side: str = "sell",
        time_in_force: str = "day",
    ) -> Any:
        """Submit a market order (used for 60-day time-stop exits at open)."""
        req = self._sdk.MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=_side(side, self._sdk),
            time_in_force=_tif(time_in_force, self._sdk),
        )
        return self._trading.submit_order(order_data=req)

    def submit_bracket_order(
        self,
        *,
        symbol: str,
        qty: int | float,
        limit_price: float,
        take_profit_price: float | None = None,
        stop_loss_price: float | None = None,
        side: str = "buy",
        time_in_force: str = "gtc",
    ) -> Any:
        """Submit a bracket order: one parent limit + REQUIRED TP and SL legs.

        Alpaca's ``BRACKET`` order class atomically attaches a single
        take-profit + single stop-loss leg to a parent entry. When the
        parent fills, the two child legs activate as an OCO pair so one
        cancels the other automatically.

        Both legs are MANDATORY — Alpaca rejects a BRACKET order with
        only one leg present (HTTP 422). The wrapper enforces that
        locally so the caller fails with a clear ``ValueError`` instead
        of an opaque SDK error at submission time. Use
        :meth:`submit_limit_order` (+ :meth:`submit_stop_order` for an
        SL leg, or another :meth:`submit_limit_order` for a TP leg) for
        the one-leg-only case.

        Note for the upcoming reconciler: ``brief_trade_setup`` ships a
        MULTI-tranche TP ladder (typically 2-3 targets at different
        prices, each with its own ``tranche_pct``). Alpaca's BRACKET
        supports only ONE TP leg per parent. So bracket is usable only
        when the candidate has exactly one TP tranche; the multi-tranche
        case still needs separate limit-sells + a single stop-loss
        managed by the reconciler. This primitive lives here so callers
        don't reach into the SDK directly for the simple case.
        """
        # Local guard: Alpaca BRACKET requires both legs. Fail fast with a
        # clear message instead of letting the SDK return a generic 422.
        # Per zen second-round review 2026-05-28.
        if take_profit_price is None or stop_loss_price is None:
            raise ValueError(
                "BRACKET orders require both take_profit_price AND stop_loss_price. "
                "For a single-leg exit, use submit_limit_order (TP) or "
                "submit_stop_order (SL) separately."
            )
        req = self._sdk.LimitOrderRequest(
            symbol=symbol,
            qty=qty,
            side=_side(side, self._sdk),
            time_in_force=_tif(time_in_force, self._sdk),
            limit_price=_round_to_alpaca_tick(limit_price),
            order_class=self._sdk.OrderClass.BRACKET,
            take_profit=self._sdk.TakeProfitRequest(
                limit_price=_round_to_alpaca_tick(take_profit_price)
            ),
            stop_loss=self._sdk.StopLossRequest(stop_price=_round_to_alpaca_tick(stop_loss_price)),
        )
        return self._trading.submit_order(order_data=req)

    def attach_exit_ladder(
        self,
        *,
        symbol: str,
        tranches: Any,
        stop_price: float,
        time_in_force: str = "gtc",
    ) -> list[Any]:
        """Alpaca decomposition of the broker-neutral OCO-ladder exit intent.

        For each take-profit tranche this submits ONE Alpaca ``OCO`` order:
        a SELL limit at the tranche's take-profit price bracketed by a
        stop-loss at the shared ``stop_price``. Alpaca's OCO returns a PARENT
        object that IS the take-profit limit (``parent.id``), carrying the
        stop-loss STOP order in ``parent.legs[0]`` (``parent.legs[0].id``).
        BOTH ids are present synchronously at submit and independently
        pollable, so the wrapper captures them into an
        :class:`~alphalens_pipeline.paper.broker.ExitLadderLeg` per tranche.

        Why M separate OCO groups rather than one full-size stop + M
        take-profits: a bare full-size stop would block every take-profit
        (Alpaca ``held_for_orders``); OCO/bracket legs are NOT additive within
        a group, but multiple OCO groups whose qtys sum to the held position
        coexist. The single disaster stop is therefore realized as M stop
        legs at one ``stop_price`` — one per group.

        ``parent.legs`` empty/missing means the stop-loss id is uncapturable;
        the wrapper raises :class:`AlpacaClientError` rather than silently
        dropping the protective stop. An empty ``tranches`` input is likewise
        rejected (no take-profit tranches = no protective stop attached). The
        recorded :class:`ExitLadderLeg` prices are the tick-rounded values
        actually submitted, not the caller's raw intent. OCO requires WHOLE
        shares.
        """
        # Lazy import to avoid a top-level data -> paper import edge (mirrors
        # the lazy import in broker.get_default_broker_client). The paper
        # package depends on this client; importing ExitLadderLeg at module
        # scope would invert that direction.
        from alphalens_pipeline.paper.broker import ExitLadderLeg

        # An empty ladder attaches no protective stop — the same "silently
        # drop the stop" failure the empty-legs guard below defends against,
        # but at the input boundary. Refuse the no-op rather than leave the
        # held position unprotected (the docstring promises M >= 1 tranches).
        tranches = list(tranches)
        if not tranches:
            raise AlpacaClientError(
                f"attach_exit_ladder for {symbol} got an empty tranche list; "
                "refusing to attach a disaster stop with no take-profit "
                "tranches (the held position would be left unprotected)."
            )

        rounded_stop = _round_to_alpaca_tick(stop_price)
        tif = _tif(time_in_force, self._sdk)
        legs: list[Any] = []
        for index, tranche in enumerate(tranches):
            rounded_tp = _round_to_alpaca_tick(tranche.take_profit_limit)
            req = self._sdk.LimitOrderRequest(
                symbol=symbol,
                qty=tranche.qty,
                side=self._sdk.OrderSide.SELL,
                time_in_force=tif,
                order_class=self._sdk.OrderClass.OCO,
                take_profit=self._sdk.TakeProfitRequest(limit_price=rounded_tp),
                stop_loss=self._sdk.StopLossRequest(stop_price=rounded_stop),
            )
            parent = self._trading.submit_order(order_data=req)
            parent_legs = getattr(parent, "legs", None)
            if not parent_legs:
                raise AlpacaClientError(
                    f"OCO submit for {symbol} tranche #{index} returned no "
                    "child legs; cannot capture the stop-loss order id. "
                    "Refusing to attach a take-profit without its protective "
                    "stop-loss (the disaster stop would be silently dropped)."
                )
            # Record the prices ACTUALLY SUBMITTED (tick-rounded), not the raw
            # intent — the order ids point at the rounded orders, so a future
            # reconciler comparing a leg to the broker order sees no sub-tick
            # drift (see ExitLadderLeg docstring).
            legs.append(
                ExitLadderLeg(
                    tranche_index=index,
                    qty=tranche.qty,
                    take_profit_limit=rounded_tp,
                    stop_price=rounded_stop,
                    # str() — Alpaca order ids are UUID objects; ExitLadderLeg
                    # types them as str and the ledger persists them as TEXT
                    # (matches the str(order.id) convention elsewhere).
                    tp_order_id=str(parent.id),
                    sl_order_id=str(parent_legs[0].id),
                )
            )
        return legs

    def cancel_order(self, order_id: str) -> None:
        """Cancel an open order. SDK returns no payload."""
        self._trading.cancel_order_by_id(order_id)

    # ----- BrokerClient enumerate primitives (reset support) -----

    def list_open_orders(self) -> list[Any]:
        """All currently-open Alpaca orders (GET /v2/orders?status=open).

        Wraps :meth:`get_orders` with ``status='open'`` under the
        BrokerClient protocol name so ``alphalens paper reset`` can sweep
        orphan orders without reaching into the vendor surface. Each
        element duck-types ``.id`` / ``.symbol`` / ``.status`` like the
        objects the reconciler already reads. The SDK caps a single
        ``status='open'`` page generously (paper accounts never approach
        the implicit 500 limit), so no pagination loop is needed for the
        paper-harness sizes; revisit if a future venue needs >500 open
        orders in one sweep.
        """
        return self.get_orders(status="open")

    def list_positions(self) -> list[Any]:
        """All open Alpaca positions (GET /v2/positions).

        Alias of :meth:`get_all_positions` under the BrokerClient
        protocol name. Each element duck-types ``.symbol`` / ``.qty``
        (signed string, negative for shorts) / ``.side``
        (PositionSide.LONG / SHORT) — the same fields the exit_manager
        time-stop reads.
        """
        return self.get_all_positions()


# Module-level lazy singleton — one AlpacaClient shared by every adapter that
# does not have its own injected client. First call reads keys from the
# environment; tests reset via _reset_default_client_for_tests(). The lock
# protects against double-construction under concurrent first calls — the
# pipeline today is single-threaded but the reconciler (PR 3) may be invoked
# from a daemon thread while the operator triggers a manual command.
# Per-profile singletons. Production paths use ``main`` exclusively; the
# PR 3 dev smoke tests use ``test``. Keeping them separate prevents a
# single ``_reset_default_client_for_tests()`` from clobbering both.
_DEFAULT_CLIENTS: dict[str, AlpacaClient] = {}
_DEFAULT_CLIENT_LOCK = threading.RLock()


def get_default_alpaca_client(*, profile: str = "main") -> AlpacaClient:
    """Return the process-wide default :class:`AlpacaClient` for the given
    profile (lazy-initialized).

    Double-checked locking: the fast path skips the lock when the profile's
    singleton is already populated; the slow path re-checks inside the lock
    so two concurrent first callers cannot each construct a fresh client.
    """
    if profile not in _VALID_PROFILES:
        raise ValueError(
            f"profile={profile!r} not in {sorted(_VALID_PROFILES)}; "
            "AlpacaClient only supports 'main' or 'test'"
        )
    cached = _DEFAULT_CLIENTS.get(profile)
    if cached is not None:
        return cached
    with _DEFAULT_CLIENT_LOCK:
        cached = _DEFAULT_CLIENTS.get(profile)
        if cached is None:
            cached = AlpacaClient.from_env(profile=profile)
            _DEFAULT_CLIENTS[profile] = cached
        return cached


def _reset_default_client_for_tests() -> None:
    """Test-only hook: clear all cached singletons so each test starts clean."""
    _DEFAULT_CLIENTS.clear()


def _reset_sdk_cache_for_tests() -> None:
    """Test-only hook: clear the cached SDK module so the next client
    construction re-imports (e.g. after ``sys.modules`` manipulation)."""
    global _SDK  # noqa: PLW0603 — documented lazy cache
    _SDK = None
