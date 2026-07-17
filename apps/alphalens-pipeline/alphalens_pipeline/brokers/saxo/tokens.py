"""Pluggable Saxo token providers + the OAuth token store.

Two providers behind the same ``TokenProvider`` Protocol (ZERO client
changes between them):

- :class:`StaticTokenProvider` — the 24-hour Developer-Portal SIM token
  (``SAXO_SIM_TOKEN``), pasted daily. Fallback when no OAuth store exists.
- :class:`OAuthTokenProvider` (P4) — OAuth authorization-code grant with the
  rotating single-use refresh token: proactive renewal at
  ``expires_in − 120 s`` on the monotonic clock, atomic newest-pair
  persistence to ``~/.alphalens/saxo_auth/token_store.json`` (0600, flock on
  a sibling ``.lock``), sibling-process adoption instead of burning a
  rotation, and a best-effort Telegram alert on refresh-chain loss
  (re-solving the job of the ``alphalens-saxo-refresh`` unit removed by
  ADR 0012).

The client's contract with a provider:

- ``get_access_token()`` is called per HTTP attempt (fresh headers each time);
- on a 401 the client calls ``invalidate()`` ONCE, retries with a fresh
  ``get_access_token()``, then raises ``SaxoAuthError``.

Chain-loss semantics: the refresh token lives ~40 min and every refresh
rotates it, so any gap longer than that (laptop sleep, reboot) kills the
chain. Recovery is always the attended ``alphalens broker auth`` re-login.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import logging
import os
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn, Protocol, runtime_checkable

from alphalens_pipeline.brokers.saxo.errors import SaxoAuthError
from alphalens_pipeline.brokers.saxo.oauth import SaxoAuthClient, TokenBundle

logger = logging.getLogger(__name__)

# Env var holding the 24h Developer-Portal SIM token. Named for its SCOPE
# (SIM), not its lifetime — P4's OAuth tokens are also SIM-scoped, so the
# name survives the provider swap.
TOKEN_ENV = "SAXO_SIM_TOKEN"

# OAuth env vars (see .env.example broker section).
APP_KEY_ENV = "SAXO_APP_KEY"
APP_SECRET_ENV = "SAXO_APP_SECRET"
REDIRECT_URL_ENV = "SAXO_AUTH_REDIRECT_URL"
TOKEN_STORE_PATH_ENV = "SAXO_TOKEN_STORE_PATH"

# Refresh at expires_in − 120 s: absorbs clock skew + request latency; with
# the documented expires_in=1200 this refreshes every ~18 min, well inside
# the ~40 min refresh-token window.
REFRESH_MARGIN_S = 120

_STORE_SCHEMA_VERSION = 1
_STORE_ENVIRONMENT = "sim"
_FINGERPRINT_PREFIX_LEN = 12

_CHAIN_LOST_MESSAGE = (
    "Saxo OAuth refresh chain lost (revoked, secret rotated, or >40 min gap) "
    "— re-run `alphalens broker auth`"
)


def default_token_store_path() -> Path:
    """``~/.alphalens/saxo_auth/token_store.json`` (hard requirement path)."""
    return Path.home() / ".alphalens" / "saxo_auth" / "token_store.json"


def resolve_token_store_path() -> Path:
    """Store path: ``SAXO_TOKEN_STORE_PATH`` override or the default."""
    override = os.environ.get(TOKEN_STORE_PATH_ENV)
    return Path(override) if override else default_token_store_path()


def app_key_fingerprint(app_key: str) -> str:
    """Short sha256 prefix identifying the app registration — NEVER the key."""
    return hashlib.sha256(app_key.encode("utf-8")).hexdigest()[:_FINGERPRINT_PREFIX_LEN]


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SaxoAuthError(
            f"{name} environment variable not set — required for Saxo OAuth "
            "(see the broker section of .env.example)"
        )
    return value


def _send_chain_loss_telegram(message: str) -> None:
    """Best-effort default chain-loss alert; every failure path is swallowed."""
    try:
        from alphalens_pipeline.data.alt_data.telegram_client import TelegramClient

        bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID")
        if not bot_token or not chat_id:
            return
        TelegramClient(bot_token).send_message(chat_id, message)
    except Exception:
        logger.warning("saxo chain-loss Telegram alert failed", exc_info=True)


@dataclass(frozen=True)
class TokenStoreState:
    """One decoded ``token_store.json`` (schema v1). All timestamps UTC-aware."""

    environment: str
    app_key_fingerprint: str
    access_token: str
    access_token_expires_at: dt.datetime
    refresh_token: str
    refresh_token_expires_at: dt.datetime
    obtained_at: dt.datetime


class TokenStore:
    """Atomic, owner-only, flock-serialized persistence for the OAuth pair.

    - **Atomic write:** tmp-in-same-dir + fsync + ``os.replace`` (the
      ``dispatch_state.py`` idiom) — readers never see a torn file.
    - **0600:** ``NamedTemporaryFile`` creates 0600 and ``os.replace``
      preserves the mode; an explicit ``chmod`` belts against exotic umasks.
      This is the first long-lived secret the repo persists to disk.
    - **Sibling ``.lock``:** ``os.replace`` swaps the store's inode, so the
      flock lives on a SEPARATE stable file. flock is per-host — two hosts
      sharing one store still burn each other's chains (single-host chain
      ownership is doctrine; see the P4 design memo open questions).
    """

    def __init__(
        self,
        path: Path,
        *,
        lock_timeout_s: float = 60.0,
        lock_poll_interval_s: float = 0.2,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self.path = path
        self.lock_path = path.with_name(path.stem + ".lock")
        self._lock_timeout_s = lock_timeout_s
        self._lock_poll_interval_s = lock_poll_interval_s
        self._sleep = sleep
        self._monotonic = monotonic

    def load(self, *, expected_fingerprint: str | None = None) -> TokenStoreState | None:
        """Decode the store; ``None`` when absent, ``SaxoAuthError`` when corrupt.

        Corrupt = unparsable JSON, missing keys, wrong ``schema_version``,
        ``environment`` other than ``"sim"``, naive timestamps, or (when
        ``expected_fingerprint`` is given) a store created under a different
        app registration. No silent fallback — masking a corrupt store behind
        the static token would hide the problem.
        """
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        try:
            payload = json.loads(raw)
            if payload["schema_version"] != _STORE_SCHEMA_VERSION:
                raise ValueError("unsupported schema_version")
            if payload["environment"] != _STORE_ENVIRONMENT:
                raise ValueError("environment is not sim")
            state = TokenStoreState(
                environment=str(payload["environment"]),
                app_key_fingerprint=str(payload["app_key_fingerprint"]),
                access_token=str(payload["access_token"]),
                access_token_expires_at=self._parse_utc(payload["access_token_expires_at"]),
                refresh_token=str(payload["refresh_token"]),
                refresh_token_expires_at=self._parse_utc(payload["refresh_token_expires_at"]),
                obtained_at=self._parse_utc(payload["obtained_at"]),
            )
        except (ValueError, KeyError, TypeError) as exc:
            raise self._corrupt_error() from exc
        if expected_fingerprint is not None and state.app_key_fingerprint != expected_fingerprint:
            raise self._corrupt_error()
        return state

    def save(self, state: TokenStoreState) -> None:
        """Persist atomically with mode 0600; parent dirs auto-created."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": _STORE_SCHEMA_VERSION,
            "environment": state.environment,
            "app_key_fingerprint": state.app_key_fingerprint,
            "access_token": state.access_token,
            "access_token_expires_at": state.access_token_expires_at.isoformat(),
            "refresh_token": state.refresh_token,
            "refresh_token_expires_at": state.refresh_token_expires_at.isoformat(),
            "obtained_at": state.obtained_at.isoformat(),
        }
        with tempfile.NamedTemporaryFile(
            mode="w",
            dir=self.path.parent,
            suffix=".tmp",
            delete=False,
            encoding="utf-8",
        ) as tmp:
            json.dump(payload, tmp)
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp_path = Path(tmp.name)
        try:
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, self.path)
        except OSError:
            tmp_path.unlink(missing_ok=True)
            raise

    def save_bundle(
        self,
        bundle: TokenBundle,
        *,
        app_key: str,
        wall_now: Callable[[], dt.datetime] = _utc_now,
    ) -> TokenStoreState:
        """Stamp wall-clock expiries off receipt time + the response's
        ``expires_in`` fields (never hardcoded) and persist atomically."""
        wall = wall_now()
        state = TokenStoreState(
            environment=_STORE_ENVIRONMENT,
            app_key_fingerprint=app_key_fingerprint(app_key),
            access_token=bundle.access_token,
            access_token_expires_at=wall + dt.timedelta(seconds=bundle.expires_in),
            refresh_token=bundle.refresh_token,
            refresh_token_expires_at=wall + dt.timedelta(seconds=bundle.refresh_token_expires_in),
            obtained_at=wall,
        )
        self.save(state)
        return state

    @contextlib.contextmanager
    def exclusive_lock(self) -> Iterator[None]:
        """Per-host exclusive lock around [re-read -> decide -> refresh -> persist].

        Non-blocking ``LOCK_EX|LOCK_NB`` poll loop with an acquire deadline —
        a wedged sibling raises an actionable error instead of hanging.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            deadline = self._monotonic() + self._lock_timeout_s
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if self._monotonic() >= deadline:
                        raise SaxoAuthError(
                            f"could not acquire the Saxo token-store lock at "
                            f"{self.lock_path} within {self._lock_timeout_s:.0f}s — "
                            "another process is refreshing and appears stuck"
                        ) from None
                    self._sleep(self._lock_poll_interval_s)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    @staticmethod
    def _parse_utc(raw: object) -> dt.datetime:
        parsed = dt.datetime.fromisoformat(str(raw))
        if parsed.tzinfo is None:
            raise ValueError("naive timestamp in token store")
        return parsed

    def _corrupt_error(self) -> SaxoAuthError:
        return SaxoAuthError(
            f"token store at {self.path} is corrupted or from a different app "
            "— delete it and re-run `alphalens broker auth`"
        )


@runtime_checkable
class TokenProvider(Protocol):
    def get_access_token(self) -> str: ...

    def invalidate(self) -> None:
        """Hint that the last token was rejected (401)."""
        ...


class StaticTokenProvider:
    """A fixed token — the 24h Developer-Portal SIM token, pasted daily."""

    def __init__(self, token: str):
        if not token:
            raise SaxoAuthError("StaticTokenProvider requires a non-empty token")
        self._token = token

    @classmethod
    def from_env(cls) -> StaticTokenProvider:
        """Construct from ``SAXO_SIM_TOKEN``. Raises ``SaxoAuthError`` if unset."""
        token = os.environ.get(TOKEN_ENV)
        if not token:
            raise SaxoAuthError(
                f"{TOKEN_ENV} environment variable not set — generate a 24h SIM "
                "token at developer.saxo, or bootstrap OAuth once via "
                "`alphalens broker auth` (no daily paste)"
            )
        return cls(token)

    def get_access_token(self) -> str:
        return self._token

    def invalidate(self) -> None:
        """No-op: a rejected static token means the 24h token expired — the
        operator regenerates it at developer.saxo; there is nothing to refresh
        in-process."""


class OAuthTokenProvider:
    """Self-refreshing OAuth provider over the rotating single-use chain.

    Scheduling is on the injectable MONOTONIC clock off local receipt time +
    ``expires_in`` (never JWT ``exp`` parsing); persistence timestamps are
    wall-clock (monotonic deadlines cannot cross processes). All state is
    guarded by one ``threading.RLock`` — the ``get_default_saxo_client()``
    singleton is shared across threads.
    """

    def __init__(
        self,
        auth_client: SaxoAuthClient,
        store: TokenStore,
        *,
        redirect_uri: str,
        refresh_margin_s: float = REFRESH_MARGIN_S,
        now: Callable[[], float] = time.monotonic,
        wall_now: Callable[[], dt.datetime] = _utc_now,
        alert: Callable[[str], None] | None = None,
    ):
        self._auth_client = auth_client
        self._store = store
        self._redirect_uri = redirect_uri
        self._refresh_margin_s = refresh_margin_s
        self._now = now
        self._wall_now = wall_now
        self._alert = alert if alert is not None else _send_chain_loss_telegram
        self._fingerprint = app_key_fingerprint(auth_client.app_key)
        self._lock = threading.RLock()
        self._access_token: str | None = None
        self._rejected_token: str | None = None
        self._access_deadline_mono: float = 0.0
        self._force_refresh = False
        self._alerted = False

    @classmethod
    def from_env(cls, *, alert: Callable[[str], None] | None = None) -> OAuthTokenProvider:
        """Construct from ``SAXO_APP_KEY`` / ``SAXO_APP_SECRET`` /
        ``SAXO_AUTH_REDIRECT_URL`` (+ optional ``SAXO_TOKEN_STORE_PATH``).

        Raises ``SaxoAuthError`` naming any missing variable — values are
        never echoed.
        """
        # Resolved through the module at CALL time (not the import-time
        # binding) so tests patching ``oauth.SaxoAuthClient`` are honored.
        from alphalens_pipeline.brokers.saxo import oauth

        app_key = _require_env(APP_KEY_ENV)
        app_secret = _require_env(APP_SECRET_ENV)
        redirect_uri = _require_env(REDIRECT_URL_ENV)
        store = TokenStore(resolve_token_store_path())
        return cls(
            oauth.SaxoAuthClient(app_key, app_secret),
            store,
            redirect_uri=redirect_uri,
            alert=alert,
        )

    def get_access_token(self) -> str:
        with self._lock:
            if (
                self._access_token is not None
                and not self._force_refresh
                and self._now() < self._access_deadline_mono - self._refresh_margin_s
            ):
                return self._access_token
            return self._refresh_slow_path()

    def invalidate(self) -> None:
        """401 hint: force a refresh on the next get. NO network here — the
        client's contract is invalidate-then-retry, and the retry's
        ``get_access_token()`` refreshes synchronously. The rejected token is
        remembered so the disk re-read never re-adopts it."""
        with self._lock:
            if self._access_token is not None:
                self._rejected_token = self._access_token
            self._access_token = None
            self._force_refresh = True

    def refresh_now(self) -> str:
        """Unconditional rotation — the keep-alive primitive.

        Skips disk adoption on purpose: only a real refresh EXTENDS the
        refresh chain (adoption merely reuses the sibling's access token).
        ``alphalens broker auth --refresh`` (and a future systemd keep-alive
        timer) call this.
        """
        with self._lock, self._store.exclusive_lock():
            state = self._store.load(expected_fingerprint=self._fingerprint)
            return self._rotate_locked(state)

    # ----- internals (called under self._lock) -----

    def _refresh_slow_path(self) -> str:
        with self._store.exclusive_lock():
            state = self._store.load(expected_fingerprint=self._fingerprint)
            wall = self._wall_now()
            margin = dt.timedelta(seconds=self._refresh_margin_s)
            if (
                state is not None
                and state.access_token
                and state.access_token not in (self._access_token, self._rejected_token)
                and state.access_token_expires_at > wall + margin
            ):
                # Another process already rotated — adopt, no refresh burned.
                remaining_s = (state.access_token_expires_at - wall).total_seconds()
                self._accept(state.access_token, self._now() + remaining_s)
                return state.access_token
            return self._rotate_locked(state)

    def _rotate_locked(self, state: TokenStoreState | None) -> str:
        """One refresh round-trip + persistence; caller holds both locks."""
        wall = self._wall_now()
        if state is None or state.refresh_token_expires_at <= wall:
            # Common post-laptop-sleep case (>40 min gap): fail fast with
            # the right message instead of a doomed HTTP call.
            self._chain_lost(cause=None)
        try:
            bundle = self._auth_client.refresh(state.refresh_token, self._redirect_uri)
        except SaxoAuthError as exc:
            self._chain_lost(cause=exc)
        receipt_mono = self._now()
        # Persist the rotated pair BEFORE returning or using the new access
        # token — the old refresh token died the moment the response arrived;
        # persistence-first minimizes the crash window.
        self._store.save_bundle(bundle, app_key=self._auth_client.app_key, wall_now=self._wall_now)
        self._accept(bundle.access_token, receipt_mono + bundle.expires_in)
        return bundle.access_token

    def _accept(self, token: str, deadline_mono: float) -> None:
        self._access_token = token
        self._access_deadline_mono = deadline_mono
        self._rejected_token = None
        self._force_refresh = False

    def _chain_lost(self, *, cause: Exception | None) -> NoReturn:
        if not self._alerted:
            self._alerted = True
            try:
                self._alert(_CHAIN_LOST_MESSAGE)
            except Exception:
                logger.warning("saxo chain-loss alert callable failed", exc_info=True)
        raise SaxoAuthError(_CHAIN_LOST_MESSAGE) from cause


__all__ = [
    "APP_KEY_ENV",
    "APP_SECRET_ENV",
    "REDIRECT_URL_ENV",
    "REFRESH_MARGIN_S",
    "TOKEN_ENV",
    "TOKEN_STORE_PATH_ENV",
    "OAuthTokenProvider",
    "StaticTokenProvider",
    "TokenProvider",
    "TokenStore",
    "TokenStoreState",
    "app_key_fingerprint",
    "default_token_store_path",
    "resolve_token_store_path",
]
