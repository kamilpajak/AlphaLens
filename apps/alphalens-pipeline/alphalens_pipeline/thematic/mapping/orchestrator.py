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
from pathlib import Path

import pandas as pd

from alphalens_pipeline.data.alt_data.polygon_client import (
    PolygonClient,
    get_default_polygon_client,
)
from alphalens_pipeline.data.parquet_io import write_parquet_atomic
from alphalens_pipeline.thematic.mapping import (
    catalyst_resolver,
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
) -> dict:
    return {
        "theme": theme,
        "ticker": cand["ticker"],
        "company_name": cand.get("company_name", ""),
        "rationale": cand.get("rationale", ""),
        # The causal path the model named from the catalyst event to this
        # company's revenue / costs / cost of capital / competitive position.
        # Persisted so the (event, ticker) plausibility audit that diagnosed
        # the ungrounded-linkage defect can be re-run on the new output —
        # without it the fix is unfalsifiable. Parquet-only telemetry: no
        # downstream stage reads it (not score, not brief, not the Django
        # model, not the SPA); the audit reads the parquet directly.
        "transmission_channel": cand.get("transmission_channel", ""),
        "llm_confidence": cand.get("confidence", 0.0),
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
    "transmission_channel",
    "llm_confidence",
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
    (3) is non-degraded — at least one verified candidate. A legacy parquet
    without the column, a config mismatch, or an empty/all-unverified set is
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
        logger.info("map_themes: frozen set degraded (empty / no verified) -> recomputing")
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
    "transmission_channel",
    "market_cap",
    "bracket_verdict",
    "catalyst_url",
    "catalyst_event_type",
    "mapper_config_version",
)


def _funnel_row(
    *,
    theme: str,
    cand: dict,
    verdict: mcap_filter.McapVerdict | None,
    catalyst: CatalystPayload,
) -> dict:
    """One PRE-bracket proposal, with the verdict that decided its fate.

    ``verdict`` is ``None`` only if the classifier somehow skipped the ticker;
    that is recorded as :data:`~mcap_filter.NO_MCAP` rather than dropped, so the
    row count always equals the proposal count and "how many did the model
    actually propose" stays answerable from this file alone.
    """
    return {
        "theme": theme,
        "ticker": cand["ticker"],
        "company_name": cand.get("company_name", ""),
        # Same default as ``_build_row``: a model that omits confidence must not
        # read as null here and 0.0 in the candidates parquet for the same name.
        # (The proposal-shadow builder keeps its own None default on purpose —
        # its rows feed a pre-registered measurement and must not shift.)
        "llm_confidence": cand.get("confidence", 0.0),
        "transmission_channel": cand.get("transmission_channel", ""),
        "market_cap": verdict.market_cap if verdict is not None else None,
        "bracket_verdict": verdict.verdict if verdict is not None else mcap_filter.NO_MCAP,
        "catalyst_url": catalyst.url,
        "catalyst_event_type": catalyst.event_type,
    }


