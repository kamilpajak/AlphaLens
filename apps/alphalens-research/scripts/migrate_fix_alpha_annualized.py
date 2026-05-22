"""Migration: correct stale `alpha_gross_4f` / `alpha_net_4f` in legacy cell JSONs.

Bug 1 root cause: `alphalens_research.attribution.factor_analysis.run_regression`
defaults `periods_per_year=252` (daily) but historical drivers passed
strided per-rebalance returns (e.g. 5d holding) without overriding. Result:
`alpha_annualized = alpha_per_period * 252` ~50× inflated for weekly stride.

This script walks each cell JSON under ``docs/research/`` (recursively) and:
1. Detects whether the cell needs migration (no `migrated_alpha_at` flag, has
   `alpha_gross_4f` field).
2. Reads stride from config (`config.stride_days` or `config.rebalance_stride`).
3. Recomputes `alpha_*_4f = alpha_*_4f / stride_days * 1` (i.e. divides by the
   over-annualization factor 252/(252/stride) = stride).
4. Writes back with `migrated_alpha_at: 2026-05-05` flag.

Idempotent: presence of `migrated_alpha_at` short-circuits.

NOTE: Active drivers (v9d_retrospective + pc_retrospective + v10_drawdown_overlay)
re-run with the per-driver fix already produce correct values. This script is
for the historical FAILed entries that nobody plans to re-run.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
RESEARCH_DIR = REPO_ROOT / "docs" / "research"

ALPHA_FIELDS = ("alpha_gross_4f", "alpha_net_4f", "alpha_annualized", "alpha_se_4f")
MIGRATED_FLAG = "migrated_alpha_at"
MIGRATED_DATE = "2026-05-05"


def _infer_stride_days(payload: dict[str, Any]) -> int | None:
    """Try to find rebalance stride in known config locations."""
    for path in [
        ("config", "stride_days"),
        ("config", "rebalance_stride"),
        ("config", "rebalance_stride_days"),
    ]:
        node = payload
        for k in path:
            if not isinstance(node, dict) or k not in node:
                node = None
                break
            node = node[k]
        if isinstance(node, (int, float)) and node > 0:
            return int(node)
    return None


def _migrate_in_place(payload: dict[str, Any], stride: int) -> int:
    """Walk payload recursively, dividing alpha_*_4f / alpha_annualized by stride.

    Returns count of fields modified."""
    n_modified = 0

    def _walk(node):
        nonlocal n_modified
        if isinstance(node, dict):
            for k, v in node.items():
                if k in ALPHA_FIELDS and isinstance(v, (int, float)) and v != 0:
                    node[k] = float(v) / stride
                    n_modified += 1
                else:
                    _walk(v)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(payload)
    return n_modified


def migrate_cell_json(path: Path, *, dry_run: bool = False) -> dict[str, Any]:
    """Migrate one cell JSON. Returns dict with status info."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"path": str(path), "status": "error_read", "error": str(exc)}

    if not isinstance(payload, dict):
        return {"path": str(path), "status": "skip_not_dict"}

    if payload.get(MIGRATED_FLAG):
        return {"path": str(path), "status": "skip_already_migrated"}

    # Heuristic: only migrate JSONs that LOOK like cell results. Must contain
    # an alpha field somewhere AND a config dict with stride info.
    has_alpha_field = False

    def _has_alpha(node):
        nonlocal has_alpha_field
        if has_alpha_field:
            return
        if isinstance(node, dict):
            for k, v in node.items():
                if k in ALPHA_FIELDS:
                    has_alpha_field = True
                    return
                _has_alpha(v)
        elif isinstance(node, list):
            for item in node:
                _has_alpha(item)

    _has_alpha(payload)
    if not has_alpha_field:
        return {"path": str(path), "status": "skip_no_alpha_fields"}

    stride = _infer_stride_days(payload)
    if stride is None or stride == 1:
        return {"path": str(path), "status": "skip_no_stride_or_daily"}

    # Active drivers (v9d_retrospective + pc_retrospective) are re-run by their
    # respective drivers — skip their cells to avoid double-correction.
    skip_dirs = {
        "v9d_retrospective_pre_2018",
        "pc_abnormal_retrospective_pre_2018",
    }
    parents = {p.name for p in path.parents}
    if parents & skip_dirs:
        return {"path": str(path), "status": "skip_active_driver_dir"}

    n_modified = _migrate_in_place(payload, stride)
    payload[MIGRATED_FLAG] = MIGRATED_DATE
    payload["migrated_stride_days"] = stride
    payload["migrated_n_alpha_fields_corrected"] = n_modified

    if not dry_run:
        path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

    return {
        "path": str(path),
        "status": "migrated",
        "stride": stride,
        "n_modified": n_modified,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--research-dir", type=Path, default=RESEARCH_DIR)
    ap.add_argument("--dry-run", action="store_true", help="Don't write changes.")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

    cells = list(args.research_dir.rglob("*.json"))
    logger.info("Scanning %d JSON files under %s", len(cells), args.research_dir)

    counts: dict[str, int] = {}
    for path in cells:
        result = migrate_cell_json(path, dry_run=args.dry_run)
        counts[result["status"]] = counts.get(result["status"], 0) + 1
        if result["status"] == "migrated":
            logger.info(
                "Migrated %s (stride=%d, fields=%d)",
                result["path"],
                result["stride"],
                result["n_modified"],
            )

    print()
    print("=== Migration summary ===")
    for status, n in sorted(counts.items()):
        print(f"  {status}: {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
