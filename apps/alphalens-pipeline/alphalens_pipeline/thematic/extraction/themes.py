"""Rolling theme aggregator + novelty scorer over Layer-2 extraction parquets.

Reads daily ``thematic_events/{YYYY-MM-DD}.parquet`` files within the lookback
window, explodes the ``themes`` column, and ranks each theme by (a) total
occurrence in the window and (b) novelty — how strongly the last 7 days
over-index versus the trailing baseline. Novelty ≥ 3 flags a theme as a Phase
C trigger candidate (per design memo §2 Layer 3 trigger condition).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import math
from collections.abc import Iterable, Sequence
from pathlib import Path

import pandas as pd

from alphalens_pipeline.data.parquet_io import write_parquet_atomic
from alphalens_pipeline.thematic.theme_text import slugify_theme

logger = logging.getLogger(__name__)

DEFAULT_EVENTS_DIR = Path.home() / ".alphalens" / "thematic_events"
DEFAULT_THEME_ROLLUP_DIR = Path.home() / ".alphalens" / "theme_rollup"
DEFAULT_WINDOW_DAYS = 30
DEFAULT_RECENT_DAYS = 7
DEFAULT_NOVELTY_THRESHOLD = 3.0
# Cap on themes handed to the mapper per run (DeepSeek v4-pro spend control).
# Named here rather than left as a CLI literal because the inclusion propensity
# is a function of it: the cut size is now part of what the rollup records.
DEFAULT_MAX_THEMES = 10
_LN10 = math.log(10.0)

# Bump for a code-level change to how novelty is COMPUTED (the roll_up ratio
# formula or normalization) that the three numeric params below cannot express.
# 2: the tie-break stopped being alphabetical (see TIEBREAK_VERSION).
_NOVELTY_CONFIG_SCHEMA = 2

# Identity of the rule that orders themes the ranking keys cannot separate.
# Bump on ANY change to the seed derivation, to the per-theme key, or to where
# the key sits in the sort — a stored rollup must never be ambiguous about which
# draw produced it, and two selection rules that pick differently from the same
# counts must not be indistinguishable in stored data.
TIEBREAK_VERSION = "sha256-asof-theme-v1"

# Truncated purely to keep ~11k rows/day of hex out of the parquet. 64 bits over
# ~11k themes puts the collision probability around 3e-12; a collision would cost
# one arbitrary ordering inside one tied pool, which is what this replaces anyway.
_TIEBREAK_KEY_HEX = 16

# The keys the selector actually ranks on, in precedence order, all descending.
# The seeded key is appended AFTER these, so randomisation applies only where
# both of them are exactly equal — inside the selector's indifference set.
_RANKING_KEYS = ("novelty_score", "count_window")


def novelty_config_version(*, window_days: int, recent_days: int, threshold: float) -> str:
    """Canonical JSON token of the novelty config that ranked a theme.

    Stamped alongside ``novelty_rank``/``novelty_score`` on the candidate parquet
    so a future EDGE attribution pass can pool only outcomes scored under the
    SAME novelty definition. A deliberate tune of the lookback window, the recent
    sub-window, or the flag threshold must make pre- vs post-change novelty values
    non-comparable — so this token fingerprints all three. Bump
    :data:`_NOVELTY_CONFIG_SCHEMA` for a code-level formula change the params
    cannot capture. Mirrors :func:`mapper_config_version` / ``ladder_config_version``.

    The tie-break identity rides along because it is part of the SELECTION rule,
    not of the score: on 12 of 17 measured production days the top-10 boundary was
    a tie, so which rule broke it decided which themes were mapped. Two rules that
    pick differently from identical counts must not share a fingerprint.
    """
    payload = {
        "schema": _NOVELTY_CONFIG_SCHEMA,
        "window_days": int(window_days),
        "recent_days": int(recent_days),
        "threshold": float(threshold),
        "tiebreak": TIEBREAK_VERSION,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _as_day(asof: dt.date) -> dt.date:
    """Collapse any date-like input to the calendar day it names.

    ``dt.datetime`` (and therefore ``pd.Timestamp``) passes every ``dt.date``
    type check there is, and differs only in what ``isoformat()`` prints. The
    seed is a per-DAY quantity, so the time part is dropped rather than allowed
    to fork the seed; a value that is not a date at all is refused.
    """
    if isinstance(asof, dt.datetime):
        return asof.date()
    if isinstance(asof, dt.date):
        return asof
    raise TypeError(f"asof must be a datetime.date, got {type(asof).__name__}")


def tiebreak_seed(asof: dt.date) -> str:
    """The day's tie-break seed — a pure function of the asof date and the rule.

    Derived with hashlib, never with the builtin ``hash()``: ``hash()`` on ``str``
    is salted per process by ``PYTHONHASHSEED``, so a key built on it would
    reshuffle on every container restart. Nothing here reads the clock, process
    RNG state, or row order, because the pipeline rebuilds the SAME asof six times
    a day and the six runs must agree on the selection to the letter.

    Deliberately NOT a function of the novelty config token: ``roll_up`` is called
    from two places with different ``recent_days``, and a seed that moved with the
    token would make the two disagree about the order they show and the order they
    use. The tie-break identity travels in the token, not the token in the seed.

    The input is normalised to a plain date first. ``dt.datetime`` and
    ``pd.Timestamp`` are ``dt.date`` SUBCLASSES, so neither the annotation nor
    pyright can stop one reaching this function, and their ``isoformat()`` emits
    ``2026-08-18T00:00:00`` — a different string, a different seed, a different
    slate. One caller reaching for ``pd.Timestamp`` would otherwise be enough to
    break the agreement between the day's six runs. Anything that is not a date
    at all raises rather than hashing into a plausible-looking seed.
    """
    return hashlib.sha256(f"{TIEBREAK_VERSION}|{_as_day(asof).isoformat()}".encode()).hexdigest()


def tiebreak_keys(theme_names: Iterable[str], *, asof: dt.date) -> list[str]:
    """Per-theme sort keys for the day's draw, one hash each.

    A PER-THEME key, not a permutation of the pool: ``extract`` appends newly
    ingested events to the same asof parquet on every slot, so the set of themes
    tied at the boundary is not fixed across the six runs of one day. Shuffling a
    list by position would reorder every survivor when one theme joins or leaves;
    a key that depends only on (asof, theme) cannot move.
    """
    seed = tiebreak_seed(asof)
    return [
        hashlib.sha256(f"{seed}|{name}".encode()).hexdigest()[:_TIEBREAK_KEY_HEX]
        for name in theme_names
    ]


def apply_tiebreak(rollup: pd.DataFrame, *, asof: dt.date) -> pd.DataFrame:
    """Rank by the novelty keys, breaking exact ties on the day's seeded key.

    Stamps ``tiebreak_key`` and returns the frame in selection order. The seeded
    key is the LAST sort key, so a theme with a strictly better novelty_score (or
    an equal score and a larger count_window) still leads — randomisation never
    leaks into the policy, only into the indifference set the policy leaves open.
    """
    if rollup is None or rollup.empty:
        return rollup
    frame = rollup.copy()
    frame["tiebreak_key"] = tiebreak_keys(frame["theme"].astype(str), asof=asof)
    keys = [c for c in _RANKING_KEYS if c in frame.columns]
    return frame.sort_values(
        [*keys, "tiebreak_key"], ascending=[*([False] * len(keys)), True]
    ).reset_index(drop=True)


def selection_propensity(rollup: pd.DataFrame, *, threshold: float, max_themes: int) -> pd.Series:
    """Probability each theme had of being selected, under the seeded draw.

    MARGINAL, not joint. The quantity a later off-policy evaluation needs is
    ``P(theme i is in the slate)`` per theme, not the probability of the exact
    slate that came out — the latter is astronomically small and useless as a
    weight.

    That substitution is only valid where the reward is ADDITIVE across the
    selected themes, because ``E[sum_i in S r_i] = sum_i P(i in S) * r_i`` is
    what lets a Horvitz-Thompson estimator drop the joint distribution. State
    the condition rather than assume it, because it holds at one stage of this
    pipeline and fails at the next:

    * at the MAPPING stage it holds — ``theme_mapper.build_prompt`` takes a
      single theme and the orchestrator loops themes independently, so a
      theme's proposals do not depend on which other themes were selected.
      These propensities are logged for that stage and weight its outcomes
      correctly;
    * at the BRIEF stage it does NOT hold — a fixed number of cards means
      themes compete, so one theme's shipped outcome depends on the rest of the
      slate. Weighting a brief-stage outcome by these marginals is invalid, and
      no arithmetic here can detect the misuse.

    Three cases, and the pool is the set of themes sharing the MARGINAL
    ranking key (the key of the last theme that fits), not the whole tied frame:

    * ranked strictly above the marginal key, and eligible -> 1.0; it was going to
      be picked whatever the seed did;
    * sharing the marginal key -> ``slots_remaining / pool_size``. Treating the
      hash as a uniform draw makes every ordering of the pool equally likely, and
      a given member lands in the first ``k`` of ``n`` positions with probability
      ``k / n``;
    * below the novelty threshold, or ranked below the pool -> 0.0. A theme the
      threshold excluded had no chance at all, which is different from "was not
      picked this time".

    The three cases sum to ``max_themes`` exactly (``n_certain + k``), which is
    the arithmetic identity that a fixed-size draw's marginals must satisfy.

    Computed on the FULL rollup, before truncation, on purpose: after ``head()``
    every survivor's propensity is 1.0 by construction and the information the
    randomisation was introduced to create is gone.
    """
    # Positional throughout: label-based assignment would double-set rows if the
    # caller handed over a frame whose index carries duplicates.
    work = rollup.reset_index(drop=True)
    prop = pd.Series(0.0, index=work.index, dtype=float)
    if work.empty or max_themes <= 0:
        return pd.Series(prop.to_numpy(), index=rollup.index, dtype=float)
    eligible = work["novelty_score"] >= threshold
    n_eligible = int(eligible.sum())
    if n_eligible == 0:
        return pd.Series(prop.to_numpy(), index=rollup.index, dtype=float)
    if n_eligible <= max_themes:
        # The cut does not bind: every eligible theme is mapped with certainty.
        prop[eligible] = 1.0
        return pd.Series(prop.to_numpy(), index=rollup.index, dtype=float)

    keys = [c for c in _RANKING_KEYS if c in work.columns]
    ranked = work.loc[eligible, keys].sort_values(keys, ascending=False)
    marginal = ranked.iloc[max_themes - 1]
    # Sorting descending puts equal-key rows next to each other, so the first
    # match is also the count of themes that beat the pool outright.
    #
    # NaN-aware, and not for tidiness: ``NaN != NaN``, so a plain equality mask
    # against a marginal row carrying one is all-False, the pool measures 0 and
    # the division below raises. Two NaN keys are INDISTINGUISHABLE to the
    # sorter, which is precisely what membership of the tie-break pool means, so
    # they belong in the same pool. The mask therefore always matches at least
    # the marginal row against itself and the pool is never empty.
    at_margin = ((ranked == marginal) | (ranked.isna() & marginal.isna())).all(axis=1).to_numpy()
    n_certain = int(at_margin.argmax())
    pool_size = int(at_margin.sum())
    slots_remaining = max_themes - n_certain

    prop.loc[ranked.index[:n_certain]] = 1.0
    prop.loc[ranked.index[at_margin]] = slots_remaining / pool_size
    return pd.Series(prop.to_numpy(), index=rollup.index, dtype=float)


_AGGREGATE_COLUMNS = [
    "theme",
    "count_window",
    "count_recent",
    "count_baseline",
    "novelty_score",
    "rate_surprise",
    "excess_activity",
    "first_seen",
    "latest_seen",
]

_OUTPUT_COLUMNS = [*_AGGREGATE_COLUMNS, "tiebreak_key"]


def _empty_frame() -> pd.DataFrame:
    return pd.DataFrame({c: pd.Series(dtype=object) for c in _OUTPUT_COLUMNS})


def _load_window(events_dir: Path, asof: dt.date, window_days: int) -> pd.DataFrame:
    rows: list[pd.DataFrame] = []
    lo = asof - dt.timedelta(days=window_days)
    for path in sorted(events_dir.glob("*.parquet")):
        try:
            date = dt.date.fromisoformat(path.stem)
        except ValueError:
            continue
        if date < lo or date > asof:
            continue
        df = pd.read_parquet(path)
        if df.empty:
            continue
        df["_event_date"] = pd.Timestamp(date, tz="UTC")
        rows.append(df)
    if not rows:
        return pd.DataFrame()
    return pd.concat(rows, ignore_index=True)


def roll_up(
    *,
    asof: dt.date,
    events_dir: Path = DEFAULT_EVENTS_DIR,
    window_days: int = DEFAULT_WINDOW_DAYS,
    recent_days: int = DEFAULT_RECENT_DAYS,
) -> pd.DataFrame:
    """Aggregate themes across ``[asof - window_days, asof]``; score recency-vs-baseline.

    ``novelty_score = count_recent / max(count_baseline, 1) * (baseline_days / recent_days)``
    so a theme appearing at the same DAILY rate in recent vs baseline scores 1.0;
    appearing 3× more frequently in the recent window scores 3.0.
    """
    if not events_dir.exists():
        return _empty_frame()

    df = _load_window(events_dir, asof, window_days)
    if df.empty:
        return _empty_frame()

    exploded = df[["_event_date", "themes"]].explode("themes").rename(columns={"themes": "theme"})
    exploded = exploded.dropna(subset=["theme"])
    # Slugify on read so format variants ("AI ethics" / "AI_ethics") collapse to
    # ONE theme across the rolling window — a write-format change can never
    # spuriously split a theme or flag it novel. Idempotent on already-slug rows.
    exploded["theme"] = exploded["theme"].astype(str).map(slugify_theme)
    exploded = exploded[exploded["theme"] != ""]
    if exploded.empty:
        return _empty_frame()

    recent_cutoff = pd.Timestamp(asof, tz="UTC") - pd.Timedelta(days=recent_days)
    exploded["is_recent"] = exploded["_event_date"] >= recent_cutoff

    baseline_days = max(window_days - recent_days, 1)
    scale = baseline_days / max(recent_days, 1)

    grouped = exploded.groupby("theme", as_index=False).agg(
        count_window=("theme", "size"),
        count_recent=("is_recent", "sum"),
        first_seen=("_event_date", "min"),
        latest_seen=("_event_date", "max"),
    )
    grouped["count_baseline"] = (grouped["count_window"] - grouped["count_recent"]).clip(lower=0)
    # ``clip(lower=1)`` absorbs the zero-baseline edge case natively:
    # count_recent / max(count_baseline, 1) * scale == count_recent * scale
    # when count_baseline == 0, so no separate new-themes branch is required.
    grouped["novelty_score"] = (
        grouped["count_recent"] / grouped["count_baseline"].clip(lower=1)
    ) * scale
    # Sample-size-aware companion to the ratio above, recorded alongside it so a
    # later selection change can be replayed from the stored rollup instead of
    # re-run. Mapped over the SCALAR :func:`rate_surprise` rather than
    # reimplemented vectorised: one implementation cannot disagree with itself,
    # and the cost is one pass over ~11k themes once per map-themes run.
    grouped["rate_surprise"] = [
        rate_surprise(
            int(recent), int(baseline), recent_days=recent_days, baseline_days=baseline_days
        )
        for recent, baseline in zip(grouped["count_recent"], grouped["count_baseline"], strict=True)
    ]
    # Third score, same treatment: stored beside the other two, selecting on neither.
    grouped["excess_activity"] = [
        excess_activity(
            int(recent), int(baseline), recent_days=recent_days, baseline_days=baseline_days
        )
        for recent, baseline in zip(grouped["count_recent"], grouped["count_baseline"], strict=True)
    ]

    # The residual order used to fall out of `groupby(sort=True)` plus a stable
    # sort, i.e. alphabetical — measured on 17 production days, every theme
    # selected out of a fully-tied boundary pool came alphabetically before every
    # theme dropped from it. `apply_tiebreak` replaces that with the day's draw.
    return apply_tiebreak(grouped[_AGGREGATE_COLUMNS], asof=asof)


def excess_activity(
    count_recent: int,
    count_baseline: int,
    *,
    recent_days: int,
    baseline_days: int,
) -> float:
    """Recent articles above the volume the baseline rate implies, on the count scale.

    ``count_recent - (recent_days / baseline_days) * count_baseline``.

    The ratio and :func:`rate_surprise` both measure RELATIVE acceleration, so both
    rank a theme that went 0 -> 6 above a large theme running 1.5x its baseline rate.
    Measured on production, that ordering is why a 40/86 theme sat at rank 263 of
    11005 under the most conservative ratio treatment available (the lower confidence
    bound), while a 6/0 theme took first place. This score asks a different question --
    how much ADDITIONAL coverage arrived -- and answers it in articles rather than in
    multiples, so a large theme with a modest lift can outrank a small one that
    tripled.

    Negative when a theme is running BELOW its baseline rate, which is meaningful and
    deliberately not clipped.

    Telemetry only: nothing selects on it. Which of the three scores earns the
    selection decision is a question for the downstream yield data the candidate
    funnel is now collecting, not for whichever one flatters a favourite theme.
    """
    # Both windows are clamped, matching the ratio's own max(recent_days, 1). Guarding
    # only the denominator would let recent_days == 0 zero the expectation and return
    # the raw recent count -- a plausible-looking number for an undefined quantity.
    expected = (max(recent_days, 1) / max(baseline_days, 1)) * count_baseline
    return float(count_recent) - expected


def rate_surprise(
    count_recent: int,
    count_baseline: int,
    *,
    recent_days: int,
    baseline_days: int,
) -> float:
    """How unlikely is ``count_recent`` under the rate the baseline implies.

    Returned as ``-log10 P(X >= count_recent)`` for a Poisson arrival process, so
    bigger means more surprising and the scale stays readable (3.0 == a one-in-a-
    thousand week).

    This exists because :func:`roll_up`'s ``novelty_score`` — a plain
    recent/baseline ratio — is blind to how much evidence stands behind it. With
    the production constants a theme with 3 recent articles against a baseline of
    1 scores 9.86 and clears the 3.0 threshold, while 120 recent against a
    baseline of 200 scores 1.97 and is rejected; 30/50, 60/100 and 120/200 all
    score identically. Three articles outranking a hundred and twenty is not a
    threshold-tuning problem, it is the wrong statistic.

    ``+0.5`` smoothing on the baseline is deliberate: a brand-new theme has no
    baseline at all, and the unsmoothed tail probability for it is 0 — an
    infinite score that would make every first-sighting singleton outrank
    everything else forever, which is the failure this is meant to replace. It is
    a choice, not a derivation: its effect on the ordering among the smallest
    themes is unvalidated, and that is the regime this score exists to fix.

    KNOWN BIAS, measured not assumed. Poisson wants variance == mean; news
    arrivals cluster, because one story yields many articles the same day. Over
    30 days of real ``thematic_events`` the index of dispersion across the 161
    themes with >=20 occurrences has median 1.31 (p25 1.03, p75 1.63) — mild, but
    the tail runs much hotter: ``earnings`` 4.22, ``defense`` 2.33, ``inflation``
    2.14. A theme with dispersion phi has its z inflated by ~sqrt(phi), so this
    score OVERSTATES the surprise of exactly the clustered themes — roughly 2x
    for ``earnings``. That is the mechanism behind the 50-day replay in which
    switching selection to this score would have added ``earnings`` 21 times.
    Correcting it needs a per-theme dispersion estimate (quasi-Poisson or
    negative binomial), which is only possible once the per-theme daily counts
    are being stored — which is what :func:`write_theme_rollup` starts doing.

    TELEMETRY ONLY, and it MUST NOT drive selection until that dispersion estimate
    exists — the bias above runs in the same direction as the themes a selector
    would most over-pick. ``flag_novel`` still ranks on the ratio.
    """
    if count_recent <= 0:
        return 0.0
    # Imported here, not at module scope: `themes` is reachable from CLI startup
    # and scipy is not cheap to import, while the edgar-detect poller (which
    # never touches this function) fires every 15 minutes.
    from scipy.stats import poisson

    expected = (count_baseline + 0.5) / max(baseline_days, 1) * recent_days
    log_tail = float(poisson.logsf(count_recent - 1, expected))
    if math.isfinite(log_tail):
        return -log_tail / _LN10
    # scipy's tail underflows to -inf well inside the reachable range (around
    # count_recent 500 against a 100 baseline). An infinity in a telemetry column
    # is worse than a plateau — it breaks ranking and poisons any later
    # aggregate — so fall back to the closed-form Chernoff/KL bound for a Poisson
    # upper tail, which is finite, strictly increasing in ``count_recent``, and
    # agrees with the exact value to ~1% where the two meet. Above that crossover
    # the stored score is therefore a conservative UPPER BOUND on the surprise,
    # not the tail itself — close ranks up there are not meaningfully separated.
    ratio = count_recent / expected
    return expected * (ratio * math.log(ratio) - ratio + 1.0) / _LN10


THEME_ROLLUP_COLUMNS = (
    "asof",
    "theme",
    "count_window",
    "count_recent",
    "count_baseline",
    "novelty_score",
    "novelty_rank",
    "rate_surprise",
    "rate_surprise_rank",
    "excess_activity",
    "excess_activity_rank",
    # Kept, not dropped: "was this theme first seen yesterday?" is a question a
    # replay will want and the columns are already computed upstream.
    "first_seen",
    "latest_seen",
    "selected",
    # What the day's draw was, and what chance each theme had under it. Without
    # the propensity a replay can see WHICH ten were mapped but not how likely
    # that was, and any reweighting of an alternative rule is undefined rather
    # than merely imprecise.
    "selection_propensity",
    "tiebreak_key",
    "tiebreak_seed",
    "tiebreak_version",
    "novelty_config_version",
)

# The subset of :data:`THEME_ROLLUP_COLUMNS` that carries numbers. Kept apart
# because :func:`read_theme_rollups` guarantees their dtype, and the guarantee
# has to name them: ``tiebreak_seed`` and ``tiebreak_key`` are hex STRINGS
# despite reading like numbers, and coercing either would destroy the seed the
# whole draw is reproducible from.
THEME_ROLLUP_NUMERIC_COLUMNS = (
    "count_window",
    "count_recent",
    "count_baseline",
    "novelty_score",
    "novelty_rank",
    "rate_surprise",
    "rate_surprise_rank",
    "excess_activity",
    "excess_activity_rank",
    "selection_propensity",
)


def _as_float_columns(frame: pd.DataFrame) -> pd.DataFrame:
    """Force every declared numeric column to ``float64``, missing values as NaN.

    Without this the dtype of a column is a function of what the store happens to
    hold. A column no file carries is filled with ``pd.NA`` and stays ``object``;
    the same column reads back ``float64``/NaN as soon as ONE file carries it.
    The object form is not merely untidy — ``np.isnan`` raises ``TypeError`` on
    it and so does ``.astype(float)``, which is every natural way to ask whether
    a run recorded a value. The real store is entirely in the object case today.

    A COERCION, never a fill: ``errors="coerce"`` turns "not recorded" into NaN
    and leaves a recorded 0.0 alone. The two must stay apart — a zero propensity
    is the positive claim that a theme could not have been selected, and it is
    the number an off-policy estimator divides by.
    """
    for column in THEME_ROLLUP_NUMERIC_COLUMNS:
        frame[column] = pd.to_numeric(frame[column], errors="coerce").astype("float64")
    return frame


def write_theme_rollup(
    asof: dt.date,
    rollup: pd.DataFrame,
    *,
    selected: Sequence[str],
    out_dir: Path,
    novelty_config_version: str,
    threshold: float,
    max_themes: int,
) -> Path | None:
    """Persist the day's FULL theme ranking; return its path, or ``None`` if empty.

    One row per theme in the window — not per theme that was mapped. Selection
    keeps ten and drops the rest, so without this the question "what would a
    different rule have picked on that day?" is unanswerable once the day passes.
    Both scores are stored with their own rank, so any policy that is a function
    of the counts can be replayed offline against real days for the price of a
    parquet read, with no LLM calls and no change to production.

    ``selected`` is the set of themes actually handed to the mapper, which is the
    ratio's top-N AFTER truncation — recording the flag separately means a replay
    never has to reconstruct the truncation rule to know what really happened.

    ``threshold`` and ``max_themes`` are the cut the caller really applied. They
    are not stored for their own sake — they are what :func:`selection_propensity`
    needs, and only the caller knows both.
    """
    if rollup is None or rollup.empty:
        return None
    frame = rollup.copy()
    frame["asof"] = asof.isoformat()
    frame["selected"] = frame["theme"].isin(set(selected))
    frame["novelty_config_version"] = novelty_config_version
    frame["selection_propensity"] = selection_propensity(
        frame, threshold=threshold, max_themes=max_themes
    )
    # The seed is stored so a run is reproducible from the file alone: seed +
    # theme + the version string is everything `tiebreak_keys` consumed.
    frame["tiebreak_seed"] = tiebreak_seed(asof)
    frame["tiebreak_version"] = TIEBREAK_VERSION
    # Dense 1-based ranks, highest score first. Computed here rather than left to
    # the reader so a replay compares the ranks that were really produced that day.
    frame["novelty_rank"] = (
        frame["novelty_score"].rank(ascending=False, method="min").astype("Int64")
    )
    frame["rate_surprise_rank"] = (
        frame["rate_surprise"].rank(ascending=False, method="min").astype("Int64")
    )
    frame["excess_activity_rank"] = (
        frame["excess_activity"].rank(ascending=False, method="min").astype("Int64")
    )
    # ``reindex`` below fixes the schema, which also means a renamed or misspelled
    # key above would ship as a silently all-null column instead of failing. Say so
    # once, at WARNING, rather than raising — the whole write is telemetry and its
    # caller swallows exceptions, so an abort here would cost the day's rollup.
    missing = [c for c in THEME_ROLLUP_COLUMNS if c not in frame.columns]
    if missing:
        logger.warning(
            "theme-rollup %s is missing %s — the column(s) will be written all-null; "
            "write_theme_rollup and THEME_ROLLUP_COLUMNS have drifted apart",
            asof.isoformat(),
            missing,
        )
    frame = frame.reindex(columns=list(THEME_ROLLUP_COLUMNS))
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{asof.isoformat()}.parquet"
    write_parquet_atomic(frame, path, index=False)
    return path


def read_theme_rollups(store_dir: Path = DEFAULT_THEME_ROLLUP_DIR) -> pd.DataFrame:
    """Read the whole rollup store, one file at a time, unioning their schemas.

    USE THIS, NOT ``pd.read_parquet(store_dir)``. A directory read hands the path
    to pyarrow as a DATASET, and a dataset takes its schema from the FIRST
    fragment: the files that predate a column are read as if the column had never
    been added, and the rows that carry it come back without it. There is no
    error, no warning and no null marker — the column is simply absent from the
    frame. The store on disk has exactly that shape (the tie-break columns
    arrived in 2026-08, the earliest files are from before it), and the earlier
    files sort first, so the naive read drops precisely
    ``selection_propensity`` — the column the store exists for.

    Per-file reads plus ``concat`` make the union explicit: a file that lacks a
    column contributes NaN for it, which is what "this run recorded no
    propensity" means. NaN and not 0.0 — a zero propensity is the positive claim
    that a theme could not have been selected, and a file written before the
    draw existed makes no claim at all. Filling it would manufacture the weights
    an off-policy estimator divides by.

    Declared columns lead the frame in :data:`THEME_ROLLUP_COLUMNS` order; any
    column a legacy file carries that the schema has since dropped follows,
    rather than being discarded — a reader that hides data is how this defect
    started. Rows come back in filename (i.e. date) order. Legacy files are NEVER
    rewritten to fit: their schema IS the record of what that run stored.

    Every column in :data:`THEME_ROLLUP_NUMERIC_COLUMNS` comes back as
    ``float64`` whatever the store contains, so the caller's dtype does not
    depend on which files happen to be on disk — see :func:`_as_float_columns`.
    """
    store_dir = Path(store_dir)
    frames: list[pd.DataFrame] = []
    if store_dir.exists():
        for path in sorted(store_dir.glob("*.parquet")):
            frames.append(pd.read_parquet(path))
    if not frames:
        empty = pd.DataFrame({c: pd.Series(dtype=object) for c in THEME_ROLLUP_COLUMNS})
        return _as_float_columns(empty)
    frame = pd.concat(frames, ignore_index=True)
    for column in THEME_ROLLUP_COLUMNS:
        if column not in frame.columns:
            frame[column] = pd.NA
    frame = _as_float_columns(frame)
    extra = [c for c in frame.columns if c not in THEME_ROLLUP_COLUMNS]
    return frame[[*THEME_ROLLUP_COLUMNS, *extra]]


def flag_novel(
    rollup: pd.DataFrame, *, threshold: float = DEFAULT_NOVELTY_THRESHOLD
) -> pd.DataFrame:
    """Filter the rollup to themes whose ``novelty_score`` clears the threshold."""
    if rollup.empty:
        return rollup
    return rollup[rollup["novelty_score"] >= threshold].reset_index(drop=True)


__all__ = [
    "DEFAULT_EVENTS_DIR",
    "DEFAULT_MAX_THEMES",
    "DEFAULT_NOVELTY_THRESHOLD",
    "DEFAULT_RECENT_DAYS",
    "DEFAULT_THEME_ROLLUP_DIR",
    "DEFAULT_WINDOW_DAYS",
    "THEME_ROLLUP_COLUMNS",
    "THEME_ROLLUP_NUMERIC_COLUMNS",
    "TIEBREAK_VERSION",
    "apply_tiebreak",
    "excess_activity",
    "flag_novel",
    "novelty_config_version",
    "rate_surprise",
    "read_theme_rollups",
    "roll_up",
    "selection_propensity",
    "tiebreak_keys",
    "tiebreak_seed",
    "write_theme_rollup",
]
