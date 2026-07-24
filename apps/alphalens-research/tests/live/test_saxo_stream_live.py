"""Opt-in attended Saxo SIM WebSocket streaming probe (dark, ADR 0014).

Design memo: ``docs/research/saxo_streaming_design_2026_07_24.md`` (LOCKED) §Live
SIM probes. This is the SHAPE-only live check for the streaming rail — it hits
the REAL SIM streaming host and asserts protocol SHAPE, never values:

  * **SIM host resolves + connects** — a WS 101-upgrade to the confirmed
    ``SIM_STREAMING_BASE_URL`` with the ``Authorization: BEARER <token>`` header.
  * **Snapshot-on-connect** — each ``/port/v1/{positions,orders}/subscriptions``
    POST returns HTTP 201 with the ``{ContextId, ReferenceId, Snapshot, State,
    ...}`` envelope (the ``Snapshot`` key is the connect-time self-reconcile the
    reader fires ``on_trigger()`` for).
  * **Heartbeat frame** — a control frame (``_heartbeat`` / any ``_``-prefixed
    reference id) is delivered on a quiet subscription within a generous window
    (cadence is unpublished ~20-30s → a miss is TRANSIENT, not a shape break).
  * **PUT re-authorize 202** — ``PUT {streaming-host}/authorize?contextid=<ctx>``
    with the current bearer returns HTTP 202 (re-auth in place, no reconnect).
  * **DELETE cleanup** — ``DELETE /port/v1/{positions,orders}/subscriptions/<ctx>``
    returns HTTP 202 per endpoint (idempotent teardown).

**SHAPE only, never values.** The probe never places an order, never parses a
payload into protection state, and reconciles nothing — it validates that the
protocol the dark reader speaks matches the live SIM gateway. Subscription +
cleanup REST route through the canonical :class:`SaxoClient` (so
``test_no_raw_saxo_http`` stays green); the WS connect + the connect / authorize
URLs come from the production :class:`SaxoStreamingClient` builders, so the probe
validates the SAME URLs the daemon uses. The client is instantiated but NEVER
``start()``-ed — no background thread, no early-wake, no reconcile.

Behind its OWN attended flag ``SAXO_STREAM_LIVE_TEST=1`` (``skipUnless``,
NON-gating) and EXCLUDED from ``just probe-live`` + the weekly CI live-probes job
(the always-run meta-assertion below pins that). It needs the FULL Saxo OAuth env
(``SAXO_APP_KEY`` / ``SAXO_APP_SECRET`` / ``SAXO_AUTH_REDIRECT_URL`` + a valid
on-disk token store) — a bare ``SAXO_SIM_TOKEN`` cannot be PUT-reauthorized, so
run it where OAuth env + a refreshable store already live (the VPS with
``/etc/alphalens/env`` sourced; the ``alphalens-saxo-refresh`` timer keeps the
token fresh). It does NOT need ``ALPHALENS_BROKER_ALLOW_ORDERS`` (never places).
Recipe:

    cd apps/alphalens-research && set -a && . /etc/alphalens/env && set +a && \
    SAXO_STREAM_LIVE_TEST=1 \
        ../../.venv/bin/python -m unittest tests.live.test_saxo_stream_live -v
"""

from __future__ import annotations

import contextlib
import os
import time
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tests.live import PermanentProbeError, TransientProbeError, run_probes

_LIVE_FLAG = "SAXO_STREAM_LIVE_TEST"
_LIVE = os.environ.get(_LIVE_FLAG) == "1"

# HTTP status the streaming protocol pins per step (shape, not value).
_SUBSCRIPTION_CREATED = 201
_REAUTH_ACCEPTED = 202
_DELETE_ACCEPTED = 202

# Heartbeat cadence is unpublished (~20-30s observed) — wait generously; a miss
# is inconclusive (TRANSIENT), never a shape break.
_HEARTBEAT_WAIT_S = 60.0
_RECV_TIMEOUT_S = 15.0

WORKSPACE_ROOT = Path(__file__).resolve().parents[4]


@dataclass
class _StreamArtifacts:
    """Everything the shape probes assert over, collected in ONE connection."""

    pos_status: int = 0
    pos_envelope: dict[str, Any] = field(default_factory=dict)
    ord_status: int = 0
    ord_envelope: dict[str, Any] = field(default_factory=dict)
    heartbeat_ref: str | None = None
    reauth_status: int = 0
    delete_statuses: list[int] = field(default_factory=list)


