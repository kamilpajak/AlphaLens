"""SQLite-backed feedback ledger.

Schema declared idempotently via ``CREATE TABLE IF NOT EXISTS`` so every
``FeedbackStore.open()`` call self-heals on a fresh checkout. No separate
migration step; first-run and every-run share one code path.

Concurrency: SQLite WAL mode allows concurrent readers while serialising
writers. The Django prod handler is the main writer, the pipeline
backfill jobs (paper-trade outcome join, v2) will be the second writer.
An advisory ``fcntl.flock`` on a sibling ``.lock`` file arbitrates,
matching the pattern in ``alphalens_pipeline.paper.ledger``. The lock
file is intentionally never cleaned up (zen pre-merge finding #6) —
single zero-byte file per DB, no harm in backups, removing it on exit
would race with a concurrent open() that's about to acquire it.

The ``DISMISS_TAXONOMY`` mapping is the single source of truth for the
2-level dismiss enum locked in the design memo. Django serializer +
SPA dropdown both consume it; if a reason moves between categories,
update here and the test in ``test_feedback_store.py`` fails, forcing a
coordinated change.

Schema migration story (zen pre-merge finding #4): v2 column additions
should use ``ALTER TABLE decisions ADD COLUMN <name> <type>`` with a
default that's compatible with existing NULL rows (typically NULL).
The bootstrap ``_ensure_schema`` is additive — it never DROPs or
ALTERs, so adding rows to ``_SCHEMA_DDL`` after a CREATE TABLE IF NOT
EXISTS is enough for fresh databases, but legacy ones need an explicit
ALTER TABLE block. Use ``PRAGMA user_version`` to track the applied
schema generation when more than one column changes accumulate. No
backfill required for any v1→v2 path foreseen in the roadmap.

Operator monitoring (zen pre-merge finding #7): until the v2 weekly
review SPA route ships, use ``alphalens feedback report`` CLI for
action distribution + dismiss-reason histogram + "other" usage
percentage. >15% other indicates a taxonomy gap (per design memo).
"""

from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import logging
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Sentinel distinguishing "argument omitted" from an explicit None in
# stamp_outcome — so the cheap fill-status pass leaves shadow/realized columns
# untouched while the shadow pass can write an explicit NULL. Typed Any so the
# parameter default stays assignable to ``float | None``.
_UNSET: Any = object()


# ----------------------------------------------------------------------
# Locked enums (mirror design memo §2 / §2.1)
# ----------------------------------------------------------------------

ACTIONS: tuple[str, ...] = (
    "interested",
    "watching",
    "dismissed",
    "paper_traded",
    "live_traded",
)

DISMISS_TAXONOMY: dict[str, tuple[str, ...]] = {
    "thesis_setup": ("wrong_theme", "too_expensive", "bad_setup"),
    "risk_quality": ("business_management", "risk_jurisdiction", "dont_understand"),
    "portfolio_style": ("already_have_exposure", "liquidity_too_low", "not_my_style"),
    "other": ("other",),
}

# Reverse index for fast pair-validation: reason → expected category.
_REASON_TO_CATEGORY: dict[str, str] = {
    reason: category for category, reasons in DISMISS_TAXONOMY.items() for reason in reasons
}


class DecisionValidationError(ValueError):
    """Raised when a `Decision` violates a locked schema invariant.

    Subclass of ValueError so Django REST serializer auto-converts to a
    400 response, and existing callers that catch ValueError keep working.
    """


