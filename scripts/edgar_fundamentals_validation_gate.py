"""Operator-triggered EDGAR fundamentals validation gate.

Implements the doctrine in ``CLAUDE.md > Research methodology > Data-vendor
PIT validation gate``: ≥5 sector-diverse anchors × 2-source triangulation
× ≤±1% delta (≤±5% on TTM fields) before SimFin can be deleted as the
prior fundamentals vendor.

Source 1: :class:`EdgarFundamentalsStore` (this project, SEC XBRL via
companyfacts JSON).
Source 2: yfinance (Yahoo's independent parser of the same SEC XBRL feed).

Pass = every non-exempt field in every anchor inside its tolerance band.
Any single excursion → HALT, escalate to operator inspection.

Usage::

    SEC_EDGAR_USER_AGENT="YourName you@example.com" \\
    python scripts/edgar_fundamentals_validation_gate.py \\
        --anchors MANH,SYM,JPM,CAT,UNH \\
        --asof 2026-05-20 \\
        --out docs/research/edgar_fundamentals_validation_2026_05_19.md

Exit code 0 on PASS, 1 on FAIL.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger("edgar_fundamentals_validation_gate")

DEFAULT_ANCHORS = ("MANH", "SYM", "JPM", "CAT", "UNH")
DEFAULT_OUT = Path("docs/research/edgar_fundamentals_validation_2026_05_19.md")

# Per-field tolerance bands. Instant balance-sheet fields share an XBRL tag
# across vendors and should match within 1% (with a small absolute dollar
# floor for low-priced quotes). TTM fields tolerate fiscal-calendar drift up
# to 5%. Exempt fields are documented in the memo but don't bind the gate.
_INSTANT_FIELDS = frozenset(
    {
        "long_term_debt",
        "short_term_debt",
        "cash_and_equivalents",
        "total_equity",
        "shares_outstanding",
        "price",
    }
)
_TTM_FIELDS = frozenset(
    {
        "ocf_ttm",
        "capex_ttm",
        "interest_expense_ttm",
        "revenue_ttm",
        "net_income_ttm",
        "operating_income_ttm",
        "da_ttm",
    }
)
_EXEMPT_FIELDS = frozenset(
    {
        "tax_rate",  # EDGAR clamps to [0, 0.35]; yfinance reports raw — divergence by design
        "fcf_margin_5y_median",  # EDGAR returns None (known TODO); yfinance lacks the metric
        "publish_date_str",  # informational, not numeric
    }
)

_INSTANT_TOLERANCE_PCT = 1.0
_TTM_TOLERANCE_PCT = 5.0
_DEFAULT_TOLERANCE_PCT = 5.0  # unknown future fields default to TTM band
_DOLLAR_FLOOR = 0.02  # absolute floor protecting penny-stock prices


@dataclass(frozen=True)
class FieldDiff:
    """Per-field diff record. Markdown-emitted into the memo."""

    field: str
    edgar_value: Any
    yf_value: Any
    abs_delta: float
    pct_delta: float
    tolerance_pct: float
    within_tolerance: bool
    exempt: bool
    note: str


def _tolerance_pct_for(field: str) -> float:
    if field in _INSTANT_FIELDS:
        return _INSTANT_TOLERANCE_PCT
    if field in _TTM_FIELDS:
        return _TTM_TOLERANCE_PCT
    return _DEFAULT_TOLERANCE_PCT


def compare_field(field: str, edgar_value: Any, yf_value: Any) -> FieldDiff:
    """Compare one field's EDGAR-vs-yfinance values against its tolerance band."""
    if field in _EXEMPT_FIELDS:
        note = (
            "exempt: clamped to [0, 0.35] in EDGAR"
            if field == "tax_rate"
            else "exempt: known TODO in EDGAR (rolling median pending)"
            if field == "fcf_margin_5y_median"
            else "exempt: informational only"
        )
        return FieldDiff(field, edgar_value, yf_value, 0.0, 0.0, 0.0, True, True, note)

    tolerance_pct = _tolerance_pct_for(field)

    if edgar_value is None and yf_value is None:
        return FieldDiff(field, None, None, 0.0, 0.0, tolerance_pct, True, False, "both None")

    if edgar_value is None or yf_value is None:
        return FieldDiff(
            field,
            edgar_value,
            yf_value,
            math.inf,
            math.inf,
            tolerance_pct,
            False,
            False,
            "one-sided None — surface for inspection",
        )

    # Numeric comparison from here.
    try:
        edgar_num = float(edgar_value)
        yf_num = float(yf_value)
    except (TypeError, ValueError):
        return FieldDiff(
            field,
            edgar_value,
            yf_value,
            math.inf,
            math.inf,
            tolerance_pct,
            False,
            False,
            "non-numeric — cannot compare",
        )

    abs_delta = abs(edgar_num - yf_num)
    denom = max(abs(edgar_num), abs(yf_num), 1e-12)
    pct_delta = (abs_delta / denom) * 100.0

    within_pct = pct_delta <= tolerance_pct
    within_floor = field == "price" and abs_delta <= _DOLLAR_FLOOR
    within = within_pct or within_floor

    note = ""
    if within_floor and not within_pct:
        note = f"within ${_DOLLAR_FLOOR:.2f} floor (price)"

    return FieldDiff(
        field,
        edgar_value,
        yf_value,
        abs_delta,
        pct_delta,
        tolerance_pct,
        within,
        False,
        note,
    )


