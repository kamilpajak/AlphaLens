"""SmokeProfile registry + supporting dataclasses for the pre-audit framework.

Per zen 2026-05-11 review: `CheckType` is an enum so :mod:`coverage` can
dispatch generically without ever branching on `DataDep.name`. Each new
data source maps onto ONE of the existing CheckType members; if a new
member is needed, prefer extending the enum + adding one branch in
:func:`coverage.check_coverage` over special-casing by dep name.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum


class CheckType(str, Enum):
    """How :mod:`coverage` should peek at a data dir to verify date coverage.

    Each member maps to one branch in :func:`coverage.check_coverage`.

    - ``PARQUET_PARTITION``: hive-partitioned ``<year>=<NNNN>`` subdirs
      (e.g. Form-4 ``transaction_year=NNNN``). Coverage = set of years
      present must include every year in the audit window.

    - ``FLAT_PARQUET``: one parquet per ticker (e.g. iVolatility SMD).
      Coverage = sample N tickers; ALL must have a date column whose
      range covers the window. Single-ticker peek is unsafe: a long-
      history ticker like AAPL would false-pass when the median R2000
      name has shorter history.

    - ``YAML_DIR``: PIT universe yamls with dates encoded in filenames
      (e.g. ``r2000_pit_YYYY-MM-DD.yaml``). Coverage = filename date
      range covers the window.

    - ``EXISTS_NONEMPTY``: existence + non-empty directory only; for
      data with no clear date metadata (e.g. companyfacts_parquet,
      ticker_cik_map). Coverage check degrades to "is it there?".
    """

    PARQUET_PARTITION = "parquet_partition"
    FLAT_PARQUET = "flat_parquet"
    YAML_DIR = "yaml_dir"
    EXISTS_NONEMPTY = "exists_nonempty"


@dataclass(frozen=True)
class DataDep:
    """A data dependency the strategy requires under ``~/.alphalens/<name>/``.

    ``min_date`` / ``max_date`` define the date range the data MUST
    span; if None, only existence is checked. The interpretation
    depends on :class:`CheckType`:

    - For ``PARQUET_PARTITION``, the partition year set must include
      every year in ``[min_date.year, max_date.year]``.
    - For ``FLAT_PARQUET``, sampled tickers' date column must satisfy
      ``min(col) <= min_date`` and ``max(col) >= max_date``.
    - For ``YAML_DIR``, filenames embed dates that must straddle the
      window.
    """

    name: str
    check_type: CheckType
    min_date: date | None = None
    max_date: date | None = None
    # FLAT_PARQUET only: how many random tickers to sample for date-range
    # verification. Higher = stronger guarantee, more I/O. 10 is the
    # bare minimum to avoid single-ticker false-pass (AAPL-only peek).
    sample_size: int = 10
    # FLAT_PARQUET only: fraction of sampled tickers that must span the
    # window for the check to PASS. Default 0.7 catches "environment
    # missing all/most data" (today's launch bug — pre-2018 iVol gap
    # would flunk ALL samples) while tolerating recent-IPO outliers
    # (~10-20% of any R2000-style universe at any given asof). Set
    # to 1.0 for strict "all must span" semantics on small/curated sets.
    min_pass_ratio: float = 0.7
    # Optional file extension or glob for FLAT_PARQUET / YAML_DIR
    # (defaults handled in coverage.py per CheckType).
    pattern: str | None = None


@dataclass(frozen=True)
class SmokeProfile:
    """Per-strategy smoke-test config.

    ``smoke_window`` is the (start, end) date range used for the smoke
    invocation — keep small (1-3 months) so the smoke completes in <2
    min on local SSD / <5 min on MooseFS pod.

    ``extra_args`` are appended verbatim to the experiment script's
    argv. Standard small-scale knobs: ``--universe-size-cap``,
    ``--skip-precheck``, ``--phase-offset 0``, ``--rebalance-stride``.
    """

    strategy: str
    smoke_window: tuple[date, date]
    extra_args: tuple[str, ...]
    data_deps: tuple[DataDep, ...]
    # Strategies with locked component scorers + _verify_component_hashes
    # in their experiment script. The smoke runner exercises the hash
    # guard implicitly by invoking that script's main(), so this flag
    # is informational only (test_preaudit_profiles asserts the hash
    # guard exists for strategies flagged True).
    has_component_hash_guard: bool = False


# Insider × P/C abnormal-volume compound is the first registered
# profile — driven by today's launch failure. Smoke window 2019-Q1
# matches the golden master fixture (cap=300, 3 months, skip-precheck).
INSIDER_PC_COMPOUND_PROFILE = SmokeProfile(
    strategy="insider_pc_compound",
    smoke_window=(date(2019, 1, 1), date(2019, 3, 31)),
    extra_args=(
        "--skip-precheck",
        "--universe-size-cap",
        "300",
        "--phase-offset",
        "0",
        "--rebalance-stride",
        "21",
    ),
    data_deps=(
        DataDep(
            name="form4_parquet",
            check_type=CheckType.PARQUET_PARTITION,
            min_date=date(2018, 1, 1),
            max_date=date(2019, 3, 31),
        ),
        DataDep(
            name="ivolatility_smd",
            check_type=CheckType.FLAT_PARQUET,
            min_date=date(2018, 4, 30),  # iVol coverage cliff (pod-side)
            max_date=date(2019, 3, 31),
            pattern="tradeDate",
            # iVol cache contains many legacy/delisted (pre-2018) +
            # recent-IPO (post-2020) tickers alongside the R2000-active
            # subset. Random sampling against the bulk gives ~40% spanning
            # any single quarter, so 0.3 is the threshold that
            # discriminates "pod has ZERO post-2018 data" (today's bug,
            # 0%) from "healthy mixed-vintage local env" (~40%) without
            # false-failing on the latter.
            #
            # Per zen 2026-05-11 review: sample_size=50 (not 10) sharpens
            # the binomial discrimination — at 20% degraded actual rate,
            # P(≥15 successes in 50 with p=0.2) ≈ 4%, vs ~32% at n=10.
            # Cost: ~50 small parquet column reads (~5 s wall), trivial
            # vs. the 600 s smoke budget.
            min_pass_ratio=0.3,
            sample_size=50,
        ),
        DataDep(
            name="prices",
            check_type=CheckType.FLAT_PARQUET,
            min_date=date(2018, 1, 1),
            max_date=date(2019, 3, 31),
            # OHLCV parquets store dates on the DatetimeIndex; coverage's
            # _peek_dates falls back to the index when the named column
            # is absent. Pattern stays "date" sentinel; falls through.
            # Empirical pod observation 2026-05-11: random sample of R2000
            # prices hits ~60% spanning any single quarter — recent IPOs
            # (e.g. ARLO 2018-08, AHCO 2018-05, CTVA 2019-05) account for
            # the gap. PIT universe filters these out at scoring time, so
            # the check correctly catches "env missing all data" (0% case)
            # while tolerating routine IPO sprinkling. Default 0.7 was
            # too strict for the empirical R2000 universe shape.
            min_pass_ratio=0.5,
        ),
        DataDep(
            name="factors",
            check_type=CheckType.EXISTS_NONEMPTY,
        ),
        DataDep(
            name="pit_universe",
            check_type=CheckType.EXISTS_NONEMPTY,
        ),
    ),
    has_component_hash_guard=True,
)


SMOKE_PROFILES: dict[str, SmokeProfile] = {
    INSIDER_PC_COMPOUND_PROFILE.strategy: INSIDER_PC_COMPOUND_PROFILE,
}


# Strategies in :data:`alphalens_cli.commands.audit._SCRIPTS` that
# DELIBERATELY do not require a SmokeProfile. Two reasons to allowlist:
# (1) the strategy is RESEARCH_ONLY / archived and won't be re-audited
# anytime soon, OR (2) its experiment script doesn't yet accept the
# generic smoke args (`--is-start`, `--is-end`, `--universe-size-cap`,
# `--out`). Add a profile + remove from this set when the strategy is
# scheduled for an audit.
SMOKE_PROFILE_EXEMPT: frozenset[str] = frozenset(
    {
        # Closed Layer 2 / RESEARCH_ONLY strategies — see paradigm_failures
        # postmortem. Listed here so the inverse drift test passes; if any
        # of these get re-opened for audit, drop from the set and add a
        # SmokeProfile.
        "tri_factor",
        "momentum_lowvol",
        "constrained_momentum",
        "constrained_contrarian",
        "quality_momentum",
        "longshort_mom_lowvol",
        "regime_overlay",
        "layer2d_prior_returns",
        "layer2d_random_null",
        "layer2d_str_and_contrarian",
        "layer2d_variants",
        "vol_target_overlay",
        "multi_source_two_stage",
        "multi_source_global_lasso",
        "multi_source_global_lasso_20d",
        "v7_options_implied",
        "v8_literature_direct",
        "v9_sign_constrained",
        "v9_cross_sectional_residual",
        "insider_form4_opportunistic",  # has its own dedicated launcher
    }
)


# -------------------------------------------------------------------------
# Result types — defined here so :mod:`coverage` and :mod:`runner` share
# the vocabulary.
# -------------------------------------------------------------------------


class CoverageStatus(str, Enum):
    PASS = "pass"
    FAIL_MISSING = "fail_missing"  # directory absent
    FAIL_EMPTY = "fail_empty"  # directory present but empty
    FAIL_GAP = "fail_gap"  # data exists but doesn't span window


@dataclass(frozen=True)
class CoverageCheck:
    """Outcome of a single :class:`DataDep` check."""

    dep: DataDep
    status: CoverageStatus
    detail: str = ""

    @property
    def passed(self) -> bool:
        return self.status is CoverageStatus.PASS


@dataclass(frozen=True)
class CoverageReport:
    """Aggregate result for a :class:`SmokeProfile`'s ``data_deps``."""

    checks: tuple[CoverageCheck, ...] = field(default_factory=tuple)

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    @property
    def failures(self) -> tuple[CoverageCheck, ...]:
        return tuple(c for c in self.checks if not c.passed)


class SmokeStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"  # subprocess exited non-zero
    TIMEOUT = "timeout"  # exceeded smoke wall budget
    UNKNOWN_STRATEGY = "unknown_strategy"
    NO_PROFILE = "no_profile"


@dataclass(frozen=True)
class SmokeResult:
    """Outcome of :func:`runner.run_smoke`."""

    status: SmokeStatus
    exit_code: int | None = None
    duration_s: float | None = None
    detail: str = ""

    @property
    def passed(self) -> bool:
        return self.status is SmokeStatus.PASS
