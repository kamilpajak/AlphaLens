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
        missing = [
            k for k in (_APP_KEY_ENV, _APP_SECRET_ENV, _REDIRECT_ENV) if not os.environ.get(k)
        ]
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