def compare(edgar_features: dict[str, Any], yf_features: dict[str, Any]) -> list[FieldDiff]:
    """Compare every field present in either dict; returns ordered diff list."""
    fields = sorted(set(edgar_features) | set(yf_features))
    return [compare_field(f, edgar_features.get(f), yf_features.get(f)) for f in fields]


def anchor_passed(diffs: list[FieldDiff]) -> bool:
    """An anchor passes if every non-exempt field is within tolerance."""
    return all(d.within_tolerance for d in diffs)


def _fmt_value(v: Any) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            return "—"
        if abs(v) >= 1_000_000:
            return f"{v / 1_000_000:.2f}M"
        if abs(v) < 1.0:
            return f"{v:.4f}"
        return f"{v:.2f}"
    return str(v)


def format_memo(asof: dt.date, results: dict[str, list[FieldDiff]]) -> str:
    """Render the gate-evidence memo as markdown for committing into the repo."""
    overall_pass = all(anchor_passed(diffs) for diffs in results.values())
    verdict = "PASS" if overall_pass else "FAIL"

    lines: list[str] = []
    lines.append(f"# EDGAR fundamentals validation gate — {asof.isoformat()}")
    lines.append("")
    lines.append(f"**Gate verdict:** {verdict}")
    lines.append("")
    lines.append(
        "5-anchor × 2-source (EDGAR vs yfinance) PIT validation per "
        "`CLAUDE.md > Research methodology > Data-vendor PIT validation gate`. "
        "Tolerance bands: ≤±1% instant balance-sheet fields, ≤±5% TTM fields "
        f"(fiscal-calendar drift). Price honors ±${_DOLLAR_FLOOR:.2f} dollar floor. "
        "`tax_rate` exempt (EDGAR clamps [0, 0.35]); `fcf_margin_5y_median` "
        "exempt (EDGAR returns None — known TODO); `publish_date_str` exempt "
        "(informational)."
    )
    lines.append("")

    for ticker in sorted(results):
        diffs = results[ticker]
        anchor_verdict = "PASS" if anchor_passed(diffs) else "FAIL"
        lines.append(f"## {ticker} — {anchor_verdict}")
        lines.append("")
        lines.append("| field | EDGAR | yfinance | Δ% | tol% | status |")
        lines.append("|---|---|---|---|---|---|")
        for d in diffs:
            status = "exempt" if d.exempt else ("✓" if d.within_tolerance else "✗")
            pct_cell = "—" if d.exempt or not math.isfinite(d.pct_delta) else f"{d.pct_delta:.2f}"
            tol_cell = "—" if d.exempt else f"{d.tolerance_pct:.1f}"
            note_suffix = f" — {d.note}" if d.note else ""
            lines.append(
                f"| {d.field} | {_fmt_value(d.edgar_value)} | "
                f"{_fmt_value(d.yf_value)} | {pct_cell} | {tol_cell} | "
                f"{status}{note_suffix} |"
            )
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(
        "Operator action: any `✗` row triggers HALT — inspect the contemporaneous "
        "SEC 10-Q/10-K filing for the ticker, decide whether EDGAR or yfinance "
        "reflects the source truth, and either widen the tolerance band with a "
        "documented reason or fix the EDGAR concept-chain mapping in "
        "`alphalens/data/fundamentals/concept_chains.py`."
    )
    return "\n".join(lines) + "\n"


# --- Live-source IO (no unit tests; covered by the operator gate run) -------


