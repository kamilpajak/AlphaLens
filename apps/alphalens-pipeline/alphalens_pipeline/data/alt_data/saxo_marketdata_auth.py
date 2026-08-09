"""Saxo LIVE OAuth for MARKET DATA ONLY (app `bracket-keeper`).

Deliberately separate from ``brokers/saxo/oauth.py``: that module carries the
SIM-only rail (ADR 0014) and must keep refusing LIVE hosts. This one is the only
file in the tree holding the LIVE auth host, and it never places an order.

The token store is a SEPARATE file from the SIM store. The refresh token is
single-use and rotates on every refresh, so exactly one process may hold a given
store; two holders invalidate each other. Persistence follows the same pattern
as ``brokers/saxo/tokens.py::TokenStore``: atomic tmp-file + fsync +
``os.replace`` (never a torn write), 0600 from creation (never a
umask-derived-then-chmod window), and a sibling ``.lock`` file
(``fcntl.flock``) serializing [read -> refresh -> persist] so two processes
racing near expiry cannot both burn the same single-use refresh token.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import json
import os
import tempfile
import time
import urllib.parse
from base64 import b64encode
from collections.abc import Iterator
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
_LOCK_TIMEOUT_S = 60.0
_LOCK_POLL_INTERVAL_S = 0.2


def resolve_live_store_path() -> Path:
    """Resolve the LIVE market-data token-store path from env (or the default),
    needing NO app credentials.

    This is the offline-``--status`` seam: store inspection reads only this
    path, mirroring the SIM rail's ``resolve_token_store_path``. A monitoring
    probe that carries only ``SAXO_LIVE_TOKEN_STORE_PATH`` (not the app
    key/secret/redirect) can still report the store state."""
    store = os.environ.get(_STORE_ENV)
    return Path(store) if store else _DEFAULT_STORE


@dataclass(frozen=True)
class LiveAuthConfig:
    app_key: str
    app_secret: str
    redirect_url: str
    store_path: Path

    @classmethod
    def from_env(cls) -> LiveAuthConfig:
        missing = [
            k for k in (_APP_KEY_ENV, _APP_SECRET_ENV, _REDIRECT_ENV) if not os.environ.get(k)
        ]
        if missing:
            raise RuntimeError(f"missing LIVE market-data env: {', '.join(missing)}")
        return cls(
            app_key=os.environ[_APP_KEY_ENV],
            app_secret=os.environ[_APP_SECRET_ENV],
            redirect_url=os.environ[_REDIRECT_ENV],
            store_path=resolve_live_store_path(),
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
    save_bundle(cfg, bundle)
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


def save_bundle(cfg: LiveAuthConfig, bundle: dict[str, Any]) -> None:
    """Persist a token bundle to the LIVE market-data store — PUBLIC entry.

    Persists atomically: tmp-in-same-dir (0600 from creation) + fsync +
    ``os.replace``. Readers never see a torn file, and there is no window
    where the store is world/group readable — unlike a plain ``write_text``
    followed by a later ``chmod``. ``exchange_code`` / ``refresh`` route
    through here, and the ``marketdata-auth`` CLI relies on that persist —
    so no caller reaches a leading-underscore private name."""
    expires_at = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=int(bundle.get("expires_in", 1200)))
    cfg.store_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "access_token": bundle["access_token"],
        "refresh_token": bundle["refresh_token"],
        "expires_at": expires_at.isoformat(),
    }
    with tempfile.NamedTemporaryFile(
        mode="w",
        dir=cfg.store_path.parent,
        suffix=".tmp",
        delete=False,
        encoding="utf-8",
    ) as tmp:
        json.dump(payload, tmp, indent=2)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_path = Path(tmp.name)
    try:
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, cfg.store_path)
    except OSError:
        tmp_path.unlink(missing_ok=True)
        raise


@contextlib.contextmanager
def _exclusive_lock(store_path: Path) -> Iterator[None]:
    """Per-host exclusive lock around [read -> refresh -> persist].

    Non-blocking ``LOCK_EX|LOCK_NB`` poll loop with an acquire deadline — a
    wedged sibling raises an actionable error instead of hanging. The lock
    lives on a sibling ``.lock`` file (not the store itself) because
    ``os.replace`` in :func:`save_bundle` swaps the store's inode on every write.
    """
    store_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = store_path.with_name(store_path.stem + ".lock")
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + _LOCK_TIMEOUT_S
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        f"could not acquire the Saxo LIVE token-store lock at {lock_path} "
                        f"within {_LOCK_TIMEOUT_S:.0f}s - another process appears stuck"
                    ) from None
                time.sleep(_LOCK_POLL_INTERVAL_S)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


@dataclass(frozen=True)
class LiveTokenStatus:
    """Offline snapshot of the LIVE market-data store — NEVER carries token
    values, so an inspector cannot leak a bearer token by printing it. The
    LIVE store persists no refresh-token expiry, so ``alive`` reflects only
    whether a well-formed store with a refresh token is present (a refresh can
    be attempted); ``access_valid`` reports the access token's own window."""

    store_path: Path
    present: bool
    access_expires_at: dt.datetime | None
    access_valid: bool
    refresh_present: bool

    @property
    def alive(self) -> bool:
        return self.present and self.refresh_present


