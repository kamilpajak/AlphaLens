"""Eager Buffett qualitative enrichment + an immutable per-(date, ticker) cache (PR-3).

The user doctrine (design memo §2): compute the qualitative Buffett layer
(moat / trend / candor / understandable + rationale, optional scuttlebutt)
**eagerly for every brief survivor** rather than on demand — the LLM cost is
trivial next to capital deployed on a poorly-researched name, and an on-demand
"fetch" gate would discourage the very analysis the tool exists for.

The expensive op is one DeepSeek Pro call (~$0.06-0.12) per candidate. To keep the
6x/day pipeline reruns + re-ingests from re-paying it, each result is cached
**immutably per (config_version, date, ticker)** under
``~/.alphalens/buffett_qual/<config_version>/<date>/<TICKER>.json``: the assessment
is point-in-time as of the brief date, so once a name is successfully classified
for a date+rubric it never needs recomputing, and a future rubric bump writes to a
new ``config_version`` tier instead of overwriting the corpus. Only genuine
successes are frozen — an all-``None`` assessment (LLM error, or a name with no
10-K) is NOT cached, so a transient failure retries on the next run.

The 10-K text itself is already cached fetch-once-per-filing by
``thematic.verification.tenk_grep`` (``~/.alphalens/thematic_tenk/``), so this
module adds only the LLM-result cache, not a second 10-K cache.

Fail-soft throughout: a fetch / LLM failure for one ticker yields dashes for that
row and never aborts the batch. Numbers are NEVER produced by the LLM (doctrine);
the qualitative layer only classifies over injected facts + 10-K text.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

import pandas as pd

from alphalens_pipeline.experts.buffett.comparison import BuffettPanel
from alphalens_pipeline.experts.buffett.qualitative import QualitativeAssessment

logger = logging.getLogger(__name__)

# The eight flat qual columns stamped onto the brief frame (mirrors the quant
# block from PR-1/PR-2: enums + bool + prose + provenance + rubric tag).
QUAL_COLUMNS: tuple[str, ...] = (
    "buffett_moat_type",
    "buffett_moat_trend",
    "buffett_management_candor",
    "buffett_understandable",
    "buffett_qualitative_rationale",
    "buffett_used_scuttlebutt",
    "buffett_qual_computed_at",
    "buffett_qual_config_version",
)

# Default runtime root for the LLM-result cache.
DEFAULT_QUAL_CACHE_DIR = Path.home() / ".alphalens" / "buffett_qual"

# Identifies the rubric that produced a qualitative verdict, so a future
# rubric / prompt / model bump can never silently overwrite or regime-blend the
# already-on-disk verdict corpus that feeds the deferred Buffett×EDGE calibration
# (the cache path is keyed by this token, and it is stamped onto every record +
# the brief parquet). The currently-shipped qualitative layer is a *pure LLM
# prompt* rubric over injected facts — ``QualitativeAssessment`` carries only
# enums / bool / free text and the LLM emits no numbers — so this is an OPAQUE
# string tag, NOT a hash, and there is no float to canonicalize.
#
# FORWARD CONSTRAINT: any future rubric that folds NUMERIC config (weights, caps,
# DCF assumptions) into the qualitative layer MUST canonicalize those floats to
# fixed-precision / basis-point integer strings before building its token —
# otherwise ``json.dumps`` platform variance (``0.1`` vs ``0.10``) would split one
# regime across two GROUP BY buckets in the calibration join.
#
# PRECONDITION (documented, not enforced): :func:`load_cache` and
# :func:`migrate_legacy_qual_cache` back-stamp this sentinel onto any untagged
# on-disk body. That is correct ONLY because the entire existing corpus came from
# this single pre-registry rubric — there is no provenance check, so an untagged
# file produced by a *different* prompt / model would be silently mislabeled v0.
BUFFETT_QUAL_CONFIG_VERSION = "buffett-pre-registry-v0"

# Default directory holding the daily thematic brief parquets (the file the qual
# columns are stamped into).
_DEFAULT_BRIEFS_DIR = Path.home() / ".alphalens" / "thematic_briefs"

# (panel, asof, scuttlebutt) -> QualitativeAssessment | None. The expensive op
# (10-K fetch + LLM). ``None`` means "no 10-K to reason over" (skip, no cost);
# an all-``None`` assessment means the LLM ran but classified nothing.
AssessOne = Callable[[BuffettPanel, dt.date, bool], "QualitativeAssessment | None"]


@dataclass(frozen=True)
class QualRecord:
    """One candidate's cached qualitative result (the eight column values).

    ``computed_at`` is an ISO-8601 UTC stamp recording when the LLM classified
    this name; ``used_scuttlebutt`` records whether the scuttlebutt context block
    was enabled for the run that produced it; ``config_version`` tags the rubric
    that produced the verdict (see :data:`BUFFETT_QUAL_CONFIG_VERSION`). The field
    is REQUIRED (no default) so every construction site is forced to pass it and a
    legacy JSON body missing the key is routed through the :func:`load_cache`
    back-stamp rather than silently defaulting.
    """

    moat_type: str | None
    moat_trend: str | None
    management_candor: str | None
    understandable: bool | None
    rationale: str | None
    used_scuttlebutt: bool
    computed_at: str
    config_version: str


def _is_real(record: QualRecord) -> bool:
    """True when the LLM classified at least one quality (a cacheable success)."""
    return any(
        v is not None
        for v in (
            record.moat_type,
            record.moat_trend,
            record.management_candor,
            record.understandable,
            record.rationale,
        )
    )


def _cache_path(
    ticker: str,
    asof: dt.date,
    cache_dir: Path,
    *,
    config_version: str,
    scuttlebutt: bool = False,
) -> Path:
    # ``config_version`` is a LEADING path tier so distinct rubrics physically
    # never collide (a future bump writes to a different tier, never overwriting
    # the corpus). Scuttlebutt changes the prompt INPUT, so a scuttlebutt run is a
    # distinct computation and keeps its own ``.sb.json`` entry — otherwise a prior
    # no-scuttlebutt result would short-circuit a later ``--scuttlebutt`` request.
    suffix = ".sb.json" if scuttlebutt else ".json"
    return cache_dir / config_version / asof.isoformat() / f"{ticker.upper()}{suffix}"


def load_cache(
    ticker: str,
    asof: dt.date,
    cache_dir: Path,
    *,
    config_version: str = BUFFETT_QUAL_CONFIG_VERSION,
    scuttlebutt: bool = False,
) -> QualRecord | None:
    """Load a cached :class:`QualRecord` for ``(config_version, asof, ticker, scuttlebutt)``.

    Returns ``None`` on a miss. Never raises — a corrupt / unreadable cache file
    logs and is treated as a miss (the name recomputes) rather than crashing the
    enrichment.

    Two guards protect the corpus: (1) a legacy body missing ``config_version`` is
    back-stamped with :data:`BUFFETT_QUAL_CONFIG_VERSION` so it constructs a valid
    v0 record instead of raising on the now-required field; (2) a body whose
    ``config_version`` does NOT match the tier it was read from is treated as a
    MISS (logged), so a stale / mis-placed file is recomputed at the correct tier
    rather than silently mis-attributed across rubrics.
    """
    path = _cache_path(
        ticker, asof, cache_dir, config_version=config_version, scuttlebutt=scuttlebutt
    )
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        data.setdefault("config_version", BUFFETT_QUAL_CONFIG_VERSION)
        if data["config_version"] != config_version:
            logger.warning(
                "buffett qual cache: tier mismatch at %s (%s != %s) — treating as miss",
                path,
                data["config_version"],
                config_version,
            )
            return None
        return QualRecord(**data)
    except Exception as exc:  # corrupt file / schema drift -> treat as miss
        logger.warning("buffett qual cache: unreadable %s: %s", path, exc)
        return None


def write_cache(
    ticker: str,
    asof: dt.date,
    cache_dir: Path,
    record: QualRecord,
    *,
    config_version: str = BUFFETT_QUAL_CONFIG_VERSION,
    scuttlebutt: bool = False,
) -> None:
    """Persist ``record`` to the per-(config_version, date, ticker, scuttlebutt) cache."""
    path = _cache_path(
        ticker, asof, cache_dir, config_version=config_version, scuttlebutt=scuttlebutt
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(record), sort_keys=True))


def migrate_legacy_qual_cache(
    cache_dir: Path, *, config_version: str = BUFFETT_QUAL_CONFIG_VERSION
) -> int:
    """Relocate legacy untagged cache files into the version-tiered layout.

    Pre-retrofit the cache laid each result at ``cache_dir/<date>/<TICKER>{.sb}.json``
    with no ``config_version``. This one-shot, idempotent helper MOVES every such
    legacy file under the version tier ``cache_dir/<config_version>/<date>/...``,
    stamping the sentinel into the body, then UNLINKS the legacy source — so the
    on-disk corpus is relocated EXACTLY ONCE (one canonical copy per verdict, no
    double-count) and a future rubric bump writes to a distinct tier instead of
    overwriting it. Returns the count of legacy files moved.

    MUST run before the first ``qual-enrich`` of a deploy that carries this change
    (the thematic script invokes ``alphalens buffett migrate-qual-cache`` as a
    pre-step): otherwise enrich misses at the new tier and recomputes every cached
    name into the v0 tier with a possibly-different (LLM-nondeterministic) verdict.

    Crash-safety: the versioned target is WRITTEN before the legacy source is
    unlinked, and an already-present target is left untouched while the source is
    still unlinked — so a re-run after a partial move converges to one copy.

    Legacy date tiers are identified by a strict ISO-date directory name; the
    version tiers (and anything else non-date) are skipped, so the helper never
    re-processes already-migrated files and a second run returns 0.

    PRECONDITION: the whole existing corpus came from the single pre-registry
    rubric — the back-stamp encodes no provenance check (see
    :data:`BUFFETT_QUAL_CONFIG_VERSION`).
    """
    if not cache_dir.is_dir():
        return 0
    migrated = 0
    for date_dir in sorted(cache_dir.iterdir()):
        if not date_dir.is_dir():
            continue
        try:
            dt.date.fromisoformat(date_dir.name)
        except ValueError:
            continue  # a version tier (or anything non-date) — never re-migrate
        for legacy in sorted(date_dir.glob("*.json")):
            try:
                body = json.loads(legacy.read_text())
            except Exception as exc:  # corrupt legacy file — leave it, skip
                logger.warning("buffett qual migrate: unreadable %s: %s", legacy, exc)
                continue
            body.setdefault("config_version", config_version)
            target = cache_dir / config_version / date_dir.name / legacy.name
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(json.dumps(body, sort_keys=True))
            legacy.unlink()  # move-not-copy: removes the second copy
            migrated += 1
    return migrated


def _record_from_assessment(
    assessment: QualitativeAssessment,
    *,
    used_scuttlebutt: bool,
    computed_at: str,
    config_version: str,
) -> QualRecord:
    return QualRecord(
        moat_type=assessment.moat_type,
        moat_trend=assessment.moat_trend,
        management_candor=assessment.management_candor,
        understandable=assessment.understandable,
        rationale=assessment.rationale,
        used_scuttlebutt=used_scuttlebutt,
        computed_at=computed_at,
        config_version=config_version,
    )


def enrich_qualitative(
    panels: list[BuffettPanel],
    *,
    asof: dt.date,
    scuttlebutt: bool = False,
    cache_dir: Path | None = None,
    assess_one: AssessOne | None = None,
    now_fn: Callable[[], dt.datetime] | None = None,
) -> list[QualRecord | None]:
    """Compute (cached) qualitative records for ``panels``, one per panel.

    One :class:`QualRecord` per panel, or ``None`` when the name has no 10-K to
    reason over. Computes once per UNIQUE ticker. A warm cache entry short-
    circuits the LLM; a fresh success is written to the cache, an all-``None``
    result is not (so it retries next run).
    """
    resolved_assess: AssessOne
    if assess_one is not None:
        resolved_assess = assess_one
    else:
        # Build the scuttlebutt client ONCE for the whole batch (not per panel)
        # and close over it so the injectable seam stays a 3-arg callable.
        sb_client = _build_scuttlebutt_client() if scuttlebutt else None

        def _default_assess(panel: BuffettPanel, asof_: dt.date, scuttle: bool):
            return assess_panel_qualitative(
                panel, asof_, scuttlebutt_client=sb_client if scuttle else None
            )

        resolved_assess = _default_assess

    clock = now_fn if now_fn is not None else (lambda: dt.datetime.now(dt.UTC))
    # Constant across the whole batch (the rubric tag), distinct from the
    # per-batch ``computed_at`` wall-clock stamp.
    cfg = BUFFETT_QUAL_CONFIG_VERSION

    by_ticker: dict[str, QualRecord | None] = {}
    out: list[QualRecord | None] = []
    for panel in panels:
        ticker = panel.ticker.upper()
        if ticker not in by_ticker:
            by_ticker[ticker] = _resolve_one(
                panel,
                asof=asof,
                scuttlebutt=scuttlebutt,
                cache_dir=cache_dir,
                assess_one=resolved_assess,
                computed_at=clock().isoformat(),
                config_version=cfg,
            )
        out.append(by_ticker[ticker])
    return out


def _resolve_one(
    panel: BuffettPanel,
    *,
    asof: dt.date,
    scuttlebutt: bool,
    cache_dir: Path | None,
    assess_one: AssessOne,
    computed_at: str,
    config_version: str,
) -> QualRecord | None:
    ticker = panel.ticker.upper()
    if cache_dir is not None:
        cached = load_cache(
            ticker, asof, cache_dir, config_version=config_version, scuttlebutt=scuttlebutt
        )
        if cached is not None:
            return cached

    try:
        assessment = assess_one(panel, asof, scuttlebutt)
    except Exception as exc:
        # Per-ticker fail-soft: a vendor hiccup / unexpected raise inside the
        # assess op must not abort the whole batch. Treat as "no result" (dashes,
        # not cached -> retried next run).
        logger.warning("buffett qual: assess failed for %s: %s", ticker, exc)
        return None
    if assessment is None:
        return None  # no 10-K — not cached, retried next run (no LLM cost)

    record = _record_from_assessment(
        assessment,
        used_scuttlebutt=scuttlebutt,
        computed_at=computed_at,
        config_version=config_version,
    )
    if cache_dir is not None and _is_real(record):
        write_cache(
            ticker, asof, cache_dir, record, config_version=config_version, scuttlebutt=scuttlebutt
        )
    return record


def stamp_columns(frame: pd.DataFrame, records: dict[str, QualRecord | None]) -> pd.DataFrame:
    """Return ``frame`` with the eight qual columns stamped by ticker.

    ``records`` maps an upper-cased ticker to its :class:`QualRecord` (or
    ``None``). Order + pre-existing columns are preserved; a missing ticker /
    ``None`` record leaves all eight columns null for that row.
    """
    out = frame.copy()
    tickers = [str(t).upper() for t in out["ticker"]] if "ticker" in out.columns else []

    def _col(rec: QualRecord | None, attr: str):
        # Defensive default: a record missing a field (older cache schema)
        # stamps null rather than raising mid-batch.
        return getattr(rec, attr, None) if rec is not None else None

    field_by_col = {
        "buffett_moat_type": "moat_type",
        "buffett_moat_trend": "moat_trend",
        "buffett_management_candor": "management_candor",
        "buffett_understandable": "understandable",
        "buffett_qualitative_rationale": "rationale",
        "buffett_used_scuttlebutt": "used_scuttlebutt",
        "buffett_qual_computed_at": "computed_at",
        "buffett_qual_config_version": "config_version",
    }
    for col, attr in field_by_col.items():
        out[col] = [_col(records.get(t), attr) for t in tickers]
    return out


# Years of 10-K history fed to the qualitative layer (#505).
_QUALITATIVE_YEARS = 3


def assess_panel_qualitative(
    panel: BuffettPanel, asof: dt.date, *, scuttlebutt_client=None
) -> QualitativeAssessment | None:
    """Per-panel qualitative op: fetch the multi-year 10-K, build facts, classify.

    The SINGLE per-candidate implementation shared by the ad-hoc ``buffett lens
    --qualitative`` path and the eager :func:`enrich_qualitative` pass. Returns
    ``None`` when the name has no fetchable 10-K (caller skips + does not cache);
    otherwise the (possibly all-``None``) :class:`QualitativeAssessment` from the
    LLM. Pass a pre-built ``scuttlebutt_client`` to add the web-grounded context
    block; ``None`` skips it. Imports are by module attribute so the CLI tests'
    monkeypatches on ``tenk_grep`` / ``qualitative`` still apply.
    """
    from alphalens_pipeline.experts.buffett import qualitative as qualitative_mod
    from alphalens_pipeline.experts.buffett import scuttlebutt as scuttlebutt_mod
    from alphalens_pipeline.experts.buffett.tenk_sections import split_10k_sections
    from alphalens_pipeline.thematic.verification import tenk_grep

    try:
        multi_year = tenk_grep.fetch_multi_year_10k_texts(
            ticker=panel.ticker, asof=asof, years=_QUALITATIVE_YEARS
        )
    except Exception as exc:
        logger.warning("buffett qual: 10-K fetch failed for %s: %s", panel.ticker, exc)
        multi_year = []
    if not multi_year:
        return None

    sections = split_10k_sections(multi_year[0][1])
    prior_year_risk_factors = [
        (date, split_10k_sections(text).item_1a) for date, text in multi_year[1:]
    ]

    scuttlebutt_text: str | None = None
    if scuttlebutt_client is not None:
        sb = scuttlebutt_mod.fetch_scuttlebutt(panel.ticker, client=scuttlebutt_client)
        scuttlebutt_text = sb.text if sb.ok else None

    facts = {
        "roic_latest": panel.roic_latest,
        "roic_3y_avg": panel.roic_3y_avg,
        "op_margin_latest": panel.op_margin_latest,
        "op_margin_3y_avg": panel.op_margin_3y_avg,
        "net_buyback": panel.net_buyback,
        "peo_to_neo_ratio": panel.peo_to_neo_ratio,
    }
    return qualitative_mod.assess_qualitative(
        ticker=panel.ticker,
        sections=sections,
        facts=facts,
        prior_year_risk_factors=prior_year_risk_factors,
        scuttlebutt=scuttlebutt_text,
    )


def enrich_brief_parquet(
    brief_date: dt.date,
    *,
    briefs_dir: Path | None = None,
    store,
    mcap_fn,
    dividends_fn,
    exec_comp_fn=None,
    scuttlebutt: bool = False,
    cache_dir: Path | None = None,
    assess_one: AssessOne | None = None,
) -> int:
    """Compute eager (cached) qual for the brief's survivors + stamp 8 columns in place.

    Builds one :class:`BuffettPanel` per brief candidate (the quant facts the qual
    prompt injects), computes the cached qualitative records, then re-writes the
    brief parquet with the eight qual columns merged by ticker. Returns the count
    of names that resolved a real (non-empty) qualitative classification.

    The same file is both the panel source (via ``build_comparison`` -> the brief
    loader) and the stamp target — so the columns ride the existing brief-parquet
    -> Django ingest rails. ``store`` / ``mcap_fn`` / ``dividends_fn`` are injected
    exactly as for the lens; ``assess_one`` is injectable for tests.
    """
    from alphalens_pipeline.experts.buffett.comparison import build_comparison

    resolved_dir = briefs_dir if briefs_dir is not None else _DEFAULT_BRIEFS_DIR
    panels = build_comparison(
        brief_date,
        briefs_dir=resolved_dir,
        store=store,
        mcap_fn=mcap_fn,
        dividends_fn=dividends_fn,
        exec_comp_fn=exec_comp_fn,
    )
    records = enrich_qualitative(
        panels,
        asof=brief_date,
        scuttlebutt=scuttlebutt,
        cache_dir=cache_dir if cache_dir is not None else DEFAULT_QUAL_CACHE_DIR,
        assess_one=assess_one,
    )
    by_ticker: dict[str, QualRecord | None] = {
        panel.ticker.upper(): rec for panel, rec in zip(panels, records, strict=True)
    }
    path = Path(resolved_dir) / f"{brief_date.isoformat()}.parquet"
    df = pd.read_parquet(path)
    df = stamp_columns(df, by_ticker)
    df.to_parquet(path, index=False)
    return sum(1 for rec in records if rec is not None and _is_real(rec))


def _build_scuttlebutt_client():
    """Build a PerplexityClient from PERPLEXITY_API_KEY, or ``None`` (fail-soft)."""
    import os

    api_key = os.getenv("PERPLEXITY_API_KEY", "").strip()
    if not api_key:
        logger.warning("buffett qual --scuttlebutt: PERPLEXITY_API_KEY not set — skipping")
        return None
    from alphalens_pipeline.literature_scanner.perplexity_client import PerplexityClient

    return PerplexityClient(api_key=api_key)


__all__ = [
    "BUFFETT_QUAL_CONFIG_VERSION",
    "DEFAULT_QUAL_CACHE_DIR",
    "QUAL_COLUMNS",
    "QualRecord",
    "assess_panel_qualitative",
    "enrich_brief_parquet",
    "enrich_qualitative",
    "load_cache",
    "migrate_legacy_qual_cache",
    "stamp_columns",
    "write_cache",
]
