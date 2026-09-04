"""Layer 3 orchestrator: propose candidates with DeepSeek v4-pro, verify via 3 gates.

For each input theme, the orchestrator (a) asks the LLM for 5-15 candidate
small/mid-cap beneficiaries (see :mod:`theme_mapper`) and (b) verifies each
candidate against three independent gates (the ETF/NPORT gate is designed but
not wired — see ``GATE_NAMES``):

1. **10-K keyword grep** — does the company's most recent 10-K mention the
   theme keywords?
2. **Recent press** — has Polygon news in the last 30 days carried the theme
   keywords for this ticker?
3. **Form-4 insider activity** — net opportunistic buys above threshold over
   the last 90 days (paradigm #11 Cohen-Malloy reuse, αt +2.71 OOS validated).

An **ETF holdings** gate (is the ticker a constituent of any thematic ETF
mapped to this theme, via an NPORT-P parser) is described in the original
design but is **not wired** into ``GATE_NAMES`` / the verify loop today.

A candidate is ``verified=True`` if **any** of the three wired gates passes. Output
is a parquet at ``~/.alphalens/thematic_candidates/{date}.parquet`` with one
row per (theme, ticker).
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from alphalens_pipeline.data.alt_data.polygon_client import (
    PolygonClient,
    get_default_polygon_client,
)
from alphalens_pipeline.data.parquet_io import write_parquet_atomic
from alphalens_pipeline.thematic.mapping import (
    catalyst_resolver,
    channel_assessor,
    proposal_shadow,
    theme_mapper,
)
from alphalens_pipeline.thematic.mapping.catalyst_contract import CatalystPayload
from alphalens_pipeline.thematic.verification import (
    insider,
    mcap_filter,
    recent_press,
    tenk_grep,
)

DEFAULT_MCAP_RANGE = (500_000_000, 10_000_000_000)

logger = logging.getLogger(__name__)

DEFAULT_OUTPUT_DIR = Path.home() / ".alphalens" / "thematic_candidates"
GATE_NAMES = ("tenk", "press", "insider")

# Diversity guardrail: each theme contributes at most _MAX_CANDIDATES_PER_THEME
# rows to the daily brief. If a top-N candidate hard-fails verification, the
# resolver backfills from the next-highest-confidence candidate, bounded by
# _MAX_VERIFY_ATTEMPTS_PER_THEME to keep API budgets predictable.
_MAX_CANDIDATES_PER_THEME = 3
_MAX_VERIFY_ATTEMPTS_PER_THEME = 5

# How many of a theme's in-bracket candidates stage B pays for. Deliberately
# EQUAL to _MAX_VERIFY_ATTEMPTS_PER_THEME rather than a knob of its own: the
# verify stage attempts the top _MAX_VERIFY_ATTEMPTS_PER_THEME by confidence and
# ships at most _MAX_CANDIDATES_PER_THEME, so a candidate ranked below that can
# never reach a brief row and assessing it buys nothing but wall-time. Stage B
# is otherwise unbounded (up to theme_mapper._MAX_CANDIDATES in bracket, times
# channel_assessor._ASSESS_VOTES draws, times max_themes), and map_themes writes
# its parquet ONCE after the whole theme loop — so a run that overruns
# TimeoutStartSec produces no candidates file at all and the next slot restarts
# from zero. Candidates past the cap are stamped ``over_assess_cap``, which is
# distinct from the bracket's ``not_assessed`` and enters no denominator.
_MAX_ASSESS_PER_THEME = _MAX_VERIFY_ATTEMPTS_PER_THEME


# Per-gate wrappers — keep tests patchable through `orchestrator.*` and let
# each gate fail closed if its underlying data path errors.


def _gate_tenk(
    *, ticker: str, theme_keywords: Iterable[str], asof: dt.date, reason: dict | None = None
) -> bool | None:
    return tenk_grep.has_theme_keywords_in_10k(
        ticker=ticker, keywords=theme_keywords, asof=asof, reason=reason
    )


def _gate_press(
    *,
    ticker: str,
    theme_keywords: Iterable[str],
    asof: dt.date,
    polygon_client: PolygonClient | None = None,
    press_df: pd.DataFrame | None = None,
    reason: dict | None = None,
) -> bool | None:
    """Press verification gate with tri-state fall-through (issue #149).

    Decision tree:
    - ``press_df`` is None (batch fetch failed): per-ticker fetch.
    - ``press_df`` is provided and frame matcher returns True/False: trust it.
    - ``press_df`` is provided but frame matcher returns None (no rows for
      this ticker): fall through to per-ticker fetch. Polygon's batch
      firehose sometimes fails to tag a ticker even when articles mention
      it; the per-ticker endpoint covers that gap.
    """
    if press_df is not None:
        result = recent_press.has_theme_in_press_frame(
            ticker=ticker, keywords=theme_keywords, press_df=press_df, reason=reason
        )
        if result is not None:
            return result
    return recent_press.has_theme_in_recent_press(
        ticker=ticker, asof=asof, keywords=theme_keywords, client=polygon_client, reason=reason
    )


def _gate_insider(*, ticker: str, asof: dt.date, reason: dict | None = None) -> bool | None:
    return insider.has_opportunistic_buy(ticker=ticker, asof=asof, reason=reason)


def _safe(name: str, fn, **kwargs) -> tuple[bool | None, dict]:
    """Run a gate function returning ``(tri-state, reason)``.

    Tri-state: ``True`` (qualifies), ``False`` (real negative), ``None`` (could
    not determine — missing data / network error; recorded as ``gates_unknown``).

    ``reason`` (PR-4) is the gate's structured WHY — ``{threshold, actual, unit}``
    — captured via an out-param so an analyst can later see why a candidate
    cleared or missed a gate. It is best-effort: a gate that raises or never
    reaches its computation leaves ``actual`` ``None``.
    """
    reason: dict = {}
    try:
        result = fn(reason=reason, **kwargs)
    except Exception as exc:
        logger.warning("verification gate %s raised: %s", name, exc, exc_info=True)
        return None, reason
    if result is None:
        return None, reason
    return bool(result), reason


def _theme_keywords(theme: str, *, pro_keywords: Iterable[str] | None = None) -> list[str]:
    """Resolve search keywords for the verification gates.

    Pro-supplied ``pro_keywords`` are preferred — they encode the LLM's
    full theme intent (synonyms, abbreviations, common phrasings). The
    naive snake↔space swap is the fallback when Pro returned nothing,
    so gates always have at least the raw theme tokens to match against.

    The fallback is intentionally narrow: it matches a 10-K passage that
    says "quantum computing" against a theme ``quantum_computing``, but
    it will NOT match "artificial intelligence" against a theme
    ``AI development`` — that recall gap is exactly what Pro-supplied
    keywords are for.
    """
    if pro_keywords:
        deduped = list(dict.fromkeys(k for k in pro_keywords if k))
        if deduped:
            return deduped
    raw = str(theme).strip()
    spaced = raw.replace("_", " ")
    return [v for v in dict.fromkeys([raw, spaced]) if v]


def verify_candidate(
    *,
    ticker: str,
    themes: Iterable[str],
    asof: dt.date,
    polygon_client: PolygonClient | None = None,
    theme_keywords: Iterable[str] | None = None,
    press_df: pd.DataFrame | None = None,
) -> dict:
    """Run all four gates against ``(ticker, themes)`` and report which passed.

    ``press_df``, when supplied, is the orchestrator's pre-fetched
    window-wide Polygon news frame; the press gate then runs purely in-memory.
    """
    themes_list = list(themes)
    if theme_keywords is None:
        expanded: list[str] = []
        for t in themes_list:
            expanded.extend(_theme_keywords(t))
        keywords = list(dict.fromkeys(expanded))
    else:
        keywords = list(theme_keywords)

    gates_passed: list[str] = []
    gates_failed: list[str] = []
    gates_unknown: list[str] = []
    gate_reasons: dict[str, dict] = {}

    def _record(name: str, outcome: tuple[bool | None, dict]):
        result, reason = outcome
        if result is True:
            gates_passed.append(name)
        elif result is False:
            gates_failed.append(name)
        else:
            gates_unknown.append(name)
        # Stamp the verdict onto the reason so the JSON is self-describing.
        reason["passed"] = result
        gate_reasons[name] = reason

    _record(
        "tenk",
        _safe(
            "tenk",
            _gate_tenk,
            ticker=ticker,
            theme_keywords=keywords,
            asof=asof,
        ),
    )
    _record(
        "press",
        _safe(
            "press",
            _gate_press,
            ticker=ticker,
            theme_keywords=keywords,
            asof=asof,
            polygon_client=polygon_client,
            press_df=press_df,
        ),
    )
    _record("insider", _safe("insider", _gate_insider, ticker=ticker, asof=asof))

    return {
        "ticker": ticker,
        "gates_passed": gates_passed,
        "gates_failed": gates_failed,
        "gates_unknown": gates_unknown,
        "verified": len(gates_passed) > 0,
        # Structured per-gate WHY (PR-4): {gate: {passed, threshold, actual, unit}}.
        "gate_verdict_json": json.dumps(gate_reasons, sort_keys=True),
    }


def _init_pro_client(api_key: str | None):
    """Build the OpenRouter LLM client once for the whole batch; ``None`` if
    construction fails. The mapper will then lazy-init per call (falling
    back to the process-wide default client).

    Without ``api_key`` this returns the process-wide default client rather
    than ``None``. That default is the ONLY construction path that reads the
    operator's ``ALPHALENS_OPENROUTER_*`` provider pin, so a caller that omits
    the key (the CLI does) gets a pinned batch client instead of an unpinned
    hand-built one. ``api_key`` stays supported for ad-hoc callers that need a
    specific credential; it opts that call out of the pin by construction.
    """
    from alphalens_pipeline.data.alt_data.openrouter_client import (
        OpenRouterClient,
        get_default_openrouter_client,
    )

    try:
        if api_key:
            return OpenRouterClient(api_key=api_key)
        return get_default_openrouter_client()
    except (RuntimeError, ValueError):
        logger.warning("OpenRouterClient construction failed; mapper will lazy-init per call")
        return None


def _fetch_press_window(asof: dt.date, polygon_client: PolygonClient | None) -> pd.DataFrame | None:
    """Pre-fetch the window-wide press frame. ``None`` on outage so callers fall back."""
    if polygon_client is None:
        return None
    try:
        return recent_press.fetch_window_universe(asof=asof, client=polygon_client)
    except Exception as exc:
        logger.warning("press window fetch failed: %s", exc, exc_info=True)
        return None


def _resolve_catalyst(
    theme: str, asof: dt.date, cache: dict[str, CatalystPayload | None]
) -> CatalystPayload | None:
    if theme not in cache:
        try:
            cache[theme] = catalyst_resolver.find_trigger_event(theme=theme, asof=asof)
        except Exception as exc:
            logger.warning("catalyst resolver failed for theme %s: %s", theme, exc, exc_info=True)
            cache[theme] = None
    return cache[theme]


def _build_row(
    *,
    theme: str,
    cand: dict,
    verdict: dict,
    market_cap: float,
    catalyst: CatalystPayload | None,
    keywords: Sequence[str],
    shadow: channel_assessor.ShadowVerdict,
) -> dict:
    """One candidate row, including its channel annotation and the theme's shadow.

    The ``channel_*`` block and the ``shadow_strict_*`` block are ANNOTATIONS.
    Nothing downstream may read them in a filter, a sort key or a score — the
    structural guard lives in
    ``tests/thematic/test_map_themes_channel_shadow.py``.
    """
    return {
        "theme": theme,
        "ticker": cand["ticker"],
        "company_name": cand.get("company_name", ""),
        "rationale": cand.get("rationale", ""),
        "llm_confidence": cand.get("confidence", 0.0),
        # Stage-B channel assessment (11 columns; ``channel_config_version`` is
        # stamped FRAME-WIDE by the driver, which is the only place the run's
        # model is known). ``cand["channel"]`` is the
        # ChannelAssessment ``_assess_channels_for_theme`` wrote onto the same
        # dict; ``None`` renders as the not-assessed shape so every row carries
        # every column.
        **channel_assessor.row_fields(cand.get("channel")),
        # What a STRICT channel gate WOULD have done with this theme. Stamped on
        # every row of the theme so a "refused" theme leaves rows that exist,
        # ship and mature — which is what makes a forward KEPT-vs-REFUSED
        # contrast computable at all. A DIFFERENT estimand from the frozen
        # Stage-1 gate; never pool the two (design memo §5).
        "shadow_strict_verdict": shadow.verdict,
        "shadow_strict_established_n": shadow.established_n,
        "shadow_strict_assessed_n": shadow.assessed_n,
        "shadow_strict_failed_n": shadow.failed_n,
        "shadow_strict_rule_version": channel_assessor.SHADOW_STRICT_RULE_VERSION,
        "market_cap": market_cap,
        "gates_passed": verdict["gates_passed"],
        "gates_passed_str": ",".join(verdict["gates_passed"]),
        "n_gates_passed": len(verdict["gates_passed"]),
        "gates_failed": verdict["gates_failed"],
        "gates_failed_str": ",".join(verdict["gates_failed"]),
        "n_gates_failed": len(verdict["gates_failed"]),
        "gates_unknown": verdict["gates_unknown"],
        "gates_unknown_str": ",".join(verdict["gates_unknown"]),
        "n_gates_unknown": len(verdict["gates_unknown"]),
        "verified": verdict["verified"],
        # Structured per-gate WHY (PR-4): a JSON string {gate: {passed, threshold,
        # actual, unit}}. "" when an older verdict dict predates the field.
        "gate_verdict_json": verdict.get("gate_verdict_json", ""),
        "source_event_url": catalyst.url if catalyst else None,
        "source_event_title": catalyst.title if catalyst else None,
        "source_event_published_at": catalyst.published_at if catalyst else None,
        "theme_search_keywords": list(keywords),
    }


_MAP_THEMES_COLUMNS: tuple[str, ...] = (
    "theme",
    "ticker",
    "company_name",
    "rationale",
    "llm_confidence",
    # Stage-B channel annotation + the derived per-theme shadow verdict.
    # Parquet-only: the Django ingest reads only enumerated model fields, so
    # these ride to ~/.alphalens/thematic_briefs/ and stop there.
    *channel_assessor.CHANNEL_ROW_COLUMNS,
    channel_assessor.CHANNEL_CONFIG_COLUMN,
    "shadow_strict_verdict",
    "shadow_strict_established_n",
    "shadow_strict_assessed_n",
    "shadow_strict_failed_n",
    "shadow_strict_rule_version",
    "market_cap",
    "gates_passed",
    "gates_passed_str",
    "n_gates_passed",
    "gates_failed",
    "gates_failed_str",
    "n_gates_failed",
    "gates_unknown",
    "gates_unknown_str",
    "n_gates_unknown",
    "verified",
    "gate_verdict_json",
    "source_event_url",
    "source_event_title",
    "source_event_published_at",
    "theme_search_keywords",
    # Idempotent-freeze fingerprint (mapper model/prompt/schema/sampling/mcap).
    # A re-run for the same asof reuses this parquet when the token still
    # matches the current config, instead of re-rolling the LLM proposal.
    "mapper_config_version",
    # Selection covariate: how novel was the theme that surfaced this ticker,
    # as ranked by the CLI's truncated head(max_themes). Telemetry only — never
    # feeds selection or ordering — but persisted here so a future N>=30 EDGE
    # attribution pass can join novelty without reconstructing the rollup (the
    # 30-day event window ages out, making after-the-fact recovery lossy). The
    # config-version token pins the window/recent/threshold that produced the
    # score so a future tune of those params keeps pre/post values non-poolable.
    "novelty_rank",
    "novelty_score",
    "novelty_config_version",
)


# ``frame.attrs`` key naming whether :func:`map_themes` re-derived the day's
# candidate set or served the frozen one. Rides on ``attrs`` beside the gauge
# counts because that is the channel this module already uses for per-run
# metadata the parquet schema has no room for.
#
# The caller needs it because the freeze IGNORES the ``themes`` argument: on
# slots 2-6 of the same asof the slate the CLI computed was never acted upon, so
# any record the CLI writes about that slate (the theme rollup, its inclusion
# propensities) would describe a decision that did not happen. Absent key means
# "unknown provenance" — read it with an explicit default and treat a missing
# value as NOT a reuse only when the frame came from this function.
FROZEN_REUSE_ATTR = "frozen_reuse"


# The candidate frame's sort. Promoted from an inline literal to a module
# constant so the structural guard in
# ``tests/thematic/test_map_themes_channel_shadow.py`` can assert that no
# ``channel_*`` column ever joins it — a channel column in the sort would be the
# rejected gate returning as an ordering rule.
_CANDIDATE_SORT_KEYS: tuple[str, ...] = (
    "theme",
    "n_gates_passed",
    "llm_confidence",
    "ticker",
)
_CANDIDATE_SORT_ASCENDING: tuple[bool, ...] = (True, False, False, True)


def _is_misrouted_theme(counts: Mapping[str, int]) -> bool:
    """True when a theme's ANSWERED majority is ``theme_misroute``.

    Answered only: ``grounding_unknown`` and the never-asked sentinels are out of
    both numerator and denominator, exactly as instrument failures are out of the
    shadow denominator. Derived rather than stored, because a stored theme-level
    grounding VERDICT is the shape most likely to be turned into a gate later.
    """
    answered = (
        counts[channel_assessor.GROUNDING_GROUNDED]
        + counts[channel_assessor.GROUNDING_THEME_MISROUTE]
        + counts[channel_assessor.GROUNDING_CANDIDATE_MISFIT]
    )
    if not answered:
        return False
    return counts[channel_assessor.GROUNDING_THEME_MISROUTE] * 2 > answered


def _channel_counts(
    theme_counts: Iterable[dict[str, int]],
    shadows: Iterable[channel_assessor.ShadowVerdict],
) -> dict[str, int]:
    """Run-level channel + shadow tallies for the CLI's Prometheus gauges.

    Emitted on EVERY run, including a quiet day and a frozen-set reuse (all
    zeros), for the same reason the two outcome gauges are: a series that
    disappears on healthy days is indistinguishable from a stopped exporter.
    """
    per_theme = list(theme_counts)
    verdicts = [s.verdict for s in shadows]
    return {
        "channel_established": sum(c[channel_assessor.SUPPORT_ESTABLISHED] for c in per_theme),
        "channel_suggestive": sum(c[channel_assessor.SUPPORT_SUGGESTIVE] for c in per_theme),
        "channel_not_established": sum(
            c[channel_assessor.SUPPORT_NOT_ESTABLISHED] for c in per_theme
        ),
        "channel_assess_failed": sum(c["assess_failed"] for c in per_theme),
        # Grounding: DETECT, STAMP, KEEP, MEASURE. These counters are the whole
        # point of the column — nothing in the pipeline reads them, and a future
        # gate on them needs its own stratified accuracy audit and its own
        # pre-registration (design memo §6).
        "channel_grounded": sum(c[channel_assessor.GROUNDING_GROUNDED] for c in per_theme),
        "channel_theme_misroute": sum(
            c[channel_assessor.GROUNDING_THEME_MISROUTE] for c in per_theme
        ),
        "channel_candidate_misfit": sum(
            c[channel_assessor.GROUNDING_CANDIDATE_MISFIT] for c in per_theme
        ),
        "channel_grounding_unknown": sum(c["grounding_unknown"] for c in per_theme),
        # Themes whose ANSWERED majority says the event is not about the theme.
        # A theme-level count because theme_misroute is candidate-INDEPENDENT:
        # every candidate of a misrouted theme should answer the same way.
        "themes_misrouted": sum(1 for c in per_theme if _is_misrouted_theme(c)),
        "themes_shadow_kept": sum(1 for v in verdicts if v == channel_assessor.SHADOW_KEEP),
        "themes_shadow_refused": sum(1 for v in verdicts if v == channel_assessor.SHADOW_REFUSE),
    }


def _outcome_counts(outcomes: Iterable[theme_mapper.MapperOutcome | None]) -> dict[str, int]:
    """Split one run's per-theme mapper outcomes into declines vs failures.

    Two counts, not five: the gauges answer "is the failure rate rising", and
    the funnel log answers "which kind" (issue #982). ``None`` — the mapper was
    never called because no catalyst resolved — counts as neither, so the
    failure gauge tracks mapper health rather than catalyst-resolution quality.
    """
    declined = sum(1 for o in outcomes if o is theme_mapper.MapperOutcome.DECLINED)
    failed = sum(
        1
        for o in outcomes
        if o is not None
        and o not in (theme_mapper.MapperOutcome.SUCCESS, theme_mapper.MapperOutcome.DECLINED)
    )
    return {"themes_declined": declined, "themes_failed": failed}


def _load_frozen_candidates(out_path: Path, config_version: str) -> pd.DataFrame | None:
    """Return a reusable frozen candidates parquet for this date, else ``None``.

    The freeze is honoured only when the existing parquet (1) is readable,
    (2) carries a ``mapper_config_version`` matching the current config, and
    (3) is non-degraded — at least one gate-verified candidate. A legacy parquet
    without the column, a config mismatch, or an empty/no-gate-verified set is
    treated as a miss so the caller recomputes (anti-poisoned-freeze: a thin
    set from a transient first-run failure must not seal the date). Mirrors the
    buffett-qual successes-only / config-version cache discipline.
    """
    if not out_path.exists():
        return None
    try:
        df = pd.read_parquet(out_path)
    except Exception as exc:  # corrupt / partial file -> recompute
        logger.warning("map_themes: unreadable frozen parquet %s: %s -> recomputing", out_path, exc)
        return None
    if "mapper_config_version" not in df.columns:
        return None  # pre-freeze parquet
    versions = set(df["mapper_config_version"].dropna().unique())
    if versions != {config_version}:
        logger.info(
            "map_themes: frozen config_version mismatch (%s != %s) -> recomputing",
            versions or "{}",
            config_version,
        )
        return None
    if df.empty or "verified" not in df.columns or not bool(df["verified"].astype(bool).any()):
        logger.info("map_themes: frozen set degraded (empty / no gate-verified) -> recomputing")
        return None
    return df


# Ceiling on how many dropped tickers the per-theme funnel line spells out. The
# list is otherwise bounded only INCIDENTALLY, by ``theme_mapper._MAX_CANDIDATES``
# in another module — raising that constant must not silently turn one INFO line
# into a wall of text. The overflow count is still reported.
_MAX_LOGGED_DROPPED_TICKERS = 10

_PROPOSAL_FUNNEL_COLUMNS = (
    "asof",
    "theme",
    "ticker",
    "company_name",
    "llm_confidence",
    "channel_support_status",
    # The grounding VALUE rides the funnel; the quote, reason and agree_n do not.
    # Off-bracket rows are never assessed, and the in-bracket detail lives in the
    # candidates parquet.
    "channel_grounding_status",
    "channel_type",
    "channel_confidence",
    # Without these two, a row whose assessment DIED is byte-identical to a
    # row the model genuinely answered "unverified" — and this file is the
    # only on-disk record of the off-bracket and never-shipped proposals, so
    # it is what a later recall / crowd-out audit reads.
    "channel_assessment_outcome",
    "channel_vote_valid_n",
    "shadow_strict_verdict",
    "channel_config_version",
    "market_cap",
    "bracket_verdict",
    "catalyst_url",
    "catalyst_event_type",
    "mapper_config_version",
)

_THEME_DECISION_COLUMNS = (
    "asof",
    "theme",
    "catalyst_url",
    "catalyst_event_type",
    "mapper_outcome",
    "decline_reason",
    "n_proposed",
    "n_in_bracket",
    "n_established",
    "n_suggestive",
    "n_not_established",
    "n_grounded",
    "n_theme_misroute",
    "n_candidate_misfit",
    "n_grounding_unknown",
    "n_assess_failed",
    "n_over_assess_cap",
    "shadow_strict_verdict",
    "shadow_strict_rule_version",
    "mapper_config_version",
    "channel_config_version",
)

# Recorded in the theme-decisions sidecar when the theme never reached the
# mapper because no catalyst event resolved. Distinct from every MapperOutcome:
# a skip is neither a decline nor a mapper failure.
_NO_CATALYST_OUTCOME = "no_catalyst"


def _funnel_row(
    *,
    theme: str,
    cand: dict,
    verdict: mcap_filter.McapVerdict | None,
    catalyst: CatalystPayload,
    shadow: channel_assessor.ShadowVerdict,
) -> dict:
    """One PRE-bracket proposal, with the verdict that decided its fate.

    ``verdict`` is ``None`` only if the classifier somehow skipped the ticker;
    that is recorded as :data:`~mcap_filter.NO_MCAP` rather than dropped, so the
    row count always equals the proposal count and "how many did the model
    actually propose" stays answerable from this file alone.

    An OFF-BRACKET proposal never reached the assessor, so it renders as
    ``channel_support_status="not_assessed"`` — never as the bottom support
    level. Merging the
    two would make "the bracket dropped it" indistinguishable from "the model
    could not name a chain", and the shadow verdict's denominator meaningless.
    """
    assessment = cand.get("channel")
    fields = channel_assessor.row_fields(assessment)
    return {
        "theme": theme,
        "ticker": cand["ticker"],
        "company_name": cand.get("company_name", ""),
        # Same default as ``_build_row``: a model that omits confidence must not
        # read as null here and 0.0 in the candidates parquet for the same name.
        # (The proposal-shadow builder keeps its own None default on purpose —
        # its rows feed a pre-registered measurement and must not shift.)
        "llm_confidence": cand.get("confidence", 0.0),
        "channel_support_status": fields["channel_support_status"],
        "channel_grounding_status": fields["channel_grounding_status"],
        "channel_type": fields["channel_type"],
        "channel_confidence": fields["channel_confidence"],
        "channel_assessment_outcome": fields["channel_assessment_outcome"],
        "channel_vote_valid_n": fields["channel_vote_valid_n"],
        "shadow_strict_verdict": shadow.verdict,
        "market_cap": verdict.market_cap if verdict is not None else None,
        "bracket_verdict": verdict.verdict if verdict is not None else mcap_filter.NO_MCAP,
        "catalyst_url": catalyst.url,
        "catalyst_event_type": catalyst.event_type,
    }


def _write_proposal_funnel_best_effort(
    asof: dt.date,
    funnel_rows: list[dict],
    config_version: str,
    channel_version: str,
    out_dir: Path,
) -> None:
    """Write the pre-bracket proposal funnel; swallow any failure.

    Sibling of the candidates parquet, and DELIBERATELY not folded into
    ``proposal_shadow``: that file feeds a pre-registered head-to-head whose rows
    are post-mcap by definition, so widening it would corrupt the measurement.
    Telemetry only — the daily thematic build must never abort because the funnel
    could not be written.
    """
    if not funnel_rows:
        return
    try:
        frame = pd.DataFrame(funnel_rows)
        frame["asof"] = asof.isoformat()
        frame["mapper_config_version"] = config_version
        frame[channel_assessor.CHANNEL_CONFIG_COLUMN] = channel_version
        # ``reindex`` below fixes the schema, which also means a renamed or
        # misspelled key in ``_funnel_row`` would ship as a silently all-null
        # column instead of failing. Say so once, at WARNING, rather than
        # raising — this is telemetry and must not abort the build.
        missing = [c for c in _PROPOSAL_FUNNEL_COLUMNS if c not in frame.columns]
        if missing:
            logger.warning(
                "map_themes %s: proposal-funnel rows are missing %s — the column(s) "
                "will be written all-null; _funnel_row and _PROPOSAL_FUNNEL_COLUMNS "
                "have drifted apart",
                asof.isoformat(),
                missing,
            )
        frame = frame.reindex(columns=list(_PROPOSAL_FUNNEL_COLUMNS))
        funnel_dir = Path(out_dir) / "proposal_funnel"
        funnel_dir.mkdir(parents=True, exist_ok=True)
        write_parquet_atomic(frame, funnel_dir / f"{asof.isoformat()}.parquet", index=False)
    except Exception:
        logger.warning(
            "map_themes %s: proposal-funnel write failed (telemetry only, ignored)",
            asof.isoformat(),
            exc_info=True,
        )


@dataclass(frozen=True, slots=True)
class ThemeProposal:
    """One theme's stage-A output, before the channel assessment.

    ``candidates`` holds the SAME dict objects as ``proposed`` (an in-bracket
    subset, re-sorted by confidence), so an annotation written onto a candidate
    dict is visible to the funnel builder for free.
    """

    proposed: list[dict]
    verdicts: list[mcap_filter.McapVerdict]  # 1:1 positional with ``proposed``
    candidates: list[dict]
    in_bracket: dict[str, float]
    keywords: list[str]
    outcome: theme_mapper.MapperOutcome
    decline_reason: str


def _propose_and_bracket(
    *,
    theme: str,
    catalyst: CatalystPayload,
    api_key: str | None,
    pro_client,
    min_cap: int,
    max_cap: int,
    asof: dt.date,
    model: str | None = None,
    listing_root: Path | None = None,
) -> ThemeProposal:
    """Stage-A proposal → real-time mcap filter → keyword harvest.

    ``catalyst`` is the theme's resolved trigger event and is REQUIRED — the
    proposal reasons from the event, not from the theme word. Typed
    non-optional so that "a proposal made without a grounded event" is
    unrepresentable; :func:`_rows_for_theme` already hard-returns before this
    call when nothing resolved.

    An empty ``candidates`` list signals "nothing further to do for this theme";
    ``outcome`` says WHETHER THAT WAS AN ANSWER OR A LOSS, which the list itself
    cannot (issue #982).

    The channel assessment deliberately runs AFTER this function, on the
    in-bracket subset only: the bracket is the largest sink (17 of 19 proposals
    dropped on 2026-08-05) and only in-bracket names can ever produce a ladder
    outcome, so only they can carry the forward contrast. Off-bracket proposals
    still get a funnel row, marked ``not_assessed``.
    """
    proposal = theme_mapper.propose_candidates(
        theme=theme,
        catalyst=catalyst,
        api_key=api_key,
        llm_client=pro_client,
        model=model or theme_mapper.DEFAULT_MODEL,
    )
    outcome = proposal["outcome"]
    decline_reason = proposal.get("decline_reason") or ""
    proposed = proposal.get("candidates") or []
    keywords = _theme_keywords(theme, pro_keywords=proposal.get("search_keywords") or [])
    if not proposed:
        # One funnel line per theme, and it has to be self-contained: an
        # operator reading `journalctl` must not have to cross-reference the
        # mapper's own log line to tell a working refusal from a lost theme.
        if outcome is theme_mapper.MapperOutcome.DECLINED:
            logger.info(
                "map_themes %s: theme %r funnel — proposed 0 (model declined: %r)",
                asof.isoformat(),
                theme,
                decline_reason,
            )
        else:
            # WARNING, not INFO: the theme was LOST. Since the event-conditioned
            # prompt made "0 candidates" a legitimate answer, a failure no longer
            # stands out by being unusual and needs the level to carry it.
            logger.warning(
                "map_themes %s: theme %r funnel — proposed 0 (MAPPER FAILED, outcome=%s)",
                asof.isoformat(),
                theme,
                outcome.value,
            )
        return ThemeProposal(
            proposed=[],
            verdicts=[],
            candidates=[],
            in_bracket={},
            keywords=keywords,
            outcome=outcome,
            decline_reason=decline_reason,
        )
    verdicts = mcap_filter.classify_by_mcap(
        [c["ticker"] for c in proposed],
        min_cap=min_cap,
        max_cap=max_cap,
        asof=asof,
        listing_root=listing_root,
    )
    in_bracket = {
        v.ticker: v.market_cap
        for v in verdicts
        if v.verdict == mcap_filter.IN_BRACKET and v.market_cap is not None
    }
    candidates = sorted(
        [c for c in proposed if c["ticker"] in in_bracket],
        key=lambda c: c.get("confidence", 0.0),
        reverse=True,
    )
    # Funnel telemetry: the mcap stage is otherwise SILENT when it drops
    # everything (nothing reaches the later kept/dropped log), which is exactly
    # how a yfinance/mcap outage collapses the whole day's briefs to zero
    # candidates invisibly. Log proposed -> in-bracket per theme so a mass
    # off-bracket / no-mcap drop is diagnosable at a glance. The dropped names
    # are NAMED, not just counted: "17 dropped" cannot tell a mega-cap-only
    # answer apart from a yfinance outage, but `NVDA=too_big` versus
    # `NVDA=no_mcap` can, without re-running the funnel against live yfinance.
    dropped = [f"{v.ticker}={v.verdict}" for v in verdicts if v.verdict != mcap_filter.IN_BRACKET]
    shown = dropped[:_MAX_LOGGED_DROPPED_TICKERS]
    overflow = len(dropped) - len(shown)
    if shown:
        overflow_note = f" (+{overflow} more)" if overflow else ""
        detail = f": {', '.join(shown)}{overflow_note}"
    else:
        detail = ""
    logger.info(
        "map_themes %s: theme %r funnel — proposed %d, in mcap bracket %d (%d dropped off-bracket / no mcap%s)",
        asof.isoformat(),
        theme,
        len(proposed),
        len(candidates),
        len(proposed) - len(candidates),
        detail,
    )
    return ThemeProposal(
        proposed=proposed,
        verdicts=list(verdicts),
        candidates=candidates,
        in_bracket=in_bracket,
        keywords=keywords,
        outcome=outcome,
        decline_reason=decline_reason,
    )


def _assess_channels_for_theme(
    *,
    theme: str,
    catalyst: CatalystPayload,
    candidates: list[dict],
    pro_client,
    api_key: str | None,
    model: str | None,
    asof: dt.date,
) -> tuple[channel_assessor.ShadowVerdict, dict[str, int]]:
    """Annotate each in-bracket candidate with its channel assessment.

    Writes the :class:`~channel_assessor.ChannelAssessment` onto the candidate
    dict under ``"channel"`` and returns ``(shadow_verdict, status_counts)``.

    PURE ENRICHMENT: this function never removes a candidate and never reorders
    the list. ``assess_candidates`` returns one result per input in input order
    for every outcome, including a total outage, so the positional write below
    cannot silently misalign.

    Only the top :data:`_MAX_ASSESS_PER_THEME` by stage-A confidence are sent to
    the model — see that constant. The remainder are ANNOTATED, not dropped:
    they keep their row, their funnel line and their place in the list, marked
    ``over_assess_cap``.
    """
    assessed_head = candidates[:_MAX_ASSESS_PER_THEME]
    over_cap_tail = candidates[_MAX_ASSESS_PER_THEME:]
    assessments = channel_assessor.assess_candidates(
        theme=theme,
        catalyst=catalyst,
        candidates=assessed_head,
        api_key=api_key,
        llm_client=pro_client,
        model=model or theme_mapper.DEFAULT_MODEL,
    )
    assessments = list(assessments) + [channel_assessor.over_assess_cap() for _ in over_cap_tail]
    for cand, assessment in zip(candidates, assessments, strict=True):
        cand["channel"] = assessment
    shadow = channel_assessor.shadow_strict_verdict(assessments)
    counts = channel_assessor.status_counts(assessments)
    # A SECOND line, deliberately not appended to the funnel line above: the
    # operator's `grep 'funnel —'` recipe and the tests that pin its exact
    # substrings must both keep working.
    logger.info(
        "map_themes %s: theme %r channel — established %d, suggestive %d, "
        "not_established %d, failed %d, over cap %d -> shadow=%s",
        asof.isoformat(),
        theme,
        counts[channel_assessor.SUPPORT_ESTABLISHED],
        counts[channel_assessor.SUPPORT_SUGGESTIVE],
        counts[channel_assessor.SUPPORT_NOT_ESTABLISHED],
        counts["assess_failed"],
        len(over_cap_tail),
        shadow.verdict,
    )
    # A THIRD line, again deliberately separate: the operator's `grep 'channel —'`
    # recipe and the tests that pin its exact substrings must both keep working.
    logger.info(
        "map_themes %s: theme %r grounding — grounded %d, theme_misroute %d, "
        "candidate_misfit %d, unknown %d",
        asof.isoformat(),
        theme,
        counts[channel_assessor.GROUNDING_GROUNDED],
        counts[channel_assessor.GROUNDING_THEME_MISROUTE],
        counts[channel_assessor.GROUNDING_CANDIDATE_MISFIT],
        counts["grounding_unknown"],
    )
    if _is_misrouted_theme(counts):
        # A PIPELINE DEFECT page, never a trading signal: the answered majority
        # says the event this theme ran on is not about the theme. Attributable
        # upstream — to extraction's theme tagging, or to catalyst_resolver
        # picking one event out of a multi-event article (EPIC #974 / #976).
        #
        # NOTHING IS DROPPED ON IT. The rows ship, in the same order, with the
        # status recorded; this line exists so the defect is visible in the
        # journal instead of being inferred from the parquet after the fact.
        logger.warning(
            "map_themes %s: theme %r — the answered majority says theme_misroute "
            "(%d of %d answered): the event is probably not about this theme. "
            "This is an upstream extraction / catalyst-resolution defect, not a "
            "signal about the candidates, and no row was dropped for it. Read it "
            "beside the within-theme agreement before acting on it.",
            asof.isoformat(),
            theme,
            counts[channel_assessor.GROUNDING_THEME_MISROUTE],
            counts[channel_assessor.GROUNDING_GROUNDED]
            + counts[channel_assessor.GROUNDING_THEME_MISROUTE]
            + counts[channel_assessor.GROUNDING_CANDIDATE_MISFIT],
        )
    if over_cap_tail:
        # The cap starts biting exactly when the crowd-out repair succeeds, so
        # it must be visible in the journal rather than inferred from the
        # parquet after the fact.
        logger.warning(
            "map_themes %s: theme %r had %d in-bracket candidates past the "
            "assessment cap of %d (unassessed, still shipped): %s",
            asof.isoformat(),
            theme,
            len(over_cap_tail),
            _MAX_ASSESS_PER_THEME,
            ", ".join(str(c["ticker"]) for c in over_cap_tail[:_MAX_LOGGED_DROPPED_TICKERS]),
        )
    return shadow, counts


def _funnel_rows_for_theme(
    *,
    theme: str,
    proposal: ThemeProposal,
    catalyst: CatalystPayload,
    shadow: channel_assessor.ShadowVerdict,
) -> list[dict]:
    """One funnel row per PRE-bracket proposal, carrying its channel annotation.

    Zip POSITIONALLY, not through a {ticker: verdict} dict. The model can
    propose the same ticker twice, and a dict would collapse both rows onto the
    LAST verdict — so a duplicate whose two mcap lookups disagreed (a cache
    write landing between them, or the PIT->live fallback firing on one call
    only) would record a verdict that never applied to the first row.
    ``classify_by_mcap`` returns one verdict per input position, so the zip is
    exact and a length mismatch would surface here rather than silently writing
    a plausible-looking NO_MCAP row.
    """
    return [
        _funnel_row(theme=theme, cand=c, verdict=v, catalyst=catalyst, shadow=shadow)
        for c, v in zip(proposal.proposed, proposal.verdicts, strict=True)
    ]


def _verify_candidates_for_theme(
    *,
    theme: str,
    candidates: list[dict],
    in_bracket: dict[str, float],
    keywords: list[str],
    catalyst: CatalystPayload | None,
    asof: dt.date,
    polygon_client: PolygonClient | None,
    press_df,
    keep_unverified: bool,
    shadow: channel_assessor.ShadowVerdict,
) -> tuple[list[dict], int, int]:
    """Run the 4-gate verify on each candidate with diversity cap + backfill.

    Candidates arrive sorted by ``llm_confidence`` desc. The loop keeps up
    to ``_MAX_CANDIDATES_PER_THEME`` rows per theme; on hard-fail, it pulls
    the next-highest-confidence candidate (backfill), capped at
    ``_MAX_VERIFY_ATTEMPTS_PER_THEME`` total verify calls. Without the
    backfill, a single failed gate would silently shrink a theme to 2 rows;
    without the attempt cap, a fully-broken external API could burn the
    entire mapper batch on retries.

    Returns (kept rows, dropped count, dropped-all-unknown count). The
    second counter tracks candidates where every gate returned UNKNOWN
    (typically Polygon outage or yfinance miss), distinct from a real
    failed-gate rejection.
    """
    rows: list[dict] = []
    dropped = 0
    dropped_all_unknown = 0
    attempts = 0
    for cand in candidates:
        if len(rows) >= _MAX_CANDIDATES_PER_THEME:
            break
        if attempts >= _MAX_VERIFY_ATTEMPTS_PER_THEME:
            break
        attempts += 1
        verdict = verify_candidate(
            ticker=cand["ticker"],
            themes=[theme],
            asof=asof,
            polygon_client=polygon_client,
            theme_keywords=keywords,
            press_df=press_df,
        )
        if not verdict["verified"] and not keep_unverified:
            dropped += 1
            if len(verdict["gates_unknown"]) == len(GATE_NAMES):
                dropped_all_unknown += 1
            continue
        rows.append(
            _build_row(
                theme=theme,
                cand=cand,
                verdict=verdict,
                market_cap=in_bracket[cand["ticker"]],
                catalyst=catalyst,
                keywords=keywords,
                shadow=shadow,
            )
        )
    return rows, dropped, dropped_all_unknown


@dataclass(frozen=True, slots=True)
class ThemeDecision:
    """One row of the theme-decisions sidecar.

    Exists because a stage-A decline and a no-catalyst skip otherwise leave ZERO
    trace on disk — the exact defect that made the ISO 40-42 forward window
    unmeasurable, since a refused theme produced no candidate row, no brief row
    and no ladder outcome to compare against.
    """

    theme: str
    catalyst_url: str | None
    catalyst_event_type: str | None
    mapper_outcome: str
    decline_reason: str
    n_proposed: int
    n_in_bracket: int
    n_established: int
    n_suggestive: int
    n_not_established: int
    # Grounding counts sit BESIDE the shadow verdict rather than inside it: the
    # shadow replays the OLD gate, which had no grounding concept, so folding
    # them in would change the estimand being shadowed. Stamped here so any
    # offline re-cut is possible without new LLM calls. No theme-level grounding
    # VERDICT column and no second rule token — it is fully re-derivable from
    # these four, and a stored verdict field is the shape most likely to become
    # a gate.
    n_grounded: int
    n_theme_misroute: int
    n_candidate_misfit: int
    n_grounding_unknown: int
    n_assess_failed: int
    n_over_assess_cap: int
    shadow_strict_verdict: str

    def to_row(self) -> dict:
        return {
            "theme": self.theme,
            "catalyst_url": self.catalyst_url,
            "catalyst_event_type": self.catalyst_event_type,
            "mapper_outcome": self.mapper_outcome,
            "decline_reason": self.decline_reason,
            "n_proposed": self.n_proposed,
            "n_in_bracket": self.n_in_bracket,
            "n_established": self.n_established,
            "n_suggestive": self.n_suggestive,
            "n_not_established": self.n_not_established,
            "n_grounded": self.n_grounded,
            "n_theme_misroute": self.n_theme_misroute,
            "n_candidate_misfit": self.n_candidate_misfit,
            "n_grounding_unknown": self.n_grounding_unknown,
            "n_assess_failed": self.n_assess_failed,
            "n_over_assess_cap": self.n_over_assess_cap,
            "shadow_strict_verdict": self.shadow_strict_verdict,
            "shadow_strict_rule_version": channel_assessor.SHADOW_STRICT_RULE_VERSION,
        }


_EMPTY_COUNTS = {
    channel_assessor.SUPPORT_ESTABLISHED: 0,
    channel_assessor.SUPPORT_SUGGESTIVE: 0,
    channel_assessor.SUPPORT_NOT_ESTABLISHED: 0,
    "assess_failed": 0,
    channel_assessor.GROUNDING_GROUNDED: 0,
    channel_assessor.GROUNDING_THEME_MISROUTE: 0,
    channel_assessor.GROUNDING_CANDIDATE_MISFIT: 0,
    "grounding_unknown": 0,
}


def _decision_for(
    *,
    theme: str,
    catalyst: CatalystPayload | None,
    proposal: ThemeProposal | None,
    shadow: channel_assessor.ShadowVerdict,
    counts: dict[str, int],
) -> ThemeDecision:
    return ThemeDecision(
        theme=theme,
        catalyst_url=catalyst.url if catalyst else None,
        catalyst_event_type=catalyst.event_type if catalyst else None,
        mapper_outcome=proposal.outcome.value if proposal else _NO_CATALYST_OUTCOME,
        decline_reason=proposal.decline_reason if proposal else "",
        n_proposed=len(proposal.proposed) if proposal else 0,
        n_in_bracket=len(proposal.candidates) if proposal else 0,
        n_established=counts[channel_assessor.SUPPORT_ESTABLISHED],
        n_suggestive=counts[channel_assessor.SUPPORT_SUGGESTIVE],
        n_not_established=counts[channel_assessor.SUPPORT_NOT_ESTABLISHED],
        n_grounded=counts[channel_assessor.GROUNDING_GROUNDED],
        n_theme_misroute=counts[channel_assessor.GROUNDING_THEME_MISROUTE],
        n_candidate_misfit=counts[channel_assessor.GROUNDING_CANDIDATE_MISFIT],
        n_grounding_unknown=counts["grounding_unknown"],
        n_assess_failed=counts["assess_failed"],
        # The cap starts biting exactly when the crowd-out repair succeeds, so
        # the truncation is recorded per theme rather than inferred later.
        n_over_assess_cap=max(
            0, (len(proposal.candidates) if proposal else 0) - _MAX_ASSESS_PER_THEME
        ),
        shadow_strict_verdict=shadow.verdict,
    )


@dataclass(frozen=True, slots=True)
class ThemeResult:
    """Everything :func:`map_themes` needs back from one theme."""

    rows: list[dict]
    dropped: int
    dropped_unknown: int
    proposals: list[dict]
    outcome: theme_mapper.MapperOutcome | None
    decision: ThemeDecision
    funnel_rows: list[dict]
    counts: dict[str, int]
    shadow: channel_assessor.ShadowVerdict


def _rows_for_theme(
    theme: str,
    *,
    asof: dt.date,
    catalyst_cache: dict[str, CatalystPayload | None],
    api_key: str | None,
    pro_client,
    min_cap: int,
    max_cap: int,
    model: str | None,
    polygon_client: PolygonClient | None,
    press_df: pd.DataFrame | None,
    keep_unverified: bool,
    listing_root: Path | None = None,
) -> ThemeResult:
    """Resolve → propose → bracket → assess → verify one theme.

    ``proposals`` is the LLM's **pre-gate** candidate set (post-mcap, before the
    verification gates that ``rows`` survives) — captured for the V-forward
    proposal-shadow log, and deliberately NOT widened with channel keys: that
    file feeds a pre-registered head-to-head whose row shape must not move.

    ``outcome`` is the mapper's :class:`MapperOutcome`, or ``None`` when the
    mapper was never called because no catalyst event resolved — a skip is
    neither a decline nor a mapper failure, so it must not land in either
    counter. The ``decision`` carries that same skip to the sidecar, where it IS
    recorded, under ``mapper_outcome="no_catalyst"``.
    """
    catalyst = _resolve_catalyst(theme, asof, catalyst_cache)
    if not catalyst:
        # UI requires source_event_url for provenance. If the theme's events
        # are all noise (e.g. ``discounts`` → 100% promo, stripped by
        # NOISE_EVENT_TYPES), skip the theme rather than burn a Pro call to
        # emit link-less rows.
        logger.info(
            "map_themes %s: skipping theme %r (no catalyst event in window)",
            asof.isoformat(),
            theme,
        )
        shadow = channel_assessor.ShadowVerdict(channel_assessor.SHADOW_REFUSE, 0, 0, 0)
        return ThemeResult(
            rows=[],
            dropped=0,
            dropped_unknown=0,
            proposals=[],
            outcome=None,
            decision=_decision_for(
                theme=theme,
                catalyst=None,
                proposal=None,
                shadow=shadow,
                counts=dict(_EMPTY_COUNTS),
            ),
            funnel_rows=[],
            counts=dict(_EMPTY_COUNTS),
            shadow=shadow,
        )
    proposal = _propose_and_bracket(
        theme=theme,
        # The SAME payload the emitted rows are stamped with (see
        # ``_build_row``), so the article the card cites as provenance is the
        # article the model reasoned from — never re-resolve it here.
        catalyst=catalyst,
        api_key=api_key,
        pro_client=pro_client,
        min_cap=min_cap,
        max_cap=max_cap,
        asof=asof,
        model=model,
        listing_root=listing_root,
    )
    if not proposal.candidates:
        # Nothing in bracket: no assessment to pay for, but the off-bracket
        # proposals still get funnel rows (marked not_assessed) and the theme
        # still gets a decision row.
        shadow = channel_assessor.ShadowVerdict(channel_assessor.SHADOW_REFUSE, 0, 0, 0)
        counts = dict(_EMPTY_COUNTS)
        return ThemeResult(
            rows=[],
            dropped=0,
            dropped_unknown=0,
            proposals=[],
            outcome=proposal.outcome,
            decision=_decision_for(
                theme=theme,
                catalyst=catalyst,
                proposal=proposal,
                shadow=shadow,
                counts=counts,
            ),
            funnel_rows=_funnel_rows_for_theme(
                theme=theme, proposal=proposal, catalyst=catalyst, shadow=shadow
            ),
            counts=counts,
            shadow=shadow,
        )
    shadow, counts = _assess_channels_for_theme(
        theme=theme,
        catalyst=catalyst,
        candidates=proposal.candidates,
        pro_client=pro_client,
        api_key=api_key,
        model=model,
        asof=asof,
    )
    proposals = [
        {
            "theme": theme,
            "ticker": cand["ticker"],
            "llm_confidence": cand.get("confidence"),
        }
        for cand in proposal.candidates
    ]
    rows, dropped, dropped_unknown = _verify_candidates_for_theme(
        theme=theme,
        candidates=proposal.candidates,
        in_bracket=proposal.in_bracket,
        keywords=proposal.keywords,
        catalyst=catalyst,
        asof=asof,
        polygon_client=polygon_client,
        press_df=press_df,
        keep_unverified=keep_unverified,
        shadow=shadow,
    )
    return ThemeResult(
        rows=rows,
        dropped=dropped,
        dropped_unknown=dropped_unknown,
        proposals=proposals,
        outcome=proposal.outcome,
        decision=_decision_for(
            theme=theme,
            catalyst=catalyst,
            proposal=proposal,
            shadow=shadow,
            counts=counts,
        ),
        funnel_rows=_funnel_rows_for_theme(
            theme=theme, proposal=proposal, catalyst=catalyst, shadow=shadow
        ),
        counts=counts,
        shadow=shadow,
    )


def _write_theme_decisions_best_effort(
    asof: dt.date,
    decisions: list[ThemeDecision],
    config_version: str,
    channel_version: str,
    out_dir: Path,
) -> None:
    """Write one row per theme the driver touched; swallow any failure.

    Sibling of the candidates parquet, written exactly like the proposal-funnel:
    telemetry only, and the daily thematic build must never abort because it
    could not be written.
    """
    if not decisions:
        return
    try:
        frame = pd.DataFrame([d.to_row() for d in decisions])
        frame["asof"] = asof.isoformat()
        frame["mapper_config_version"] = config_version
        frame["channel_config_version"] = channel_version
        missing = [c for c in _THEME_DECISION_COLUMNS if c not in frame.columns]
        if missing:
            logger.warning(
                "map_themes %s: theme-decision rows are missing %s — the column(s) "
                "will be written all-null; ThemeDecision.to_row and "
                "_THEME_DECISION_COLUMNS have drifted apart",
                asof.isoformat(),
                missing,
            )
        frame = frame.reindex(columns=list(_THEME_DECISION_COLUMNS))
        decisions_dir = Path(out_dir) / "theme_decisions"
        decisions_dir.mkdir(parents=True, exist_ok=True)
        write_parquet_atomic(frame, decisions_dir / f"{asof.isoformat()}.parquet", index=False)
    except Exception:
        logger.warning(
            "map_themes %s: theme-decisions write failed (telemetry only, ignored)",
            asof.isoformat(),
            exc_info=True,
        )


def _write_proposal_shadow_best_effort(
    asof: dt.date, llm_proposals: list[dict], config_version: str, out_dir: Path
) -> None:
    """Write the V-forward proposal-shadow parquet; swallow any failure.

    The shadow is written under ``out_dir / "proposal_shadow"`` — a sibling of
    the candidates parquet it mirrors — so it inherits the caller's output
    location (production ``~/.alphalens/thematic_candidates/proposal_shadow``;
    tests writing to a tmp dir stay hermetic). Telemetry only — the daily
    thematic build must never abort because the shadow log could not be written.
    """
    if not llm_proposals:
        return
    try:
        proposal_shadow.write_proposal_shadow(
            asof,
            llm_proposals,
            out_dir=Path(out_dir) / "proposal_shadow",
            mapper_config_version=config_version,
        )
    except Exception:
        logger.warning(
            "map_themes %s: proposal-shadow write failed (telemetry only, ignored)",
            asof.isoformat(),
            exc_info=True,
        )


def map_themes(
    *,
    themes: Iterable[str],
    asof: dt.date,
    api_key: str | None = None,
    polygon_api_key: str | None = None,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    keep_unverified: bool = False,
    market_cap_range: tuple[int, int] = DEFAULT_MCAP_RANGE,
    rebuild: bool = False,
    model: str | None = None,
    theme_novelty: Mapping[str, tuple[int, float]] | None = None,
    novelty_config_version: str | None = None,
    listing_store_root: Path | None = None,
) -> pd.DataFrame:
    """For each theme, propose candidates, post-filter by real-time mcap, then verify.

    ``listing_store_root`` (#1074): the grouped-daily store root for the
    deterministic listing pre-check in the mcap bracket. ``None`` (default)
    disables the check — the production CLI wires
    ``rs_history.DEFAULT_RS_HISTORY_ROOT`` in, so library callers and tests
    never read the operator's home store implicitly.

    The DeepSeek v4-pro client is built ONCE for the whole batch (avoid per-theme
    handshake), and the Polygon news window is fetched ONCE for all
    candidates (avoid per-candidate 5-req/min rate-limit sleep). After Pro
    returns candidates, ``mcap_filter.filter_by_mcap`` drops anything outside
    ``market_cap_range`` via yfinance — the LLM cannot do this reliably
    because its mcap snapshot is stuck at training-cutoff prices. Writes a
    unified parquet to ``output_dir / {asof}.parquet`` and returns it.
    """
    # NOT re-derived from OPENROUTER_API_KEY when omitted: that fallback made
    # "caller passed no key" indistinguishable from "caller passed one", so
    # every run built a hand-keyed client and silently opted out of the
    # operator's ALPHALENS_OPENROUTER_* provider pin. Omitted now means
    # "use the process-wide default client", which reads the same env var for
    # the key AND applies the pin. See :func:`_init_pro_client`.
    #
    # The legacy ``polygon_api_key`` parameter is preserved for source-compat
    # with call sites that still pass it (``alphalens_cli/commands/thematic.py``,
    # ``scripts/replay_nvda_qubt.py``, several unit tests). When provided
    # explicitly, build a fresh PolygonClient from that key directly (bypasses
    # env lookup so tests don't need to mutate environment state). When absent
    # but ``POLYGON_API_KEY`` is in env, fall through to the lazy singleton.
    # When neither is present, run with ``polygon_client=None`` — the press
    # gate then short-circuits into batch-skip + per-ticker fallback (same as
    # the historical "no key" code path).
    if polygon_api_key:
        polygon_client = PolygonClient(polygon_api_key)
    elif os.environ.get("POLYGON_API_KEY"):
        polygon_client = get_default_polygon_client()
    else:
        polygon_client = None

    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / f"{asof.isoformat()}.parquet"

    # Idempotent freeze: the 6×/day reruns for the same closed-session date must
    # not re-roll the (server-side non-deterministic) DeepSeek MoE proposal — a
    # borderline candidate would otherwise appear in one run and vanish in the
    # next, silently mutating the recommended set the EDGE feedback record is
    # keyed on. Reuse the frozen parquet when its config token still matches.
    channel_version = channel_assessor.channel_config_version(model=model)
    config_version = theme_mapper.mapper_config_version(
        market_cap_range=market_cap_range,
        model=model,
        channel_config_version=channel_version,
    )
    if not rebuild:
        frozen = _load_frozen_candidates(out_path, config_version)
        if frozen is not None:
            logger.info(
                "map_themes %s: reusing %d frozen candidate(s) (idempotent freeze; "
                "pass --rebuild to force recompute)",
                asof.isoformat(),
                len(frozen),
            )
            # No LLM call was made, so there is nothing to count — but the keys
            # must exist: the CLI emits them on every run and a gauge that
            # disappears on frozen days is itself an alertable condition.
            frozen.attrs.update(_outcome_counts([]))
            # Same rule as the two outcome gauges: no LLM call was made, so
            # there is nothing to count, but the keys must EXIST. A series that
            # disappears on a frozen day is indistinguishable from a stopped
            # exporter.
            frozen.attrs.update(_channel_counts([], []))
            # The `themes` argument was NOT used: this set was proposed by an
            # earlier slot, under that slot's theme slate. Say so, or the caller
            # records its own slate as the one that ran.
            frozen.attrs[FROZEN_REUSE_ATTR] = True
            return frozen

    pro_client = _init_pro_client(api_key)
    press_df = _fetch_press_window(asof, polygon_client)

    min_cap, max_cap = market_cap_range
    rows: list[dict] = []
    dropped_total = 0
    dropped_all_unknown = 0
    catalyst_cache: dict[str, CatalystPayload | None] = {}
    llm_proposals: list[dict] = []
    funnel_rows: list[dict] = []
    outcomes: list[theme_mapper.MapperOutcome | None] = []
    decisions: list[ThemeDecision] = []
    theme_counts: list[dict[str, int]] = []
    shadows: list[channel_assessor.ShadowVerdict] = []
    for theme in themes:
        result = _rows_for_theme(
            theme,
            asof=asof,
            catalyst_cache=catalyst_cache,
            api_key=api_key,
            pro_client=pro_client,
            min_cap=min_cap,
            max_cap=max_cap,
            model=model,
            polygon_client=polygon_client,
            press_df=press_df,
            keep_unverified=keep_unverified,
            listing_root=listing_store_root,
        )
        rows.extend(result.rows)
        llm_proposals.extend(result.proposals)
        funnel_rows.extend(result.funnel_rows)
        dropped_total += result.dropped
        dropped_all_unknown += result.dropped_unknown
        outcomes.append(result.outcome)
        decisions.append(result.decision)
        theme_counts.append(result.counts)
        shadows.append(result.shadow)

    if rows:
        df = (
            pd.DataFrame(rows)
            # ``ticker`` is the deterministic tie-break so ties on
            # (n_gates_passed, llm_confidence) don't produce
            # run-to-run ordering jitter (e.g. when Pro returns two
            # candidates at the same confidence).
            .sort_values(
                list(_CANDIDATE_SORT_KEYS),
                ascending=list(_CANDIDATE_SORT_ASCENDING),
            )
            .reset_index(drop=True)
        )
    else:
        df = pd.DataFrame(columns=list(_MAP_THEMES_COLUMNS))
    # Stamp the per-theme novelty rank/score so the candidate parquet carries the
    # selection covariate "how novel was the theme that surfaced this ticker".
    # An unmapped theme (or no mapping at all) leaves NA rather than erroring —
    # the columns always exist so downstream schema stays stable. Int64 keeps
    # rank nullable; novelty_score is plain float.
    novelty = theme_novelty or {}
    rank_map = {theme: rank for theme, (rank, _score) in novelty.items()}
    score_map = {theme: score for theme, (_rank, score) in novelty.items()}
    df["novelty_rank"] = df["theme"].map(rank_map).astype("Int64")
    df["novelty_score"] = pd.to_numeric(df["theme"].map(score_map), errors="coerce")
    df["novelty_config_version"] = novelty_config_version
    # Stamp the freeze fingerprint so a later rerun can decide whether to reuse
    # this set (config match) or recompute (deliberate config bump). Written
    # atomically so a crash mid-write can never leave a partial parquet that a
    # later run would treat as a valid freeze.
    df["mapper_config_version"] = config_version
    # Frame-wide, beside the mapper token, so the two can never disagree about
    # which model produced the run (``model=`` overrides both).
    df[channel_assessor.CHANNEL_CONFIG_COLUMN] = channel_version
    df.attrs["dropped_total"] = dropped_total
    df.attrs["dropped_all_unknown"] = dropped_all_unknown
    df.attrs.update(_outcome_counts(outcomes))
    df.attrs.update(_channel_counts(theme_counts, shadows))
    # Stamped on BOTH branches, never only on the frozen one: a key that exists
    # solely when the answer is True makes "fresh run" and "frame from some other
    # producer" the same observation to the caller.
    df.attrs[FROZEN_REUSE_ATTR] = False
    write_parquet_atomic(df, out_path, index=False)
    # V-forward telemetry: log BOTH ungated proposal sources (LLM pre-gate +
    # mechanical salience) for a clean forward head-to-head (design memo
    # theme_mapper_mechanical_rule_headtohead_2026_07_12 §8). Best-effort — a
    # shadow-write failure must never abort the daily build. Only fires on a
    # fresh (non-frozen) run, alongside the candidates parquet it mirrors.
    _write_proposal_shadow_best_effort(asof, llm_proposals, config_version, output_dir)
    # Pre-bracket funnel: one row per proposal WITH the reason it survived or
    # died. The shadow above is post-mcap by design, so without this the names
    # the bracket rejects leave no trace anywhere on disk.
    _write_proposal_funnel_best_effort(
        asof, funnel_rows, config_version, channel_version, output_dir
    )
    # One row per theme the driver TOUCHED, including the ones that produced no
    # candidate row at all (a stage-A decline, a no-catalyst skip). Without it a
    # refusal is invisible on disk, which is exactly why the pre-registered
    # ISO 40-42 forward window could not compute a KEPT-vs-REFUSED contrast.
    _write_theme_decisions_best_effort(asof, decisions, config_version, channel_version, output_dir)
    if dropped_total > 0:
        logger.info(
            "map_themes %s: kept %d / dropped %d (all-unknown %d)",
            asof.isoformat(),
            len(df),
            dropped_total,
            dropped_all_unknown,
        )
    return df


def write_empty_candidates(
    *,
    asof: dt.date,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    market_cap_range: tuple[int, int] = DEFAULT_MCAP_RANGE,
    model: str | None = None,
    novelty_config_version: str | None = None,
) -> Path:
    """Write a typed-empty candidates parquet for ``asof`` and return its path.

    A zero-novel-themes day (a quiet/holiday window, or the first run for a
    fresh date) produces no themes to map. The map-themes CLI must NOT call the
    LLM in that case, but it still has to leave the candidates parquet on disk:
    the next stage (``score``) hard-errors on a missing Phase C parquet, and
    under ``run_thematic_day.sh``'s ``set -euo pipefail`` that aborts the whole
    daily build before ``brief`` + ``rebuild_briefs_cache`` — so a genuinely
    quiet day produced no brief at all.

    The frame carries the full candidate schema + the freeze ``config_version``
    stamp, identical to the all-candidates-dropped branch of :func:`map_themes`,
    so ``score`` / ``brief`` / Django ingest read it like any other empty day.
    It stays recompute-eligible: :func:`_load_frozen_candidates` treats an empty
    set as degraded, so a later 6×/day slot that DOES surface novel themes for
    the same date recomputes instead of reusing this empty freeze.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / f"{asof.isoformat()}.parquet"
    channel_version = channel_assessor.channel_config_version(model=model)
    config_version = theme_mapper.mapper_config_version(
        market_cap_range=market_cap_range,
        model=model,
        channel_config_version=channel_version,
    )
    df = pd.DataFrame(columns=list(_MAP_THEMES_COLUMNS))
    df["mapper_config_version"] = config_version
    df[channel_assessor.CHANNEL_CONFIG_COLUMN] = channel_version
    # Mirror the all-dropped branch of map_themes: record the active novelty
    # config so the empty-day parquet schema is identical to a non-empty day.
    df["novelty_config_version"] = novelty_config_version
    write_parquet_atomic(df, out_path, index=False)
    logger.info("map_themes %s: 0 novel themes -> wrote empty candidate set", asof.isoformat())
    return out_path


__all__ = [
    "DEFAULT_OUTPUT_DIR",
    "FROZEN_REUSE_ATTR",
    "GATE_NAMES",
    "map_themes",
    "verify_candidate",
    "write_empty_candidates",
]