def _load_store(store_path: Path) -> dict[str, Any]:
    """Read + shape-validate the LIVE token store; NO app credentials, NO
    network. Raises ``RuntimeError`` for an absent or corrupt store, never
    echoing the file content (it may hold a live bearer token)."""
    if not store_path.is_file():
        raise RuntimeError(
            f"no LIVE token store at {store_path} - run the market-data auth bootstrap"
        )
    try:
        state = json.loads(store_path.read_text())
        dt.datetime.fromisoformat(state["expires_at"])
        str(state["access_token"])
        str(state["refresh_token"])
    except (ValueError, KeyError, TypeError) as exc:
        # Never echo the file content — it may hold a live bearer token.
        raise RuntimeError(
            f"LIVE token store at {store_path} is corrupt - delete it and "
            "re-run the market-data auth bootstrap"
        ) from exc
    return state


def inspect_store(store_path: Path | None = None) -> LiveTokenStatus:
    """Inspect the LIVE market-data store OFFLINE — zero network, no side
    effects, no token values, and NO app credentials.

    Mirrors the SIM rail's store-only ``--status``: needs only the token-store
    path (resolved from env or the default when not given), never the app
    key/secret/redirect. An absent store yields ``present=False``; a corrupt
    one raises ``RuntimeError`` (never echoing the file content)."""
    path = store_path if store_path is not None else resolve_live_store_path()
    if not path.is_file():
        return LiveTokenStatus(
            store_path=path,
            present=False,
            access_expires_at=None,
            access_valid=False,
            refresh_present=False,
        )
    state = _load_store(path)
    expires_at = dt.datetime.fromisoformat(state["expires_at"])
    return LiveTokenStatus(
        store_path=path,
        present=True,
        access_expires_at=expires_at,
        access_valid=expires_at > dt.datetime.now(dt.UTC),
        refresh_present=bool(state.get("refresh_token")),
    )


class LiveTokenProvider:
    """Reads the store, refreshing when the access token is near expiry."""

    def __init__(self, cfg: LiveAuthConfig):
        self._cfg = cfg

    def status(self) -> LiveTokenStatus:
        """Inspect the store OFFLINE — zero network, no side effects, and no
        token values returned. Thin wrapper over :func:`inspect_store` bound to
        this provider's store path."""
        return inspect_store(self._cfg.store_path)

    def _load(self) -> dict[str, Any]:
        return _load_store(self._cfg.store_path)

    def access_token(self) -> str:
        with _exclusive_lock(self._cfg.store_path):
            state = self._load()
            expires_at = dt.datetime.fromisoformat(state["expires_at"])
            remaining = (expires_at - dt.datetime.now(dt.UTC)).total_seconds()
            if remaining > _REFRESH_MARGIN_S:
                return state["access_token"]
            return refresh(self._cfg, refresh_token=state["refresh_token"])["access_token"]

    def force_refresh(self) -> str:
        with _exclusive_lock(self._cfg.store_path):
            state = self._load()
            return refresh(self._cfg, refresh_token=state["refresh_token"])["access_token"]
