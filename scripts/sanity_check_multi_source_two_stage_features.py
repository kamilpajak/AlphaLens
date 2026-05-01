"""Phase A sanity-check driver for the multi_source_two_stage feature joiner.

Runs `build_feature_frame` on a small live data subset and emits a markdown
report covering: coverage waterfall, per-feature NaN rates, summary stats,
regime distribution, holdout extrapolation, and pairwise Spearman collinearity.

Outputs PASS / FIXES-REQUIRED / PHASE-B-INFEASIBLE verdict at top of the
report so the next step is unambiguous.

This is NOT an experiment: no scoring, no portfolio, no Carhart attribution.
Just feature plumbing inspection per `pit_audit_2026_04_30_findings.md` and
the approved Phase A plan.

Usage::

    .venv/bin/python scripts/sanity_check_multi_source_two_stage_features.py \\
        --n-tickers 50 --start 2020-01-01 --end 2020-04-01

Tunable knobs:
    --n-tickers     int   default 50
    --start         date  default 2020-01-01
    --end           date  default 2020-04-01
    --rebalance-stride int default 5
    --out           path  default docs/research/multi_source_two_stage_phase_a_2026_04_30.md
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from dotenv import load_dotenv

from alphalens.archive.screeners.insider.parquet_scorer import ParquetInsiderScorer
from alphalens.data.alt_data.yfinance_cache import load_cached_histories
from alphalens.data.factors import load_carhart_daily
from alphalens.data.macro.fred_client import FREDClient
from alphalens.data.store.history import HistoryStore
from alphalens.data.store.survivorship_pit import load_delisting_events
from alphalens.screeners.multi_source_two_stage import (
    FEATURE_NAMES,
    build_feature_frame,
)

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO_ROOT / "docs" / "research" / "multi_source_two_stage_phase_a_2026_04_30.md"
PRICES_DIR = Path.home() / ".alphalens" / "prices"
INSIDER_PARQUET = Path.home() / ".alphalens" / "insider_form4.parquet"
SURVIVORSHIP_PARQUET = Path.home() / ".alphalens" / "survivorship" / "delisted_2021_2026.parquet"
SURVIVORSHIP_YAML = (
    REPO_ROOT
    / "alphalens"
    / "archive"
    / "screeners"
    / "lean"
    / "lean_project"
    / "delisted_universe.yaml"
)

# Phase A decision-gate thresholds (per approved plan)
HOLDOUT_OBS_FLOOR = 5_000  # PHASE-B-INFEASIBLE if extrapolated < this
HOLDOUT_OBS_COMFORTABLE = 8_000
NAN_RATE_FAIL = 0.50  # any feature with NaN > this triggers FIXES-REQUIRED
NAN_RATE_WARN = 0.20
SOURCE_JOIN_FLOOR = 0.30  # any source <30% join rate after waterfall = INFEASIBLE
COLLINEARITY_THRESHOLD = 0.90
EXPECTED_COLLINEAR_PAIRS = frozenset(
    {
        frozenset({"ret_60d", "rank_momentum_60d"}),
        frozenset({"vol_realized_20d", "rank_lowvol_20d"}),
        frozenset({"dollar_volume_z_20d", "rank_dollar_volume_size"}),
    }
)


# ---------------------------------------------------------------------------
# Argument parsing


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--n-tickers", type=int, default=50)
    ap.add_argument("--start", type=date.fromisoformat, default=date(2020, 1, 1))
    ap.add_argument("--end", type=date.fromisoformat, default=date(2020, 4, 1))
    ap.add_argument("--rebalance-stride", type=int, default=5)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--seed", type=int, default=42, help="random ticker selection seed")
    return ap


# ---------------------------------------------------------------------------
# Data loading helpers


def _select_tickers(
    n_tickers: int, seed: int, available: Sequence[str], required: Sequence[str]
) -> list[str]:
    """Random subset of `n_tickers` from available; always include required (e.g. SPY)."""
    rng = np.random.default_rng(seed)
    pool = [t for t in available if t not in required]
    chosen = rng.choice(pool, size=min(n_tickers, len(pool)), replace=False).tolist()
    return sorted(set(required) | set(chosen))


def _list_available_tickers() -> list[str]:
    return sorted(p.stem.upper() for p in PRICES_DIR.glob("*.parquet"))


def _load_fred_series_dict() -> dict[str, pd.Series]:
    """Load VIXCLS, DGS10, DGS3MO. DGS3MO requires FRED_API_KEY if not cached."""
    client = FREDClient.from_env()
    out: dict[str, pd.Series] = {}
    for sid in ("VIXCLS", "DGS10", "DGS3MO"):
        out[sid] = client.fetch_series(sid)
    return out


def _build_calendar(
    start: date, end: date, store: HistoryStore, benchmark: str, stride: int
) -> list[date]:
    """Strided benchmark calendar, mirroring engine convention."""
    full = HistoryStore.benchmark_calendar(store, benchmark, start, end)
    if not full:
        return []
    sliced = full[::stride]
    return [ts.date() for ts in sliced]


# ---------------------------------------------------------------------------
# Report generation


def _coverage_waterfall(
    universe_size: int, n_asof: int, frame: pd.DataFrame
) -> list[tuple[str, int, str]]:
    """Track cell counts down the join. Each tuple = (label, cells, note)."""
    starting_cells = universe_size * n_asof
    rows = [("Starting (universe × asof)", starting_cells, "")]
    rows.append(
        (
            "After per-ticker compute (truncate_to + insider lookup)",
            len(frame),
            f"{100 * len(frame) / starting_cells:.1f}% of starting" if starting_cells else "n/a",
        )
    )
    return rows


def _per_feature_stats(frame: pd.DataFrame) -> pd.DataFrame:
    stats_rows = []
    for col in FEATURE_NAMES:
        s = frame[col]
        nan_rate = s.isna().mean()
        present = s.dropna()
        stats_rows.append(
            {
                "feature": col,
                "n_present": int(present.shape[0]),
                "nan_rate": float(nan_rate),
                "mean": float(present.mean()) if len(present) else float("nan"),
                "std": float(present.std()) if len(present) > 1 else float("nan"),
                "min": float(present.min()) if len(present) else float("nan"),
                "p25": float(present.quantile(0.25)) if len(present) else float("nan"),
                "p50": float(present.quantile(0.50)) if len(present) else float("nan"),
                "p75": float(present.quantile(0.75)) if len(present) else float("nan"),
                "max": float(present.max()) if len(present) else float("nan"),
            }
        )
    return pd.DataFrame(stats_rows)


def _regime_distribution(frame: pd.DataFrame) -> pd.Series:
    return frame["regime"].value_counts(normalize=True).sort_index()


def _spearman_collinearity(frame: pd.DataFrame) -> list[tuple[str, str, float]]:
    """Pairs with |ρ| > COLLINEARITY_THRESHOLD."""
    sub = frame[list(FEATURE_NAMES)].dropna()
    if len(sub) < 2:
        return []
    corr = sub.corr(method="spearman")
    pairs: list[tuple[str, str, float]] = []
    for i, a in enumerate(FEATURE_NAMES):
        for b in FEATURE_NAMES[i + 1 :]:
            r = float(corr.loc[a, b])
            if abs(r) >= COLLINEARITY_THRESHOLD:
                pairs.append((a, b, r))
    return pairs


def _extrapolate_holdout_obs(
    sample_obs: int,
    sample_n_asof: int,
    sample_n_tickers: int,
    holdout_n_asof: int,
    full_universe_size: int,
) -> int:
    """Linear extrapolation of obs density to holdout window × full universe."""
    if sample_n_asof == 0 or sample_n_tickers == 0:
        return 0
    obs_per_cell = sample_obs / (sample_n_asof * sample_n_tickers)
    return int(round(obs_per_cell * holdout_n_asof * full_universe_size))


def _verdict(
    nan_rates: pd.Series,
    coverage_drops: list[float],
    extrapolated_holdout: int,
    unexpected_collinear: list[tuple[str, str, float]],
) -> tuple[str, str]:
    """Returns (verdict_label, rationale_lines)."""
    fail_reasons: list[str] = []
    warn_reasons: list[str] = []

    bad_nan_features = [feat for feat, rate in nan_rates.items() if rate > NAN_RATE_FAIL]
    if bad_nan_features:
        fail_reasons.append(
            f"{len(bad_nan_features)} feature(s) have NaN > {NAN_RATE_FAIL:.0%}: "
            f"{', '.join(bad_nan_features)}"
        )

    warn_nan = [feat for feat, rate in nan_rates.items() if NAN_RATE_WARN < rate <= NAN_RATE_FAIL]
    if warn_nan:
        warn_reasons.append(
            f"{len(warn_nan)} feature(s) with NaN in ({NAN_RATE_WARN:.0%}, "
            f"{NAN_RATE_FAIL:.0%}]: {', '.join(warn_nan)}"
        )

    if any(d < SOURCE_JOIN_FLOOR for d in coverage_drops):
        fail_reasons.append(
            f"At least one coverage step retained < {SOURCE_JOIN_FLOOR:.0%} of cells"
        )

    if extrapolated_holdout < HOLDOUT_OBS_FLOOR:
        fail_reasons.append(
            f"Extrapolated holdout obs {extrapolated_holdout} < floor {HOLDOUT_OBS_FLOOR}"
        )
    elif extrapolated_holdout < HOLDOUT_OBS_COMFORTABLE:
        warn_reasons.append(
            f"Extrapolated holdout obs {extrapolated_holdout} below comfortable "
            f"threshold {HOLDOUT_OBS_COMFORTABLE}"
        )

    if unexpected_collinear:
        warn_reasons.append(
            f"{len(unexpected_collinear)} unexpected collinear pair(s) "
            f"(|ρ| ≥ {COLLINEARITY_THRESHOLD})"
        )

    if fail_reasons:
        verdict = (
            "PHASE-B-INFEASIBLE"
            if any("holdout" in r or "coverage" in r for r in fail_reasons)
            else "FIXES-REQUIRED"
        )
    elif warn_reasons:
        verdict = "PASS (with warnings)"
    else:
        verdict = "PASS"

    rationale = ""
    if fail_reasons:
        rationale += "**Failures:**\n" + "\n".join(f"- {r}" for r in fail_reasons) + "\n\n"
    if warn_reasons:
        rationale += "**Warnings:**\n" + "\n".join(f"- {r}" for r in warn_reasons) + "\n\n"
    if not (fail_reasons or warn_reasons):
        rationale = "All Phase A gates satisfied."
    return verdict, rationale


def _format_table(df: pd.DataFrame, float_cols: Sequence[str] = ()) -> str:
    """Markdown table from a DataFrame."""
    if df.empty:
        return "_(empty)_"
    cols = list(df.columns)
    lines = ["| " + " | ".join(cols) + " |"]
    lines.append("| " + " | ".join("---" for _ in cols) + " |")
    for _, row in df.iterrows():
        cells = []
        for c in cols:
            v = row[c]
            if (c in float_cols and isinstance(v, float)) or isinstance(v, float):
                cells.append(f"{v:.4f}" if not pd.isna(v) else "—")
            else:
                cells.append(str(v))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def _write_report(
    out_path: Path,
    *,
    args: argparse.Namespace,
    universe_size: int,
    full_universe_size: int,
    asof_dates: Sequence[date],
    frame: pd.DataFrame,
    fred_dgs3mo_present: bool,
) -> str:
    """Write the markdown report; return verdict label."""
    coverage = _coverage_waterfall(universe_size, len(asof_dates), frame)
    feat_stats = _per_feature_stats(frame)
    regime_dist = _regime_distribution(frame)
    collinear_pairs = _spearman_collinearity(frame)
    unexpected = [
        (a, b, r)
        for (a, b, r) in collinear_pairs
        if frozenset({a, b}) not in EXPECTED_COLLINEAR_PAIRS
    ]
    expected_seen = [
        (a, b, r) for (a, b, r) in collinear_pairs if frozenset({a, b}) in EXPECTED_COLLINEAR_PAIRS
    ]

    # Holdout extrapolation: 2024-04-30 → 2026-04-30 ≈ 500 trading days,
    # with stride=5 → ~100 rebalance dates. Use full ticker universe.
    holdout_n_asof = 100
    extrapolated = _extrapolate_holdout_obs(
        sample_obs=len(frame),
        sample_n_asof=len(asof_dates),
        sample_n_tickers=universe_size,
        holdout_n_asof=holdout_n_asof,
        full_universe_size=full_universe_size,
    )

    coverage_drops = [
        coverage[i][1] / coverage[0][1] if coverage[0][1] else 0.0 for i in range(1, len(coverage))
    ]

    verdict, rationale = _verdict(
        feat_stats.set_index("feature")["nan_rate"],
        coverage_drops,
        extrapolated,
        unexpected,
    )

    lines = [
        "# Multi-source two-stage screener — Phase A sanity report",
        "",
        f"**Verdict:** {verdict}",
        "",
        rationale,
        "## Run parameters",
        "",
        f"- Sample window: {args.start} → {args.end}",
        f"- Sample tickers: {universe_size} (full available pool: {full_universe_size})",
        f"- Rebalance stride: {args.rebalance_stride}",
        f"- Asof rebalance dates: {len(asof_dates)}",
        f"- Output rows (after per-ticker compute): {len(frame)}",
        f"- FRED DGS3MO available: {'yes' if fred_dgs3mo_present else 'NO — fell back / missing'}",
        "",
        "## Coverage waterfall",
        "",
        "| Step | Cells | Note |",
        "| --- | ---: | --- |",
    ]
    for label, cells, note in coverage:
        lines.append(f"| {label} | {cells:,} | {note} |")
    lines.append("")

    lines += [
        "## Per-feature stats",
        "",
        _format_table(feat_stats),
        "",
        "## Regime distribution",
        "",
    ]
    if regime_dist.empty:
        lines.append("_(no regime labels)_")
    else:
        lines.append("| Regime | Share |")
        lines.append("| --- | ---: |")
        for regime, share in regime_dist.items():
            lines.append(f"| {regime} | {share:.2%} |")
    lines.append("")

    lines += [
        "## Holdout extrapolation",
        "",
        f"- Sample density: {len(frame) / max(1, len(asof_dates) * universe_size):.4f} obs/cell",
        f"- Extrapolated holdout obs (~{holdout_n_asof} rebalance dates × full ~{full_universe_size}-ticker universe): "
        f"**{extrapolated:,}**",
        f"- Floor (PHASE-B-INFEASIBLE below): {HOLDOUT_OBS_FLOOR:,}",
        f"- Comfortable threshold: {HOLDOUT_OBS_COMFORTABLE:,}",
        "",
        "## Pairwise Spearman collinearity (|ρ| ≥ 0.90)",
        "",
    ]
    if expected_seen:
        lines.append("**Expected and acceptable** (pre-flagged in plan):")
        lines.append("")
        for a, b, r in expected_seen:
            lines.append(f"- `{a}` ↔ `{b}` (ρ = {r:+.3f})")
        lines.append("")
    if unexpected:
        lines.append("**Unexpected — investigate before Phase B:**")
        lines.append("")
        for a, b, r in unexpected:
            lines.append(f"- `{a}` ↔ `{b}` (ρ = {r:+.3f})")
        lines.append("")
    if not (expected_seen or unexpected):
        lines.append("_(no pair exceeded the threshold)_")
        lines.append("")

    lines += [
        "## Notes",
        "",
        "- Universe inclusion implication: features needing ≥252-day lookback impose an "
        "implicit ≥1-year history filter on participating tickers. Acknowledged, not a bug.",
        "- F4 fire-sale exclusion is active via ParquetInsiderScorer(delisting_events=...). "
        "Insider features default to 0.0 when scorer returns None (matches 'no signal').",
        "- VIX-quartile thresholds frozen at end of train period per pre-registration.",
        "- This report covers only Phase A (feature plumbing). Phase B (Lasso, holdout) blocked "
        "until verdict above is PASS.",
    ]

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines))
    return verdict


# ---------------------------------------------------------------------------
# Main


def main() -> int:
    load_dotenv()  # FRED_API_KEY etc.
    ap = _build_parser()
    args = ap.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    benchmark = "SPY"

    # 1. Available universe
    available = _list_available_tickers()
    logger.info("found %d tickers in %s", len(available), PRICES_DIR)
    full_universe_size = len(available)

    # 2. Pick subset
    chosen = _select_tickers(args.n_tickers, args.seed, available, required=[benchmark])
    logger.info(
        "selected %d tickers (incl benchmark %s): %s%s",
        len(chosen),
        benchmark,
        ", ".join(chosen[:8]),
        " …" if len(chosen) > 8 else "",
    )

    # 3. Load OHLCV (yfinance cache convention)
    histories = load_cached_histories(chosen, PRICES_DIR)
    missing = [t for t in chosen if t not in histories]
    if missing:
        logger.warning("missing OHLCV for %d ticker(s): %s", len(missing), missing[:10])
    history_store = HistoryStore(histories)
    logger.info("history store has %d tickers", len(history_store.tickers()))

    # 4. Insider scorer with delisting events (F4 fix)
    delisting_events = load_delisting_events(
        parquet_path=SURVIVORSHIP_PARQUET if SURVIVORSHIP_PARQUET.exists() else None,
        yaml_path=SURVIVORSHIP_YAML if SURVIVORSHIP_YAML.exists() else None,
    )
    logger.info("loaded %d delisting events", len(delisting_events))
    insider_scorer = ParquetInsiderScorer(INSIDER_PARQUET, delisting_events=delisting_events)
    logger.info("insider parquet stats: %s", insider_scorer.stats)

    # 5. FF/Carhart factors
    # Buffer: 252d before start so rolling-β has lookback room
    ff_start = args.start - timedelta(days=400)
    carhart = load_carhart_daily(start=ff_start, end=args.end)
    logger.info(
        "carhart factors: %d rows %s..%s columns=%s",
        len(carhart),
        carhart.index.min().date() if len(carhart) else None,
        carhart.index.max().date() if len(carhart) else None,
        list(carhart.columns),
    )

    # 6. FRED daily series
    try:
        fred = _load_fred_series_dict()
        fred_dgs3mo_present = "DGS3MO" in fred and len(fred["DGS3MO"]) > 0
    except Exception as exc:
        logger.error("FRED load failed: %s", exc)
        return 1
    logger.info(
        "FRED series loaded: %s",
        {k: f"{len(v)} obs" for k, v in fred.items()},
    )

    # 7. Calendar
    asof_dates = _build_calendar(
        args.start, args.end, history_store, benchmark, args.rebalance_stride
    )
    if not asof_dates:
        logger.error("empty benchmark calendar — abort")
        return 1
    logger.info(
        "calendar: %d asof dates (stride=%d) %s..%s",
        len(asof_dates),
        args.rebalance_stride,
        asof_dates[0],
        asof_dates[-1],
    )

    # 8. Build feature frame
    universe = [t for t in chosen if t != benchmark]
    train_end = asof_dates[-1]  # full sample is treated as train for thresholds
    frame = build_feature_frame(
        history_store=history_store,
        insider_scorer=insider_scorer,
        carhart_factors=carhart,
        fred_series=fred,
        universe=universe,
        asof_dates=asof_dates,
        train_end=train_end,
        benchmark=benchmark,
    )
    logger.info("feature frame shape: %s", frame.shape)

    # 9. Write report
    verdict = _write_report(
        args.out,
        args=args,
        universe_size=len(universe),
        full_universe_size=full_universe_size,
        asof_dates=asof_dates,
        frame=frame,
        fred_dgs3mo_present=fred_dgs3mo_present,
    )
    logger.info("report written to %s — verdict: %s", args.out, verdict)
    return 0


if __name__ == "__main__":
    sys.exit(main())