# ----------------------------------------------------------------------
# Decision dataclass — validates in __post_init__
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class Decision:
    """One user-authored row in the feedback ledger.

    Cross-field invariants checked in ``__post_init__`` rather than via
    SQL ``CHECK`` constraints because the 2-level dismiss taxonomy pair
    (category, reason) does not encode cleanly as a single CHECK
    expression — a 9-row CASE WHEN would be unreadable. Python validation
    keeps the rules co-located with the dataclass that defines the
    schema surface.

    ``frozen=True`` is defensive (zen pre-merge finding #2): no callsite
    mutates a Decision today, but freezing it prevents accidental field
    reassignment from skipping the ``__post_init__`` rules.

    Read-time validation contract (zen pre-merge finding #3): every
    Decision constructed via the public ``__init__`` runs full
    validation. The ``_from_row()`` classmethod used by
    :func:`_row_to_decision` deliberately bypasses validation so that
    tightening rules in a future v2 does not retroactively break the
    READ path for legacy rows. WRITES go through ``__init__`` and stay
    fully validated.
    """

    brief_date: dt.date
    ticker: str
    theme: str
    surfaced_at: dt.datetime
    action: str
    action_at: dt.datetime
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    dismiss_category: str | None = None
    dismiss_reason: str | None = None
    dismiss_note: str | None = None
    confidence_subjective: int | None = None
    paper_trade_plan_id: str | None = None
    position_size_usd: float | None = None
    entry_price: float | None = None
    market_regime_at_entry: str | None = None
    # ---- v3 brief-metadata columns (click-time, stamped from Brief; design memo Variant A) ----
    layer4_score: int | None = None
    rank_in_day: int | None = None
    cohort_size_in_day: int | None = None
    gate_verdict_json: str | None = None
    brief_model_used: str | None = None
    # ---- v2 outcome-join columns (Track A v2; design memo §4 + §8 L3) ----
    # All job-set by the post-hoc decision<->paper outcome-join (see
    # ``outcome_join.join_decision_outcomes``), NEVER user-writable. The
    # paper harness auto-submits every verified candidate independent of any
    # click, so a decision is linked to its plan outcome by (brief_date,
    # ticker, account) after the plan closes. ``fill_status`` carries the §4
    # FILLED / UNFILLED / PARTIAL distinction so never-filled candidates are
    # recorded, not dropped (Glosten/Linnainmaa fill-only adverse selection).
    # ``shadow_return`` (arrival-price counterfactual, decimal fraction) +
    # ``realized_return`` ((blended_exit − blended_entry)/blended_entry, also a
    # fraction so the §6 execution gap ``realized_return − shadow_return`` is
    # unit-consistent) are computed by the PR-3 shadow-return pass; PR-1's
    # fill-status join leaves them untouched.
    outcome_plan_id: str | None = None
    fill_status: str | None = None
    exit_kind: str | None = None
    shadow_return: float | None = None
    realized_return: float | None = None
    outcome_computed_at: dt.datetime | None = None

    def __post_init__(self) -> None:
        self._validate_action()
        self._validate_dismiss_fields()
        self._validate_confidence_subjective()
        self._validate_live_traded_only_fields()

    def _validate_action(self) -> None:
        if self.action not in ACTIONS:
            raise DecisionValidationError(
                f"action={self.action!r} not in {ACTIONS}",
            )

    def _validate_dismiss_fields(self) -> None:
        if self.action == "dismissed":
            if not self.dismiss_category or not self.dismiss_reason:
                raise DecisionValidationError(
                    "action='dismissed' requires both dismiss_category and dismiss_reason"
                )
            # Pair-integrity: reason must belong to the named category.
            expected_category = _REASON_TO_CATEGORY.get(self.dismiss_reason)
            if expected_category is None:
                raise DecisionValidationError(
                    f"dismiss_reason={self.dismiss_reason!r} not in taxonomy"
                )
            if expected_category != self.dismiss_category:
                raise DecisionValidationError(
                    f"dismiss_reason={self.dismiss_reason!r} belongs to "
                    f"category={expected_category!r}, not {self.dismiss_category!r}"
                )
            # `other` reason needs a free-text note so the catch-all stays
            # diagnostic; without this, "other" becomes useless noise.
            if self.dismiss_reason == "other" and not self.dismiss_note:
                raise DecisionValidationError(
                    "dismiss_reason='other' requires a non-empty dismiss_note"
                )
        elif self.dismiss_category is not None or self.dismiss_reason is not None:
            raise DecisionValidationError(
                f"action={self.action!r} must not carry dismiss_category / dismiss_reason"
            )

    def _validate_confidence_subjective(self) -> None:
        if self.confidence_subjective is None:
            return
        if not 1 <= self.confidence_subjective <= 5:
            raise DecisionValidationError(
                f"confidence_subjective={self.confidence_subjective} not in 1..5"
            )

    def _validate_live_traded_only_fields(self) -> None:
        # position_size_usd + entry_price are only meaningful for live_traded;
        # paper_traded carries them indirectly via paper_trade_plan_id.
        if self.action != "live_traded" and (
            self.position_size_usd is not None or self.entry_price is not None
        ):
            raise DecisionValidationError(
                f"action={self.action!r} must not carry "
                "position_size_usd / entry_price (live_traded only)"
            )

    @classmethod
    def _from_row(cls, **fields) -> Decision:
        """Construct a Decision from a DB row, BYPASSING __post_init__ rules.

        Use only from :func:`_row_to_decision`. Future tightening of
        validation rules (e.g. adding a new required field) would
        otherwise break the READ path for legacy rows persisted under
        looser v1 rules — see class docstring "Read-time validation
        contract". WRITE path callers use the public constructor and
        stay fully validated.

        Works with ``frozen=True`` via ``object.__setattr__`` since the
        normal assignment path is locked.
        """
        instance = cls.__new__(cls)
        for key, value in fields.items():
            object.__setattr__(instance, key, value)
        return instance


