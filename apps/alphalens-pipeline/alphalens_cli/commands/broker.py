"""CLI: ``alphalens broker`` — broker execution layer (SIM-only, ADR 0014).

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
    alphalens broker orders                  — open orders
    alphalens broker cancel <order_id>       — cancel (entry cancel cascades the bracket)
    alphalens broker reconcile [--json]      — READ-ONLY journal vs broker verdicts (P3):
        WORKING / PAST-TTL divergence / FILLED (+closed r) / CANCELLED / REJECTED /
        EXPIRED / UNRESOLVED(reason); exit 1 on any unresolved or divergent row

All ``brokers`` imports are lazy inside command bodies — the ``alphalens``
binary's startup time is paid by the 15-min Layer-1 edgar-detect cron
(+913ms precedent; see CLAUDE.md lazy-CLI convention).
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Callable
from pathlib import Path

import typer

broker_app = typer.Typer(
    name="broker",
    help="Broker execution layer — SIM-only (ADR 0014).",
    no_args_is_help=True,
)

_DEFAULT_BRIEFS_DIR = Path.home() / ".alphalens" / "thematic_briefs"


def _fail(message: str) -> typer.Exit:
    typer.secho(message, fg=typer.colors.RED, err=True)
    return typer.Exit(code=1)


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
    typer.echo("refresh      DEAD")
    raise _fail("refresh chain is dead — re-run `alphalens broker auth`")


def _auth_refresh() -> None:
    """One silent refresh cycle — the future keep-alive timer's primitive."""
    from alphalens_pipeline.brokers.saxo.errors import SaxoAuthError
    from alphalens_pipeline.brokers.saxo.tokens import OAuthTokenProvider

    try:
        OAuthTokenProvider.from_env().refresh_now()
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
        help="One silent refresh cycle via the OAuth provider (keep-alive "
        "primitive for a future systemd timer). No browser.",
    ),
    timeout: int = typer.Option(
        300, "--timeout", help="Seconds to wait for the browser redirect (attended flow)."
    ),
    no_browser: bool = typer.Option(
        False, "--no-browser", help="Print the authorize URL only (headless / SSH)."
    ),
) -> None:
    """Bootstrap or inspect the Saxo OAuth session (SIM-only, Code grant).

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


@broker_app.command(name="account")
def account_command() -> None:
    """Print the broker account snapshot (cash, total value, margin)."""
    from alphalens_pipeline.brokers.contract import BrokerError
    from alphalens_pipeline.brokers.registry import get_default_broker

    try:
        snapshot = get_default_broker().get_account()
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
    from alphalens_pipeline.brokers.contract import BrokerError
    from alphalens_pipeline.brokers.registry import get_default_broker

    try:
        positions = get_default_broker().get_positions()
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
    from alphalens_pipeline.brokers.contract import BrokerError
    from alphalens_pipeline.brokers.registry import get_default_broker

    try:
        ref = get_default_broker().resolve_instrument(ticker, exchange)
    except BrokerError as exc:
        raise _fail(f"broker resolve failed: {exc}") from exc

    typer.echo(f"ticker        {ref.ticker}")
    typer.echo(f"exchange_mic  {ref.exchange_mic}")
    typer.echo(f"asset_type    {ref.asset_type}")
    typer.echo(f"broker_id     {ref.broker_instrument_id}")
    typer.echo(f"symbol        {ref.broker_symbol}")
    typer.echo(f"currency      {ref.currency or 'n/a'}")


def _echo_bracket_table(brackets: list) -> None:
    typer.echo(
        f"{'#':>2s}  {'qty':>6s}  {'entry':>10s}  {'stop':>10s}  {'tp':>10s}  "
        f"{'ttl':>4s}  client_request_id"
    )
    for index, bracket in enumerate(brackets):
        tp = "-" if bracket.take_profit is None else f"{bracket.take_profit:.4f}"
        stop = "-" if bracket.stop_loss is None else f"{bracket.stop_loss:.4f}"
        typer.echo(
            f"{index:>2d}  {bracket.quantity:>6d}  {bracket.entry_limit:>10.4f}  "
            f"{stop:>10s}  {tp:>10s}  {bracket.entry_ttl_days:>4d}  "
            f"{bracket.client_request_id}"
        )


def _assert_fx_precheck_cross_checks(
    *,
    index: int,
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
            f"{ticker}: precheck {index} EstimatedCashRequiredCurrency="
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
            f"{ticker}: precheck {index} carries no usable "
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
        raise _fail(f"{ticker}: precheck {index} FX divergence check failed: {exc}") from exc
    if divergence > divergence_max_pct:
        raise _fail(
            f"{ticker}: precheck {index} FX divergence {divergence:.2f}% exceeds the "
            f"{divergence_max_pct}% bound — sizing rate {sizing_rate:.6f} "
            f"(account->instrument) vs Saxo {conversion_rate:.6f} "
            "(instrument->account, inverted before comparing); refusing placement"
        )
    typer.echo(
        f"precheck {index}: fx cross-check ok — saxo rate {conversion_rate:.6f} "
        f"(instrument->account), divergence {divergence:.2f}% <= {divergence_max_pct}%"
    )
    return conversion_rate


def _resolve_instrument_and_plan(
    *,
    wanted: str,
    exchange: str | None,
    equity: float | None,
    scale_factor: float,
    trade_setup: object,
) -> tuple:
    """Resolve the instrument, read the account, and size the setup plan.

    Extracted from ``submit_command`` to keep it a short orchestration: the
    broker read, the cross-currency FX-rate resolution, and the sizing call
    live here. Lazy imports keep the ``alphalens`` binary's startup cost off
    this path (lazy-CLI doctrine). Returns
    ``(broker, account, sizing_equity, instrument, fx, plan)``.
    """
    from alphalens_pipeline.brokers import execution as execution_policy
    from alphalens_pipeline.brokers.contract import BrokerError
    from alphalens_pipeline.brokers.execution import build_fx_conversion
    from alphalens_pipeline.brokers.registry import get_default_broker
    from alphalens_pipeline.brokers.routing import resolve_us_instrument
    from alphalens_pipeline.paper.sizing import (
        TradeSetupNotPlannableError,
        compute_setup_plan,
    )

    try:
        broker = get_default_broker()
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
        plan = compute_setup_plan(
            brief_trade_setup=trade_setup,
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
    from alphalens_pipeline.brokers.contract import BrokerError
    from alphalens_pipeline.brokers.execution import fx_precheck_divergence_pct

    precheck_summaries: list[dict] = []
    precheck_conversion_rate: float | None = None
    precheck_fn = getattr(broker, "precheck_bracket_order", None)
    if precheck_fn is None:
        typer.echo("precheck: not supported by this broker — skipping")
        return precheck_summaries, precheck_conversion_rate
    for index, bracket in enumerate(brackets):
        try:
            payload = precheck_fn(bracket)
        except BrokerError as exc:
            raise _fail(f"precheck failed for bracket {index}: {exc}") from exc
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
            f"precheck {index}: result={summary['PreCheckResult']!r} "
            f"est_cash={est_cash_label} costs={summary['Costs']!r}"
        )
        if fx is not None:
            precheck_conversion_rate = _assert_fx_precheck_cross_checks(
                index=index,
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
    broker: object,
    brackets: list,
    brief_date: dt.date,
    wanted: str,
    instrument: object,
    precheck_summaries: list[dict],
    account_currency: str,
    sizing_equity: float,
    fx: object,
    precheck_conversion_rate: float | None,
) -> None:
    """Place each bracket, journal the outcome, then raise on any failure.

    Extracted from ``submit_command``. The submission record is written in a
    ``finally`` so a mid-run BrokerError still journals the already-placed
    entries; the command then exits non-zero with the reconcile hint.
    """
    from alphalens_pipeline.brokers.contract import BrokerError
    from alphalens_pipeline.brokers.execution import execution_config_version
    from alphalens_pipeline.brokers.submission_log import (
        append_submission_record,
        build_submission_record,
    )

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
    from alphalens_pipeline.paper.sizing import (
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


@broker_app.command(name="orders")
def orders_command() -> None:
    """List open orders (entry + exit children; UNKNOWN never guessed)."""
    from alphalens_pipeline.brokers.contract import BrokerError
    from alphalens_pipeline.brokers.registry import get_default_broker

    try:
        states = get_default_broker().list_open_orders()
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
        help="Submission journal path (default: ~/.alphalens/broker_orders/submissions.jsonl).",
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
    from alphalens_pipeline.brokers.contract import BrokerError
    from alphalens_pipeline.brokers.reconcile import (
        has_failures,
        reconcile_brackets,
        summarize,
    )
    from alphalens_pipeline.brokers.registry import get_default_broker
    from alphalens_pipeline.brokers.submission_log import (
        DEFAULT_SUBMISSIONS_PATH,
        iter_submission_records,
    )

    path = journal or DEFAULT_SUBMISSIONS_PATH
    malformed: list[str] = []
    records = list(iter_submission_records(path, malformed=malformed))
    if malformed:
        typer.secho(
            f"journal: skipped {len(malformed)} malformed line(s) in {path}",
            fg=typer.colors.YELLOW,
            err=True,
        )
    if not records:
        typer.echo(f"no submission records in {path} — nothing to reconcile")
        return

    try:
        verdicts = reconcile_brackets(records, get_default_broker())
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


@broker_app.command(name="cancel")
def cancel_command(
    order_id: str = typer.Argument(..., help="Broker OrderId (entry cancel cascades exits)."),
) -> None:
    """Cancel an order. Deliberately usable without the placement env gate."""
    from alphalens_pipeline.brokers.contract import BrokerError
    from alphalens_pipeline.brokers.registry import get_default_broker

    try:
        get_default_broker().cancel_order(order_id)
    except BrokerError as exc:
        raise _fail(f"broker cancel failed: {exc}") from exc
    typer.echo(f"cancelled {order_id} (an entry cancel cascades to its bracket children)")
