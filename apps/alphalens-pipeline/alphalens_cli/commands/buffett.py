"""CLI: ``alphalens buffett`` — the Mode-A observational Buffett lens (#511).

Ships a single ``lens`` command: for a thematic brief date it scores the brief's
candidate tickers on the Buffett quantitative DELTA (owner-earnings yield, DCF
margin of safety, multi-year ROIC / operating-margin trend, net-buyback proxy,
dividend yield — the metrics the brief does NOT already carry) and prints an
aligned comparison table, optionally writing a parquet via ``--out``.

It is **additive and unwired**: nothing in the daily thematic-build pipeline,
systemd, Django, or the SPA runs it. The operator runs it ad hoc.

The heavy fundamentals store + assembler are imported lazily inside the command
body, matching the lazy-CLI-import convention (the Layer-1 ``edgar-detect`` cron
must not pay pandas / store import cost it never uses).
"""

from __future__ import annotations

import datetime as dt
import logging
import textwrap
from pathlib import Path

import typer

logger = logging.getLogger(__name__)

buffett_app = typer.Typer(
    name="buffett",
    help="Buffett quantitative lens (Mode A: observational comparison over the brief).",
    no_args_is_help=True,
)

_ALPHALENS_HOME = Path.home() / ".alphalens"
_DEFAULT_BRIEFS_DIR = _ALPHALENS_HOME / "thematic_briefs"

# Column headers for the printed comparison table. Order matches the row tuple
# built in ``_format_table``.
_COLUMNS = (
    "TICKER",
    "THEME",
    "OE-YLD%",
    "ROIC%",
    "MOS%",
    "OPMGN%",
    "BUYBK%",
    "DIV%",
    "COV",
)

# Extra columns appended (in this order) when ``--qualitative`` is set.
_QUALITATIVE_COLUMNS = ("MOAT", "TREND", "CANDOR", "UNDERSTOOD")


def _fmt_num(value: float | None, *, decimals: int = 1) -> str:
    """Render a number with fixed decimals, or ``-`` for ``None``."""
    if value is None:
        return "-"
    return f"{value:.{decimals}f}"


def _fmt_str(value: str | None) -> str:
    """Render an optional string cell, or ``-`` for ``None`` (the missing case)."""
    return value if value else "-"


def _fmt_bool(value: bool | None) -> str:
    """Render an optional bool as yes / no, or ``-`` for ``None``."""
    if value is None:
        return "-"
    return "yes" if value else "no"


def _qualitative_cells(assessment) -> tuple[str, ...]:  # QualitativeAssessment | None
    """The four qualitative cells (MOAT / TREND / CANDOR / UNDERSTOOD) for a row.

    ``None`` (no assessment ran for this ticker) renders as four dash cells.
    """
    if assessment is None:
        return ("-", "-", "-", "-")
    return (
        _fmt_str(assessment.moat_type),
        _fmt_str(assessment.moat_trend),
        _fmt_str(assessment.management_candor),
        _fmt_bool(assessment.understandable),
    )


def _format_table(panels: list, assessments: list | None = None) -> str:
    """Build an aligned monospaced comparison table from the panels.

    Pure string assembly so the CLI body stays thin (and a future test could
    pin the rendering). Column widths size to the widest cell. When
    ``assessments`` is given (one per panel, same order, ``None`` allowed for a
    panel that had no 10-K), the four qualitative columns are appended.
    """
    qualitative = assessments is not None
    header = _COLUMNS + (_QUALITATIVE_COLUMNS if qualitative else ())
    rows: list[tuple[str, ...]] = [header]
    for idx, p in enumerate(panels):
        base = (
            p.ticker,
            (p.theme or "")[:28],
            _fmt_num(p.owner_earnings_yield_pct),
            _fmt_num(p.roic_latest),
            _fmt_num(p.margin_of_safety_pct),
            _fmt_num(p.op_margin_latest),
            _fmt_num(p.buyback_pct),
            _fmt_num(p.dividend_yield_pct),
            _fmt_num(p.data_coverage, decimals=2),
        )
        if qualitative:
            assessment = assessments[idx] if idx < len(assessments) else None
            base = base + _qualitative_cells(assessment)
        rows.append(base)
    n_cols = len(header)
    widths = [max(len(row[i]) for row in rows) for i in range(n_cols)]
    lines = []
    for r_idx, row in enumerate(rows):
        cells = [cell.ljust(widths[i]) for i, cell in enumerate(row)]
        lines.append("  ".join(cells).rstrip())
        if r_idx == 0:
            lines.append("  ".join("-" * widths[i] for i in range(n_cols)))
    return "\n".join(lines)


