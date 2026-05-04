"""Synthetic SEC companyfacts JSON fixtures for parquet-backed store tests.

Three CIK profiles cover the realistic range of data sparsity that the
event_drift v4 stores must handle:

  * **Apple-shaped (CIK 0000320193)**: 8 quarters of EPS Basic + Diluted plus
    all 7 Sloan accruals concepts, including one period_end with two filed
    dates (restatement) so first-filed semantics are exercised. Foster SUE
    (residual_window=4) and Sloan accruals both return real values.

  * **Sparse small-cap (CIK 0000888888)**: 6 quarters of EPS Basic only
    (no Diluted) and 6 quarters of every Sloan concept *except*
    DepreciationAndAmortization. Foster SUE works; Sloan returns None
    because a required concept is absent.

  * **Recent IPO (CIK 0000999999)**: 2 quarters of EPS Basic, no balance
    sheet. Both Foster SUE (insufficient history for 4-quarter residual)
    and Sloan accruals return None.

Helpers also persist these fixtures to a parquet directory at the layout
``{root}/{cik}.parquet`` for integration tests of stores against a real
on-disk Arrow file.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow.parquet as pq

from alphalens.data.fundamentals.companyfacts_parquet import (
    companyfacts_json_to_parquet_table,
)

APPLE_CIK = "0000320193"
SPARSE_CIK = "0000888888"
IPO_CIK = "0000999999"


_SLOAN_CONCEPTS = (
    "AssetsCurrent",
    "CashAndCashEquivalentsAtCarryingValue",
    "LiabilitiesCurrent",
    "LongTermDebtCurrent",
    "IncomeTaxesPayable",
    "DepreciationAndAmortization",
    "Assets",
)


# Quarter calendar used by all fixtures (period_end -> filed_date, fp, fy).
# Eight consecutive quarters spanning 2022Q1..2023Q4 plus 2024Q1..Q2.
_QUARTERS = [
    {"end": "2022-04-02", "filed": "2022-05-15", "fy": 2022, "fp": "Q2", "form": "10-Q"},
    {"end": "2022-07-02", "filed": "2022-08-15", "fy": 2022, "fp": "Q3", "form": "10-Q"},
    {"end": "2022-10-01", "filed": "2022-11-14", "fy": 2022, "fp": "Q4", "form": "10-Q"},
    {"end": "2022-12-31", "filed": "2023-02-15", "fy": 2022, "fp": "FY", "form": "10-K"},
    {"end": "2023-04-01", "filed": "2023-05-15", "fy": 2023, "fp": "Q2", "form": "10-Q"},
    {"end": "2023-07-01", "filed": "2023-08-14", "fy": 2023, "fp": "Q3", "form": "10-Q"},
    {"end": "2023-09-30", "filed": "2023-11-13", "fy": 2023, "fp": "Q4", "form": "10-Q"},
    {"end": "2023-12-30", "filed": "2024-02-15", "fy": 2023, "fp": "FY", "form": "10-K"},
]


def _instant_entry(
    end: str, filed: str, val: float, fy: int, fp: str, form: str, accn: str
) -> dict:
    """Balance-sheet (instant) record. No ``start`` per US-GAAP convention."""
    return {
        "end": end,
        "val": val,
        "accn": accn,
        "fy": fy,
        "fp": fp,
        "form": form,
        "filed": filed,
    }


def _duration_entry(
    start: str, end: str, filed: str, val: float, fy: int, fp: str, form: str, accn: str
) -> dict:
    return {
        "start": start,
        "end": end,
        "val": val,
        "accn": accn,
        "fy": fy,
        "fp": fp,
        "form": form,
        "filed": filed,
    }


def build_apple_facts() -> dict:
    """Realistic large-cap shape: 8 quarters EPS B+D, 7 Sloan concepts, one restatement."""
    eps_basic_values = [1.50, 1.25, 1.30, 1.42, 1.55, 1.27, 1.35, 1.48]
    eps_diluted_values = [1.48, 1.23, 1.28, 1.40, 1.53, 1.25, 1.33, 1.46]
    accn_template = "0000320193-{idx:02d}"

    eps_basic_entries = []
    eps_diluted_entries = []
    for idx, (q, basic, diluted) in enumerate(
        zip(_QUARTERS, eps_basic_values, eps_diluted_values, strict=True)
    ):
        # EPS is a duration concept; quarter window = end - 90 days approx.
        from datetime import date, timedelta

        end_d = date.fromisoformat(q["end"])
        start_d = end_d - timedelta(days=90)
        eps_basic_entries.append(
            _duration_entry(
                start=start_d.isoformat(),
                end=q["end"],
                filed=q["filed"],
                val=basic,
                fy=q["fy"],
                fp=q["fp"],
                form=q["form"],
                accn=accn_template.format(idx=idx),
            )
        )
        eps_diluted_entries.append(
            _duration_entry(
                start=start_d.isoformat(),
                end=q["end"],
                filed=q["filed"],
                val=diluted,
                fy=q["fy"],
                fp=q["fp"],
                form=q["form"],
                accn=accn_template.format(idx=idx),
            )
        )

    # Restatement: 2023-04-01 EPS Basic later refiled 2024-01-15 with revised value.
    eps_basic_entries.append(
        _duration_entry(
            start="2023-01-01",
            end="2023-04-01",
            filed="2024-01-15",  # later than original 2023-05-15
            val=1.58,  # restated upward from 1.55
            fy=2023,
            fp="Q2",
            form="10-Q/A",
            accn="0000320193-99",
        )
    )

    facts: dict = {
        "cik": int(APPLE_CIK),
        "entityName": "AppleFixture Inc.",
        "facts": {
            "us-gaap": {
                "EarningsPerShareBasic": {
                    "label": "EPS Basic",
                    "description": "Earnings per share, basic",
                    "units": {"USD/shares": eps_basic_entries},
                },
                "EarningsPerShareDiluted": {
                    "label": "EPS Diluted",
                    "description": "Earnings per share, diluted",
                    "units": {"USD/shares": eps_diluted_entries},
                },
            },
        },
    }

    # Synthesize plausible balance-sheet trajectories for all 7 Sloan concepts.
    sloan_baselines = {
        "AssetsCurrent": 130_000_000_000.0,
        "CashAndCashEquivalentsAtCarryingValue": 30_000_000_000.0,
        "LiabilitiesCurrent": 110_000_000_000.0,
        "LongTermDebtCurrent": 9_500_000_000.0,
        "IncomeTaxesPayable": 6_500_000_000.0,
        "DepreciationAndAmortization": 2_800_000_000.0,
        "Assets": 350_000_000_000.0,
    }
    for concept, baseline in sloan_baselines.items():
        entries = []
        for idx, q in enumerate(_QUARTERS):
            # Slight drift each quarter to make accruals deltas non-trivial.
            val = baseline * (1.0 + 0.012 * idx)
            entries.append(
                _instant_entry(
                    end=q["end"],
                    filed=q["filed"],
                    val=val,
                    fy=q["fy"],
                    fp=q["fp"],
                    form=q["form"],
                    accn=accn_template.format(idx=idx),
                )
            )
        facts["facts"]["us-gaap"][concept] = {
            "label": concept,
            "description": "...",
            "units": {"USD": entries},
        }

    return facts


def build_sparse_smallcap_facts() -> dict:
    """Sparse small-cap: EPS Basic only, missing DepreciationAndAmortization.

    Foster SUE produces a value (6 quarters >= 5-quarter window), Sloan
    accruals returns None (required concept absent).
    """
    eps_values = [0.10, 0.12, 0.08, 0.15, 0.09, 0.18]
    quarters = _QUARTERS[:6]
    accn_template = "0000888888-{idx:02d}"

    from datetime import date, timedelta

    eps_entries = []
    for idx, (q, val) in enumerate(zip(quarters, eps_values, strict=True)):
        end_d = date.fromisoformat(q["end"])
        start_d = end_d - timedelta(days=90)
        eps_entries.append(
            _duration_entry(
                start=start_d.isoformat(),
                end=q["end"],
                filed=q["filed"],
                val=val,
                fy=q["fy"],
                fp=q["fp"],
                form=q["form"],
                accn=accn_template.format(idx=idx),
            )
        )

    facts: dict = {
        "cik": int(SPARSE_CIK),
        "entityName": "SparseSmallCap Inc.",
        "facts": {
            "us-gaap": {
                "EarningsPerShareBasic": {
                    "label": "EPS Basic",
                    "description": "Earnings per share, basic",
                    "units": {"USD/shares": eps_entries},
                },
            },
        },
    }

    # Provide all Sloan concepts EXCEPT DepreciationAndAmortization.
    baselines = {
        "AssetsCurrent": 50_000_000.0,
        "CashAndCashEquivalentsAtCarryingValue": 8_000_000.0,
        "LiabilitiesCurrent": 35_000_000.0,
        "LongTermDebtCurrent": 2_000_000.0,
        "IncomeTaxesPayable": 1_200_000.0,
        "Assets": 120_000_000.0,
    }
    for concept, baseline in baselines.items():
        entries = []
        for idx, q in enumerate(quarters):
            entries.append(
                _instant_entry(
                    end=q["end"],
                    filed=q["filed"],
                    val=baseline * (1.0 + 0.005 * idx),
                    fy=q["fy"],
                    fp=q["fp"],
                    form=q["form"],
                    accn=accn_template.format(idx=idx),
                )
            )
        facts["facts"]["us-gaap"][concept] = {
            "label": concept,
            "description": "...",
            "units": {"USD": entries},
        }

    return facts


def build_recent_ipo_facts() -> dict:
    """Recent IPO: only 2 quarters of EPS Basic, no balance sheet.

    Foster SUE returns None (residual_window=4 needs 5+ quarters of history).
    Sloan accruals returns None (no balance sheet at all).
    """
    eps_values = [0.05, 0.07]
    quarters = _QUARTERS[-2:]  # latest two quarters
    accn_template = "0000999999-{idx:02d}"

    from datetime import date, timedelta

    entries = []
    for idx, (q, val) in enumerate(zip(quarters, eps_values, strict=True)):
        end_d = date.fromisoformat(q["end"])
        start_d = end_d - timedelta(days=90)
        entries.append(
            _duration_entry(
                start=start_d.isoformat(),
                end=q["end"],
                filed=q["filed"],
                val=val,
                fy=q["fy"],
                fp=q["fp"],
                form=q["form"],
                accn=accn_template.format(idx=idx),
            )
        )

    return {
        "cik": int(IPO_CIK),
        "entityName": "RecentIPO Inc.",
        "facts": {
            "us-gaap": {
                "EarningsPerShareBasic": {
                    "label": "EPS Basic",
                    "description": "Earnings per share, basic",
                    "units": {"USD/shares": entries},
                },
            },
        },
    }


_BUILDERS = {
    APPLE_CIK: build_apple_facts,
    SPARSE_CIK: build_sparse_smallcap_facts,
    IPO_CIK: build_recent_ipo_facts,
}


def write_all_fixtures_as_parquet(output_dir: Path) -> dict[str, Path]:
    """Persist the three fixture CIKs as parquet files in ``output_dir``.

    Returns a mapping ``cik -> path`` for direct test access. The output
    directory is created if absent. Existing files are overwritten so the
    fixture stays in sync with the builders without a manual cleanup step.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    for cik, builder in _BUILDERS.items():
        table = companyfacts_json_to_parquet_table(builder())
        target = output_dir / f"{cik}.parquet"
        pq.write_table(table, target, compression="zstd")
        written[cik] = target
    return written


def all_fixture_ciks() -> tuple[str, ...]:
    return tuple(_BUILDERS.keys())