class TestStreamProbeStaysOutOfAutomation(unittest.TestCase):
    """Hermetic meta-assertion (always runs): the streaming-probe flag must never
    join ``just probe-live`` or the weekly CI live-probes job — it touches the
    live SIM gateway and is attended-only (mirrors the OCO / order-probe pins)."""

    def test_flag_absent_from_justfile_and_ci_workflows(self) -> None:
        scan_targets = [WORKSPACE_ROOT / "justfile"]
        workflows_dir = WORKSPACE_ROOT / ".github" / "workflows"
        scan_targets.extend(sorted(workflows_dir.glob("*.yml")))
        scan_targets.extend(sorted(workflows_dir.glob("*.yaml")))
        self.assertTrue(scan_targets, "expected the justfile + CI workflow files to exist")
        for target in scan_targets:
            with self.subTest(file=str(target.relative_to(WORKSPACE_ROOT))):
                self.assertNotIn(
                    _LIVE_FLAG,
                    target.read_text(encoding="utf-8", errors="replace"),
                    f"{target.name} must never set {_LIVE_FLAG} — the streaming probe "
                    "hits the live SIM gateway and is attended-only by design",
                )


def _classify(exc: Exception) -> Exception:
    """Map a raw network/gateway error to the transient/permanent contract."""
    msg = str(exc).lower()
    if any(tok in msg for tok in ("429", "timeout", "timed out", "connection", "refused", "reset")):
        return TransientProbeError(str(exc))
    return PermanentProbeError(f"saxo stream probe failed: {exc}")


