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

logger = logging.getLogger(__name__)


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
        UNIQUE(brief_date, ticker, theme)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_decisions_brief_date ON decisions(brief_date)",
    "CREATE INDEX IF NOT EXISTS idx_decisions_ticker ON decisions(ticker)",
    "CREATE INDEX IF NOT EXISTS idx_decisions_action ON decisions(action)",
)


def _connect(path: Path) -> sqlite3.Connection:
    """Open a connection with WAL + foreign keys + immediate commit."""
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Apply DDL idempotently."""
    for ddl in _SCHEMA_DDL:
        conn.execute(ddl)


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
            "SELECT id FROM decisions WHERE brief_date = ? AND ticker = ? AND theme = ?",
            (decision.brief_date.isoformat(), decision.ticker, decision.theme),
        ).fetchone()
        was_created = existing is None
        target_id = existing["id"] if existing else decision.id
        self.conn.execute(
            """
            INSERT OR REPLACE INTO decisions (
                id, brief_date, ticker, theme, surfaced_at, action, action_at,
                dismiss_category, dismiss_reason, dismiss_note,
                confidence_subjective, paper_trade_plan_id,
                position_size_usd, entry_price, market_regime_at_entry
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            ),
        )
        return target_id, was_created

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
    )
