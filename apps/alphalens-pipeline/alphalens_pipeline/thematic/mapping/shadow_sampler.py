"""Draw the themes the selector did NOT pick, for the shadow-arm measurement.

Implements the sampling clauses of
``docs/research/theme_shadow_arm_contract_2026_08_23.md``. Where this file and
a clause disagree, the clause wins: it was committed first, and a script
drifting from its own contract is the failure this project has paid for most.

The question the draw serves: the mapper proposes mostly mega-caps, and that can
come either from WHICH themes get picked or from how the model behaves inside a
theme. This arm addresses the first. It asks the mapper about themes the
selector passed over, records the answer, and **never lets it reach a card**.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from alphalens_pipeline.thematic.extraction.themes import (
    _RANKING_KEYS,
    DEFAULT_MAX_THEMES,
    DEFAULT_NOVELTY_THRESHOLD,
)

# Contract §3. Ten from each band, twenty a day.
SHADOW_THEMES_PER_BAND = 10
# Contract §3: the selector's own novelty threshold PLUS an article floor. The
# unrestricted eligible pool ran to 3099 themes on 2026-08-21, of which only 226
# had five or more recent articles; the rest is single-article noise no ranking
# would reach, and comparing against it would answer a question nobody asked.
MIN_COUNT_RECENT = 5
# The band boundary: "near" is what a small change to the selector could reach.
NEAR_BAND_END = 30
# Bump on any change to how the draw is derived. A stored draw must never be
# ambiguous about which rule produced it.
SHADOW_DRAW_VERSION = "sha256-asof-shadow-v1"

SHADOW_STORE_DIR = Path.home() / ".alphalens" / "theme_shadow"

# Present on every written row even when the mapper returned nothing at all, so
# the read and the metrics emitter never need to ask whether a column exists.
CORE_COLUMNS = ("theme", "ticker", "bracket_verdict")


@dataclass(frozen=True)
class ShadowDraw:
    """One day's shadow themes, split into the contract's two bands."""

    asof: dt.date
    near: list[str]
    far: list[str]
    seed: str
    eligible_pool: int

    @property
    def all_themes(self) -> list[str]:
        return [*self.near, *self.far]

    @staticmethod
    def seed_for(asof: dt.date) -> str:
        """Date-derived seed.

        ``hashlib``, never the builtin ``hash()``: the latter is salted per
        process by ``PYTHONHASHSEED``, so the same day would draw differently on
        every run and the measurement would not be reproducible.
        """
        return hashlib.sha256(f"{SHADOW_DRAW_VERSION}|{asof.isoformat()}".encode()).hexdigest()


def shadow_store_path(asof: dt.date, store_dir: Path = SHADOW_STORE_DIR) -> Path:
    """Where a day's shadow output lives.

    Contract §4: outside ``thematic_candidates/`` and ``thematic_briefs/``, so
    the brief stage cannot reach it even by accident. The test asserts the path,
    not the intention.
    """
    return store_dir / f"{asof.isoformat()}.parquet"


def already_collected(asof: dt.date, *, store_dir: Path = SHADOW_STORE_DIR) -> bool:
    """True when this date's shadow draw has already been collected.

    The daily pipeline fires SIX times on the same asof. Without this the
    collector would redraw on every slot — 120 themes a day instead of the 20
    the contract fixes, six times the spend, and a sample size that depends on
    how many slots happened to succeed rather than on the design.
    """
    return shadow_store_path(asof, store_dir=store_dir).exists()


def _eligible(rollup: pd.DataFrame) -> pd.DataFrame:
    keep = rollup[
        (rollup["novelty_score"] >= DEFAULT_NOVELTY_THRESHOLD)
        & (rollup["count_recent"] >= MIN_COUNT_RECENT)
    ]
    keys = [c for c in _RANKING_KEYS if c in keep.columns]
    return keep.sort_values(keys, ascending=False).reset_index(drop=True)


def _take(pool: Sequence[str], rng: np.random.Generator, n: int) -> list[str]:
    if not len(pool):
        return []
    size = min(n, len(pool))
    picked = rng.choice(np.asarray(pool, dtype=object), size=size, replace=False)
    return [str(p) for p in picked]


def sample_shadow_themes(
    rollup: pd.DataFrame,
    *,
    asof: dt.date,
    selected: Iterable[str],
    per_band: int = SHADOW_THEMES_PER_BAND,
) -> ShadowDraw:
    """The day's shadow draw (contract §3).

    ``selected`` is excluded outright — the arms must be disjoint or the
    comparison is partly against itself. Exclusion happens BEFORE the bands are
    cut, so a selected theme never occupies a slot it would then vacate.

    A pool shorter than ``per_band`` yields what it has rather than raising: a
    thin day is a smaller sample, not a failed collection, and §9's attrition
    table is where that becomes visible.
    """
    seed = ShadowDraw.seed_for(asof)
    eligible = _eligible(rollup)
    taken = set(selected)
    unpicked = eligible[~eligible["theme"].isin(taken)]

    # Bands are cut on the ORIGINAL ranking, so "near" keeps its meaning even
    # when the selector reached deeper than the top ten on this day.
    ranks = {t: i for i, t in enumerate(eligible["theme"])}
    near_pool = [t for t in unpicked["theme"] if DEFAULT_MAX_THEMES <= ranks[t] < NEAR_BAND_END]
    far_pool = [t for t in unpicked["theme"] if ranks[t] >= NEAR_BAND_END]

    rng = np.random.default_rng(int(seed[:16], 16))
    return ShadowDraw(
        asof=asof,
        near=_take(near_pool, rng, per_band),
        far=_take(far_pool, rng, per_band),
        seed=seed,
        eligible_pool=len(eligible),
    )


def build_shadow_frame(funnel: pd.DataFrame, draw: ShadowDraw) -> pd.DataFrame:
    """The mapper's funnel for one shadow day, stamped for the read.

    Two things it does that a plain copy would not:

    * a drawn theme that produced NO proposal still gets a row, with a null
      ticker. The unit of the measurement is the theme-day (contract §2), so
      dropping the empty ones would silently raise the yield of whichever arm
      happened to have more of them — which is the arm the contract is testing;
    * a funnel row for a theme the draw never asked for is a hard error. It can
      only mean the mapper was called with the wrong list, which contaminates
      the arms, and a contaminated arm is worse than a missing day.
    """
    band = dict.fromkeys(draw.near, "near") | dict.fromkeys(draw.far, "far")
    # A day where the mapper declined EVERY theme writes a funnel with no
    # columns at all, not a funnel with zero rows. Reading `theme` off that
    # raises and the whole day is lost — yet "every drawn theme yielded
    # nothing" is a real observation the theme-day unit needs, not an error.
    if "theme" not in funnel.columns:
        funnel = pd.DataFrame({"theme": pd.Series(dtype=object)})
    stray = sorted(set(funnel["theme"].dropna().astype(str)) - set(band))
    if stray:
        raise ValueError(
            f"shadow funnel carries themes outside the draw: {stray}. "
            "The mapper was called with a list this draw did not produce."
        )

    rows = funnel.copy()
    missing = [t for t in draw.all_themes if t not in set(rows["theme"].dropna().astype(str))]
    if missing:
        blank = pd.DataFrame({"theme": missing})
        rows = pd.concat([rows, blank], ignore_index=True)

    # Stable schema regardless of what the mapper returned. A day where every
    # theme declined would otherwise produce a frame without `ticker`, and every
    # consumer — the metrics emitter, the read — would need its own guard.
    for col in CORE_COLUMNS:
        if col not in rows.columns:
            rows[col] = pd.Series([pd.NA] * len(rows), dtype=object)

    rows["shadow_band"] = rows["theme"].map(band)
    rows["shadow_seed"] = draw.seed
    rows["asof"] = draw.asof
    rows["eligible_pool"] = draw.eligible_pool
    return rows.reset_index(drop=True)


__all__ = [
    "MIN_COUNT_RECENT",
    "NEAR_BAND_END",
    "SHADOW_DRAW_VERSION",
    "SHADOW_STORE_DIR",
    "SHADOW_THEMES_PER_BAND",
    "ShadowDraw",
    "already_collected",
    "build_shadow_frame",
    "sample_shadow_themes",
    "shadow_store_path",
]