_RATIONALE_WRAP_WIDTH = 96


def _format_rationale_block(panels: list, assessments: list) -> str | None:
    """A 'Why' block of the per-candidate qualitative rationale below the table.

    The rationale (the LLM's "why" behind the moat / candor / understandability
    classification) is too long for a table cell, so it is rendered here as a
    wrapped paragraph per ticker. Candidates whose 10-K could not be assessed
    (``None`` assessment or empty rationale) are skipped. Returns ``None`` when
    no candidate has a rationale, so the caller can omit the block entirely.
    """
    entries: list[str] = []
    for idx, panel in enumerate(panels):
        assessment = assessments[idx] if idx < len(assessments) else None
        rationale = getattr(assessment, "rationale", None)
        if not isinstance(rationale, str) or not rationale.strip():
            continue
        wrapped = textwrap.fill(
            rationale.strip(),
            width=_RATIONALE_WRAP_WIDTH,
            initial_indent="  ",
            subsequent_indent="     ",
        )
        entries.append(f"  {panel.ticker}:\n{wrapped}")
    if not entries:
        return None
    return "Why (qualitative rationale):\n" + "\n".join(entries)


@buffett_app.command(name="lens")
def lens_command(
    brief_date: str = typer.Argument(
        ..., metavar="DATE", help="Brief date (YYYY-MM-DD) to score the candidates of."
    ),
    briefs_dir: Path = typer.Option(
        _DEFAULT_BRIEFS_DIR,
        "--briefs-dir",
        help="Directory of daily thematic brief parquets.",
    ),
    out: Path | None = typer.Option(
        None,
        "--out",
        help="Optional parquet path to write the full comparison table to.",
    ),
    qualitative: bool = typer.Option(
        False,
        "--qualitative",
        help=(
            "Opt-in: run DeepSeek Pro over each candidate's 10-K text + the "
            "pre-computed numeric facts to classify business understandability, "
            "moat type + trend, and management candor (adds MOAT/TREND/CANDOR/"
            "UNDERSTOOD columns). One LLM call per candidate (~$0.05-0.10 each); "
            "off by default so the lens costs nothing."
        ),
    ),
    scuttlebutt: bool = typer.Option(
        False,
        "--scuttlebutt",
        help=(
            "Opt-in add-on to --qualitative: fetch web-grounded scuttlebutt "
            "(competitive position, customer/supplier concentration, management "
            "reputation) via Perplexity per candidate and feed it to the classifier "
            "as UNVERIFIED context (~$0.02-0.05 each). Needs PERPLEXITY_API_KEY; "
            "requires --qualitative; qual-only (adds no numbers)."
        ),
    ),
) -> None:
    """Score a brief date's candidates on the Buffett quantitative delta.

    Loads the brief, computes one :class:`BuffettPanel` per candidate (owner-
    earnings yield, DCF margin of safety, ROIC + op-margin trend, net-buyback,
    dividend yield), prints an aligned table, and writes a parquet when ``--out``
    is given. Many small / recent thematic names resolve few fields — the ``COV``
    column reports that coverage honestly rather than fabricating numbers.

    With ``--qualitative`` each candidate's latest 10-K is fetched and split into
    its Business / Risk-Factors / MD&A sections, the already-computed numeric
    facts are injected, and DeepSeek Pro classifies understandability, moat, and
    candor — the LLM emits NO numbers (doctrine: numbers are computed in Python
    and injected). It is fail-soft: a name with no fetchable 10-K shows dashes.
    """
    try:
        target = dt.date.fromisoformat(brief_date)
    except ValueError as exc:
        raise typer.BadParameter(f"DATE must be YYYY-MM-DD: {exc}") from exc

    # Lazy imports — keep the CLI startup cheap for the frequent cron paths.
    from alphalens_pipeline.data.alt_data.yfinance_client import get_default_yfinance_client
    from alphalens_pipeline.data.store.edgar_fundamentals import EdgarFundamentalsStore
    from alphalens_pipeline.experts.buffett.comparison import build_comparison
    from alphalens_pipeline.thematic.verification.mcap_filter import fetch_mcap

    store = EdgarFundamentalsStore(with_prices=True)
    dividends_fn = get_default_yfinance_client().dividends

    try:
        panels = build_comparison(
            target,
            briefs_dir=briefs_dir,
            store=store,
            mcap_fn=fetch_mcap,
            dividends_fn=dividends_fn,
            exec_comp_fn=_build_exec_comp_fn(),
        )
    except FileNotFoundError as exc:
        raise typer.BadParameter(str(exc)) from exc

    if not panels:
        typer.echo(f"No candidates in brief for {target.isoformat()}.")
        return

    if scuttlebutt and not qualitative:
        typer.echo("Note: --scuttlebutt requires --qualitative; ignoring --scuttlebutt.")

    assessments = (
        _run_qualitative(panels, asof=target, scuttlebutt=scuttlebutt) if qualitative else None
    )

    typer.echo(f"Buffett lens (Mode A) — {target.isoformat()} — {len(panels)} candidates")
    typer.echo(_format_table(panels, assessments))

    if assessments is not None:
        rationale_block = _format_rationale_block(panels, assessments)
        if rationale_block is not None:
            typer.echo("")
            typer.echo(rationale_block)

    if out is not None:
        from dataclasses import asdict

        import pandas as pd

        records = [asdict(p) for p in panels]
        if assessments is not None:
            for record, assessment in zip(records, assessments, strict=True):
                record.update(_assessment_record(assessment))
        df = pd.DataFrame(records)
        out.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(out)
        typer.echo(f"Wrote {len(df)} rows → {out}")