# ----------------------------------------------------------------------
# Schema DDL
# ----------------------------------------------------------------------

_SCHEMA_DDL = (
    """
    CREATE TABLE IF NOT EXISTS decisions (
        id TEXT PRIMARY KEY,
        brief_date TEXT NOT NULL,
        ticker TEXT NOT NULL,
        theme TEXT NOT NULL,
        surfaced_at TEXT NOT NULL,
        action TEXT NOT NULL CHECK(
            action IN ('interested', 'watching', 'dismissed', 'paper_traded', 'live_traded')
        ),
        action_at TEXT NOT NULL,
        dismiss_category TEXT,
        dismiss_reason TEXT,
        dismiss_note TEXT,
        confidence_subjective INTEGER CHECK(
            confidence_subjective IS NULL OR confidence_subjective BETWEEN 1 AND 5
        ),
        paper_trade_plan_id TEXT,
        position_size_usd REAL,
        entry_price REAL,
        market_regime_at_entry TEXT,
        layer4_score INTEGER,
        rank_in_day INTEGER,
        cohort_size_in_day INTEGER,
        gate_verdict_json TEXT,
        brief_model_used TEXT,
        outcome_plan_id TEXT,
        fill_status TEXT,
        exit_kind TEXT,
        shadow_return REAL,
        realized_return REAL,
        outcome_computed_at TEXT,
        sequence_str TEXT,
        mfe REAL,
        mae REAL,
        forward_return REAL,
        ladder_classification TEXT,
        blended_entry REAL,
        realized_r REAL,
        horizon_open TEXT,
        ambiguous_bars INTEGER,
        ratchet_realized_r REAL,
        UNIQUE(brief_date, ticker, theme)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_decisions_brief_date ON decisions(brief_date)",
    "CREATE INDEX IF NOT EXISTS idx_decisions_ticker ON decisions(ticker)",
    "CREATE INDEX IF NOT EXISTS idx_decisions_action ON decisions(action)",
)

# Schema generation tracked via ``PRAGMA user_version``. 0 = v1 (15 columns).
# 1 = v2 outcome-join columns added (PR-1). 2 = ``realized_pnl`` renamed to
# ``realized_return`` (PR-3) so it is a decimal fraction unit-consistent with
# ``shadow_return`` for the §6 execution-gap subtraction. 3 = v3 click-time
# brief-metadata columns added (Variant A) — stamped from the Brief on every
# insert (NOT carried-forward like the job-set outcome columns). 4 = ladder-replay
# outcome columns added (broker-free price-path replay) — job-set by the nightly
# ladder backfill, like the v2 outcome columns (carried-forward on upsert).
_SCHEMA_GENERATION = 4

# v2 outcome-join columns, as (name, sqlite_type). Present in ``_SCHEMA_DDL``
# above for fresh databases AND added to legacy v1 databases by
# ``_migrate_schema`` via ``ALTER TABLE ADD COLUMN`` (NULL-compatible with
# existing rows). Kept as a single source so the CREATE block and the ALTER
# block cannot drift.
_OUTCOME_COLUMNS: tuple[tuple[str, str], ...] = (
    ("outcome_plan_id", "TEXT"),
    ("fill_status", "TEXT"),
    ("exit_kind", "TEXT"),
    ("shadow_return", "REAL"),
    ("realized_return", "REAL"),
    ("outcome_computed_at", "TEXT"),
)

# v3 click-time brief-metadata columns, as (name, sqlite_type). Same single-
# source-of-truth role as ``_OUTCOME_COLUMNS``: present in ``_SCHEMA_DDL`` for
# fresh databases AND added to gen-2 databases by ``_migrate_schema`` via
# ``ALTER TABLE ADD COLUMN``. UNLIKE the outcome columns these are written
# DIRECTLY from the Decision on every insert (click-time, not carried-forward).
_BRIEF_METADATA_COLUMNS: tuple[tuple[str, str], ...] = (
    ("layer4_score", "INTEGER"),
    ("rank_in_day", "INTEGER"),
    ("cohort_size_in_day", "INTEGER"),
    ("gate_verdict_json", "TEXT"),
    ("brief_model_used", "TEXT"),
)

# Generation 4 ladder-replay outcome columns, as (name, sqlite_type). Same
# single-source-of-truth role as ``_OUTCOME_COLUMNS``: present in ``_SCHEMA_DDL``
# for fresh databases AND added to gen-3 databases by ``_migrate_schema`` via
# ``ALTER TABLE ADD COLUMN``. Job-set by the nightly broker-free ladder backfill
# (``alphalens_pipeline.feedback.ladder_backfill``), never by the user POST path.
# ``horizon_open`` is stored as TEXT (str of the bool) so the engine's tri-state
# (True / False / never-replayed-NULL) survives the round-trip.
_LADDER_OUTCOME_COLUMNS: tuple[tuple[str, str], ...] = (
    ("sequence_str", "TEXT"),
    ("mfe", "REAL"),
    ("mae", "REAL"),
    ("forward_return", "REAL"),
    ("ladder_classification", "TEXT"),
    ("blended_entry", "REAL"),
    ("realized_r", "REAL"),
    ("horizon_open", "TEXT"),
    ("ambiguous_bars", "INTEGER"),
    ("ratchet_realized_r", "REAL"),
)


def _connect(path: Path) -> sqlite3.Connection:
    """Open a connection with WAL + foreign keys + immediate commit."""
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Apply DDL idempotently, then run the additive column migration."""
    for ddl in _SCHEMA_DDL:
        conn.execute(ddl)
    _migrate_schema(conn)


def _migrate_schema(conn: sqlite3.Connection) -> None:
    """Add v2 outcome columns to legacy databases (zen pre-merge finding #4).

    The bootstrap CREATE TABLE IF NOT EXISTS does NOT alter an existing
    table, so a v1 database (15 columns) never gains the v2 columns from
    the DDL loop alone. This guards on ``PRAGMA table_info`` (NOT on
    ``user_version`` alone): a freshly-created v2 database already carries
    the columns from the CREATE block yet still has ``user_version = 0`` on
    first open, so a blind ``ALTER TABLE ADD COLUMN`` would raise
    "duplicate column name". Skipping already-present columns makes the
    migration idempotent for both fresh and legacy databases.

    Generation 2 (PR-3) renames the gen-1 ``realized_pnl`` column to
    ``realized_return``. The rename runs BEFORE the additive ADD COLUMN loop
    so a gen-1 database carries its (always-NULL — PR-1 never populated it)
    column over rather than gaining a second empty ``realized_return``.

    Generation 3 (v3 Variant A) adds the click-time brief-metadata columns
    (``_BRIEF_METADATA_COLUMNS``) via the same idempotent table_info-guarded
    ADD COLUMN loop as the outcome columns.

    Generation 4 (ladder replay) adds the broker-free price-path replay outcome
    columns (``_LADDER_OUTCOME_COLUMNS``) via the same idempotent table_info-
    guarded ADD COLUMN loop. They are job-set by the nightly ladder backfill
    (carried-forward on upsert like the v2 outcome columns), never user-written.
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(decisions)")}
    # gen 1 -> gen 2: realized_pnl (dollars, never populated) -> realized_return
    # (decimal fraction). Only when the legacy name is present and the new one
    # is absent — idempotent for fresh gen-2 databases.
    if "realized_pnl" in existing and "realized_return" not in existing:
        conn.execute("ALTER TABLE decisions RENAME COLUMN realized_pnl TO realized_return")
        existing.discard("realized_pnl")
        existing.add("realized_return")
    for name, sql_type in _OUTCOME_COLUMNS:
        if name not in existing:
            # Identifiers are module constants, not user input — safe to inline.
            conn.execute(f"ALTER TABLE decisions ADD COLUMN {name} {sql_type}")
    # gen 2 -> gen 3: v3 click-time brief-metadata columns. Same idempotent
    # table_info-guarded ADD COLUMN pattern as the outcome loop above.
    for name, sql_type in _BRIEF_METADATA_COLUMNS:
        if name not in existing:
            # Identifiers are module constants, not user input — safe to inline.
            conn.execute(f"ALTER TABLE decisions ADD COLUMN {name} {sql_type}")
    # gen 3 -> gen 4: ladder-replay outcome columns. Same idempotent
    # table_info-guarded ADD COLUMN pattern.
    for name, sql_type in _LADDER_OUTCOME_COLUMNS:
        if name not in existing:
            # Identifiers are module constants, not user input — safe to inline.
            conn.execute(f"ALTER TABLE decisions ADD COLUMN {name} {sql_type}")
    conn.execute(f"PRAGMA user_version = {_SCHEMA_GENERATION}")


# ----------------------------------------------------------------------
# FeedbackStore — context manager that holds the file lock for the scope
# ----------------------------------------------------------------------


class FeedbackStore:
    """Connection + advisory-lock wrapper for ``feedback.db``.

    Usage::

        with FeedbackStore.open(Path("~/.alphalens/feedback.db")) as fb:
            row_id = fb.insert(decision)
            fetched = fb.get(row_id)

    The advisory lock matches the pattern in ``alphalens_pipeline.paper.
    ledger`` so the SPA POST path and any future pipeline backfill cron
    serialise correctly. Tests run sequentially in one process so the
    lock is a no-op there; in production it arbitrates between Django
    gunicorn workers (multiple, but WAL handles read-concurrency) and
    pipeline cron jobs (single writer at most).
    """

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    @classmethod
    @contextmanager
    def open(cls, path: Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = path.with_name(path.name + ".lock")
        lock_handle = open(lock_path, "w")  # noqa: SIM115 — released in finally
        try:
            fcntl.flock(lock_handle, fcntl.LOCK_EX)
        except OSError as exc:  # pragma: no cover - platform fallback
            logger.warning("feedback-store advisory lock unavailable (%s): %s", lock_path, exc)
        conn = _connect(path)
        _ensure_schema(conn)
        try:
            yield cls(conn)
        finally:
            conn.close()
            with contextlib.suppress(OSError):  # pragma: no cover - filesystem fallback
                fcntl.flock(lock_handle, fcntl.LOCK_UN)
            lock_handle.close()

    # ---- writes -------------------------------------------------------

    def insert(self, decision: Decision) -> tuple[str, bool]:
        """Upsert a decision keyed on (brief_date, ticker, theme).

        Idempotent on the unique key: a user flipping interested → dismissed
        on the same candidate replaces the prior row rather than tripping
        a UNIQUE constraint. Returns ``(row_id, was_created)`` where
        ``was_created=True`` means a brand-new row, ``False`` means a row
        with the same unique-key existed and was replaced (id preserved
        so the SPA's local undo reference stays valid). The Django POST
        view uses ``was_created`` to pick HTTP 201 vs 200 per REST
        convention (zen pre-merge finding #5).
        """
        existing = self.conn.execute(
            """SELECT id, outcome_plan_id, fill_status, exit_kind,
                      shadow_return, realized_return, outcome_computed_at,
                      sequence_str, mfe, mae, forward_return, ladder_classification,
                      blended_entry, realized_r, horizon_open, ambiguous_bars,
                      ratchet_realized_r
               FROM decisions WHERE brief_date = ? AND ticker = ? AND theme = ?""",
            (decision.brief_date.isoformat(), decision.ticker, decision.theme),
        ).fetchone()
        was_created = existing is None
        target_id = existing["id"] if existing else decision.id
        # Outcome + ladder columns are job-set by the post-hoc outcome-join and
        # the nightly ladder backfill, never by the user POST path. On upsert (a
        # user flipping interested -> dismissed) INSERT OR REPLACE rewrites the
        # WHOLE row, so carry both groups of job-set values forward rather than
        # letting them revert to NULL. A brand-new row has none yet (all NULL).
        # (zen CRITICAL: omitting the ladder group here silently wiped a
        # previously-replayed ladder outcome on any user re-click.)
        if existing is not None:
            outcome_values = (
                existing["outcome_plan_id"],
                existing["fill_status"],
                existing["exit_kind"],
                existing["shadow_return"],
                existing["realized_return"],
                existing["outcome_computed_at"],
            )
            ladder_values = tuple(existing[name] for name, _t in _LADDER_OUTCOME_COLUMNS)
        else:
            outcome_values = (None, None, None, None, None, None)
            ladder_values = (None,) * len(_LADDER_OUTCOME_COLUMNS)
        self.conn.execute(
            """
            INSERT OR REPLACE INTO decisions (
                id, brief_date, ticker, theme, surfaced_at, action, action_at,
                dismiss_category, dismiss_reason, dismiss_note,
                confidence_subjective, paper_trade_plan_id,
                position_size_usd, entry_price, market_regime_at_entry,
                layer4_score, rank_in_day, cohort_size_in_day,
                gate_verdict_json, brief_model_used,
                outcome_plan_id, fill_status, exit_kind,
                shadow_return, realized_return, outcome_computed_at,
                sequence_str, mfe, mae, forward_return, ladder_classification,
                blended_entry, realized_r, horizon_open, ambiguous_bars,
                ratchet_realized_r
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                target_id,
                decision.brief_date.isoformat(),
                decision.ticker,
                decision.theme,
                decision.surfaced_at.isoformat(),
                decision.action,
                decision.action_at.isoformat(),
                decision.dismiss_category,
                decision.dismiss_reason,
                decision.dismiss_note,
                decision.confidence_subjective,
                decision.paper_trade_plan_id,
                decision.position_size_usd,
                decision.entry_price,
                decision.market_regime_at_entry,
                # v3 brief-metadata: written DIRECTLY from the Decision on every
                # insert (click-time), NOT carried-forward like the job-set groups.
                decision.layer4_score,
                decision.rank_in_day,
                decision.cohort_size_in_day,
                decision.gate_verdict_json,
                decision.brief_model_used,
                *outcome_values,
                *ladder_values,
            ),
        )
        return target_id, was_created

    def stamp_outcome(
        self,
        decision_id: str,
        *,
        fill_status: str,
        exit_kind: str,
        outcome_plan_id: str,
        outcome_computed_at: dt.datetime,
        shadow_return: float | None = _UNSET,
        realized_return: float | None = _UNSET,
    ) -> None:
        """Stamp the paper-trade outcome onto a decision (job-set, v2).

        A targeted ``UPDATE`` rather than an :meth:`insert` rebuild: it does
        not reconstruct a frozen ``Decision`` (so ``__post_init__`` does NOT
        re-run — a legacy row that would fail tightened write-time rules is
        still stampable, consistent with the read-time bypass contract), it
        holds the lock only per-decision, and it is naturally idempotent.

        Two-pass safety (PR-3): ``shadow_return`` / ``realized_return`` use a
        sentinel default, NOT ``None``. The cheap fill-status pass
        (``outcome_join.join_decision_outcomes``) omits them, so they are left
        out of the ``SET`` clause and a previously-computed value SURVIVES. The
        rate-limited shadow pass (``shadow_return.compute_shadow_returns``)
        passes them explicitly — even an explicit ``None`` is then written
        (e.g. an UNFILLED row gets ``realized_return = NULL``). A blind
        always-write would let the daily fill-status re-run wipe the nightly
        shadow value back to NULL.
        """
        set_clauses = [
            "fill_status = ?",
            "exit_kind = ?",
            "outcome_plan_id = ?",
            "outcome_computed_at = ?",
        ]
        params: list[object] = [
            fill_status,
            exit_kind,
            outcome_plan_id,
            outcome_computed_at.isoformat(),
        ]
        # Column names are module-fixed identifiers, not user input — safe to
        # inline; only the VALUES are parameterised.
        if shadow_return is not _UNSET:
            set_clauses.append("shadow_return = ?")
            params.append(shadow_return)
        if realized_return is not _UNSET:
            set_clauses.append("realized_return = ?")
            params.append(realized_return)
        params.append(decision_id)
        self.conn.execute(
            # set_clauses are module-fixed identifiers, never user input; only
            # the VALUES are parameterised (? placeholders).
            f"UPDATE decisions SET {', '.join(set_clauses)} WHERE id = ?",  # nosec B608
            params,
        )

    def stamp_ladder_outcome(
        self,
        decision_id: str,
        *,
        sequence_str: str | None = _UNSET,
        mfe: float | None = _UNSET,
        mae: float | None = _UNSET,
        forward_return: float | None = _UNSET,
        ladder_classification: str | None = _UNSET,
        blended_entry: float | None = _UNSET,
        realized_r: float | None = _UNSET,
        horizon_open: str | None = _UNSET,
        ambiguous_bars: int | None = _UNSET,
        ratchet_realized_r: float | None = _UNSET,
    ) -> None:
        """Stamp the broker-free ladder-replay outcome onto a decision (gen 4).

        Mirrors :meth:`stamp_outcome` exactly: a targeted ``UPDATE`` rather than
        an :meth:`insert` rebuild (so ``__post_init__`` does NOT re-run and a
        legacy row stays stampable), holding the lock only per-decision, and
        naturally idempotent. Every field uses the ``_UNSET`` sentinel so a
        two-pass write (e.g. the cheap substrate pass then the as-specified pass)
        leaves omitted columns untouched, and an explicit ``None`` (e.g. a
        BAD_GEOMETRY ``realized_r``) is written rather than silently dropped.
        ``horizon_open`` is stored as TEXT (``str(bool)``).
        """
        candidates: list[tuple[str, object]] = [
            ("sequence_str", sequence_str),
            ("mfe", mfe),
            ("mae", mae),
            ("forward_return", forward_return),
            ("ladder_classification", ladder_classification),
            ("blended_entry", blended_entry),
            ("realized_r", realized_r),
            ("horizon_open", horizon_open),
            ("ambiguous_bars", ambiguous_bars),
            ("ratchet_realized_r", ratchet_realized_r),
        ]
        set_clauses: list[str] = []
        params: list[object] = []
        for name, value in candidates:
            if value is not _UNSET:
                # Column names are module-fixed identifiers, not user input —
                # safe to inline; only the VALUES are parameterised.
                set_clauses.append(f"{name} = ?")
                params.append(value)
        if not set_clauses:
            return  # all-unset → no-op, never an empty UPDATE
        params.append(decision_id)
        self.conn.execute(
            # set_clauses are module-fixed identifiers, never user input; only
            # the VALUES are parameterised (? placeholders).
            f"UPDATE decisions SET {', '.join(set_clauses)} WHERE id = ?",  # nosec B608
            params,
        )

    def delete(self, decision_id: str) -> None:
        """Hard delete by id. No-op on unknown id (idempotent undo)."""
        self.conn.execute("DELETE FROM decisions WHERE id = ?", (decision_id,))

    # ---- reads --------------------------------------------------------

    def get(self, decision_id: str) -> Decision | None:
        row = self.conn.execute("SELECT * FROM decisions WHERE id = ?", (decision_id,)).fetchone()
        return _row_to_decision(row) if row else None

    def list_by_brief_date(self, brief_date: dt.date) -> list[Decision]:
        rows = self.conn.execute(
            "SELECT * FROM decisions WHERE brief_date = ? ORDER BY action_at",
            (brief_date.isoformat(),),
        ).fetchall()
        return [_row_to_decision(r) for r in rows]

    def list_by_ticker(self, ticker: str) -> list[Decision]:
        rows = self.conn.execute(
            "SELECT * FROM decisions WHERE ticker = ? ORDER BY brief_date",
            (ticker,),
        ).fetchall()
        return [_row_to_decision(r) for r in rows]

    def iter_matured_decisions(self) -> list[tuple[str, str, float | None, float | None]]:
        """Project ``(regime, fill_status, shadow_return, realized_return)`` for the
        execution-mode estimator (Track A v2 PR-4).

        Two contracts that matter for the ≥50 gate:

        * **Maturity = ``shadow_return IS NOT NULL``**, NOT ``outcome_computed_at``.
          The cheap fill-status join (``join_decision_outcomes``) stamps
          ``outcome_computed_at`` WITHOUT pricing ``shadow_return`` (the
          rate-limited shadow pass does that later). Using ``outcome_computed_at``
          as the maturity signal would count un-priced rows toward the pooled
          gate and could flip it active below the true priced sample. The shadow
          pass is the real maturity event.
        * **De-duplicated to one row per ``(brief_date, ticker)``.** The ticker-day
          outcome join stamps the SAME plan outcome onto every same-day
          multi-theme decision, so the rows are identical on
          ``(regime, fill_status, shadow_return, realized_return)``; counting
          decision rows would double-count one independent economic outcome and
          corrupt n / fill_rate / MO / g at the n≈50 scale where it matters.

        NULL ``market_regime_at_entry`` is coalesced to ``"unknown"`` so the
        estimator's non-actionable-regime rule fires.
        """
        rows = self.conn.execute(
            "SELECT brief_date, ticker, "
            "COALESCE(market_regime_at_entry, 'unknown') AS regime, "
            "fill_status, shadow_return, realized_return "
            "FROM decisions WHERE shadow_return IS NOT NULL "
            "ORDER BY brief_date, ticker"
        ).fetchall()
        seen: set[tuple[str, str]] = set()
        out: list[tuple[str, str, float | None, float | None]] = []
        for r in rows:
            key = (r["brief_date"], r["ticker"].upper())
            if key in seen:
                continue
            seen.add(key)
            out.append((r["regime"], r["fill_status"], r["shadow_return"], r["realized_return"]))
        return out

    def iter_decisions_for_ladder(self, *, lookback_start: dt.date) -> list[tuple[str, str, str]]:
        """Project ``(id, brief_date, ticker)`` for rows awaiting a ladder replay.

        Selects decisions whose ``brief_date >= lookback_start`` AND whose
        ``ladder_classification`` is still NULL (not yet replayed), ordered
        newest-first so the freshest just-matured dates are stamped before an
        older already-replayed tail (consistent with the shadow-return sweep's
        newest→oldest ordering). The caller groups by ``(brief_date, ticker)``,
        replays once per group, and stamps every member id.

        Click-orthogonality: this projects ONLY id / brief_date / ticker — NEVER
        a click column (action / dismiss_* / confidence_subjective). The ladder
        replay is a job-set outcome path that must stay orthogonal to the human
        click signal (v3 feedback-ledger orthogonality discipline).
        """
        rows = self.conn.execute(
            "SELECT id, brief_date, ticker FROM decisions "
            "WHERE brief_date >= ? AND ladder_classification IS NULL "
            "ORDER BY brief_date DESC",
            (lookback_start.isoformat(),),
        ).fetchall()
        return [(r["id"], r["brief_date"], r["ticker"]) for r in rows]


def _row_to_decision(row: sqlite3.Row) -> Decision:
    """Reconstruct a Decision from a SELECT row, parsing ISO timestamps.

    Uses ``Decision._from_row`` so that tightening of validation rules
    in a future v2 doesn't retroactively break READ of legacy rows.
    See ``Decision`` class docstring "Read-time validation contract".
    """
    return Decision._from_row(
        id=row["id"],
        brief_date=dt.date.fromisoformat(row["brief_date"]),
        ticker=row["ticker"],
        theme=row["theme"],
        surfaced_at=dt.datetime.fromisoformat(row["surfaced_at"]),
        action=row["action"],
        action_at=dt.datetime.fromisoformat(row["action_at"]),
        dismiss_category=row["dismiss_category"],
        dismiss_reason=row["dismiss_reason"],
        dismiss_note=row["dismiss_note"],
        confidence_subjective=row["confidence_subjective"],
        paper_trade_plan_id=row["paper_trade_plan_id"],
        position_size_usd=row["position_size_usd"],
        entry_price=row["entry_price"],
        market_regime_at_entry=row["market_regime_at_entry"],
        layer4_score=row["layer4_score"],
        rank_in_day=row["rank_in_day"],
        cohort_size_in_day=row["cohort_size_in_day"],
        gate_verdict_json=row["gate_verdict_json"],
        brief_model_used=row["brief_model_used"],
        outcome_plan_id=row["outcome_plan_id"],
        fill_status=row["fill_status"],
        exit_kind=row["exit_kind"],
        shadow_return=row["shadow_return"],
        realized_return=row["realized_return"],
        outcome_computed_at=(
            dt.datetime.fromisoformat(row["outcome_computed_at"])
            if row["outcome_computed_at"]
            else None
        ),
    )