def _build_yfinance_features(ticker: str) -> dict[str, Any]:
    """Build the same 16-field dict EDGAR returns, but from yfinance.

    yfinance is the independent second source for the triangulation. It
    parses the same SEC XBRL feed through Yahoo's pipeline so divergence
    surfaces real interpretation gaps rather than data-vendor disagreement.
    """
    import yfinance as yf

    yt = yf.Ticker(ticker)

    income = yt.quarterly_income_stmt
    cash = yt.quarterly_cashflow
    balance = yt.quarterly_balance_sheet

    def _row_ttm(frame: Any, *candidates: str) -> float | None:
        if frame is None or getattr(frame, "empty", True):
            return None
        for name in candidates:
            if name in frame.index:
                row = frame.loc[name].dropna().iloc[:4]
                if len(row) >= 1:
                    return float(row.sum())
        return None

    def _row_latest(frame: Any, *candidates: str) -> float | None:
        if frame is None or getattr(frame, "empty", True):
            return None
        for name in candidates:
            if name in frame.index:
                row = frame.loc[name].dropna()
                if len(row) >= 1:
                    return float(row.iloc[0])
        return None

    fast = yt.fast_info
    price = getattr(fast, "last_price", None)
    shares = getattr(fast, "shares", None)

    return {
        "ocf_ttm": _row_ttm(cash, "Operating Cash Flow", "Total Cash From Operating Activities"),
        "capex_ttm": _capex_positive(_row_ttm(cash, "Capital Expenditure", "Capital Expenditures")),
        "interest_expense_ttm": _row_ttm(income, "Interest Expense"),
        "tax_rate": None,  # exempt from gate; not built
        "revenue_ttm": _row_ttm(income, "Total Revenue", "Operating Revenue"),
        "fcf_margin_5y_median": None,  # exempt from gate; not built
        "price": float(price) if price is not None else None,
        "shares_outstanding": float(shares) if shares is not None else None,
        "long_term_debt": _row_latest(balance, "Long Term Debt"),
        "short_term_debt": _row_latest(balance, "Current Debt", "Short Term Debt"),
        "cash_and_equivalents": _row_latest(
            balance, "Cash And Cash Equivalents", "Cash Cash Equivalents And Short Term Investments"
        ),
        "net_income_ttm": _row_ttm(income, "Net Income", "Net Income Common Stockholders"),
        "publish_date_str": None,  # exempt
        "operating_income_ttm": _row_ttm(income, "Operating Income", "EBIT"),
        "total_equity": _row_latest(
            balance, "Stockholders Equity", "Total Equity Gross Minority Interest"
        ),
        "da_ttm": _row_ttm(
            cash, "Depreciation And Amortization", "Depreciation Amortization Depletion"
        ),
    }


def _capex_positive(value: float | None) -> float | None:
    """yfinance reports capex as negative cash outflow; EDGAR stores it positive.
    Align to EDGAR's sign so the comparison is apples-to-apples."""
    if value is None:
        return None
    return abs(value)


def run_gate(anchors: list[str], asof: dt.date) -> dict[str, list[FieldDiff]]:
    """Live gate: fetch EDGAR + yfinance for each anchor, return diffs per anchor."""
    from alphalens.data.store.edgar_fundamentals import EdgarFundamentalsStore

    edgar_store = EdgarFundamentalsStore(with_prices=True)
    edgar_store.preload(list(anchors))

    results: dict[str, list[FieldDiff]] = {}
    for ticker in anchors:
        logger.info("validating %s @ %s", ticker, asof.isoformat())
        try:
            edgar_features = edgar_store.ev_fcff_features_as_of(ticker, asof) or {}
        except Exception as exc:
            logger.error("EDGAR fetch failed for %s: %s", ticker, exc)
            edgar_features = {}
        try:
            yf_features = _build_yfinance_features(ticker)
        except Exception as exc:
            logger.error("yfinance fetch failed for %s: %s", ticker, exc)
            yf_features = {}
        results[ticker] = compare(edgar_features, yf_features)
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--anchors",
        default=",".join(DEFAULT_ANCHORS),
        help=f"Comma-separated tickers (default: {','.join(DEFAULT_ANCHORS)})",
    )
    parser.add_argument(
        "--asof",
        default=dt.date.today().isoformat(),
        help="Asof date YYYY-MM-DD (default: today)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        help=f"Memo output path (default: {DEFAULT_OUT})",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    anchors = [t.strip().upper() for t in args.anchors.split(",") if t.strip()]
    asof = dt.date.fromisoformat(args.asof)

    results = run_gate(anchors, asof)
    memo = format_memo(asof, results)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(memo)
    logger.info("memo written to %s", args.out)

    overall_pass = all(anchor_passed(diffs) for diffs in results.values())
    print(f"GATE VERDICT: {'PASS' if overall_pass else 'FAIL'}")
    return 0 if overall_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
