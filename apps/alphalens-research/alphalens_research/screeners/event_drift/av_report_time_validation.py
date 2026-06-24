"""§3.1 AV ``reportTime`` spot-check — paradigm-14 PEAD v2 launch gate #3.

Memo §17.5 gate #3 (the **spot-check** form, not the force-all-post-market
alternative which would require a v3 — see §18.4). The gate validates that the
Alpha Vantage ``reportTime`` the engine consumes is reliable on the five §3.1
anchor events, so the §5 entry-timing rule (pre-market → enter close(E[i]);
post-market → enter close(E[i]+1)) never enters a position using
not-yet-public information.

It validates the COERCED value (``av_earnings_ingestion._coerce_report_time``
defaults a missing/unknown field to ``post-market``) because that is what the
backtest actually uses. Two mismatch classes are distinguished:

  * BENIGN — observed ``post-market`` where reality is ``pre-market``. AV
    lacking the field defaults conservative; the engine simply enters one
    trading day later (lost drift capture, NO lookahead).
  * DANGEROUS — observed ``pre-market`` where reality is ``post-market``. The
    engine would enter on the announcement day's close using information not
    public until after the close. This is the only unsafe direction.

Acceptance (≥ 4 of 5 agree AND zero dangerous mismatches) is a reported,
diagnostic-only gate; it carries no v3 and no Bonferroni increment.

Anchor ground truth was cross-checked against contemporaneous public sources
(multi-source web research, 2026-06-24). It is operator-verifiable — each
anchor carries a one-line ``source`` note — and must be re-confirmed if the
anchor set ever changes.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date

from alphalens_research.screeners.event_drift.av_earnings_ingestion import (
    AVEarningsAnnouncement,
    ReportTime,
)

# Memo §3.1 acceptance: at least this many anchors must agree.
_ACCEPTANCE_MIN_AGREE = 4


@dataclass(frozen=True)
class ReportTimeAnchor:
    """One §3.1 anchor with its operator-curated ground-truth ``reportTime``."""

    ticker: str
    reported_date: date
    expected: ReportTime
    source: str


# The five §3.1 anchors. ``expected`` cross-checked 2026-06-24 against
# contemporaneous press-release / earnings-calendar sources. RSG's after-close
# timing is inferred from its recurring practice (no explicit 2018 notation
# survives), hence the qualified note.
REPORT_TIME_ANCHORS: tuple[ReportTimeAnchor, ...] = (
    ReportTimeAnchor(
        "AAPL",
        date(2018, 2, 1),
        "post-market",
        "Apple FY2018-Q1 results released after the US market close (press release ~4:30pm ET).",
    ),
    ReportTimeAnchor(
        "JPM",
        date(2018, 1, 12),
        "pre-market",
        "JPMorgan Q4-2017 results released before the US market open (~6:45am ET).",
    ),
    ReportTimeAnchor(
        "UNH",
        date(2018, 1, 16),
        "pre-market",
        "UnitedHealth Group Q4-2017 results released before the US market open (~5:55am ET).",
    ),
    ReportTimeAnchor(
        "CAT",
        date(2018, 1, 25),
        "pre-market",
        "Caterpillar Q4-2017 results released before the US market open (~6:30am ET).",
    ),
    ReportTimeAnchor(
        "RSG",
        date(2018, 2, 8),
        "post-market",
        "Republic Services Q4-2017 after the close (inferred from recurring after-close practice).",
    ),
)


@dataclass(frozen=True)
class AnchorVerdict:
    """Per-anchor comparison of the coerced AV ``report_time`` vs ground truth."""

    ticker: str
    reported_date: date
    expected: ReportTime
    observed: ReportTime | None  # None → the anchor event was absent from the cache
    agrees: bool
    dangerous: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "ticker": self.ticker,
            "reported_date": self.reported_date.isoformat(),
            "expected": self.expected,
            "observed": self.observed,
            "agrees": self.agrees,
            "dangerous": self.dangerous,
        }


@dataclass(frozen=True)
class ReportTimeValidationResult:
    """Aggregate §3.1 reportTime spot-check verdict."""

    verdicts: tuple[AnchorVerdict, ...]
    n_agree: int
    n_dangerous: int
    n_total: int
    passed: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "gate": "av_report_time_pit_spotcheck",
            "memo_ref": "paradigm14_pead_v2_design_2026_05_13.md §3.1 / §17.5 gate #3",
            "passed": self.passed,
            "n_agree": self.n_agree,
            "n_dangerous": self.n_dangerous,
            "n_total": self.n_total,
            "acceptance_min_agree": _ACCEPTANCE_MIN_AGREE,
            "verdicts": [v.to_dict() for v in self.verdicts],
        }


def _find_event(
    events: Sequence[AVEarningsAnnouncement], reported_date: date
) -> AVEarningsAnnouncement | None:
    return next((e for e in events if e.reported_date == reported_date), None)


def evaluate_report_time_anchors(
    loaded: Mapping[str, Sequence[AVEarningsAnnouncement]],
    anchors: Sequence[ReportTimeAnchor] = REPORT_TIME_ANCHORS,
) -> ReportTimeValidationResult:
    """Compare the coerced AV ``report_time`` for each anchor against its
    ground truth.

    ``loaded`` maps ticker → that ticker's loaded AV events (as produced by
    ``av_earnings_ingestion.load_av_earnings``). An anchor whose event is
    absent yields ``observed=None`` (a benign coverage gap, not a dangerous
    mismatch).

    PASS iff at least ``_ACCEPTANCE_MIN_AGREE`` anchors agree AND zero anchors
    are flagged dangerous (observed ``pre-market`` where reality is
    ``post-market`` — the only lookahead direction).
    """
    verdicts: list[AnchorVerdict] = []
    for anchor in anchors:
        event = _find_event(loaded.get(anchor.ticker, ()), anchor.reported_date)
        observed: ReportTime | None = event.report_time if event is not None else None
        agrees = observed == anchor.expected
        dangerous = observed == "pre-market" and anchor.expected == "post-market"
        verdicts.append(
            AnchorVerdict(
                ticker=anchor.ticker,
                reported_date=anchor.reported_date,
                expected=anchor.expected,
                observed=observed,
                agrees=agrees,
                dangerous=dangerous,
            )
        )

    n_agree = sum(1 for v in verdicts if v.agrees)
    n_dangerous = sum(1 for v in verdicts if v.dangerous)
    passed = n_agree >= _ACCEPTANCE_MIN_AGREE and n_dangerous == 0
    return ReportTimeValidationResult(
        verdicts=tuple(verdicts),
        n_agree=n_agree,
        n_dangerous=n_dangerous,
        n_total=len(verdicts),
        passed=passed,
    )