@unittest.skipUnless(_LIVE, f"set {_LIVE_FLAG}=1 to run the attended Saxo SIM streaming probe")
class TestSaxoStreamLiveProbe(unittest.TestCase):
    def test_connect_subscribe_snapshot_heartbeat_reauth_delete(self) -> None:
        import asyncio

        from alphalens_pipeline.brokers.saxo.client import get_default_saxo_client
        from alphalens_pipeline.brokers.saxo.streaming import (
            SIM_STREAMING_BASE_URL,
            SaxoStreamingClient,
            parse_stream_frames,
        )
        from alphalens_pipeline.brokers.saxo.tokens import (
            OAuthTokenProvider,
            StaticTokenProvider,
        )

        provider = OAuthTokenProvider.from_env()
        if isinstance(provider, StaticTokenProvider):  # defensive — from_env returns OAuth
            self.skipTest(
                "OAuth provider required for the streaming probe (static token cannot reauthorize)"
            )
        client = get_default_saxo_client()
        context_id = f"streamprobe-{os.getpid()}-{int(time.time())}"

        # Instantiated but NEVER start()-ed: no thread, no early-wake, no reconcile.
        # Reused ONLY for the production connect / authorize URL builders (so the
        # probe validates the SAME URLs the daemon uses) and the WS connector.
        stream_client = SaxoStreamingClient(
            provider,
            client,
            context_id=context_id,
            on_trigger=lambda: None,
            on_heartbeat=lambda _ts: None,
            streaming_base_url=SIM_STREAMING_BASE_URL,
        )

        try:
            artifacts = asyncio.run(self._collect(stream_client, client, context_id, provider))
        except Exception as exc:  # a total connect failure → nothing to shape-check
            classified = _classify(exc)
            if isinstance(classified, TransientProbeError):
                self.skipTest(f"SIM streaming host unreachable — attended probe skipped: {exc}")
            raise classified from exc

        def _probe_snapshot_on_connect() -> None:
            if artifacts.pos_status != _SUBSCRIPTION_CREATED:
                raise PermanentProbeError(
                    f"positions subscription POST returned {artifacts.pos_status}, "
                    f"expected {_SUBSCRIPTION_CREATED}"
                )
            for key in ("ContextId", "ReferenceId", "Snapshot", "State"):
                if key not in artifacts.pos_envelope:
                    raise PermanentProbeError(
                        f"positions subscription envelope missing {key!r} "
                        f"(keys={sorted(artifacts.pos_envelope)})"
                    )

        def _probe_orders_subscription() -> None:
            if artifacts.ord_status != _SUBSCRIPTION_CREATED:
                raise PermanentProbeError(
                    f"orders subscription POST returned {artifacts.ord_status}, "
                    f"expected {_SUBSCRIPTION_CREATED}"
                )

        def _probe_heartbeat_frame() -> None:
            if artifacts.heartbeat_ref is None:
                # Cadence unpublished — a miss within the window is inconclusive.
                raise TransientProbeError(
                    f"no control/heartbeat frame within {_HEARTBEAT_WAIT_S:.0f}s "
                    "(cadence unpublished — inconclusive, not a shape break)"
                )
            if not artifacts.heartbeat_ref.startswith("_"):
                raise PermanentProbeError(
                    f"heartbeat frame reference id {artifacts.heartbeat_ref!r} is not a "
                    "control ('_'-prefixed) reference id"
                )

        def _probe_put_reauthorize() -> None:
            if artifacts.reauth_status != _REAUTH_ACCEPTED:
                raise PermanentProbeError(
                    f"PUT authorize returned {artifacts.reauth_status}, expected {_REAUTH_ACCEPTED}"
                )

        def _probe_delete_cleanup() -> None:
            if not artifacts.delete_statuses:
                raise PermanentProbeError("DELETE cleanup returned no per-endpoint statuses")
            bad = [s for s in artifacts.delete_statuses if s != _DELETE_ACCEPTED]
            if bad:
                raise PermanentProbeError(
                    f"DELETE cleanup returned {artifacts.delete_statuses}, "
                    f"expected all {_DELETE_ACCEPTED}"
                )

        # parse_stream_frames is exercised inside _collect on real frames — a
        # pure-parser reference so the shape check ties back to the reader's decode.
        self.assertTrue(callable(parse_stream_frames))

        run_probes(
            self,
            {
                "snapshot-on-connect (positions subscription 201 + Snapshot envelope)": _probe_snapshot_on_connect,
                "orders subscription 201": _probe_orders_subscription,
                "heartbeat/control frame delivered": _probe_heartbeat_frame,
                "PUT re-authorize returns 202": _probe_put_reauthorize,
                "DELETE subscription cleanup returns 202": _probe_delete_cleanup,
            },
            label="saxo-stream",
        )

    async def _collect(
        self,
        stream_client: Any,
        client: Any,
        context_id: str,
        provider: Any,
    ) -> _StreamArtifacts:
        """Open ONE live connection, subscribe, wait for a heartbeat, re-authorize,
        then DELETE the subs. Captures SHAPE artifacts only. Cleanup runs in a
        ``finally`` so a mid-flow failure never leaks a live SIM subscription."""
        import asyncio

        from alphalens_pipeline.brokers.saxo.streaming import parse_stream_frames

        artifacts = _StreamArtifacts()
        token = provider.get_access_token()
        stream_client.push_token(token)

        connect_url = stream_client._build_connect_url(None)
        conn = await stream_client._ws_connect(connect_url, {"Authorization": f"BEARER {token}"})
        try:
            client_key = client.get_client_info()["ClientKey"]
            artifacts.pos_status, artifacts.pos_envelope = client.create_positions_subscription(
                context_id=context_id, reference_id="pos", client_key=client_key
            )
            artifacts.ord_status, artifacts.ord_envelope = client.create_orders_subscription(
                context_id=context_id, reference_id="ord", client_key=client_key
            )

            loop = asyncio.get_running_loop()
            deadline = loop.time() + _HEARTBEAT_WAIT_S
            while loop.time() < deadline and artifacts.heartbeat_ref is None:
                remaining = min(_RECV_TIMEOUT_S, max(0.0, deadline - loop.time()))
                try:
                    frame = await asyncio.wait_for(conn.recv(), timeout=remaining)
                except TimeoutError:
                    continue
                if isinstance(frame, str):
                    frame = frame.encode("utf-8")
                for msg in parse_stream_frames(frame):
                    if msg.reference_id.startswith("_"):
                        artifacts.heartbeat_ref = msg.reference_id
                        break

            # PUT re-authorize in place with the CURRENT bearer (shape: expect 202).
            resp = stream_client._session.put(
                stream_client._authorize_url(),
                headers={"Authorization": f"BEARER {token}"},
                timeout=30.0,
            )
            artifacts.reauth_status = resp.status_code
        finally:
            with contextlib.suppress(Exception):
                await conn.close()
            with contextlib.suppress(Exception):
                artifacts.delete_statuses = [
                    status for status, _ in client.delete_all_subscriptions(context_id)
                ]
        return artifacts


if __name__ == "__main__":
    unittest.main()
