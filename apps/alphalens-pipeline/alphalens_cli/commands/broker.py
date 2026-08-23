"""CLI: ``alphalens broker`` — broker execution layer (SIM by default, ADR 0014; LIVE via the ADR 0017 standing grant).

Subcommands (P1 reads + P2 orders + P3 reconcile + P4 OAuth):

    alphalens broker auth                    — attended OAuth login (browser +
        localhost listener); --status = offline chain inspection (exit 0 iff
        alive); --refresh = one silent refresh cycle (keep-alive primitive)
    alphalens broker account                 — account snapshot (cash / value / margin)
    alphalens broker positions               — open positions
    alphalens broker resolve KO [--exchange XNYS]  — instrument resolution (symbol -> Uic)
    alphalens broker submit KO --date 2026-07-16   — DRY-RUN by default: bracket
        table + precheck; sending needs --execute AND an interactive confirm
        (--yes skips the prompt) AND ALPHALENS_BROKER_ALLOW_ORDERS=1 in the env
    alphalens broker arm KO --date 2026-07-20 [--env sim|live]   — validate
        against the brief, append an "armed" pick to <env>/picks.jsonl (the
        auto-manager hand-off seam; --env selects the instance, default sim)
    alphalens broker orders                  — open orders
    alphalens broker cancel <order_id>       — cancel (entry cancel cascades the bracket)
    alphalens broker reconcile [--json]      — READ-ONLY journal vs broker verdicts (P3):
        WORKING / PAST-TTL divergence / FILLED (+closed r) / CANCELLED / REJECTED /
        EXPIRED / UNRESOLVED(reason); exit 1 on any unresolved or divergent row
    alphalens broker reconcile-fills [--out P] [--json]  — READ-ONLY offline fill
        reconciler (build-seq 1b-ii): joins each fired TP tranche to its ACTUAL
        broker fill by sell_order_id, computes implementation shortfall, writes
        the exec-quality parquet (places/cancels/amends NOTHING)
    alphalens broker manage [--once]         — auto-manager control loop for the
        instance selected by ALPHALENS_BROKER_ENVIRONMENT (default sim; live
        boots only through the ADR 0017 LIVE factory + boot-assert)
    alphalens broker marketdata-auth         — LIVE ``saxo_auth_live`` OAuth
        bootstrap / --status / --refresh (since ADR 0017 the same chain also
        feeds the LIVE order rail)

Every one-off broker-touching command (account / positions / resolve /
submit / orders / reconcile / reconcile-fills / cancel) resolves its broker
through :func:`_cli_broker` — env-aware per ``ALPHALENS_BROKER_ENVIRONMENT``,
the same seam the per-env journal paths use — and echoes one
``env=<env> gateway=<label>`` line to stderr. Under ``env=live`` reads route
into the ADR 0017 LIVE factory and ad-hoc placement (``submit``) refuses
(the daemon is the only LIVE placement path).

All ``brokers`` imports are lazy inside command bodies — the ``alphalens``
binary's startup time is paid by the 15-min Layer-1 edgar-detect cron
(+913ms precedent; see CLAUDE.md lazy-CLI convention).
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import typer

if TYPE_CHECKING:
    # Type-only imports for the extracted submit helpers. Guarded by
    # TYPE_CHECKING so they never run at import time (lazy-CLI startup budget) —
    # `from __future__ import annotations` keeps the annotations as strings.
    from alphalens_pipeline.brokers.notifications import NotificationPort
    from broker_contract.contract import Broker, InstrumentRef
    from broker_contract.fx import FxConversion

logger = logging.getLogger(__name__)

broker_app = typer.Typer(
    name="broker",
    help=(
        "Broker execution layer — SIM by default (ADR 0014); LIVE only via the "
        "ADR 0017 standing account-bound grant (the "
        "ALPHALENS_BROKER_ENVIRONMENT=live daemon)."
    ),
    no_args_is_help=True,
)

_DEFAULT_BRIEFS_DIR = Path.home() / ".alphalens" / "thematic_briefs"

# Status line shared by `broker auth --status` and `broker marketdata-auth
# --status` — the column padding aligns with the sibling `access`/`store` rows.
_REFRESH_DEAD_LINE = "refresh      DEAD"

# Advisory-only instrument hint MIC stamped on every armed TradeIntent (PR-7,
# broker-manager extraction memo section 5). The daemon resolves the REAL
# instrument via resolve_us_instrument at drain time — this hint is
# informational metadata for a human/future multi-exchange client, never used
# for routing.
_ARM_INSTRUMENT_MIC = "XNYS"

# `arm --env` default — mirrors state_paths.ENV_SIM, kept as a literal so the
# option default is available without importing `state_paths` at module scope
# (lazy-CLI doctrine, module docstring above). The real validation (and the
# full sim/live vocabulary) is owned by the seam at call time, not here.
_DEFAULT_ARM_ENV = "sim"


def _fail(message: str) -> typer.Exit:
    typer.secho(message, fg=typer.colors.RED, err=True)
    return typer.Exit(code=1)


def _guard_state_layout() -> None:
    """Fail loud before any journal-touching command runs if durable broker
    state is still in the pre-migration flat layout (ADR 0016 D4).

    Lazy-imports the seam (lazy-CLI doctrine, module docstring above) and
    converts a :class:`BrokerStateLayoutError` into the standard ``_fail``
    pattern used by every other refusal in this module. Called once per
    command, before that command reads or writes any per-env journal — a
    daemon or CLI command started against a flat legacy tree would silently
    treat itself as having no prior state, which is worse than refusing to
    run (see ``state_paths.assert_no_legacy_flat_state`` docstring).
    """
    from alphalens_pipeline.brokers.automanager.state_paths import (
        BrokerStateLayoutError,
        assert_no_legacy_flat_state,
    )

    try:
        assert_no_legacy_flat_state()
    except BrokerStateLayoutError as exc:
        raise _fail(str(exc)) from exc


def _cli_broker(*, mutating: bool) -> Broker:
    """Resolve the broker for a one-off command per ``ALPHALENS_BROKER_ENVIRONMENT``.

    The environment is read through ``state_paths.broker_environment()`` — the
    SAME seam every per-env journal path resolves through — so the gateway a
    command talks to and the journals it touches can never disagree (pre-fix,
    ``reconcile`` under ``env=live`` read the live journal but asked the SIM
    gateway). Under ``sim`` (the default) this is exactly the registry
    ``get_default_broker()`` path. Under ``live``:

    * ``mutating=True`` (ad-hoc placement, i.e. ``submit``) refuses loud
      BEFORE any broker construction — ADR 0017: the ``manage`` daemon is the
      ONLY LIVE placement path (the §4a probes are deliberate standalone
      scripts). ``submit`` has no broker-free preview (the dry-run still
      reads the account and prechecks server-side), so the WHOLE command
      refuses.
    * everything else — reads plus the risk-reducing ``cancel`` (same
      doctrine that keeps it ungated by ``ALLOW_ORDERS``; the LIVE
      manual-flatten runbook needs it) — builds the LIVE broker via the
      ADR 0017 factory ``create_saxo_broker_live_from_env`` (lazy import,
      house doctrine). The registry stays SIM-only per ADR 0017 — ``live``
      is never registered in ``_BROKER_FACTORIES``. The factory demands the
      daemon's FULL LIVE boot surface (the eight rail pins in soak bounds,
      ``SAXO_LIVE_ACCOUNT_KEY``, the standing grant, the ``SAXO_LIVE_*``
      auth env) before any network call, so ad-hoc LIVE commands only work
      from a shell that sources the daemon EnvironmentFile; a raw
      ``KeyError`` / ``SaxoError`` escaping the factory is converted to the
      standard ``_fail`` refusal here. The returned token provider is
      dropped — it exists for the daemon's SessionKeeper, which a one-shot
      command does not run.

    Echoes one ``env=<env> gateway=<sim|live|none|refused>`` line to STDERR at
    resolution time (stdout carries the result only — CLI convention);
    ``gateway=none`` marks the mutating-on-live refusal and ``gateway=refused``
    a failed LIVE construction — the ``live`` label is emitted only AFTER the
    factory succeeds, so the echo never claims a gateway that was not built.
    The ``mutating`` keyword has NO default on purpose: a future mutating
    caller must state its intent or it will not compile into the live-capable
    branch by accident (zen review).
    """
    from alphalens_pipeline.brokers.automanager import state_paths

    try:
        env = state_paths.broker_environment()
    except ValueError as exc:
        raise _fail(str(exc)) from exc

    if env != state_paths.ENV_LIVE:
        typer.secho(f"env={env} gateway=sim", err=True)
        from alphalens_pipeline.brokers.registry import get_default_broker

        return get_default_broker()

    if mutating:
        typer.secho(f"env={env} gateway=none", err=True)
        raise _fail(
            "env=live: ad-hoc placement on LIVE is forbidden by design "
            "(ADR 0017) — the `alphalens broker manage` daemon is the only "
            "LIVE placement path, and `submit` has no broker-free preview "
            "(the dry-run still reads the account and prechecks server-side), "
            "so the whole command refuses. Re-run with "
            "ALPHALENS_BROKER_ENVIRONMENT=sim, or hand the pick to the live "
            "daemon via `alphalens broker arm ... --env live`."
        )

    from alphalens_pipeline.brokers.saxo.broker import create_saxo_broker_live_from_env

    try:
        broker, _provider = create_saxo_broker_live_from_env()
    except KeyError as exc:
        # ``gateway=refused``: no live gateway was ever constructed — the echo
        # must not claim one (zen review: emit the success label only on success).
        typer.secho(f"env={env} gateway=refused", err=True)
        raise _fail(
            f"env=live: LIVE broker construction failed — missing env var {exc}. "
            "Ad-hoc LIVE commands need the daemon's full LIVE boot surface "
            "(rail pins + SAXO_LIVE_* auth env); source the daemon "
            "EnvironmentFile first."
        ) from exc
    except RuntimeError as exc:
        # Covers BrokerError (the rails' BrokerCapabilityError) AND SaxoError
        # (SaxoLiveEnvironmentBlockedError) — both RuntimeError subclasses,
        # but only the former would be rendered by the commands' `except
        # BrokerError`; converting HERE keeps every call site clean. The broad
        # catch is fail-closed by design, so keep the traceback for genuine
        # factory bugs in the log (they would otherwise render as a refusal).
        typer.secho(f"env={env} gateway=refused", err=True)
        logger.warning(
            "env=live: LIVE broker construction refused (%s)",
            type(exc).__name__,
            exc_info=True,
        )
        raise _fail(f"env=live: LIVE broker construction refused — {exc}") from exc
    typer.secho(f"env={env} gateway=live", err=True)
    return broker


def _wait_for_oauth_callback(port: int, path: str, timeout_s: int) -> tuple[str, str]:
    """One-shot localhost listener for the OAuth redirect; returns (code, state).

    Binds ``127.0.0.1`` (the bind ADDRESS is local plumbing — only the URL
    STRING registered at the portal must say ``localhost``). Any request off
    the redirect path gets a 404; the first request carrying ``code`` gets a
    tiny "you can close this tab" page. Raises ``TimeoutError`` when nothing
    lands within ``timeout_s``.
    """
    import http.server
    import time
    import urllib.parse

    result: dict[str, str] = {}

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urllib.parse.urlsplit(self.path)
            params = urllib.parse.parse_qs(parsed.query)
            code = (params.get("code") or [""])[0]
            if parsed.path != path or not code:
                self.send_error(404)
                return
            result["code"] = code
            result["state"] = (params.get("state") or [""])[0]
            body = b"<html><body>Authorized &mdash; you can close this tab.</body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: object) -> None:
            """Silence the default stderr access log (it would echo the query)."""

    server = http.server.HTTPServer(("127.0.0.1", port), _Handler)
    server.timeout = 1.0
    deadline = time.monotonic() + timeout_s
    try:
        while "code" not in result and time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    if "code" not in result:
        raise TimeoutError(f"no OAuth redirect within {timeout_s}s")
    return result["code"], result["state"]


def _auth_status() -> None:
    """Offline store inspection — zero network. Exit 0 iff the chain is alive."""
    import datetime as _dt

    from alphalens_pipeline.brokers.saxo.errors import SaxoAuthError
    from alphalens_pipeline.brokers.saxo.tokens import TokenStore, resolve_token_store_path

    store = TokenStore(resolve_token_store_path())
    try:
        state = store.load()
    except SaxoAuthError as exc:
        raise _fail(str(exc)) from exc
    typer.echo(f"store        {store.path}")
    if state is None:
        typer.echo("refresh      ABSENT — no OAuth session yet")
        raise _fail("no token store — run `alphalens broker auth` to bootstrap OAuth")
    now = _dt.datetime.now(_dt.UTC)
    access_left = (state.access_token_expires_at - now).total_seconds() / 60
    refresh_left = (state.refresh_token_expires_at - now).total_seconds() / 60
    typer.echo(f"environment  {state.environment}")
    typer.echo(f"app          {state.app_key_fingerprint} (sha256 fingerprint prefix)")
    typer.echo(f"obtained     {state.obtained_at.isoformat(timespec='seconds')}")
    if access_left > 0:
        typer.echo(f"access       valid, ~{access_left:.0f} min remaining")
    else:
        typer.echo("access       expired")
    if refresh_left > 0:
        typer.echo(f"refresh      ALIVE, ~{refresh_left:.0f} min remaining")
        return
    typer.echo(_REFRESH_DEAD_LINE)
    raise _fail("refresh chain is dead — re-run `alphalens broker auth`")


def _telegram_chain_loss_notify() -> NotificationPort:
    """Best-effort Saxo chain-loss Telegram alert (PR-4 composition root).

    This is the CLI's concrete ``NotificationPort`` for the OAuth provider's
    chain-loss alert — the exact behavior of the old
    ``tokens._send_chain_loss_telegram`` default, moved up here so
    ``brokers/`` stays free of the telegram import. Every failure path is
    swallowed (missing env = silent no-op; a send exception is logged, never
    raised) so a broken alert path can never crash the caller."""

    def _notify(message: str) -> None:
        import os

        from alphalens_pipeline.data.alt_data.telegram_client import TelegramClient

        try:
            bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
            chat_id = os.environ.get("TELEGRAM_CHAT_ID")
            if not bot_token or not chat_id:
                return
            TelegramClient(bot_token).send_message(chat_id, message)
        except Exception:
            logger.warning("saxo chain-loss Telegram alert failed", exc_info=True)

    return _notify


def _telegram_daemon_notify() -> NotificationPort:
    """Env-driven Telegram alert sink over the canonical TelegramClient
    (TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID) for the ``manage`` daemon's tick
    alerts. ``control_loop.build_default_deps`` journald-mirrors this via
    ``_journaled_alert`` before every delivery attempt. send_message never
    raises, so a delivery blip cannot crash a tick. Shared by the SIM and
    LIVE daemon instances; the composition root wraps it with
    ``_environment_labeled_notify`` (ADR 0017 §5).

    Operational alert bodies carry raw request-id reprs / reasons with `_`,
    `*`, `[` — under the client's default parse_mode="Markdown" those trip a
    Telegram 400 and the alert is SILENTLY dropped (defeating the
    safety-alert path). Send plain: parse_mode="" disables entity parsing so
    the body goes through verbatim."""
    import os

    from alphalens_pipeline.data.alt_data.telegram_client import TelegramClient

    client = TelegramClient(os.environ["TELEGRAM_BOT_TOKEN"])
    chat_id = os.environ["TELEGRAM_CHAT_ID"]

    def _notify(message: str) -> None:
        client.send_message(chat_id, message, parse_mode="")

    return _notify


def _environment_labeled_notify(sink: NotificationPort) -> NotificationPort:
    """Wrap a ``NotificationPort`` sink with an ``[env]`` label (ADR 0017 §5,
    memo "Telegram").

    This is the ONE composition-root wrapper: applied to both the daemon
    alert sink and the chain-loss sink, at the one site that resolves
    ``ALPHALENS_BROKER_ENVIRONMENT`` (``state_paths.broker_environment``,
    lazy-imported per house doctrine). Without it, a SIM and a LIVE daemon
    sharing one Telegram chat produce indistinguishable alert streams. The
    label is resolved ONCE at wrap time (the environment is fixed for the
    process lifetime) and the wrapped sink is a pure pass-through — the
    message is preserved verbatim after the prefix, and no other delivery
    behavior (parse_mode, throttling, ``_journaled_alert`` mirroring in
    ``control_loop``) changes. Deliberately NOT baked into
    ``_telegram_daemon_notify`` / ``_telegram_chain_loss_notify`` themselves,
    so those factories — and their existing tests — stay label-free and
    reusable outside a labeled context."""
    from alphalens_pipeline.brokers.automanager import state_paths

    prefix = f"[{state_paths.broker_environment()}] "

    def _notify(message: str) -> None:
        sink(prefix + message)

    return _notify


def _auth_refresh() -> None:
    """One silent refresh cycle — the ``alphalens-saxo-refresh.timer`` keep-alive primitive."""
    from alphalens_pipeline.brokers.saxo.errors import SaxoAuthError
    from alphalens_pipeline.brokers.saxo.tokens import OAuthTokenProvider

    try:
        OAuthTokenProvider.from_env(
            alert=_environment_labeled_notify(_telegram_chain_loss_notify())
        ).refresh_now()
    except SaxoAuthError as exc:
        raise _fail(f"refresh failed: {exc}") from exc
    typer.echo("refreshed — the rotated pair was persisted to the token store")


def _parse_redirect_url(redirect_url: str) -> tuple[int, str]:
    """Validate the registered redirect URL; return (port, path)."""
    import urllib.parse

    parts = urllib.parse.urlsplit(redirect_url)
    if parts.scheme != "http" or parts.hostname != "localhost":
        raise _fail(
            f"SAXO_AUTH_REDIRECT_URL={redirect_url!r} must use hostname "
            "'localhost' over plain http (Saxo rejects 127.0.0.1 "
            "registrations), e.g. a value registered as localhost with an "
            "explicit port and path"
        )
    port = parts.port or 80
    return port, parts.path or "/"


@broker_app.command(name="auth")
def auth_command(
    status: bool = typer.Option(
        False,
        "--status",
        help="Offline: print token-store state (zero network); exit 0 iff the "
        "refresh chain is alive.",
    ),
    refresh: bool = typer.Option(
        False,
        "--refresh",
        help="One silent refresh cycle via the OAuth provider (the keep-alive "
        "primitive the alphalens-saxo-refresh.timer runs every ~20 min). "
        "No browser.",
    ),
    timeout: int = typer.Option(
        300, "--timeout", help="Seconds to wait for the browser redirect (attended flow)."
    ),
    no_browser: bool = typer.Option(
        False, "--no-browser", help="Print the authorize URL only (headless / SSH)."
    ),
) -> None:
    """Bootstrap or inspect the Saxo SIM OAuth session (Code grant; since
    ADR 0017 the LIVE order rail reuses the separate ``marketdata-auth``
    ``saxo_auth_live`` chain instead — this command never touches it).

    Attended flow: opens the SIM login in a browser, catches the redirect on
    a one-shot localhost listener, exchanges the code, and persists the token
    pair (0600) to the store. The refresh chain dies after ~40 min without a
    refresh — re-run this command whenever ``--status`` reports it dead.
    Tokens are never printed or logged.
    """
    import contextlib
    import hmac
    import webbrowser

    from alphalens_pipeline.brokers.saxo.errors import SaxoAuthError
    from alphalens_pipeline.brokers.saxo.oauth import SaxoAuthClient, generate_state
    from alphalens_pipeline.brokers.saxo.tokens import (
        APP_KEY_ENV,
        APP_SECRET_ENV,
        REDIRECT_URL_ENV,
        TokenStore,
        _require_env,
        resolve_token_store_path,
    )

    if status:
        _auth_status()
        return
    if refresh:
        _auth_refresh()
        return

    try:
        app_key = _require_env(APP_KEY_ENV)
        app_secret = _require_env(APP_SECRET_ENV)
        redirect_url = _require_env(REDIRECT_URL_ENV)
    except SaxoAuthError as exc:
        raise _fail(str(exc)) from exc
    port, callback_path = _parse_redirect_url(redirect_url)
    typer.echo(
        "note: the redirect URL must byte-match the portal registration "
        "(Code-grant matching is port- AND path-exact)"
    )

    auth_client = SaxoAuthClient(app_key, app_secret)
    state = generate_state()
    authorize_url = auth_client.build_authorize_url(redirect_url, state)
    typer.echo("open this URL to authorize (SIM credentials):")
    typer.echo(authorize_url)
    if not no_browser:
        with contextlib.suppress(Exception):
            webbrowser.open(authorize_url)

    typer.echo(f"waiting up to {timeout}s for the redirect on {redirect_url} ...")
    try:
        code, received_state = _wait_for_oauth_callback(port, callback_path, timeout)
    except TimeoutError as exc:  # BEFORE OSError — TimeoutError is its subclass
        raise _fail(
            f"{exc} — check that the registered redirect URL matches "
            f"{redirect_url!r} exactly, then retry"
        ) from exc
    except OSError as exc:
        raise _fail(
            f"could not listen on localhost:{port} ({exc}) — free the port or "
            "change the registered redirect URL (and the env var) to another one"
        ) from exc

    if not hmac.compare_digest(state.encode("utf-8"), received_state.encode("utf-8")):
        raise _fail(
            "state parameter mismatch on the OAuth redirect (possible CSRF or "
            "a stale browser tab) — nothing was exchanged; retry "
            "`alphalens broker auth`"
        )

    try:
        bundle = auth_client.exchange_code(code, redirect_url)
    except SaxoAuthError as exc:
        raise _fail(
            f"token exchange failed: {exc} — check SAXO_APP_KEY / "
            "SAXO_APP_SECRET / the registered redirect URL"
        ) from exc

    store = TokenStore(resolve_token_store_path())
    stored = store.save_bundle(bundle, app_key=app_key)
    typer.echo("authorized — OAuth session established (tokens are never displayed)")
    typer.echo(f"store           {store.path}")
    typer.echo(
        f"access expires  ~{bundle.expires_in // 60} min ({stored.access_token_expires_at.isoformat(timespec='seconds')})"
    )
    typer.echo(
        f"refresh expires ~{bundle.refresh_token_expires_in // 60} min "
        f"({stored.refresh_token_expires_at.isoformat(timespec='seconds')})"
    )
    typer.echo(
        "warning: the refresh chain dies after ~40 min without a refresh; "
        "re-run this command if `alphalens broker auth --status` reports it dead"
    )


def _marketdata_auth_status() -> None:
    """Offline LIVE market-data store inspection — zero network, no token
    values surface, and NO app credentials. Exit 0 iff the refresh chain is
    alive.

    Mirrors the SIM rail's ``broker auth --status``: reads only the token-store
    path (``SAXO_LIVE_TOKEN_STORE_PATH`` or the default), never the app
    key/secret/redirect — so a monitoring probe on a host that carries only the
    store path can still report the store state instead of failing on missing
    LIVE env."""
    from alphalens_pipeline.data.alt_data.saxo_marketdata_auth import inspect_store

    try:
        state = inspect_store()
    except RuntimeError as exc:
        raise _fail(str(exc)) from exc
    typer.echo(f"store        {state.store_path}")
    if not state.present:
        typer.echo("refresh      ABSENT — no LIVE market-data OAuth session yet")
        raise _fail(
            "no token store — run `alphalens broker marketdata-auth` to bootstrap the "
            "LIVE market-data OAuth session"
        )
    now = dt.datetime.now(dt.UTC)
    if state.access_expires_at is not None:
        access_left = (state.access_expires_at - now).total_seconds() / 60
        label = "valid" if state.access_valid else "expired"
        # Echo the remaining minutes even when expired (SIM `broker auth`
        # parity) — a negative value reads as `~-N min`.
        typer.echo(f"access       {label}, ~{access_left:.0f} min remaining")
    else:
        typer.echo("access       expired")
    if state.refresh_expires_at is not None:
        # The store records the refresh token's own expiry: report TRUE liveness
        # instead of trusting that a bare refresh-token string is still valid.
        refresh_left = (state.refresh_expires_at - now).total_seconds() / 60
        if refresh_left > 0:
            typer.echo(f"refresh      ALIVE, ~{refresh_left:.0f} min remaining")
            return
        typer.echo(_REFRESH_DEAD_LINE)
        raise _fail("refresh chain is dead — re-run `alphalens broker marketdata-auth`")
    if state.refresh_present:
        # Legacy store without a recorded refresh expiry: unknown window, fall
        # back to reporting the present single-use rotating token as alive.
        typer.echo("refresh      ALIVE — single-use rotating token present")
        return
    typer.echo(_REFRESH_DEAD_LINE)
    raise _fail("refresh chain is dead — re-run `alphalens broker marketdata-auth`")


def _marketdata_auth_refresh(cfg: object) -> None:
    """One silent LIVE market-data refresh cycle (keep-alive primitive)."""
    from alphalens_pipeline.data.alt_data.saxo_marketdata_auth import LiveTokenProvider

    try:
        LiveTokenProvider(cfg).force_refresh()  # type: ignore[arg-type]
    except RuntimeError as exc:
        raise _fail(f"refresh failed: {exc}") from exc
    typer.echo("refreshed — the rotated LIVE market-data pair was persisted to the token store")


@broker_app.command(name="marketdata-auth")
def marketdata_auth_command(
    status: bool = typer.Option(
        False,
        "--status",
        help="Offline: print the LIVE market-data token-store state (zero network); "
        "exit 0 iff the refresh chain is alive.",
    ),
    refresh: bool = typer.Option(
        False,
        "--refresh",
        help="One silent refresh cycle via the LIVE market-data token provider "
        "(keep-alive primitive for a systemd timer). No browser.",
    ),
    timeout: int = typer.Option(
        300, "--timeout", help="Seconds to wait for the browser redirect (attended flow)."
    ),
    no_browser: bool = typer.Option(
        False, "--no-browser", help="Print the authorize URL only (headless / SSH)."
    ),
) -> None:
    """Bootstrap or inspect the Saxo LIVE OAuth session (app ``bracket-keeper``).

    Originally market-data-only; since ADR 0017 the LIVE ORDER rail REUSES this
    same ``saxo_auth_live`` chain (``create_saxo_broker_live_from_env`` adapts it
    via ``live_tokens.LiveOrderTokenProvider``) — keeping this chain alive is a
    LIVE-daemon precondition. It mirrors ``alphalens broker auth`` but targets
    the LIVE auth path: a SEPARATE token store (``~/.alphalens/saxo_auth_live/``),
    a SEPARATE app registration, and the LIVE logon host. The SIM order-rail
    OAuth store (``~/.alphalens/saxo_auth/``) is never touched. Tokens are never
    printed or logged.
    """
    import contextlib
    import hmac
    import secrets
    import webbrowser

    from alphalens_pipeline.data.alt_data.saxo_marketdata_auth import (
        LiveAuthConfig,
        build_authorize_url,
        exchange_code,
    )

    # --status is offline store inspection: it needs only the token-store path,
    # NOT the app creds, so resolve it BEFORE from_env (SIM `--status` parity).
    if status:
        _marketdata_auth_status()
        return

    # --refresh and the attended flow both hit the LIVE token endpoint, so they
    # do require the full app config.
    try:
        cfg = LiveAuthConfig.from_env()
    except RuntimeError as exc:
        raise _fail(str(exc)) from exc

    if refresh:
        _marketdata_auth_refresh(cfg)
        return

    port, callback_path = _parse_redirect_url(cfg.redirect_url)
    typer.echo(
        "note: the redirect URL must byte-match the portal registration "
        "(Code-grant matching is port- AND path-exact)"
    )

    state = secrets.token_urlsafe(32)
    authorize_url = build_authorize_url(cfg, state)
    typer.echo("open this URL to authorize (LIVE market-data credentials):")
    typer.echo(authorize_url)
    if not no_browser:
        with contextlib.suppress(Exception):
            webbrowser.open(authorize_url)

    typer.echo(f"waiting up to {timeout}s for the redirect on {cfg.redirect_url} ...")
    try:
        code, received_state = _wait_for_oauth_callback(port, callback_path, timeout)
    except TimeoutError as exc:  # BEFORE OSError — TimeoutError is its subclass
        raise _fail(
            f"{exc} — check that the registered redirect URL matches "
            f"{cfg.redirect_url!r} exactly, then retry"
        ) from exc
    except OSError as exc:
        raise _fail(
            f"could not listen on localhost:{port} ({exc}) — free the port or "
            "change the registered redirect URL (and the env var) to another one"
        ) from exc

    if not hmac.compare_digest(state.encode("utf-8"), received_state.encode("utf-8")):
        raise _fail(
            "state parameter mismatch on the OAuth redirect (possible CSRF or "
            "a stale browser tab) — nothing was exchanged; retry "
            "`alphalens broker marketdata-auth`"
        )

    try:
        bundle = exchange_code(cfg, code=code)
    except RuntimeError as exc:
        raise _fail(
            f"token exchange failed: {exc} — check SAXO_LIVE_APP_KEY / "
            "SAXO_LIVE_APP_SECRET / the registered redirect URL"
        ) from exc

    # exchange_code persists via the public save_bundle; report from the bundle
    # WITHOUT echoing any token value.
    typer.echo(
        "authorized — LIVE market-data OAuth session established (tokens are never displayed)"
    )
    typer.echo(f"store           {cfg.store_path}")
    typer.echo(f"access expires  ~{int(bundle.get('expires_in', 0)) // 60} min")
    refresh_expires_in = bundle.get("refresh_token_expires_in")
    if refresh_expires_in:
        typer.echo(f"refresh expires ~{int(refresh_expires_in) // 60} min")
    else:
        typer.echo("refresh         stored (single-use, rotates on every refresh)")


@broker_app.command(name="account")
def account_command() -> None:
    """Print the broker account snapshot (cash, total value, margin)."""
    from broker_contract.contract import BrokerError

    try:
        snapshot = _cli_broker(mutating=False).get_account()
    except BrokerError as exc:
        raise _fail(f"broker account failed: {exc}") from exc

    margin = "n/a" if snapshot.margin_available is None else f"{snapshot.margin_available:,.2f}"
    typer.echo(f"account   {snapshot.account_id}")
    typer.echo(f"currency  {snapshot.currency}")
    typer.echo(f"cash      {snapshot.cash:,.2f}")
    typer.echo(f"total     {snapshot.total_value:,.2f}")
    typer.echo(f"margin    {margin}")
    typer.echo(f"asof      {snapshot.asof.isoformat(timespec='seconds')}")


@broker_app.command(name="positions")
def positions_command() -> None:
    """List open positions (signed quantity, avg price, market value, PnL)."""
    from broker_contract.contract import BrokerError

    try:
        positions = _cli_broker(mutating=False).get_positions()
    except BrokerError as exc:
        raise _fail(f"broker positions failed: {exc}") from exc

    if not positions:
        typer.echo("no open positions")
        return
    for position in positions:
        market_value = "n/a" if position.market_value is None else f"{position.market_value:,.2f}"
        pnl = "n/a" if position.unrealized_pnl is None else f"{position.unrealized_pnl:+,.2f}"
        typer.echo(
            f"{position.instrument.broker_symbol:16s} "
            f"qty {position.quantity:+10.2f}  "
            f"avg {position.avg_price:10.2f}  "
            f"mv {market_value:>12s}  "
            f"pnl {pnl:>12s}  "
            f"id {position.position_id}"
        )


@broker_app.command(name="resolve")
def resolve_command(
    ticker: str = typer.Argument(..., help="Plain ticker, e.g. KO."),
    exchange: str = typer.Option(
        "XNYS",
        "--exchange",
        help="ISO 10383 MIC of the listing venue (XNYS, XNAS, XWAR).",
    ),
) -> None:
    """Resolve (ticker, MIC) to the broker instrument handle (Saxo: Uic)."""
    from broker_contract.contract import BrokerError

    try:
        ref = _cli_broker(mutating=False).resolve_instrument(ticker, exchange)
    except BrokerError as exc:
        raise _fail(f"broker resolve failed: {exc}") from exc

    typer.echo(f"ticker        {ref.ticker}")
    typer.echo(f"exchange_mic  {ref.exchange_mic}")
    typer.echo(f"asset_type    {ref.asset_type}")
    typer.echo(f"broker_id     {ref.broker_instrument_id}")
    typer.echo(f"symbol        {ref.broker_symbol}")
    typer.echo(f"currency      {ref.currency or 'n/a'}")


def _echo_bracket_table(brackets: list) -> None:
    # Human labels only: E{n}/SL/TP columns and the FAITHFUL entry label from
    # each bracket's setup-plan tier_index (E2 when tier 0 was dropped for zero
    # qty), never a 0-based placement index. The machine journal keeps raw
    # values. Width 3 fits E10 (lazy-CLI: import inside the body).
    from alphalens_pipeline.brokers.automanager.labels import human_entry_label

    typer.echo(
        f"{'E':>3s}  {'qty':>6s}  {'entry':>10s}  {'SL':>10s}  {'TP':>10s}  "
        f"{'ttl':>4s}  client_request_id"
    )
    for bracket in brackets:
        tp = "-" if bracket.take_profit is None else f"{bracket.take_profit:.4f}"
        stop = "-" if bracket.stop_loss is None else f"{bracket.stop_loss:.4f}"
        typer.echo(
            f"{human_entry_label(bracket.tier_index):>3s}  {bracket.quantity:>6d}  "
            f"{bracket.entry_limit:>10.4f}  {stop:>10s}  {tp:>10s}  "
            f"{bracket.entry_ttl_days:>4d}  {bracket.client_request_id}"
        )


def _assert_fx_precheck_cross_checks(
    *,
    entry_label: str,
    ticker: str,
    payload: dict,
    fx: object,
    account_currency: str,
    divergence_max_pct: float,
    divergence_fn: Callable[[float, float], float],
) -> float:
    """FX-path precheck cross-checks (FX-leg memo §4.3 item 5); refuse on any miss.

    (a) ``EstimatedCashRequiredCurrency`` must equal the account currency —
    anything else (including absent) means the account model is not what we
    think. (b) Saxo's ``InstrumentToAccountConversionRate`` (instrument->
    account direction) inverted must agree with the sizing rate within the
    policy bound. Returns the verbatim precheck rate for the journal.
    ``fx`` / ``divergence_fn`` stay duck-typed so this helper adds no
    top-level pipeline import (lazy-CLI doctrine).
    """
    est_cash_currency = payload.get("EstimatedCashRequiredCurrency")
    if est_cash_currency != account_currency:
        raise _fail(
            f"{ticker}: precheck {entry_label} EstimatedCashRequiredCurrency="
            f"{est_cash_currency!r} does not match the account currency "
            f"{account_currency!r} — the account model is not what we think; "
            "refusing placement"
        )
    conversion_rate_raw = payload.get("InstrumentToAccountConversionRate")
    try:
        conversion_rate = float(conversion_rate_raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        conversion_rate = 0.0
    if conversion_rate <= 0:
        raise _fail(
            f"{ticker}: precheck {entry_label} carries no usable "
            f"InstrumentToAccountConversionRate ({conversion_rate_raw!r}) — the "
            "independent FX cross-check cannot run; refusing placement"
        )
    sizing_rate: float = fx.rate  # type: ignore[attr-defined]
    try:
        divergence = divergence_fn(sizing_rate, conversion_rate)
    except ValueError as exc:
        # Belt: both rates are validated positive above/at FxConversion build,
        # but a helper-level ValueError must surface as a clean refusal, never
        # a traceback (review finding, PR #849).
        raise _fail(f"{ticker}: precheck {entry_label} FX divergence check failed: {exc}") from exc
    if divergence > divergence_max_pct:
        raise _fail(
            f"{ticker}: precheck {entry_label} FX divergence {divergence:.2f}% exceeds the "
            f"{divergence_max_pct}% bound — sizing rate {sizing_rate:.6f} "
            f"(account->instrument) vs Saxo {conversion_rate:.6f} "
            "(instrument->account, inverted before comparing); refusing placement"
        )
    typer.echo(
        f"precheck {entry_label}: fx cross-check ok — saxo rate {conversion_rate:.6f} "
        f"(instrument->account), divergence {divergence:.2f}% <= {divergence_max_pct}%"
    )
    return conversion_rate


def _resolve_instrument_and_plan(
    *,
    wanted: str,
    exchange: str | None,
    equity: float | None,
    scale_factor: float,
    trade_setup: dict,
) -> tuple:
    """Resolve the instrument, read the account, and size the setup plan.

    Extracted from ``submit_command`` to keep it a short orchestration: the
    broker read, the cross-currency FX-rate resolution, and the sizing call
    live here. Lazy imports keep the ``alphalens`` binary's startup cost off
    this path (lazy-CLI doctrine). Returns
    ``(broker, account, sizing_equity, instrument, fx, plan)``.
    """
    from alphalens_pipeline.brokers import execution as execution_policy
    from alphalens_pipeline.brokers.execution import build_fx_conversion
    from alphalens_pipeline.brokers.routing import resolve_us_instrument
    from alphalens_pipeline.paper.sizing import parse_brief_to_spec
    from broker_contract.contract import BrokerError
    from broker_contract.sizing import TradeSetupNotPlannableError, compute_setup_plan

    try:
        # mutating=True: submit is ad-hoc PLACEMENT — under env=live the whole
        # command refuses HERE, before the account read below (there is no
        # broker-free preview; ADR 0017 keeps LIVE placement daemon-only).
        broker = _cli_broker(mutating=True)
        # The account read is unconditional now: the BUDGET is the account
        # currency (FX-leg memo §7 Q1 operator decision), so the currency
        # compare needs AccountSnapshot.currency even with --equity given.
        account = broker.get_account()
        sizing_equity = equity if equity is not None else account.total_value
        instrument = resolve_us_instrument(broker, wanted, exchange_mic=exchange)
        if not instrument.currency:
            raise _fail(
                f"{wanted}: broker {broker.name!r} resolve stamped no instrument "
                "currency — cannot verify the account-vs-instrument currency; "
                "refusing to size (never MIC-inferred, never guessed)"
            )
        fx = None
        if instrument.currency != account.currency:
            get_fx_rate = getattr(broker, "get_fx_rate", None)
            if get_fx_rate is None:
                raise _fail(
                    f"{wanted} trades in {instrument.currency} but the account is "
                    f"{account.currency}, and broker {broker.name!r} exposes no "
                    "get_fx_rate capability — refusing to size cross-currency "
                    f"(policy {execution_policy._MISSING_FX_RATE_POLICY!r})"
                )
            fx = build_fx_conversion(get_fx_rate(account.currency, instrument.currency))
        spec = parse_brief_to_spec(trade_setup)
        plan = compute_setup_plan(
            spec,
            paper_equity=sizing_equity,
            scale_factor=scale_factor,
            fx=fx,
        )
    except TradeSetupNotPlannableError as exc:
        raise _fail(f"{wanted} is not plannable: {exc}") from exc
    except BrokerError as exc:
        raise _fail(f"broker submit failed: {exc}") from exc
    return broker, account, sizing_equity, instrument, fx, plan


def _run_prechecks(
    *,
    broker: object,
    brackets: list,
    fx: object,
    wanted: str,
    account_currency: str,
) -> tuple[list[dict], float | None]:
    """Precheck every bracket server-side (places nothing); FX-path cross-checks.

    Extracted from ``submit_command``. On the FX path the precheck is also the
    SECOND, independent rate source (see the caller's comment). Returns
    ``(precheck_summaries, precheck_conversion_rate)``.
    """
    from alphalens_pipeline.brokers import execution as execution_policy
    from alphalens_pipeline.brokers.automanager.labels import human_entry_label
    from alphalens_pipeline.brokers.execution import fx_precheck_divergence_pct
    from broker_contract.contract import BrokerError

    precheck_summaries: list[dict] = []
    precheck_conversion_rate: float | None = None
    precheck_fn = getattr(broker, "precheck_bracket_order", None)
    if precheck_fn is None:
        typer.echo("precheck: not supported by this broker — skipping")
        return precheck_summaries, precheck_conversion_rate
    for bracket in brackets:
        # FAITHFUL entry label from the setup-plan tier_index, not the 0-based
        # placement position (E2 when tier 0 was dropped for zero qty).
        entry_label = human_entry_label(bracket.tier_index)
        try:
            payload = precheck_fn(bracket)
        except BrokerError as exc:
            raise _fail(f"precheck failed for entry tier {entry_label}: {exc}") from exc
        est_cash_currency = payload.get("EstimatedCashRequiredCurrency")
        summary = {
            "client_request_id": bracket.client_request_id,
            "PreCheckResult": payload.get("PreCheckResult"),
            "EstimatedCashRequired": payload.get("EstimatedCashRequired"),
            "EstimatedCashRequiredCurrency": est_cash_currency,
            "InstrumentToAccountConversionRate": payload.get("InstrumentToAccountConversionRate"),
            "Costs": payload.get("Cost", payload.get("Costs")),
        }
        precheck_summaries.append(summary)
        est_cash_label = (
            f"{summary['EstimatedCashRequired']!r}"
            if est_cash_currency is None
            else f"{summary['EstimatedCashRequired']!r} {est_cash_currency}"
        )
        typer.echo(
            f"precheck {entry_label}: result={summary['PreCheckResult']!r} "
            f"est_cash={est_cash_label} costs={summary['Costs']!r}"
        )
        if fx is not None:
            precheck_conversion_rate = _assert_fx_precheck_cross_checks(
                entry_label=entry_label,
                ticker=wanted,
                payload=payload,
                fx=fx,
                account_currency=account_currency,
                divergence_max_pct=(execution_policy._FX_PRECHECK_RATE_DIVERGENCE_MAX_PCT),
                divergence_fn=fx_precheck_divergence_pct,
            )
    return precheck_summaries, precheck_conversion_rate


def _place_and_record(
    *,
    broker: Broker,
    brackets: list,
    brief_date: dt.date,
    wanted: str,
    instrument: InstrumentRef,
    precheck_summaries: list[dict],
    account_currency: str,
    sizing_equity: float,
    fx: FxConversion | None,
    precheck_conversion_rate: float | None,
) -> None:
    """Place each bracket, journal the outcome, then raise on any failure.

    Extracted from ``submit_command``. The submission record is written in a
    ``finally`` so a mid-run BrokerError still journals the already-placed
    entries; the command then exits non-zero with the reconcile hint.

    Only ever reached on ``--execute`` (dry-run returns before this is
    called), so the legacy-layout guard belongs HERE rather than earlier in
    ``submit_command`` — a dry-run must never refuse on stale local state it
    is not about to touch. The guard runs before the first
    ``broker.place_bracket_order`` call: placing orders and then failing to
    journal them would be the worst outcome (ADR 0016 D4).
    """
    _guard_state_layout()

    from alphalens_pipeline.brokers.execution import execution_config_version
    from alphalens_pipeline.brokers.submission_log import (
        append_submission_record,
        build_submission_record,
    )
    from broker_contract.contract import BrokerError

    placed_records: list[dict] = []
    failure_note: str | None = None
    try:
        for bracket in brackets:
            placed = broker.place_bracket_order(bracket)
            placed_records.append(
                {
                    "client_request_id": bracket.client_request_id,
                    "entry_order_id": placed.entry_order_id,
                    "exit_order_ids": list(placed.exit_order_ids),
                    "qty": bracket.quantity,
                    "entry": bracket.entry_limit,
                    "stop": bracket.stop_loss,
                    "tp": bracket.take_profit,
                    "ttl": bracket.entry_ttl_days,
                }
            )
            typer.echo(
                f"placed entry={placed.entry_order_id} "
                f"exits={','.join(placed.exit_order_ids) or '-'} "
                f"(request {bracket.client_request_id})"
            )
    except BrokerError as exc:
        failure_note = (
            f"placement stopped after {len(placed_records)}/{len(brackets)} bracket(s): {exc}"
        )
    finally:
        if placed_records or failure_note:
            record = build_submission_record(
                brief_date=brief_date.isoformat(),
                ticker=wanted,
                mic=instrument.exchange_mic,
                uic=instrument.broker_instrument_id,
                brackets=placed_records,
                precheck=precheck_summaries,
                note=failure_note,
                sizing_currency=account_currency,
                instrument_currency=instrument.currency,
                sizing_equity=sizing_equity,
                fx=fx,
                precheck_conversion_rate=precheck_conversion_rate,
            )
            path = append_submission_record(record)
            typer.echo(f"submission recorded: {path}")

    token = execution_config_version()
    typer.echo(f"execution_config_version {token}")
    if failure_note:
        placed_ids = [r["entry_order_id"] for r in placed_records]
        raise _fail(
            f"{failure_note}\nalready-placed entry orders: {placed_ids or 'none'} — "
            "reconcile via 'alphalens broker orders' / 'alphalens broker cancel <id>'"
        )


@broker_app.command(name="submit")
def submit_command(
    ticker: str = typer.Argument(..., help="Plain ticker from the brief, e.g. KO."),
    date: str = typer.Option(..., "--date", help="Brief date (YYYY-MM-DD)."),
    briefs_dir: Path = typer.Option(
        _DEFAULT_BRIEFS_DIR, "--briefs-dir", help="Thematic briefs parquet directory."
    ),
    exchange: str | None = typer.Option(
        None,
        "--exchange",
        help="Explicit ISO 10383 MIC; omit to probe US venues (XNYS then XNAS). "
        "Non-US venues (XWAR) are explicit-only.",
    ),
    equity: float | None = typer.Option(
        None, "--equity", help="Sizing equity in account currency; default: broker total value."
    ),
    scale_factor: float = typer.Option(
        1.0, "--scale-factor", help="Daily global scale factor (see paper/sizing.py); default 1.0."
    ),
    execute: bool = typer.Option(
        False,
        "--execute",
        help="Actually place the brackets (default is DRY-RUN: table + precheck only). "
        "Also requires ALPHALENS_BROKER_ALLOW_ORDERS=1 in the environment.",
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip the interactive confirmation (scripted use)."
    ),
) -> None:
    """Decompose one candidate's trade setup into per-tier brackets and submit.

    DRY-RUN BY DEFAULT: prints the decomposed bracket table and runs the
    order precheck (validates server-side, places NOTHING). Sending requires
    --execute AND an interactive confirmation (--yes skips it) AND the
    ALPHALENS_BROKER_ALLOW_ORDERS=1 env gate enforced inside the broker.
    """
    from alphalens_pipeline.brokers.execution import decompose_setup_plan
    from alphalens_pipeline.paper.brief_loader import load_brief
    from broker_contract.sizing import (
        setup_plan_gross_guard_limit,
        setup_plan_gross_notional,
    )

    try:
        brief_date = dt.date.fromisoformat(date)
    except ValueError as exc:
        raise _fail(f"invalid --date {date!r}: {exc}") from exc

    try:
        candidates = load_brief(brief_date, briefs_dir)
    except (FileNotFoundError, ValueError) as exc:
        raise _fail(str(exc)) from exc

    wanted = ticker.upper()
    candidate = next((c for c in candidates if c.ticker.upper() == wanted), None)
    if candidate is None:
        raise _fail(f"{wanted} not in the {brief_date} brief ({len(candidates)} candidates)")
    if candidate.trade_setup is None:
        raise _fail(f"{wanted} has no parseable brief_trade_setup on {brief_date}")

    broker, account, sizing_equity, instrument, fx, plan = _resolve_instrument_and_plan(
        wanted=wanted,
        exchange=exchange,
        equity=equity,
        scale_factor=scale_factor,
        trade_setup=candidate.trade_setup,
    )

    gross = setup_plan_gross_notional(plan)
    gross_limit = setup_plan_gross_guard_limit(plan)
    if gross > gross_limit:
        raise _fail(
            f"{wanted}: planned gross {gross:,.2f} {instrument.currency} exceeds the "
            f"gross safety guard {gross_limit:,.2f} {instrument.currency} "
            "(GROSS_SAFETY_FRAC x equity, one currency through the sizing rate) — "
            "nothing submitted"
        )

    brackets = decompose_setup_plan(plan, instrument)
    if not brackets:
        raise _fail(f"{wanted}: every entry tier sized to zero shares — nothing to submit")

    typer.echo(
        f"{wanted} @ {instrument.exchange_mic} (Uic {instrument.broker_instrument_id})  "
        f"equity={sizing_equity:,.2f} {account.currency}  scale_factor={scale_factor}"
    )
    if fx is not None:
        typer.echo(
            f"fx: {fx.account_currency} {plan.total_notional:,.2f} -> "
            f"{fx.instrument_currency} {plan.sizing_notional:,.2f} @ {fx.rate:.4f} mid "
            f"({fx.price_type}, buffer {fx.sizing_buffer_pct:.1f}%, {fx.source})"
        )
    _echo_bracket_table(brackets)

    # Precheck every bracket (validates server-side, places nothing). On the
    # FX path the precheck is also the SECOND, independent rate source: its
    # EstimatedCashRequiredCurrency must match the account currency, and its
    # InstrumentToAccountConversionRate (instrument->account direction — the
    # INVERSE of the sizing rate) must agree with the sizing rate within the
    # policy divergence bound; any failure refuses placement.
    precheck_summaries, precheck_conversion_rate = _run_prechecks(
        broker=broker,
        brackets=brackets,
        fx=fx,
        wanted=wanted,
        account_currency=account.currency,
    )

    if not execute:
        typer.echo("DRY-RUN: nothing was sent. Re-run with --execute to place these brackets.")
        return

    if not yes:
        typer.confirm(
            f"Send {len(brackets)} bracket(s) for {wanted} to the Saxo SIM gateway?",
            abort=True,
        )

    _place_and_record(
        broker=broker,
        brackets=brackets,
        brief_date=brief_date,
        wanted=wanted,
        instrument=instrument,
        precheck_summaries=precheck_summaries,
        account_currency=account.currency,
        sizing_equity=sizing_equity,
        fx=fx,
        precheck_conversion_rate=precheck_conversion_rate,
    )


@broker_app.command(name="arm")
def arm_command(
    ticker: str = typer.Argument(..., help="Plain ticker from the brief, e.g. KO."),
    date: str = typer.Option(..., "--date", help="Brief date (YYYY-MM-DD)."),
    briefs_dir: Path = typer.Option(
        _DEFAULT_BRIEFS_DIR, "--briefs-dir", help="Thematic briefs parquet directory."
    ),
    env: str = typer.Option(
        _DEFAULT_ARM_ENV,
        "--env",
        help="Broker instance inbox to arm into: 'sim' or 'live' (ADR 0016). Default: sim.",
    ),
) -> None:
    """Arm a picked candidate — parse the brief into a TradeIntent client-side
    and append it to the picks queue.

    A PURE EXECUTOR: it carries no selection / filtering logic. After the
    structural checks (parquet present, ticker present, row has a plannable
    trade_setup) it parses the brief's trade_setup into a
    :class:`~broker_contract.trade_intent.schema.TradeIntent` (memo
    section 5, PR-7) and appends ONE 'armed' line carrying the full intent to
    picks.jsonl. The VPS control loop drains the queue and never touches a
    brief; this command places nothing.

    No selection-policy filter (2026-08-03): the arm-time earnings-window gate
    was removed. Selection filters — earnings-window avoidance included — belong
    at brief-creation (the selection tier), so a filtered-out candidate never
    reaches the brief. The client invoking arm is responsible for knowing what
    it arms; the command never second-guesses it.

    A pick belongs to exactly one instance (ADR 0016 D6): ``--env`` selects
    which instance's inbox (``<env>/picks.jsonl``) the armed intent lands in,
    via the ``state_paths`` seam — the daemon drains only its own inbox, so
    cross-instance placement is impossible by construction.
    """
    from alphalens_pipeline.brokers.automanager import state_paths
    from alphalens_pipeline.brokers.automanager.picks import arm_pick
    from alphalens_pipeline.paper.brief_loader import load_brief
    from alphalens_pipeline.paper.sizing import build_exit_geometry_spec, parse_brief_to_spec
    from broker_contract.sizing import TradeSetupNotPlannableError
    from broker_contract.trade_intent.schema import InstrumentHint, IntentMeta, TradeIntent

    try:
        brief_date = dt.date.fromisoformat(date)
    except ValueError as exc:
        raise _fail(f"invalid --date {date!r}: {exc}") from exc

    try:
        picks_target = state_paths.picks_path(env=env)
    except ValueError as exc:
        raise _fail(str(exc)) from exc

    _guard_state_layout()

    try:
        candidates = load_brief(brief_date, briefs_dir)
    except (FileNotFoundError, ValueError) as exc:
        raise _fail(str(exc)) from exc

    wanted = ticker.upper()
    candidate = next((c for c in candidates if c.ticker.upper() == wanted), None)
    if candidate is None:
        raise _fail(f"{wanted} not in the {brief_date} brief ({len(candidates)} candidates)")
    if candidate.trade_setup is None:
        raise _fail(f"{wanted}: no plannable trade_setup on {brief_date}")

    try:
        spec = parse_brief_to_spec(candidate.trade_setup)
    except TradeSetupNotPlannableError as exc:
        raise _fail(f"{wanted}: trade_setup not plannable — {exc}") from exc

    exit_spec = build_exit_geometry_spec(
        candidate.trade_setup, candidate.technical_pct_off_52w_high
    )

    intent = TradeIntent(
        intent_id=f"{wanted}:{brief_date.isoformat()}",
        instrument=InstrumentHint(ticker=wanted, mic=_ARM_INSTRUMENT_MIC),
        spec=spec,
        exit=exit_spec,
        meta=IntentMeta(
            armed_ts=dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
            brief_date=brief_date.isoformat(),
        ),
    )
    arm_pick(intent, path=picks_target)
    typer.echo(f"armed {wanted} @ {brief_date.isoformat()} -> {picks_target}")


@broker_app.command(name="orders")
def orders_command() -> None:
    """List open orders (entry + exit children; UNKNOWN never guessed)."""
    from broker_contract.contract import BrokerError

    try:
        states = _cli_broker(mutating=False).list_open_orders()
    except BrokerError as exc:
        raise _fail(f"broker orders failed: {exc}") from exc

    if not states:
        typer.echo("no open orders")
        return
    for state in states:
        symbol = state.instrument.broker_symbol if state.instrument else "?"
        typer.echo(
            f"{state.order_id:12s} {state.status.value:16s} "
            f"filled {state.filled_quantity:10.2f}  {symbol:16s} raw={state.raw_status}"
        )


@broker_app.command(name="reconcile")
def reconcile_command(
    journal: Path | None = typer.Option(
        None,
        "--journal",
        help="Submission journal path (default: "
        "~/.alphalens/broker_orders/<env>/submissions.jsonl) "
        "(<env> = $ALPHALENS_BROKER_ENVIRONMENT, default sim).",
    ),
    as_json: bool = typer.Option(
        False,
        "--json",
        help="Emit the verdict dicts as JSON (incl. raw Status/SubStatus diagnostics, "
        "reason codes, realized r) for scripting.",
    ),
) -> None:
    """Reconcile journaled brackets against the broker — STRICTLY READ-ONLY.

    No order placement, no cancels; the journal is never rewritten (verdicts
    are recomputed at read time from the append-only SoT + the broker's
    open-orders view + the vendor's audit-log resolution capability).
    Exit code 0 when clean, 1 when any UNRESOLVED or divergent row exists
    (scriptable; a still-working entry PAST its TTL is a divergence).
    """
    from alphalens_pipeline.brokers.automanager import state_paths
    from alphalens_pipeline.brokers.reconcile import (
        has_failures,
        reconcile_brackets,
        summarize,
    )
    from alphalens_pipeline.brokers.submission_log import iter_submission_records
    from broker_contract.contract import BrokerError

    if journal is None:
        # An explicit --journal is user-directed and skips the guard — the
        # operator is pointing at a specific file on purpose, not relying on
        # the seam's default per-env resolution.
        _guard_state_layout()
    path = journal or state_paths.submissions_path()
    malformed: list[str] = []
    records = list(iter_submission_records(path, malformed=malformed))
    if malformed:
        typer.secho(
            f"journal: skipped {len(malformed)} malformed line(s) in {path}",
            fg=typer.colors.YELLOW,
            err=True,
        )
    if not records:
        # No broker is resolved on this path, but the operator still deserves
        # the env line every other broker-touching invocation prints — an empty
        # LIVE journal reading as "nothing to reconcile" with no env context
        # is exactly the ambiguity the echo exists to remove (zen review).
        from alphalens_pipeline.brokers.automanager import state_paths

        typer.secho(f"env={state_paths.broker_environment()} gateway=none", err=True)
        typer.echo(f"no submission records in {path} — nothing to reconcile")
        return

    try:
        verdicts = reconcile_brackets(records, _cli_broker(mutating=False))
    except BrokerError as exc:
        raise _fail(f"broker reconcile failed: {exc}") from exc

    if as_json:
        typer.echo(json.dumps([v.as_dict() for v in verdicts], indent=2, default=str))
        if has_failures(verdicts):
            # Silent nonzero exit keeps stdout pure JSON for scripting.
            raise typer.Exit(code=1)
        return

    typer.echo(
        f"{'brief_date':10s}  {'ticker':6s}  {'qty':>8s}  {'entry_order_id':14s}  "
        f"{'verdict':30s}  {'activity_time':28s}  note"
    )
    for verdict in verdicts:
        note_parts = [part for part in (verdict.note, verdict.reason) if part]
        typer.echo(
            f"{verdict.brief_date:10s}  {verdict.ticker:6s}  {verdict.qty:>8.0f}  "
            f"{verdict.entry_order_id:14s}  {verdict.verdict:30s}  "
            f"{(verdict.activity_time or '-'):28s}  {'; '.join(note_parts) or '-'}"
        )
    summary = summarize(verdicts)
    typer.echo(
        f"{summary['total']} bracket(s): {summary['working']} working, "
        f"{summary['terminal']} terminal, {summary['unresolved']} unresolved, "
        f"{summary['divergent']} divergent"
    )
    if has_failures(verdicts):
        raise _fail("reconciliation found unresolved or divergent bracket(s) — see rows above")


@broker_app.command(name="reconcile-fills")
def reconcile_fills_command(
    out: Path | None = typer.Option(
        None,
        "--out",
        help="Parquet output path (default: "
        "~/.alphalens/exec_quality/<env>/tranche_fills.parquet) "
        "(<env> = $ALPHALENS_BROKER_ENVIRONMENT, default sim).",
    ),
    as_json: bool = typer.Option(
        False,
        "--json",
        help="Emit the reconciled records as JSON (one dict per fire) for scripting.",
    ),
) -> None:
    """Join each fired TP tranche to its ACTUAL broker fill (OFFLINE) — READ-ONLY.

    Reads the ``tranche_fired`` decision-side telemetry lines from the
    standalone-stop journal, resolves each ``sell_order_id`` through the
    broker's audit-log capability, computes the implementation shortfall, and
    (over)writes the execution-quality parquet. Places / cancels / amends
    NOTHING — the only side effect is the parquet write.
    """
    from dataclasses import asdict

    from alphalens_pipeline.brokers.automanager import control_loop, state_paths
    from alphalens_pipeline.brokers.automanager.exec_quality import (
        FILL_STATUS_FILLED,
        FILL_STATUS_PENDING,
        FILL_STATUS_UNRESOLVED,
        reconcile_fills,
        write_exec_quality_parquet,
    )
    from alphalens_pipeline.brokers.reconcile import SupportsOrderResolution
    from broker_contract.contract import BrokerError

    _guard_state_layout()

    out_path = out or state_paths.exec_quality_parquet()
    lines = list(control_loop._iter_standalone_stop_journal())

    broker = _cli_broker(mutating=False)
    if not isinstance(broker, SupportsOrderResolution):
        raise _fail(
            "the configured broker does not support order-outcome resolution "
            "(reconcile-fills needs the audit-log capability)"
        )
    try:
        records = reconcile_fills(lines, broker)
    except BrokerError as exc:
        raise _fail(f"broker reconcile-fills failed: {exc}") from exc

    written = write_exec_quality_parquet(records, out_path)

    if as_json:
        # stdout stays exactly one JSON value for scripting (sibling `reconcile`
        # pattern) — the parquet is still written above regardless of format.
        typer.echo(json.dumps([asdict(r) for r in records], indent=2, default=str))
        return

    total = len(records)
    filled = sum(1 for r in records if r.fill_status == FILL_STATUS_FILLED)
    pending = sum(1 for r in records if r.fill_status == FILL_STATUS_PENDING)
    unresolved = sum(1 for r in records if r.fill_status == FILL_STATUS_UNRESOLVED)
    priced_bps = [
        r.slippage_bps
        for r in records
        if r.fill_status == FILL_STATUS_FILLED and r.slippage_bps is not None
    ]
    mean_bps = sum(priced_bps) / len(priced_bps) if priced_bps else None

    # Human TP label only (tp1 -> TP1); the parquet and the --json path above
    # keep the raw ``record.tag``.
    from alphalens_pipeline.brokers.automanager.labels import tp_label_from_tag

    typer.echo(
        f"{'uic':>8s}  {'TP':6s}  {'sell_order_id':16s}  {'status':10s}  "
        f"{'fill_price':>10s}  {'slippage_bps':>12s}"
    )
    for record in records:
        fill_price = "-" if record.fill_price is None else f"{record.fill_price:.4f}"
        slippage = "-" if record.slippage_bps is None else f"{record.slippage_bps:.2f}"
        typer.echo(
            f"{record.uic:>8d}  {tp_label_from_tag(record.tag):6s}  "
            f"{record.sell_order_id:16s}  {record.fill_status:10s}  "
            f"{fill_price:>10s}  {slippage:>12s}"
        )

    mean_str = "n/a" if mean_bps is None else f"{mean_bps:.2f} bps"
    typer.echo(f"{total} fire(s): {filled} filled, {pending} pending, {unresolved} unresolved")
    typer.echo(f"mean slippage over filled: {mean_str}")
    typer.echo(f"wrote {written}")


@broker_app.command(name="cancel")
def cancel_command(
    order_id: str = typer.Argument(..., help="Broker OrderId (entry cancel cascades exits)."),
) -> None:
    """Cancel an order. Deliberately usable without the placement env gate."""
    from broker_contract.contract import BrokerError

    try:
        _cli_broker(mutating=False).cancel_order(order_id)
    except BrokerError as exc:
        raise _fail(f"broker cancel failed: {exc}") from exc
    typer.echo(f"cancelled {order_id} (an entry cancel cascades to its bracket children)")


@broker_app.command(name="manage")
def manage_command(
    once: bool = typer.Option(False, "--once", help="Run a single control-loop tick and exit."),
    poll_seconds: float = typer.Option(
        45.0, "--poll-seconds", help="Seconds to sleep between ticks in daemon mode (30-60s)."
    ),
) -> None:
    """Run the auto-manager loop for the instance selected by
    ALPHALENS_BROKER_ENVIRONMENT (default sim; ``live`` boots only through the
    ADR 0017 LIVE factory + boot-assert): drain armed picks, place the in-band
    subset + standalone disaster stop, reconcile, and manage each base position
    to terminal. Kill this instance with `touch ~/.alphalens/broker_orders/<env>/KILL`;
    `touch ~/.alphalens/broker_orders/KILL` is the GLOBAL kill (every instance,
    ADR 0016 D3). Placement still needs ALPHALENS_BROKER_ALLOW_ORDERS=1 (enforced
    inside the broker)."""
    from alphalens_pipeline.brokers.automanager.control_loop import build_default_deps, run_daemon
    from broker_contract.contract import BrokerError

    try:
        deps = build_default_deps(
            notify=_environment_labeled_notify(_telegram_daemon_notify()),
            chain_loss_notify=_environment_labeled_notify(_telegram_chain_loss_notify()),
        )
        # Dark streaming early-wake: build_default_deps returns wake_event/stream_tick
        # only when ALPHALENS_BROKER_STREAMING_ENABLED=1 and the reader started; both
        # None otherwise -> run_daemon is byte-identical to today's poll-only loop.
        try:
            run_daemon(
                deps,
                once=once,
                poll_seconds=poll_seconds,
                wake_event=deps.wake_event,
                on_tick=deps.stream_tick,
            )
        finally:
            # Stop the streaming reader (DELETE subs + join the thread) so a --once
            # run or a daemon exit never leaves an orphan subscription/thread.
            if deps.stream_trigger is not None:
                deps.stream_trigger.stop()
    except BrokerError as exc:
        raise _fail(f"broker manage failed: {exc}") from exc
    if once:
        typer.echo("manage: single tick complete")


# ``stream-status`` result contract version (rearm design memo §6 INC-6).
# Within this major version fields are only ever ADDED, never renamed/retyped.
_STREAM_STATUS_SCHEMA = "alphalens.broker.stream-status/v1"

# Stable machine-readable error code for a missing stream textfile — never
# renamed (CLI doctrine: domain detail lives in error.code, the process exit
# stays on the small documented set; 4 = not found).
_STREAM_STATUS_MISSING_CODE = "stream_metrics_missing"

_STREAM_STATUS_EXIT_NOT_FOUND = 4

# Full PromQL line: ``name{labels} value`` (labels optional). The daemon
# writes labels into the metric key (textfile.py doctrine), so parsing strips
# them back off to the base gauge name.
_PROM_LINE_RE = re.compile(
    r"^(?P<name>[A-Za-z_:][A-Za-z0-9_:]*)(?:\{[^}]*\})?\s+(?P<value>[-+0-9.eEnaif]+)\s*$"
)


@broker_app.command(name="stream-status")
def stream_status_command(
    env: str = typer.Option(
        _DEFAULT_ARM_ENV,
        "--env",
        help="Broker instance whose stream gauges to read (sim|live).",
    ),
    output_format: str = typer.Option(
        "human",
        "--format",
        help="Output format: human|json (json = exactly one JSON value on stdout).",
    ),
) -> None:
    """Read-only snapshot of the SIM order-stream breaker/liveness gauges.

    Reads the ``alphalens_domain_broker-manager-<env>-stream.prom`` textfile
    the daemon rewrites every tick (through the existing
    ``ALPHALENS_TEXTFILE_DIR`` resolution) — no broker call, no auth, no
    mutation, safe while the daemon runs. One internal result object rendered
    two ways (repo CLI doctrine); exit 4 when the textfile is absent (the
    daemon never ticked with streaming on, or the wrong --env)."""
    from alphalens_pipeline.brokers.automanager import state_paths
    from alphalens_pipeline.observability import textfile

    if output_format not in ("human", "json"):
        raise _fail(f"unknown --format {output_format!r} (expected human|json)")
    try:
        job = state_paths.stream_metrics_job(env)
    except ValueError as exc:
        raise _fail(str(exc)) from exc

    path = textfile._resolve_dir() / f"alphalens_domain_{job}.prom"
    if not path.is_file():
        error = {
            "code": _STREAM_STATUS_MISSING_CODE,
            "message": f"no stream gauge textfile at {path}",
            "retryable": False,
            "details": {"path": str(path), "env": env, "job": job},
            "suggestions": [
                {
                    "argv": [
                        "systemctl",
                        "--user",
                        "status",
                        "alphalens-broker-manager.service",
                    ],
                    "why": (
                        "the daemon writes the gauges every tick only while it "
                        "runs with ALPHALENS_BROKER_STREAMING_ENABLED=1"
                    ),
                }
            ],
        }
        typer.secho(json.dumps(error), err=True)
        raise typer.Exit(code=_STREAM_STATUS_EXIT_NOT_FOUND)

    gauges: dict[str, float] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = _PROM_LINE_RE.match(line.strip())
        if match:
            try:
                gauges[match.group("name")] = float(match.group("value"))
            except ValueError:  # a malformed value line is skipped, not fatal
                continue

    result = {
        "schema": _STREAM_STATUS_SCHEMA,
        "env": env,
        "job": state_paths.metrics_job(env),
        "source": str(path),
        "gauges": gauges,
    }

    if output_format == "json":
        typer.echo(json.dumps(result))
        return

    def _gauge(name: str) -> float | None:
        return gauges.get(f"alphalens_broker_manager_stream_{name}")

    def _fmt(value: float | None) -> str:
        return "absent" if value is None else f"{value:g}"

    breaker_open = _gauge("breaker_open")
    breaker_state = (
        "absent"
        if breaker_open is None
        else ("OPEN (episode running)" if breaker_open else "closed")
    )
    reader_up = _gauge("reader_up")
    reader_state = "absent" if reader_up is None else ("up" if reader_up else "down")
    typer.echo(f"env                   {env}")
    typer.echo(f"breaker               {breaker_state}")
    typer.echo(f"reader                {reader_state}")
    typer.echo(f"last message age (s)  {_fmt(_gauge('last_message_age_seconds'))}")
    typer.echo(f"consecutive failures  {_fmt(_gauge('consecutive_failures'))}")
    typer.echo(f"trips / re-arm cycles {_fmt(_gauge('trips_total'))}")
    typer.echo(f"in session            {_fmt(_gauge('in_session'))}")
