"""Layer 4 orchestrator — composes the 4 signal modules per candidate.

Reads a Phase C ``thematic_candidates`` DataFrame, resolves each candidate's
SimFin industry peers once, runs the 4 signal scorers, then composes a
weighted ``layer4_weighted_score`` (insider 2× weight; others 1×; clipped to
1-5 per the locked plan §C5 doctrine).

Per-industry caching: a single :class:`_PerIndustryCache` instance computes
peer lists + memoizes the SimFin feature dicts so the same industry's peers
are not re-fetched across candidates that share an industry.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from alphalens_pipeline.data.alt_data.yfinance_client import get_default_yfinance_client
from alphalens_pipeline.thematic.mapping.catalyst_contract import CatalystPayload
from alphalens_pipeline.thematic.screening import (
    catalyst_signals,
    fcff_signal,
    insider_signal,
    magic_formula,
    sector_peers,
    technicals_signal,
    valuation_signal,
)
from alphalens_pipeline.thematic.screening import (
    selection_score as selection_score_mod,
)
from alphalens_pipeline.thematic.screening._common import filter_peers_by_mcap_price

logger = logging.getLogger(__name__)


def _yfinance_mcap_for_gate(ticker: str, asof: dt.date) -> float | None:
    """External mcap reference for ``valuation_signal``'s consistency gate.

    Thin wrapper around
    :func:`alphalens_pipeline.thematic.verification.mcap_filter.fetch_mcap` so the
    scorer doesn't import yfinance directly and the import remains lazy
    (avoids slowing down CLI startup per CLAUDE.md "lazy CLI imports").
    """
    from alphalens_pipeline.thematic.verification.mcap_filter import fetch_mcap

    return fetch_mcap(ticker, asof=asof)


# ---- Per-signal "positive" rules ----------------------------------------


def insider_is_positive(*, score_usd: float | None) -> bool:
    """Positive when net opportunistic buy clears the verification threshold."""
    if score_usd is None:
        return False
    return score_usd > 50_000.0


def fcff_is_positive(*, sector_percentile: float | None) -> bool:
    """Positive when FCFF yield is at or above the sector median."""
    if sector_percentile is None:
        return False
    return sector_percentile >= 50.0


def valuation_is_positive(*, composite_percentile: float | None) -> bool:
    """Positive when the cheaper-is-better composite is at or above sector median.

    Retained for the secondary sector-percentile transparency line in the
    brief renderer. The Magic Formula cohort rank is now the primary
    valuation gate consumed by ``compose_weighted_score``.
    """
    if composite_percentile is None:
        return False
    return composite_percentile > 50.0


def technicals_are_positive(*, rsi: float | None, ma_distance_pct: float | None) -> bool:
    """Positive when RSI is in [30, 70] AND price is within ±15% of MA50.

    Cheap heuristic: extreme RSI (overbought/oversold) and large MA50
    distance suggest the move has already exhausted, so skip. Operator can
    revisit these thresholds after observing real candidates.
    """
    if rsi is None or ma_distance_pct is None:
        return False
    return 30.0 <= rsi <= 70.0 and -15.0 <= ma_distance_pct <= 15.0


_DEFAULT_SIGNAL_SHAPES = {
    "insider": {"score_usd": None, "sector_percentile": None},
    "fcff": {"yield_pct": None, "sector_percentile": None},
    "valuation": {
        "pe": None,
        "ps": None,
        "ev_rev": None,
        "fcf_margin": None,
        "composite_sector_percentile": None,
        "financials_publish_date": None,
        "financials_age_days": None,
    },
    "technicals": {
        "rsi": None,
        "ma50_distance_pct": None,
        "atr_pct": None,
        "volume_zscore": None,
        "pct_off_52w_high": None,
        "pct_off_52w_low": None,
        "ma200_distance_pct": None,
        "ma200_slope_pct_per_day": None,
        "summary": "no data",
    },
}


def _safe_signal(name: str, fn, **kwargs):
    """Run a signal scorer; on exception log + return the default shape.

    Mirrors the Phase C orchestrator ``_safe`` pattern: a single ticker's
    data anomaly (corrupt Form-4 partition, SimFin parse error, yfinance
    schema drift) must not abort the entire batch. Caller sees the
    "missing-data" dict for that signal and continues with the others.
    """
    try:
        return fn(**kwargs)
    except Exception as exc:
        logger.warning(
            "signal %s raised for ticker=%s: %s", name, kwargs.get("ticker"), exc, exc_info=True
        )
        return dict(_DEFAULT_SIGNAL_SHAPES[name])


def compose_weighted_score(
    *,
    fcff_positive: bool,
    magic_formula_top_quartile: bool,
    deep_drawdown_reversal: bool,
    technicals_positive: bool,
    catalyst_strength: float,
) -> int:
    """Combine boolean signals + catalyst strength into a 1-5 confidence score.

    Components:
    - fcff 1×, technicals 1×
    - value/reversal slot 1×: fires on EITHER Magic Formula top-quartile
      (mature value pick) OR deep-drawdown-reversal (thematic momentum
      pick) — two different alpha drivers, same slot
    - catalyst_floor 0-2: strong catalyst (≥0.70) lifts cohort by 2;
      moderate (≥0.45) by 1; weak by 0

    Insider is INTENTIONALLY ABSENT (was a 2× term per Cohen-Malloy paradigm
    #11 doctrine): the old absolute $50k gate never fired in practice, so the
    term contributed 0; the v2 buy-only insider signal is held out of the
    ordering score until a Phase-4 offline lift test validates its incremental
    value (insider stays a display/rank dimension meanwhile). Re-introducing it
    here, with a small evidence-based weight, is the explicit Phase-4 step.

    Result clipped to [1, 5].
    """
    val_or_reversal = int(magic_formula_top_quartile or deep_drawdown_reversal)
    raw = (
        int(fcff_positive)
        + val_or_reversal
        + int(technicals_positive)
        + catalyst_signals.catalyst_floor(catalyst_strength)
    )
    return max(1, min(5, raw))


# ---- Data-fetcher factories (kept thin so tests can swap them) ----------


def _build_feature_fetcher(tickers: list[str] | None = None) -> Callable[[str, date], dict | None]:
    """Build an EDGAR-backed ``ev_fcff_features_as_of`` lookup.

    ``tickers`` is forwarded to :meth:`EdgarFundamentalsStore.preload` so
    the store on-demand-fetches any missing CIK companyfacts from SEC
    (throttled to 10 req/s; first cold-cache run adds ~12 s per 100 missing
    tickers, subsequent runs are free).

    On preload failure (network outage, SEC 5xx) the store keeps any locally
    cached parquets and just logs the failed CIKs; this fetcher then returns
    the cached features per ticker (or None when no cache + no live fetch).
    """
    from alphalens_pipeline.data.store.edgar_fundamentals import EdgarFundamentalsStore

    try:
        store = EdgarFundamentalsStore(with_prices=True)
        store.preload(tickers or [])
    except Exception as exc:
        logger.warning("EDGAR preload aborted, valuation/fcff signals will be missing: %s", exc)
        return lambda ticker, asof: None

    cache: dict[tuple[str, date], dict | None] = {}

    def fetcher(ticker: str, asof: date) -> dict | None:
        key = (ticker.upper(), asof)
        if key in cache:
            return cache[key]
        features = store.ev_fcff_features_as_of(ticker, asof)
        cache[key] = features
        return features

    return fetcher


def _build_ohlcv_loader() -> Callable[[str, date], pd.DataFrame]:
    """Build an OHLCV loader backed by the canonical :class:`YFinanceClient`.

    Disk cache lives at ``~/.alphalens/thematic_ohlcv/{TICKER}_{asof}.parquet``
    and an in-process memo avoids refetching within one run — both now owned by
    the client's :meth:`cached_daily_ohlcv`. The client adds throttle + retry
    and a stale-fallback (newest ``{TICKER}_*.parquet`` on a rate-limited /
    empty live fetch) so a Yahoo 429 burst no longer empties the brief.

    A fresh client per loader keeps the in-run memo scoped to a single scoring
    pass (the throttle still shares the implicit Yahoo budget across passes is
    intentionally not required here — each daily pipeline run is one pass).
    """
    return lambda ticker, asof: get_default_yfinance_client().cached_daily_ohlcv(ticker, asof=asof)


# ---- Main entry point ---------------------------------------------------


def _collect_universe(candidates: pd.DataFrame) -> set[str]:
    """Build the EDGAR preload universe: candidate tickers + each industry's
    raw (unfiltered) peers across both 4-digit and 3-digit cohorts.

    Side effect: this is a PRE-FETCH-ONLY walk — peers are gathered
    without applying the tradeability filter because the EDGAR fetcher
    needs price + shares for every ticker in order to compute mcap.
    The actual cohort selection (with filter) happens in
    ``_resolve_industry`` AFTER the fetcher is built, and is what
    populates the per-candidate peer cache for downstream signal scorers.
    """
    universe: set[str] = set()
    for _, cand in candidates.iterrows():
        tkr = str(cand["ticker"]).upper()
        universe.add(tkr)
        ind = sector_peers.get_industry_id(tkr)
        if ind is None:
            continue
        # Walk both candidate cohorts so the fetcher preloads every
        # potential peer's price + shares. Use raw lookups directly —
        # the tradeability filter will be re-applied in
        # ``_resolve_industry`` with the fetcher wired in.
        universe.update(sector_peers.iter_industry_peers(ind))
        from alphalens_pipeline.data.fundamentals import sic_index as _sic_index

        universe.update(_sic_index._load_sic3_peers().get(ind // 10, []))
    return universe


def _score_signals(
    *, ticker: str, asof: dt.date, peers: list[str], feature_fetcher, ohlcv_loader
) -> tuple[dict, dict, dict, dict]:
    """Run the four per-candidate signal scorers under ``_safe_signal``."""
    ins = _safe_signal(
        "insider", insider_signal.score_insider, ticker=ticker, asof=asof, peers=peers
    )
    fcff = _safe_signal(
        "fcff",
        fcff_signal.score_fcff,
        ticker=ticker,
        asof=asof,
        peers=peers,
        feature_fetcher=feature_fetcher,
    )
    val = _safe_signal(
        "valuation",
        valuation_signal.score_valuation,
        ticker=ticker,
        asof=asof,
        peers=peers,
        feature_fetcher=feature_fetcher,
        external_mcap_fetcher=_yfinance_mcap_for_gate,
    )
    tech = _safe_signal(
        "technicals",
        technicals_signal.score_technicals,
        ticker=ticker,
        asof=asof,
        loader=ohlcv_loader,
    )
    return ins, fcff, val, tech


@dataclass(frozen=True, slots=True)
class IndustryCohort:
    """Identity + cohort metadata stamped onto every enrichment row."""

    industry_id: int | None
    industry_name: str | None
    sector_name: str | None
    peer_cohort_level: str


@dataclass(frozen=True, slots=True)
class MagicFormulaInputs:
    """Magic-Formula fields derived from a candidate's SimFin features dict."""

    ev_ebitda: float | None
    roic: float | None
    roe: float | None
    health_pass: bool


def _compute_magic_formula_fields(features: dict) -> MagicFormulaInputs:
    """EV/EBITDA, ROIC, ROE, health-gate verdict from the SimFin features dict."""
    market_cap = _market_cap_from_features(features)
    return MagicFormulaInputs(
        ev_ebitda=magic_formula.compute_ev_ebitda(
            features, market_cap=market_cap if market_cap is not None else float("nan")
        ),
        roic=magic_formula.compute_roic(features),
        roe=magic_formula.compute_roe(features),
        health_pass=magic_formula.passes_health_gate(features),
    )


def _build_candidate_row(
    *,
    ticker: str,
    cohort: IndustryCohort,
    ins: dict,
    fcff: dict,
    val: dict,
    tech: dict,
    magic_formula_inputs: MagicFormulaInputs,
    catalyst_event: CatalystPayload | None,
    subject_catalyst: CatalystPayload | None,
    cs_val: float,
) -> dict:
    """Assemble one candidate's enrichment row (deferred Magic-Formula rank
    + weighted score + reversal detection are post-loop because they need
    the whole cohort).
    """
    # Thin cohort: the candidate's own multiples / yield / score are still
    # valid signals, but a percentile derived from an empty (or sub-floor)
    # peer set would be misleading. Per issue #197 the brief should show
    # the "thin cohort" badge instead of a colored percentile bar — null
    # the percentiles here so downstream renderers see the same "no
    # signal" sentinel they already handle.
    is_thin = cohort.peer_cohort_level == "thin"
    insider_pctl = None if is_thin else ins["sector_percentile"]
    fcff_pctl = None if is_thin else fcff["sector_percentile"]
    val_pctl = None if is_thin else val["composite_sector_percentile"]
    # Option (b) #395: subject-match template event wins as the template-fact
    # source; else fall back to the theme catalyst's own template fields.
    template_source = subject_catalyst or catalyst_event
    return {
        "ticker": ticker,
        "industry_id": cohort.industry_id if cohort.industry_id is not None else np.nan,
        "industry_name": cohort.industry_name,
        "sector_name": cohort.sector_name,
        "peer_cohort_level": cohort.peer_cohort_level,
        "insider_score_usd": ins["score_usd"],
        "insider_score_sector_percentile": insider_pctl,
        # Poolability key stamped on EVERY row (even when score_usd is null), so
        # the deferred Insider×EDGE calibration partitions old vs new signal
        # semantics and never pools across versions. Mirrors panel_config_version.
        "insider_signal_version": insider_signal.INSIDER_SIGNAL_VERSION,
        # Poolability key for the selection scorer (ATR tilt + weights). Partitions
        # EDGE cohort so old briefs (pre-scorer) stay a frozen pool; recalibration
        # bumps this string.
        "scorer_config_version": selection_score_mod.SCORER_CONFIG_VERSION,
        "fcff_yield_pct": fcff["yield_pct"],
        "fcff_yield_sector_percentile": fcff_pctl,
        "valuation_pe": val["pe"],
        "valuation_ps": val["ps"],
        "valuation_ev_rev": val["ev_rev"],
        "valuation_ev_ebitda": magic_formula_inputs.ev_ebitda,
        "valuation_fcf_margin": val["fcf_margin"],
        "valuation_composite_sector_percentile": val_pctl,
        "valuation_financials_publish_date": val.get("financials_publish_date"),
        "valuation_financials_age_days": val.get("financials_age_days"),
        "roic_pct": magic_formula_inputs.roic,
        "roe_pct": magic_formula_inputs.roe,
        "magic_formula_health_pass": magic_formula_inputs.health_pass,
        "technical_rsi": tech["rsi"],
        "technical_ma50_distance_pct": tech["ma50_distance_pct"],
        "technical_atr_pct": tech["atr_pct"],
        "technical_volume_zscore": tech["volume_zscore"],
        "technical_pct_off_52w_high": tech.get("pct_off_52w_high"),
        "technical_pct_off_52w_low": tech.get("pct_off_52w_low"),
        "technical_ma200_distance_pct": tech.get("ma200_distance_pct"),
        "technical_ma200_slope_pct_per_day": tech.get("ma200_slope_pct_per_day"),
        "technicals_summary_str": tech["summary"],
        "catalyst_strength": cs_val,
        "catalyst_event_type": catalyst_event.event_type if catalyst_event else None,
        "catalyst_confidence": catalyst_event.confidence if catalyst_event else None,
        # PR-3: typed-fact provenance from the template engine. The
        # orchestrator's _row_to_facts deserialises the JSON column back
        # to a dict; brief generator's prompt-rendering branch fires on
        # presence. None on Flash-extracted catalysts + no-catalyst rows.
        #
        # Option (b) #395: a subject-match template event (this ticker IS the
        # filing subject) is the preferred fact source over a theme-path
        # template catalyst; falls back to the theme catalyst's own template
        # fields (PR-3) when there is no subject match. AUGMENT-ONLY -- this
        # touches ONLY the two template columns; catalyst_event_type /
        # _confidence / _strength / _url below still read from the theme
        # catalyst, so SCORES are unchanged. (Second-order: the orchestrator's
        # _sort_and_dedup_for_brief uses _template_facts_richness as its LAST
        # per-ticker drop_duplicates tie-break, so a now-richer subject-stamped
        # row can win that tie over an equally-scored theme row for the same
        # ticker -- intended; layer4_weighted_score / rankings are untouched.)
        "catalyst_template_id": template_source.template_id if template_source else None,
        "catalyst_template_facts_json": (
            json.dumps(template_source.template_facts, sort_keys=True)
            if template_source and template_source.template_facts
            else None
        ),
        # Stashed for the reversal detector (source_event_url is
        # in candidates.parquet, merged AFTER this frame). Dropped
        # before the public DataFrame is built.
        "_catalyst_url": catalyst_event.url if catalyst_event else None,
        # Signal positives stashed for the post-loop weighted-score
        # composition; dropped before the public DataFrame is built.
        # ``fcff_positive`` reads the cohort-adjusted percentile so a
        # thin-cohort candidate doesn't get a +1 lift from the
        # midpoint-50 fallback (which would otherwise pass the ≥50 test).
        # Insider is HELD OUT of layer4_weighted_score (display/rank dimension
        # only) pending a Phase-4 offline lift test — it contributed 0 in
        # practice (no candidate cleared the old $50k gate) and the v2 buy-only
        # signal must not silently start re-weighting the score before its
        # incremental value is validated. `insider_is_positive` is retained for
        # that future re-integration.
        "_fcff_positive": fcff_is_positive(sector_percentile=fcff_pctl),
        "_technicals_positive": technicals_are_positive(
            rsi=tech["rsi"], ma_distance_pct=tech["ma50_distance_pct"]
        ),
    }


def score_candidates(candidates: pd.DataFrame, *, asof: dt.date) -> pd.DataFrame:
    """Enrich the Phase C candidates frame with Layer 4 columns.

    Does not drop or reorder rows; appends 18 new columns (industry trio +
    4 signals × 3-5 fields each + weighted score). All numerical columns are
    nullable; string ``technicals_summary_str`` always renders.
    """
    if candidates.empty:
        return candidates.copy()

    peer_cache: dict[int, tuple[list[str], str]] = {}
    universe = _collect_universe(candidates)

    feature_fetcher = _build_feature_fetcher(sorted(universe))
    ohlcv_loader = _build_ohlcv_loader()

    # Per-theme catalyst lookup cache. ``find_trigger_event`` returns the
    # latest theme-tagged event (with full extraction metadata) so multiple
    # candidates sharing a theme reuse a single resolution.
    from alphalens_pipeline.thematic.mapping import catalyst_resolver

    catalyst_cache: dict[str, CatalystPayload | None] = {}

    # Option (b) #395: subject-match template catalysts. Index template-
    # extracted events by primary-entity ticker ONCE per batch so a candidate
    # that is ITSELF the subject of a template filing (M&A target, regulated
    # party, earnings name) gets its template_id + facts stamped, independent of
    # theme (template events carry themes=[], so the theme-keyed path never
    # reaches them). O(events) build, O(1) per-candidate lookup. Degrades to the
    # theme-only path on any failure.
    try:
        template_entity_index = catalyst_resolver.build_template_entity_index(asof=asof)
    except Exception as exc:
        logger.warning("template entity index build failed: %s", exc)
        template_entity_index = {}

    def _tradeable_filter(peers_to_check: list[str]) -> list[str]:
        return filter_peers_by_mcap_price(
            peers_to_check, feature_fetcher=feature_fetcher, asof=asof
        )

    def _resolve_industry(
        ticker: str,
    ) -> tuple[int | None, str | None, str | None, list[str], str]:
        industry_id = sector_peers.get_industry_id(ticker)
        if industry_id is None:
            return None, None, None, [], "thin"
        industry_name, sector_name = sector_peers.industry_label(industry_id)
        if industry_id not in peer_cache:
            # ``peer_filter`` runs BEFORE the min_cohort check inside the
            # resolver, so a raw 4-digit cohort of 10 peers — 7 of which
            # are warrants / shells / penny stocks — correctly falls back
            # to sic3 (or thin) rather than rendering a "sic4" badge over
            # an effective cohort of 3 (Gemini 3 Pro PR-215 finding).
            peer_cache[industry_id] = sector_peers.iter_industry_peers_fallback(
                industry_id, peer_filter=_tradeable_filter
            )
        peers, level = peer_cache[industry_id]
        return industry_id, industry_name, sector_name, peers, level

    def _resolve_catalyst_event(theme: str) -> CatalystPayload | None:
        if theme not in catalyst_cache:
            try:
                catalyst_cache[theme] = catalyst_resolver.find_trigger_event(theme=theme, asof=asof)
            except Exception as exc:
                logger.warning("catalyst lookup failed for theme=%r: %s", theme, exc)
                catalyst_cache[theme] = None
        return catalyst_cache[theme]

    rows: list[dict] = []
    for _, cand in candidates.iterrows():
        ticker = str(cand["ticker"]).upper()
        industry_id, industry_name, sector_name, peers, peer_cohort_level = _resolve_industry(
            ticker
        )
        # When the fallback resolver returns "thin", peers is empty and
        # every signal scorer will compute candidate yield/score against
        # an empty cohort. Per ``_common.percentile_rank``, an empty
        # cohort returns 50.0 ("no information" midpoint) — surfaced via
        # ``peer_cohort_level`` so the UI can suppress the percentile bar.
        ins, fcff, val, tech = _score_signals(
            ticker=ticker,
            asof=asof,
            peers=peers,
            feature_fetcher=feature_fetcher,
            ohlcv_loader=ohlcv_loader,
        )
        # Magic Formula inputs — derived from the (cached) SimFin features
        # dict. ``feature_fetcher`` memoises per (ticker, asof) so this call
        # is free after the val/fcff signal scorers above.
        features = feature_fetcher(ticker, asof) or {}
        magic_formula_inputs = _compute_magic_formula_fields(features)
        # Catalyst lookup — cached per theme. Returns extended event dict
        # (url, title, published_at, event_type, confidence, second_order_
        # implications). None when no theme-tagged event survives noise filter.
        catalyst_event = _resolve_catalyst_event(str(cand.get("theme", "")))
        cs_val = catalyst_signals.compute_catalyst_strength(catalyst_event)
        # Option (b) #395: if THIS ticker is the subject of a template filing in
        # the window, that event supplies the typed template facts (stamps
        # template_id/facts ONLY -- does NOT override catalyst_strength /
        # event_type). O(1) lookup against the per-batch index.
        try:
            subject_catalyst = catalyst_resolver.find_template_catalyst_for_ticker(
                ticker=ticker, asof=asof, entity_index=template_entity_index
            )
        except Exception as exc:
            logger.warning("subject-match catalyst lookup failed for %r: %s", ticker, exc)
            subject_catalyst = None
        cohort = IndustryCohort(
            industry_id=industry_id,
            industry_name=industry_name,
            sector_name=sector_name,
            peer_cohort_level=peer_cohort_level,
        )
        rows.append(
            _build_candidate_row(
                ticker=ticker,
                cohort=cohort,
                ins=ins,
                fcff=fcff,
                val=val,
                tech=tech,
                magic_formula_inputs=magic_formula_inputs,
                catalyst_event=catalyst_event,
                subject_catalyst=subject_catalyst,
                cs_val=cs_val,
            )
        )
    enrichment = pd.DataFrame(rows)

    # Cohort-relative Magic Formula rank — operates on the survivor basket
    # rather than sector peers. ``valuation_pe`` here is the SimFin-derived
    # P/E from valuation_signal (NOT the Magic-Formula variant) to preserve
    # the existing column semantic; both compute the same way (market_cap /
    # net_income_ttm) so reuse is safe.
    enrichment["magic_formula_rank"] = magic_formula.compute_cohort_rank(enrichment)
    cohort_n = int(enrichment["magic_formula_health_pass"].sum())
    enrichment["magic_formula_cohort_n"] = cohort_n

    # Deep-drawdown-reversal is per-candidate (computed once columns are
    # final). The detector reads ``source_event_url`` so we temporarily
    # mirror the stashed ``_catalyst_url`` into that column for the apply,
    # then drop the temp. (candidates.parquet's own source_event_url
    # arrives via the final merge below — we don't want to collide.)
    enrichment["source_event_url"] = enrichment["_catalyst_url"]
    enrichment["deep_drawdown_reversal"] = enrichment.apply(
        catalyst_signals.is_deep_drawdown_reversal, axis=1
    )
    enrichment = enrichment.drop(columns=["source_event_url", "_catalyst_url"])

    enrichment["layer4_weighted_score"] = [
        compose_weighted_score(
            fcff_positive=row["_fcff_positive"],
            magic_formula_top_quartile=magic_formula.is_top_quartile(
                rank=row["magic_formula_rank"], cohort_n=cohort_n
            ),
            deep_drawdown_reversal=bool(row["deep_drawdown_reversal"]),
            technicals_positive=row["_technicals_positive"],
            catalyst_strength=float(row["catalyst_strength"]),
        )
        for _, row in enrichment.iterrows()
    ]
    enrichment = enrichment.drop(columns=["_fcff_positive", "_technicals_positive"])

    # selection_score = layer4 − atr_penalty; reuse the just-computed penalty
    # column (don't recompute it per row) so the two can't drift apart.
    enrichment["atr_penalty"] = enrichment["technical_atr_pct"].map(selection_score_mod.atr_penalty)
    enrichment["selection_score"] = (
        enrichment["layer4_weighted_score"].astype(float) - enrichment["atr_penalty"]
    )

    # Merge on ticker to preserve original order + Phase C columns.
    merged = candidates.copy().reset_index(drop=True)
    merged["ticker"] = merged["ticker"].astype(str).str.upper()
    return merged.merge(enrichment, on="ticker", how="left")


def _market_cap_from_features(features: dict) -> float | None:
    """Compute market cap from SimFin price × shares. None if either missing
    or NaN/non-positive. NaN guard matters here: ``float(nan) <= 0`` is
    False, so without the isnan check NaN would silently pass through.
    """
    import math

    price = features.get("price")
    shares = features.get("shares_outstanding")
    if price is None or shares is None:
        return None
    try:
        p = float(price)
        s = float(shares)
    except (TypeError, ValueError):
        return None
    if math.isnan(p) or math.isnan(s) or p <= 0 or s <= 0:
        return None
    return p * s


__all__ = [
    "compose_weighted_score",
    "fcff_is_positive",
    "insider_is_positive",
    "score_candidates",
    "technicals_are_positive",
    "valuation_is_positive",
]
