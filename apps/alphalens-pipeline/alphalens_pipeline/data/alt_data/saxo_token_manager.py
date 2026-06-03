"""Policy brain for the Saxo OAuth token chain.

Pure logic over an injected clock + monotonic + transport + store. NO network
and NO real sleep in tests. The manager is SINGLE-WRITER: only :meth:`refresh`
(the ``alphalens-saxo-refresh`` keep-alive) calls ``/token`` with
grant_type=refresh_token. Every other consumer uses :meth:`get_access_token`,
which reads the file, uses a fresh token, and fails loud
(:class:`SaxoReauthRequiredError` / :class:`SaxoBootstrapNeededError`) if the
token is missing/expired — it NEVER refreshes (locked design §Scope decision).

Responsibilities (locked design §Token lifecycle):

* ``needs_refresh`` — trips if EITHER the wall check OR the in-process
  monotonic deadline trips (forward NTP step / long pause between
  check-and-use that wall alone would miss).
* Skew-aware safety margins with an explicit budget
  (``MAX_TOLERATED_CLOCK_SKEW_S`` + RTT + jitter => 300s).
* Deadline-bounded retry against ``refresh_token_expires_at`` — never sleeps
  past the deadline; raises :class:`SaxoTransientError` loudly instead. The
  chain is NOT marked dead on transient exhaustion (the next keep-alive fire
  retries).
* Locally-expired short-circuit — never POSTs a wall-expired refresh token
  (it can only burn into invalid_grant); sets reason=expired_locally.
* Min-rotation-interval guard against a backward-NTP-step double rotation.
* Env-record interlock — record['environment'] must equal the requested env.
* In-file lease + cross-process flock (fail-loud) so a manual
  ``alphalens saxo refresh`` racing the timer cannot double-rotate; the lock
  is NOT held across the network (lease released before the POST).
* Write-ahead intent journal + crash recovery (:meth:`recover`).

Tri-state ``chain_state``: 0 healthy / 1 reauth_required / 2 bootstrap_needed
/ 3 corrupt — the gauge the alerts read.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

# Re-export so callers have one import surface.
from alphalens_pipeline.data.alt_data.saxo_client import (
    SaxoBootstrapNeededError,
    SaxoClient,
    SaxoEnvironmentMismatchError,
    SaxoReauthRequiredError,
    SaxoTransientError,
)
from alphalens_pipeline.data.alt_data.saxo_token_store import (
    SCHEMA_VERSION,
    SaxoTokenRecord,
    SaxoTokenStore,
    SaxoTokenStoreCorruptError,
)

logger = logging.getLogger(__name__)

# --- safety margins (explicit skew budget, clock-skew Finding 1) -----------
# ACCESS_SAFETY_MARGIN_S = skew + RTT + jitter + guard. Pinned as an auditable
# constant so the chrony/timesyncd deploy precondition is visible in code.
MAX_TOLERATED_CLOCK_SKEW_S = 60
MAX_TOKEN_RTT_S = 30
SCHEDULER_JITTER_S = 15
_MARGIN_GUARD_S = 195  # rounds the decomposition up to a clean 300s
ACCESS_SAFETY_MARGIN_S = (
    MAX_TOLERATED_CLOCK_SKEW_S + MAX_TOKEN_RTT_S + SCHEDULER_JITTER_S + _MARGIN_GUARD_S
)
# Hard backstop on the refresh-token wall: refuse to POST inside this margin
# of the refresh-token expiry (the deadline-bounded retry uses HARD_FLOOR_S).
REFRESH_SAFETY_MARGIN_S = 300
HARD_FLOOR_S = 30

# Min-rotation guard: do not re-rotate if the token was rotated < this many
# seconds ago while the access token is still valid (backward-NTP-step guard).
MIN_ROTATION_INTERVAL_S = 60

# Deadline-bounded retry backoff schedule (seconds). The loop stops BEFORE a
# sleep would cross (refresh_token_expires_at - HARD_FLOOR_S); it is the
# deadline, not the count, that bounds it.
_BACKOFF_SCHEDULE_S = (5, 15, 30, 60)

# Fallback token TTLs if the live response omits the field (token contract;
# logged as a warning, never silently assumed without a log).
_FALLBACK_ACCESS_TTL_S = 1200
_FALLBACK_REFRESH_TTL_S = 2400

# Lease TTL: how long a written ``journal_state=refreshing`` lease is honored
# by a peer before it is treated as a dead holder and taken over. Comfortably
# larger than the /token RTT + retry budget.
LEASE_TTL_S = 90

# chain_state gauge values.
CHAIN_HEALTHY = 0
CHAIN_REAUTH_REQUIRED = 1
CHAIN_BOOTSTRAP_NEEDED = 2
CHAIN_CORRUPT = 3


class SaxoTokenManager:
    """Single-writer token policy over an injected store + client + clocks."""

    def __init__(
        self,
        *,
        store: SaxoTokenStore,
        client: SaxoClient,
        environment: str,
        wall_clock: Callable[[], float] = time.time,
        mono_clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._store = store
        self._client = client
        self._environment = environment
        self._wall = wall_clock
        self._mono = mono_clock
        self._sleep = sleep
        # In-process monotonic deadline for the currently-cached access token.
        self._access_mono_deadline: float | None = None

    # --- record load + interlock -------------------------------------------

    def _load_checked(self) -> SaxoTokenRecord | None:
        """Read the record and assert the env interlock. ``None`` if no file."""
        record = self._store.read()
        if record is None:
            return None
        if record.environment != self._environment:
            raise SaxoEnvironmentMismatchError(
                f"token record environment {record.environment!r} does not match "
                f"requested {self._environment!r} — refusing to read/write the wrong chain."
            )
        return record

    # --- read path (NEVER refreshes) ---------------------------------------

    def get_access_token(self) -> str:
        """Return a fresh access token, or fail loud. NEVER refreshes.

        Raises :class:`SaxoBootstrapNeededError` if no chain exists,
        :class:`SaxoReauthRequiredError` if the chain is broken / the access
        token is inside its safety margin (a stale token is the keep-alive's
        problem to fix, not the reader's).
        """
        record = self._load_checked()
        if record is None:
            raise SaxoBootstrapNeededError(
                f"no Saxo token chain for env {self._environment!r} — "
                f"run `alphalens saxo auth --env {self._environment}`."
            )
        if record.reauth_required:
            raise SaxoReauthRequiredError(
                f"Saxo {self._environment} chain is reauth_required "
                f"(reason={record.reauth_reason})",
                reason=record.reauth_reason,
            )
        now = self._wall()
        if now >= record.access_token_expires_at - ACCESS_SAFETY_MARGIN_S:
            raise SaxoReauthRequiredError(
                f"Saxo {self._environment} access token is inside its safety "
                "margin; the keep-alive must rotate it (reader does not refresh).",
                reason="expired_locally",
            )
        # Stamp the in-process monotonic deadline (wall vs monotonic split):
        # clamp to [0, access TTL] so a wall jump cannot inflate it.
        remaining = record.access_token_expires_at - now
        self._access_mono_deadline = self._mono() + max(0.0, min(remaining, _FALLBACK_ACCESS_TTL_S))
        return record.access_token

    # --- needs_refresh (wall OR monotonic) ---------------------------------

    def needs_refresh(self, record: SaxoTokenRecord | None = None) -> bool:
        """True if the access token is inside its margin by WALL or MONOTONIC.

        Wall handles cross-restart persistence; monotonic catches a forward
        NTP step / long pause between check-and-use.
        """
        if record is None:
            record = self._load_checked()
        if record is None:
            return False
        now = self._wall()
        wall_due = now >= record.access_token_expires_at - ACCESS_SAFETY_MARGIN_S
        mono_due = (
            self._access_mono_deadline is not None and self._mono() >= self._access_mono_deadline
        )
        return bool(wall_due or mono_due)

    # --- refresh path (SINGLE WRITER) --------------------------------------

    def refresh(self) -> None:
        """Proactively rotate the chain if it is inside the safety margin.

        No-op if the access token is comfortably fresh. Otherwise: acquire the
        lock, double-check (a peer may have just rotated), write the journal
        lease, RELEASE the lock, POST with deadline-bounded retry, re-acquire,
        commit the rotated token.
        """
        record = self._load_checked()
        if record is None:
            raise SaxoBootstrapNeededError(
                f"no Saxo token chain for env {self._environment!r} — run "
                f"`alphalens saxo auth --env {self._environment}`."
            )
        if record.reauth_required:
            raise SaxoReauthRequiredError(
                f"Saxo {self._environment} chain already reauth_required "
                f"(reason={record.reauth_reason})",
                reason=record.reauth_reason,
            )

        now = self._wall()
        # Stale restored backup: a present file whose refresh token already
        # expired is bootstrap/reauth, never healthy, never POSTed.
        if now >= record.refresh_token_expires_at:
            self._mark_reauth(record, reason="expired_locally")
            raise SaxoReauthRequiredError(
                f"Saxo {self._environment} refresh token expired locally "
                "(downtime) — manual re-auth required.",
                reason="expired_locally",
            )

        if not self.needs_refresh(record):
            return

        # Min-rotation guard: backward NTP step right after a rotation must not
        # double-rotate while the access token is still genuinely valid.
        if (
            now - record.rotated_at < MIN_ROTATION_INTERVAL_S
            and now < record.access_token_expires_at - ACCESS_SAFETY_MARGIN_S
        ):
            logger.info("saxo refresh suppressed by min-rotation guard")
            return

        self._do_refresh_under_lock(record)

    def _do_refresh_under_lock(self, record: SaxoTokenRecord) -> None:
        # Phase 1: acquire, double-check, write the lease, RELEASE.
        with self._store.locked():
            fresh = self._load_checked()
            if fresh is None:
                fresh = record
            # A peer rotated while we waited: nothing to do.
            if not self.needs_refresh(fresh):
                return
            now = self._wall()
            # An unexpired peer lease means another writer is mid-refresh.
            if (
                fresh.journal_state == "refreshing"
                and fresh.journal_attempted_at is not None
                and now - fresh.journal_attempted_at < LEASE_TTL_S
            ):
                logger.info("saxo refresh deferring to a fresh peer lease")
                return
            candidate_rt = fresh.refresh_token
            leased = self._with_fields(fresh, journal_state="refreshing", journal_attempted_at=now)
            self._store.write(leased)

        # Phase 2: network (lock NOT held).
        try:
            payload = self._refresh_with_deadline(candidate_rt, leased.refresh_token_expires_at)
        except SaxoReauthRequiredError as exc:
            # Permanent rejection: mark the chain dead under a fresh lock so the
            # sticky reauth flag + gauge + alert path fire exactly once.
            with self._store.locked():
                current = self._load_checked() or leased
                self._mark_reauth(current, reason="server_rejected")
            raise SaxoReauthRequiredError(str(exc), reason="server_rejected") from exc

        # Phase 3: re-acquire, commit the rotated token.
        with self._store.locked():
            self._commit_rotation(candidate_rt, payload)

    def _refresh_with_deadline(self, refresh_token: str, deadline_expiry: float) -> dict:
        """POST with deadline-bounded retry. Raises on exhaustion / permanent.

        ``deadline_expiry`` is the refresh-token wall expiry; the loop stops
        before a sleep would cross ``deadline_expiry - HARD_FLOOR_S``.
        """
        attempt = 0
        while True:
            try:
                return self._client.refresh_token(refresh_token=refresh_token)
            except SaxoReauthRequiredError:
                # Permanent — caller's lock context records reauth. Re-raise so
                # the chain is marked dead exactly once at the commit site.
                raise
            except SaxoTransientError:
                backoff = _BACKOFF_SCHEDULE_S[min(attempt, len(_BACKOFF_SCHEDULE_S) - 1)]
                attempt += 1
                if self._wall() + backoff >= deadline_expiry - HARD_FLOOR_S:
                    # The next sleep would run the grant off the cliff — stop
                    # and raise loudly. The chain is NOT marked dead (the RT is
                    # still valid; the next keep-alive fire retries).
                    raise SaxoTransientError(
                        f"saxo refresh exhausted the deadline budget for env "
                        f"{self._environment} (would cross refresh expiry)"
                    ) from None
                self._sleep(backoff)

    def _commit_rotation(self, used_rt: str, payload: dict) -> None:
        fresh = self._load_checked()
        base = fresh if fresh is not None else None
        now = self._wall()
        new_refresh = payload.get("refresh_token")
        if not new_refresh:
            # 2xx without a rotated refresh token: contract break. Do NOT keep
            # the (now server-invalidated) old RT as active — leave the journal
            # in 'refreshing' so the state is visibly non-active.
            from alphalens_pipeline.data.alt_data.saxo_client import SaxoTokenContractError

            raise SaxoTokenContractError("saxo /token 2xx rotation omitted the refresh_token field")
        access_ttl = self._coerce_ttl(payload.get("expires_in"), _FALLBACK_ACCESS_TTL_S, "access")
        refresh_ttl = self._coerce_ttl(
            payload.get("refresh_token_expires_in"), _FALLBACK_REFRESH_TTL_S, "refresh"
        )
        last_full_auth = base.last_full_auth_at if base is not None else now
        rotated = SaxoTokenRecord(
            schema_version=SCHEMA_VERSION,
            environment=self._environment,
            access_token=str(payload["access_token"]),
            refresh_token=str(new_refresh),
            previous_refresh_token=used_rt,
            access_token_expires_at=now + access_ttl,
            refresh_token_expires_at=now + refresh_ttl,
            rotated_at=now,
            reauth_required=False,
            reauth_reason="none",
            journal_state="active",
            journal_attempted_at=None,
            last_full_auth_at=last_full_auth,
        )
        self._store.write(rotated)
        # Refresh the in-process monotonic deadline for the new access token.
        self._access_mono_deadline = self._mono() + access_ttl

    def _coerce_ttl(self, value: object, fallback: int, label: str) -> float:
        if value is None:
            logger.warning(
                "saxo /token response omitted the %s TTL; falling back to %ss", label, fallback
            )
            return float(fallback)
        try:
            return float(value)
        except (TypeError, ValueError):
            logger.warning(
                "saxo /token %s TTL %r is non-numeric; falling back to %ss",
                label,
                value,
                fallback,
            )
            return float(fallback)

    # --- crash recovery (intent journal) -----------------------------------

    def recover(self) -> None:
        """If the chain is mid-refresh (journal_state=refreshing), retry the
        journaled refresh token exactly once.

        2xx -> recovered (the prior POST never reached Saxo or its response was
        lost). invalid_grant -> the RT was consumed and the new one was lost ->
        set reauth_required reason=lost_rotation + alert. Strictly cannot lose.
        """
        record = self._load_checked()
        if record is None or record.journal_state != "refreshing":
            return
        with self._store.locked():
            fresh = self._load_checked()
            if fresh is None or fresh.journal_state != "refreshing":
                return
            try:
                payload = self._client.refresh_token(refresh_token=fresh.refresh_token)
            except SaxoReauthRequiredError as exc:
                self._mark_reauth(fresh, reason="lost_rotation")
                raise SaxoReauthRequiredError(
                    f"saxo {self._environment} journal recovery hit invalid_grant — "
                    "the rotated token was lost; manual re-auth required.",
                    reason="lost_rotation",
                ) from exc
            self._commit_rotation(fresh.refresh_token, payload)

    # --- state helpers ------------------------------------------------------

    def _mark_reauth(self, record: SaxoTokenRecord, *, reason: str) -> None:
        """Sticky reauth flag — goes through the full durable rename path."""
        self._store.write(self._with_fields(record, reauth_required=True, reauth_reason=reason))

    @staticmethod
    def _with_fields(record: SaxoTokenRecord, **fields: object) -> SaxoTokenRecord:
        data = {
            "schema_version": record.schema_version,
            "environment": record.environment,
            "access_token": record.access_token,
            "refresh_token": record.refresh_token,
            "previous_refresh_token": record.previous_refresh_token,
            "access_token_expires_at": record.access_token_expires_at,
            "refresh_token_expires_at": record.refresh_token_expires_at,
            "rotated_at": record.rotated_at,
            "reauth_required": record.reauth_required,
            "reauth_reason": record.reauth_reason,
            "journal_state": record.journal_state,
            "journal_attempted_at": record.journal_attempted_at,
            "last_full_auth_at": record.last_full_auth_at,
        }
        data.update(fields)
        return SaxoTokenRecord(**data)  # type: ignore[arg-type]

    def chain_state(self) -> int:
        """Tri-state gauge: 0 healthy / 1 reauth / 2 bootstrap / 3 corrupt."""
        try:
            record = self._store.read()
        except SaxoTokenStoreCorruptError:
            return CHAIN_CORRUPT
        if record is None:
            return CHAIN_BOOTSTRAP_NEEDED
        try:
            if record.environment != self._environment:
                # Wrong-env record is a corrupt/misconfigured chain for THIS
                # env's purposes.
                return CHAIN_CORRUPT
        except AttributeError:  # pragma: no cover - dataclass always has it
            return CHAIN_CORRUPT
        if record.reauth_required:
            return CHAIN_REAUTH_REQUIRED
        return CHAIN_HEALTHY


__all__ = [
    "ACCESS_SAFETY_MARGIN_S",
    "CHAIN_BOOTSTRAP_NEEDED",
    "CHAIN_CORRUPT",
    "CHAIN_HEALTHY",
    "CHAIN_REAUTH_REQUIRED",
    "HARD_FLOOR_S",
    "LEASE_TTL_S",
    "MAX_TOLERATED_CLOCK_SKEW_S",
    "MIN_ROTATION_INTERVAL_S",
    "REFRESH_SAFETY_MARGIN_S",
    "SaxoBootstrapNeededError",
    "SaxoTokenManager",
]
