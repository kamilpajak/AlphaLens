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


def _fmt_num(value: float | None, *, decimals: int = 1) -> str:
    """Render a number with fixed decimals, or ``-`` for ``None``."""
    if value is None:
        return "-"
    return f"{value:.{decimals}f}"


def _format_table(panels: list) -> str:  # list[BuffettPanel]
    """Build an aligned monospaced comparison table from the panels.

    Pure string assembly so the CLI body stays thin (and a future test could
    pin the rendering). Column widths size to the widest cell.
    """
    rows: list[tuple[str, ...]] = [_COLUMNS]
    for p in panels:
        rows.append(
            (
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
        )
    widths = [max(len(row[i]) for row in rows) for i in range(len(_COLUMNS))]
    lines = []
    for r_idx, row in enumerate(rows):
        cells = [cell.ljust(widths[i]) for i, cell in enumerate(row)]
        lines.append("  ".join(cells).rstrip())
        if r_idx == 0:
            lines.append("  ".join("-" * widths[i] for i in range(len(_COLUMNS))))
    return "\n".join(lines)


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
) -> None:
    """Score a brief date's candidates on the Buffett quantitative delta.

    Loads the brief, computes one :class:`BuffettPanel` per candidate (owner-
    earnings yield, DCF margin of safety, ROIC + op-margin trend, net-buyback,
    dividend yield), prints an aligned table, and writes a parquet when ``--out``
    is given. Many small / recent thematic names resolve few fields — the ``COV``
    column reports that coverage honestly rather than fabricating numbers.
    """
    try:
        target = dt.date.fromisoformat(brief_date)
    except ValueError as exc:
        raise typer.BadParameter(f"DATE must be YYYY-MM-DD: {exc}") from exc

    # Lazy imports — keep the CLI startup cheap for the frequent cron paths.
    from alphalens_pipeline.buffett.comparison import build_comparison
    from alphalens_pipeline.data.alt_data.yfinance_client import get_default_yfinance_client
    from alphalens_pipeline.data.store.edgar_fundamentals import EdgarFundamentalsStore
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
        )
    except FileNotFoundError as exc:
        raise typer.BadParameter(str(exc)) from exc

    if not panels:
        typer.echo(f"No candidates in brief for {target.isoformat()}.")
        return

    typer.echo(f"Buffett lens (Mode A) — {target.isoformat()} — {len(panels)} candidates")
    typer.echo(_format_table(panels))

    if out is not None:
        from dataclasses import asdict

        import pandas as pd

        df = pd.DataFrame([asdict(p) for p in panels])
        out.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(out)
        typer.echo(f"Wrote {len(df)} rows → {out}")


__all__ = ["buffett_app", "lens_command"]
