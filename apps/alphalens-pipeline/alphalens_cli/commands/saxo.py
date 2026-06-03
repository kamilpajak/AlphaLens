"""CLI: ``alphalens saxo {auth,refresh,status,probe}``.

* ``auth`` — one-time interactive Authorization-Code + PKCE/S256 bootstrap.
  ``--manual`` (DEFAULT on the headless VPS): the operator opens the printed
  authorize URL in any browser, approves, then pastes the full redirect URL
  back via a NON-ECHOING stdin read (``_read_redirect_url`` / getpass). There
  is deliberately NO ``--code`` / ``--secret`` / ``--token`` argv option —
  shell-history + ``ps`` would leak it (secret-leak Finding 6).
* ``refresh`` — the SINGLE-WRITER keep-alive ``ExecStart``. Loads the chain,
  proactively rotates if inside the safety margin, recovers a mid-refresh
  crash, and emits the allow-listed Prometheus gauges.
* ``status`` — chain health (ages / booleans / expiry-deltas ONLY, never any
  token substring, no network call).
* ``probe`` — read-only ``GET /port/v1/users/me`` end-to-end smoke.

``SAXO_ENV`` is required (no silent sim default); empty string rejected; live
requires an affirmative ``SAXO_ALLOW_LIVE``. All endpoints are hardcoded
per-env inside :class:`SaxoClient` — never derived from an env string.

Lazy imports inside command bodies keep the ``alphalens`` CLI startup cheap
(the Layer-1 ``edgar-detect`` cron must not pay for httpx/secrets import on
every fire) and keep the pipeline package free of top-level research imports.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import secrets
from typing import TYPE_CHECKING

import typer

# Pure-stdlib emitter — cheap at module top; tests patch it as
# ``saxo.emit_domain_metrics`` (same pattern as paper / cache / thematic).
from alphalens_pipeline.observability.textfile import emit_domain_metrics

if TYPE_CHECKING:
    from alphalens_pipeline.data.alt_data.saxo_client import SaxoClient
    from alphalens_pipeline.data.alt_data.saxo_token_store import SaxoTokenStore

logger = logging.getLogger(__name__)

saxo_app = typer.Typer(
    name="saxo",
    help="Saxo OpenAPI auth + 24/7 token renewal (auth / refresh / status / probe).",
    no_args_is_help=True,
)

# Domain-metric job id — matches the alphalens-emit-job-metrics hook on the
# alphalens-saxo-refresh unit so both halves land in the same textfile dir.
_REFRESH_JOB = "saxo-refresh"

_ENV_HELP = "Saxo environment (sim|live). Required — never defaulted."


def _pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) for PKCE/S256."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode("ascii")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _read_redirect_url() -> str:
    """Read the pasted redirect URL via a NON-ECHOING stdin read.

    Funnelled through one helper so the non-echoing contract is testable and a
    future ``--code`` argv option cannot sneak in. Uses getpass so the URL
    (which carries the auth ``code``) never echoes to the terminal / journal.
    """
    import getpass

    return getpass.getpass("Paste the full redirect URL (input hidden): ").strip()


def _build_store(environment: str) -> SaxoTokenStore:
    from alphalens_pipeline.data.alt_data.saxo_token_store import (
        SaxoTokenStore,
        default_token_store_dir,
    )

    return SaxoTokenStore(default_token_store_dir(), environment=environment)


def _build_client(environment: str) -> SaxoClient:
    from alphalens_pipeline.data.alt_data.saxo_client import SaxoClient

    # from_env validates SAXO_ENV/ALLOW_LIVE/APP_SECRET; pass the resolved env
    # so a caller-supplied --env stays the single source of truth.
    os.environ["SAXO_ENV"] = environment
    return SaxoClient.from_env()


def _build_manager(store: SaxoTokenStore, client: SaxoClient, environment: str):
    from alphalens_pipeline.data.alt_data.saxo_token_manager import SaxoTokenManager

    return SaxoTokenManager(store=store, client=client, environment=environment)


# --- status (no network, no token material) --------------------------------


def render_status(store: SaxoTokenStore, *, environment: str, now: float) -> str:
    """Render chain health as ages / booleans / expiry-deltas ONLY.

    NEVER includes a token substring. Returns a multi-line string the CLI
    prints; factored out so the no-token-material contract is unit-testable.
    """
    from alphalens_pipeline.data.alt_data.saxo_token_store import SaxoTokenStoreCorruptError

    try:
        record = store.read()
    except SaxoTokenStoreCorruptError:
        return f"environment={environment} chain_state=corrupt (unparseable token file)"
    if record is None:
        return f"environment={environment} chain_state=bootstrap_needed (no token chain on disk)"
    access_delta = record.access_token_expires_at - now
    refresh_delta = record.refresh_token_expires_at - now
    full_auth_age = now - record.last_full_auth_at
    lines = [
        f"environment={record.environment}",
        f"reauth_required={record.reauth_required}",
        f"reauth_reason={record.reauth_reason}",
        f"journal_state={record.journal_state}",
        f"access_token_expires_in_s={access_delta:.0f}",
        f"refresh_token_expires_in_s={refresh_delta:.0f}",
        f"last_full_auth_age_s={full_auth_age:.0f}",
    ]
    return "\n".join(lines)


@saxo_app.command(name="status")
def status_command(
    env: str = typer.Option(..., "--env", help=_ENV_HELP),
) -> None:
    """Print chain health (no network, no token material)."""
    import time

    store = _build_store(env)
    typer.echo(render_status(store, environment=env, now=time.time()))


# --- refresh (single-writer keep-alive) ------------------------------------


def run_refresh(
    *,
    store: SaxoTokenStore,
    environment: str,
    client: SaxoClient,
    now: float,
    emit: bool = True,
) -> int:
    """Single-writer refresh + crash-recovery + metric emit. Returns chain_state.

    Factored out of the typer command so tests drive it with an injected store
    + MockTransport client. Recovery runs first (a mid-refresh crash must be
    resolved before a fresh rotation); then the proactive refresh.
    """
    from alphalens_pipeline.data.alt_data.saxo_client import (
        SaxoReauthRequiredError,
        SaxoTransientError,
    )

    manager = _build_manager(store, client, environment)
    failure_class: str | None = None
    try:
        manager.recover()
        manager.refresh()
    except SaxoReauthRequiredError:
        failure_class = "permanent"
        logger.warning("saxo %s chain requires manual re-auth", environment)
    except SaxoTransientError:
        failure_class = "transient"
        logger.info("saxo %s refresh deferred (transient); next fire retries", environment)

    state = manager.chain_state()
    if emit:
        _emit_refresh_metrics(store, environment=environment, now=now, failure_class=failure_class)
    return state


def _emit_refresh_metrics(
    store: SaxoTokenStore,
    *,
    environment: str,
    now: float,
    failure_class: str | None,
) -> None:
    """Emit the allow-listed Saxo gauges.

    Only numeric values, only ``environment`` (+ ``class`` on the failures
    counter) as labels — NEVER token material (enforced by
    ``test_saxo_metrics_allowlist``).
    """
    from alphalens_pipeline.data.alt_data.saxo_token_manager import SaxoTokenManager
    from alphalens_pipeline.data.alt_data.saxo_token_store import SaxoTokenStoreCorruptError

    # chain_state via a throwaway manager (read-only, no client needed).
    manager = SaxoTokenManager(store=store, client=None, environment=environment)  # type: ignore[arg-type]
    chain_state = manager.chain_state()
    env_label = f'{{environment="{environment}"}}'
    metrics: dict[str, float] = {
        f"alphalens_saxo_chain_state{env_label}": chain_state,
        f"alphalens_saxo_metrics_fetched_at_timestamp_seconds{env_label}": now,
    }
    try:
        record = store.read()
    except SaxoTokenStoreCorruptError:
        record = None
    if record is not None:
        metrics[f"alphalens_saxo_reauth_required{env_label}"] = 1 if record.reauth_required else 0
        metrics[f"alphalens_saxo_refresh_token_expires_at_timestamp_seconds{env_label}"] = (
            record.refresh_token_expires_at
        )
        metrics[f"alphalens_saxo_token_chain_last_refresh_timestamp_seconds{env_label}"] = (
            record.rotated_at
        )
        metrics[f"alphalens_saxo_token_chain_last_full_auth_timestamp_seconds{env_label}"] = (
            record.last_full_auth_at
        )
    if failure_class is not None:
        metrics[
            f'alphalens_saxo_refresh_failures_total{{environment="{environment}",'
            f'class="{failure_class}"}}'
        ] = 1
    emit_domain_metrics(_REFRESH_JOB, metrics)


@saxo_app.command(name="refresh")
def refresh_command(
    env: str = typer.Option(..., "--env", help=_ENV_HELP),
) -> None:
    """Single-writer keep-alive: rotate the chain if inside the safety margin."""
    import time

    store = _build_store(env)
    client = _build_client(env)
    run_refresh(store=store, environment=env, client=client, now=time.time(), emit=True)


# --- probe (read-only end-to-end smoke) ------------------------------------


@saxo_app.command(name="probe")
def probe_command(
    env: str = typer.Option(..., "--env", help=_ENV_HELP),
) -> None:
    """Read-only GET /port/v1/users/me — proves token -> bearer -> 2xx."""
    store = _build_store(env)
    client = _build_client(env)
    manager = _build_manager(store, client, env)
    access_token = manager.get_access_token()
    payload = client.get_user_me(access_token=access_token)
    # Surface only a non-secret identity field, never the token.
    user_id = payload.get("UserId", "<unknown>")
    typer.echo(f"saxo {env} probe OK — UserId={user_id}")


# --- auth (PKCE bootstrap) -------------------------------------------------


@saxo_app.command(name="auth")
def auth_command(
    env: str = typer.Option(..., "--env", help=_ENV_HELP),
    manual: bool = typer.Option(
        True,
        "--manual/--loopback",
        help="--manual (default, headless VPS): paste the redirect URL via "
        "hidden stdin. --loopback: local-dev 127.0.0.1 catcher.",
    ),
) -> None:
    """One-time interactive Authorization-Code + PKCE/S256 bootstrap."""
    import time
    from urllib.parse import parse_qs, urlparse

    from alphalens_pipeline.data.alt_data.saxo_token_store import (
        SCHEMA_VERSION,
        SaxoTokenRecord,
    )

    client = _build_client(env)
    store = _build_store(env)

    state = secrets.token_urlsafe(24)
    verifier, challenge = _pkce_pair()
    url = client.authorize_url(state=state, code_challenge=challenge)
    typer.echo("Open this URL in a browser, log in to Saxo, and approve:")
    typer.echo("")
    typer.echo(url)
    typer.echo("")

    if not manual:
        raise typer.BadParameter(
            "--loopback is local-dev only and not wired in this build; use --manual on the VPS."
        )

    redirect_url = _read_redirect_url()
    parsed = parse_qs(urlparse(redirect_url).query)
    returned_state = (parsed.get("state") or [""])[0]
    code = (parsed.get("code") or [""])[0]
    if returned_state != state:
        raise typer.BadParameter("state mismatch — aborting (possible CSRF / wrong URL).")
    if not code:
        raise typer.BadParameter("no authorization code found in the pasted redirect URL.")

    payload = client.exchange_code(code=code, code_verifier=verifier)
    now = time.time()
    access_ttl = float(payload.get("expires_in") or 1200)
    refresh_ttl = float(payload.get("refresh_token_expires_in") or 2400)
    record = SaxoTokenRecord(
        schema_version=SCHEMA_VERSION,
        environment=env,
        access_token=str(payload["access_token"]),
        refresh_token=str(payload["refresh_token"]),
        previous_refresh_token=None,
        access_token_expires_at=now + access_ttl,
        refresh_token_expires_at=now + refresh_ttl,
        rotated_at=now,
        reauth_required=False,
        reauth_reason="none",
        journal_state="active",
        journal_attempted_at=None,
        last_full_auth_at=now,
    )
    with store.locked():
        store.write(record)
    typer.echo(f"saxo {env} token chain bootstrapped. Run `alphalens saxo status --env {env}`.")