def _assessment_record(assessment) -> dict:  # QualitativeAssessment | None
    """The qualitative fields as a flat dict for the parquet row (``None``-safe)."""
    return {
        "moat_type": assessment.moat_type if assessment is not None else None,
        "moat_trend": assessment.moat_trend if assessment is not None else None,
        "management_candor": assessment.management_candor if assessment is not None else None,
        "understandable": assessment.understandable if assessment is not None else None,
        "qualitative_rationale": assessment.rationale if assessment is not None else None,
    }


def _build_exec_comp_fn():
    """Build the (ticker, asof) -> ExecCompFacts callable for the panel (#507 PR-7b).

    Resolves ticker -> CIK (reusing the 10-K resolver) then reads DEF 14A
    pay-vs-performance from SEC XBRL frames via the canonical client. Returns a
    NOT_DISCLOSED facts object when the CIK can't be resolved — never raises (the
    panel's own ``_safe`` wrapper is the second net). Kept lazy so the cron path
    never imports the SEC client.
    """
    from alphalens_pipeline.data.alt_data.sec_edgar_client import get_default_sec_client
    from alphalens_pipeline.experts.buffett.exec_comp import (
        ExecCompCoverage,
        ExecCompFacts,
        exec_comp_as_of,
    )
    from alphalens_pipeline.thematic.verification.tenk_grep import _resolve_cik

    client = get_default_sec_client()

    def _fn(ticker: str, asof: dt.date) -> ExecCompFacts:
        cik = _resolve_cik(ticker)
        if cik is None:
            return ExecCompFacts(cik="", coverage=ExecCompCoverage.NOT_DISCLOSED)
        return exec_comp_as_of(cik, asof, client=client)

    return _fn


def _build_scuttlebutt_client():
    """Build a PerplexityClient from PERPLEXITY_API_KEY, or ``None`` (fail-soft).

    Missing / empty key → ``None`` + one warning, so ``--scuttlebutt`` degrades to
    "no scuttlebutt" rather than crashing the lens. Kept here (lazy) so the
    non-scuttlebutt path never imports the client.
    """
    import os

    from alphalens_pipeline.literature_scanner.perplexity_client import PerplexityClient

    api_key = os.getenv("PERPLEXITY_API_KEY", "").strip()
    if not api_key:
        logger.warning("buffett --scuttlebutt: PERPLEXITY_API_KEY not set — skipping scuttlebutt")
        return None
    return PerplexityClient(api_key=api_key)


def _run_qualitative(panels: list, *, asof: dt.date, scuttlebutt: bool = False) -> list:
    """Run the per-candidate qualitative LLM layer, one assessment per panel.

    Thin wrapper over the shared per-panel op
    :func:`~alphalens_pipeline.experts.buffett.qual_enrichment.assess_panel_qualitative`
    (the eager pipeline pass uses the same op). The scuttlebutt client is built
    ONCE and reused across panels. Every step is fail-soft — a fetch failure or a
    name with no 10-K yields ``None`` for that row (rendered as dashes). No result
    cache here: the ad-hoc lens always recomputes (caching is the eager
    ``qual-enrich`` pass's job).
    """
    from alphalens_pipeline.experts.buffett.qual_enrichment import assess_panel_qualitative

    client = _build_scuttlebutt_client() if scuttlebutt else None
    return [assess_panel_qualitative(panel, asof, scuttlebutt_client=client) for panel in panels]


__all__ = ["buffett_app", "lens_command"]