def _write_proposal_funnel_best_effort(
    asof: dt.date, funnel_rows: list[dict], config_version: str, out_dir: Path
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


def _propose_and_filter_candidates(
    *,
    theme: str,
    catalyst: CatalystPayload,
    api_key: str | None,
    pro_client,
    min_cap: int,
    max_cap: int,
    asof: dt.date,
    model: str | None = None,
    funnel_sink: list[dict] | None = None,
) -> tuple[list[dict], dict[str, float], list[str], theme_mapper.MapperOutcome]:
    """Pro proposal → real-time mcap filter → keyword harvest.

    ``catalyst`` is the theme's resolved trigger event and is REQUIRED — the
    proposal reasons from the event, not from the theme word. Typed
    non-optional so that "a proposal made without a grounded event" is
    unrepresentable; :func:`_rows_for_theme` already hard-returns before this
    call when nothing resolved.

    Returns (in-bracket candidate dicts, ticker→mcap map, search keywords,
    mapper outcome). Empty candidates list signals "nothing further to do for
    this theme"; the outcome says WHETHER THAT WAS AN ANSWER OR A LOSS, which
    the list itself cannot (issue #982).

    ``funnel_sink``, when given, is appended with one dict per PRE-BRACKET
    proposal (see :func:`_funnel_row`). It is an out-parameter rather than a
    fifth return value so the existing four-tuple contract — and every test that
    patches this function — stays intact. Telemetry only; nothing reads it back.
    """
    proposal = theme_mapper.propose_candidates(
        theme=theme,
        catalyst=catalyst,
        api_key=api_key,
        llm_client=pro_client,
        model=model or theme_mapper.DEFAULT_MODEL,
    )
    outcome = proposal["outcome"]
    proposed = proposal.get("candidates") or []
    if not proposed:
        # One funnel line per theme, and it has to be self-contained: an
        # operator reading `journalctl` must not have to cross-reference the
        # mapper's own log line to tell a working refusal from a lost theme.
        if outcome is theme_mapper.MapperOutcome.DECLINED:
            logger.info(
                "map_themes %s: theme %r funnel — proposed 0 (model declined: %r)",
                asof.isoformat(),
                theme,
                proposal.get("no_candidates_reason") or "",
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
        return [], {}, [], outcome
    verdicts = mcap_filter.classify_by_mcap(
        [c["ticker"] for c in proposed],
        min_cap=min_cap,
        max_cap=max_cap,
        asof=asof,
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
    if funnel_sink is not None:
        # Zip POSITIONALLY, not through a {ticker: verdict} dict. The model can
        # propose the same ticker twice, and a dict would collapse both rows onto
        # the LAST verdict — so a duplicate whose two mcap lookups disagreed (a
        # cache write landing between them, or the PIT->live fallback firing on
        # one call only) would record a verdict that never applied to the first
        # row. ``classify_by_mcap`` returns one verdict per input position, so the
        # zip is exact and a length mismatch would surface here rather than
        # silently writing a plausible-looking NO_MCAP row.
        funnel_sink.extend(
            _funnel_row(theme=theme, cand=c, verdict=v, catalyst=catalyst)
            for c, v in zip(proposed, verdicts, strict=True)
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
    keywords = _theme_keywords(theme, pro_keywords=proposal.get("search_keywords") or [])
    return candidates, in_bracket, keywords, outcome


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
            )
        )
    return rows, dropped, dropped_all_unknown


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
    funnel_sink: list[dict] | None = None,
) -> tuple[list[dict], int, int, list[dict], theme_mapper.MapperOutcome | None]:
    """Resolve → propose → verify one theme.

    Returns ``(rows, dropped, dropped_unknown, proposals, outcome)``.

    ``funnel_sink`` is threaded straight through to
    :func:`_propose_and_filter_candidates` — see its docstring for why the
    pre-bracket funnel travels as an out-parameter rather than a return value.

    ``proposals`` is the LLM's **pre-gate** candidate set (post-mcap, before the
    verification gates that ``rows`` survives) — captured for the V-forward
    proposal-shadow log. ``outcome`` is the mapper's :class:`MapperOutcome`, or
    ``None`` when the mapper was never called because no catalyst event
    resolved — a skip is neither a decline nor a mapper failure, so it must not
    land in either counter. Extracted from :func:`map_themes`'s loop so the
    driver stays within the cognitive-complexity budget.
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
        return [], 0, 0, [], None
    candidates, in_bracket, keywords, outcome = _propose_and_filter_candidates(
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
        funnel_sink=funnel_sink,
    )
    if not candidates:
        return [], 0, 0, [], outcome
    proposals = [
        {
            "theme": theme,
            "ticker": cand["ticker"],
            "llm_confidence": cand.get("confidence"),
        }
        for cand in candidates
    ]
    rows, dropped, dropped_unknown = _verify_candidates_for_theme(
        theme=theme,
        candidates=candidates,
        in_bracket=in_bracket,
        keywords=keywords,
        catalyst=catalyst,
        asof=asof,
        polygon_client=polygon_client,
        press_df=press_df,
        keep_unverified=keep_unverified,
    )
    return rows, dropped, dropped_unknown, proposals, outcome


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
) -> pd.DataFrame:
    """For each theme, propose candidates, post-filter by real-time mcap, then verify.

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
    config_version = theme_mapper.mapper_config_version(
        market_cap_range=market_cap_range, model=model
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
    for theme in themes:
        theme_rows, dropped, dropped_unknown, proposals, outcome = _rows_for_theme(
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
            funnel_sink=funnel_rows,
        )
        rows.extend(theme_rows)
        llm_proposals.extend(proposals)
        dropped_total += dropped
        dropped_all_unknown += dropped_unknown
        outcomes.append(outcome)

    if rows:
        df = (
            pd.DataFrame(rows)
            # ``ticker`` is the deterministic tie-break so ties on
            # (n_gates_passed, llm_confidence) don't produce
            # run-to-run ordering jitter (e.g. when Pro returns two
            # candidates at the same confidence).
            .sort_values(
                ["theme", "n_gates_passed", "llm_confidence", "ticker"],
                ascending=[True, False, False, True],
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
    df.attrs["dropped_total"] = dropped_total
    df.attrs["dropped_all_unknown"] = dropped_all_unknown
    df.attrs.update(_outcome_counts(outcomes))
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
    _write_proposal_funnel_best_effort(asof, funnel_rows, config_version, output_dir)
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
    config_version = theme_mapper.mapper_config_version(
        market_cap_range=market_cap_range, model=model
    )
    df = pd.DataFrame(columns=list(_MAP_THEMES_COLUMNS))
    df["mapper_config_version"] = config_version
    # Mirror the all-dropped branch of map_themes: record the active novelty
    # config so the empty-day parquet schema is identical to a non-empty day.
    df["novelty_config_version"] = novelty_config_version
    write_parquet_atomic(df, out_path, index=False)
    logger.info("map_themes %s: 0 novel themes -> wrote empty candidate set", asof.isoformat())
    return out_path


__all__ = [
    "DEFAULT_OUTPUT_DIR",
    "GATE_NAMES",
    "map_themes",
    "verify_candidate",
    "write_empty_candidates",
]
