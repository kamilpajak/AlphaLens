"""Operating characteristics of the #1227 registration, measured on today's panel.

WHY THIS EXISTS. `atr_confirmation_prereg_2026_09.md` §4 publishes a table of
promotion probabilities, and §5 discloses that the "evidence against" arm fires
"about 13% of the time even if the true effect is exactly zero". Both rest on an
implied slope standard error of 0.0552 which §9 states was **back-derived from
the recorded power curve rather than measured** — and that curve was WITHDRAWN on
2026-09-24 because it had been computed on a panel counted in the wrong unit. The
memo says in two places that the operating characteristics must be re-derived
once the panel reaches its floors. The panel reached them; this is the
re-derivation.

It cannot be done by reading the panel. The real standard error of the registered
slope is a fact about held-out `sel_ar_20` values, and reading those IS the
one-shot look. So it is measured the only other way available: simulate on the
REAL held-out cluster structure with `a20_power.simulate_panel`, then apply the
confirmation script's own `atr_slope` and `slope_ci` to each simulated panel. The
spread of those slopes is the standard error the registration actually has.

WHAT THIS READS, AND WHAT IT DOES NOT. Burnt outcomes (brief_date <=
`DISCOVERY_CUTOFF`) are spent under ledger rule 3 and `a20_power.py` reads them
for exactly this purpose: scale, ICC and the signal correlation structure. The
held-out side is read ONLY through `a20_power.HELD_OUT_COLUMNS`, an allowlist
carrying no outcome and no signal column, which supplies the cluster sizes.
Every outcome any slope here is computed against is simulated. No held-out
`sel_ar_*` value is read, so this is not a look and charges nothing.

`assert_no_stored_outcome` is the mechanical half of that claim: it refuses any
frame carrying a column the stamper writes. It is pinned against
`selection_label.VALUE_KEYS` rather than a prefix pattern, because a prefix list
goes stale the day a label family is added and the test would not notice.

THIS REGISTERS NOTHING. It changes no frozen constant and reports no verdict
about ATR. It describes the instrument, not the measurement.

Usage:

    python 2026_11_a20_operating_characteristics.py --mode slopes
    python 2026_11_a20_operating_characteristics.py --mode arms

`slopes` walks the grid §4 uses and reports the standard error plus the three
tilt arms, from the point estimate alone. `arms` is the expensive mode: it also
builds the registered cluster-bootstrap interval for every simulated panel, which
is what the "evidence against" arm needs and what a normal approximation cannot
supply. Recorded runs and their seeds:
`docs/research/atr_confirmation_prereg_2026_09.md` section 10.
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

#: Grid of true |rho| for the slope mode. The outer six match the table in
#: section 4 of the memo; 0.03 is added because it is the pessimistic point the
#: interval mode walks, and its three tilt arms deserve the same 3000 panels as
#: the rest rather than the interval mode's 400.
SLOPE_GRID: tuple[float, ...] = (0.0, 0.03, 0.05, 0.10, 0.15, 0.189, 0.25)
SLOPE_SIMS = 3000
SLOPE_SEED = 20261106

#: The interval mode is about 5 s per simulated panel, so it walks two points:
#: a true zero, which is the one the registration discloses a figure for, and a
#: deliberately PESSIMISTIC point. 0.03 is NOT ATR's burnt effect — that is
#: -0.358, which this script prints on every run — it is the order of magnitude
#: the news-axis candidates showed on the burnt panel
#: (`2026_10_jev_discovery_burnt.py`), carried here as "ATR's true effect has
#: collapsed to under a tenth of its in-sample value".
ARM_RHOS: tuple[float, ...] = (0.0, 0.03)

#: ONE run of this many panels does NOT pin the evidence-against arm, and the
#: binomial error it prints understates the spread. The arm fires when a panel
#: has BOTH a small |beta| and an unusually narrow interval, and the half-width
#: carries an sd of 0.0127 against a mean of 0.087 - 15% of its own size - so the
#: firing event sits in a JOINT tail. Measured 2026-11-07 across eight seeds at a
#: true zero: 18.50, 19.75, 19.75, 20.00, 20.75, 21.75, 24.25 and 28.00 percent,
#: every one of them reproducible at its seed. Between-seed sd 3.11pp against a
#: binomial 2.06pp. The published figure pools the seeds; a single run is a
#: datapoint, not the answer. Memo section 10.3.
ARM_SIMS = 400

#: Disjoint from every seed `SLOPE_GRID` reaches. The two modes must not draw
#: the SAME panels, or the memo's cross-check between them compares a sample
#: with its own prefix: at `ARM_SEED = SLOPE_SEED + 1` the arms-mode rho=0
#: panels were byte-identical to the first 400 slopes-mode rho=0 panels, and the
#: memo called that "two independent instruments". `test_the_two_modes_cannot_
#: draw_the_same_panels` is the guard.
ARM_SEED = 20261206

#: Offset for the interval bootstrap's own seed, so a panel draw and the
#: bootstrap resampling of that same panel never start from one number.
CI_SEED_OFFSET = 1_000_000

#: How often the interval mode prints a progress line. Display only.
ARM_PROGRESS_EVERY = 50

#: One-sided 0.05 normal quantile. Used ONLY to locate the significance boundary
#: for the floor-binds check; the registered test is a wild cluster bootstrap
#: (`one_sided_p`, B = 9999) and this is a normal approximation to it, disclosed
#: in the memo's section 10.6.
ONE_SIDED_95_Z = 1.6449

#: The shrinkage the power gate itself applies to the nuisance signals.
NUISANCE_SHRINKAGE = 0.50

#: The signal this registration tests, by its key in `a20_power.SIGNALS`.
ATR_KEY = "atr"

#: Default effect-to-column order. `a20_power.SIGNALS` is the authority and the
#: runtime path passes `tuple(power.SIGNALS)`; this literal exists so the pure
#: helper is callable in a test without loading the power script.
ATR_FIRST_ORDER: tuple[str, ...] = ("atr", "ma50", "press")

#: The figure §4 publishes, carried here so the output states what it replaces.
PUBLISHED_IMPLIED_SE = 0.0552

#: Event-window labels from before `sel_ar_20`. Not in `VALUE_KEYS`, still an
#: outcome, and still present in older parquets.
_LEGACY_OUTCOME_PREFIX = "car_"

#: Where the held-out cluster structure comes from. `gate` is the panel the
#: confirmation will actually run on — labels joined to the brief store for the
#: signal columns, through the confirmation script's own blind path. `power` is
#: the label store alone, which is what `a20_power.py` counts for the power
#: figure. Measured 2026-11-06 the two differ: 385 episodes in 63 clusters
#: against 398 in 64, because 19 resolved briefed label rows carry no brief row
#: (all of them `pre_open_recovered` names, which the journal stores by name and
#: so without signals). `gate` is the default because this probe publishes
#: characteristics OF THE REGISTERED TEST, and that test runs on the join.
STRUCTURE_SOURCES: tuple[str, ...] = ("gate", "power")
DEFAULT_STRUCTURE = "gate"


class BlindnessError(RuntimeError):
    """A frame that should carry structure only carries a stored outcome."""


class FloorDoesNotBindError(RuntimeError):
    """The significance boundary moved past DELTA, so the promote arm is no
    longer the floor alone and the slope-only grid would overstate it."""


def _load_sibling(stem: str, name: str) -> Any:
    """Load a sibling script by path; its filename is not an identifier."""
    path = Path(__file__).resolve().parent / f"{stem}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # In sys.modules BEFORE it executes: @dataclass resolves its own class's
    # module to read annotations, and a module missing from the table makes that
    # lookup return None.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def assert_no_stored_outcome(frame: pd.DataFrame) -> None:
    """Refuse a frame carrying any label value the stamper writes.

    Pinned against `VALUE_KEYS`, the stamper's own list, so adding a label family
    cannot silently widen what a "structure only" read is allowed to return. The
    simulated outcome this probe grades is written under the registered name
    AFTER this check, never before it.
    """
    from alphalens_pipeline.feedback.selection_label import VALUE_KEYS

    offending = [
        column
        for column in frame.columns
        if column in set(VALUE_KEYS) or str(column).startswith(_LEGACY_OUTCOME_PREFIX)
    ]
    if offending:
        raise BlindnessError(
            f"structure read returned stored outcome columns {sorted(offending)}; "
            f"reading a held-out label value IS the one-shot look"
        )


def classify_tilt(beta: float, delta: float) -> str:
    """The registration's tilt mapping, as a function of the slope alone.

    This is `decide()`'s mapping with the significance leg assumed satisfied,
    which holds because the FLOOR binds: the one-sided boundary sits closer to
    zero than -delta, so a slope past the floor has already cleared the p-value.
    `tests/test_a20_operating_characteristics.py` pins every branch against
    `decide()` itself rather than against this description.
    """
    if beta <= -delta:
        return "confirmed"
    if beta < 0.0:
        return "unchanged"
    return "retired"


def is_evidence_against(lo: float, hi: float, delta: float) -> bool:
    """Arm (iii): the whole interval strictly inside (-delta, +delta).

    Containment is STRICT, as §5 states: an interval whose endpoint sits exactly
    on the floor has not excluded it.
    """
    return lo > -delta and hi < delta


def inject_effects(
    burnt: Mapping[str, float],
    *,
    rho: float,
    sd_y: float,
    order: Sequence[str] = ATR_FIRST_ORDER,
) -> dict[str, float]:
    """Effects to simulate: the grid's ATR effect, the nuisance signals halved.

    `rho` indexes the grid by MAGNITUDE and the injected ATR effect always
    carries the registered negative direction. A positive ATR effect is the July
    kill trigger, not a point on this grid, so a signed argument must not be able
    to flip the direction under test.

    `order` IS THE EFFECT-TO-COLUMN MAPPING. `a20_power.simulate_panel` takes
    `names = list(effects)` and multiplies `effects[name]` by the design's i-th
    column in that order, so the returned dict's insertion order decides which
    signal each effect lands on. It must therefore match the column order of
    `burnt_signals`, which is built from `a20_power.SIGNALS`. Getting this wrong
    does not raise: measured 2026-11-06, an order of (ma50, press, atr) put the
    MA50 effect on the ATR column and reported 84.8% promotion under a true zero.
    """
    missing = [name for name in order if name not in burnt]
    uncovered = [name for name in burnt if name not in order]
    if missing or uncovered:
        raise ValueError(
            f"order {tuple(order)} does not match the burnt effects {tuple(burnt)}; "
            f"missing {missing}, uncovered {uncovered}. The order maps effects onto "
            f"simulated design columns, so a mismatch silently mislabels a signal."
        )
    injected: dict[str, float] = {}
    for name in order:
        injected[name] = (
            -abs(rho) * sd_y if name == ATR_KEY else burnt[name] * NUISANCE_SHRINKAGE * sd_y
        )
    return injected


def simulated_frame(
    *,
    outcome: np.ndarray,
    design: np.ndarray,
    clusters: np.ndarray,
    outcome_name: str,
    signal_columns: Sequence[str],
) -> pd.DataFrame:
    """A panel in the shape `atr_slope` and `slope_ci` expect.

    `simulate_panel` returns a design whose FIRST column is the intercept.
    Carrying it through as a signal would hand the shared OLS a duplicated
    intercept and a rank-deficient design, which does not raise — the fit uses a
    pseudo-inverse — so it is dropped here by offsetting the column index.
    """
    frame = pd.DataFrame({outcome_name: outcome, "arrival": clusters})
    for position, column in enumerate(signal_columns):
        frame[column] = design[:, position + 1]
    return frame


def monte_carlo_error(frequency: float, count: int) -> float:
    """Standard error of a simulated frequency. NaN at a zero count."""
    if count <= 0:
        return float("nan")
    return float(np.sqrt(frequency * (1.0 - frequency) / count))


def cluster_sizes_from(
    *, source: str, power: Any, conf: Any, labels_dir: Path, briefs_dir: Path
) -> list[int]:
    """Held-out episodes per arrival cluster, from one of two instruments.

    The `gate` path calls the confirmation script's `_held_out_episodes` with
    `with_signals=False`. That is the preflight path and reads no outcome column.
    `held_out_panel` is the other entry point into the same panel and its
    docstring says calling it IS the start of the look, so the choice of function
    here is load-bearing and is pinned by test.
    """
    if source not in STRUCTURE_SOURCES:
        raise ValueError(
            f"unknown structure source {source!r}; expected one of {STRUCTURE_SOURCES}"
        )
    if source == "gate" and conf is None:
        raise ValueError(
            "the 'gate' structure needs the confirmation module: the panel it counts is "
            "the label store joined to the brief store. Pass conf=, or ask for 'power'."
        )
    if source == "power" and power is None:
        raise ValueError("the 'power' structure needs the a20_power module; pass power=")
    if source == "power":
        held = power.load_held_out(labels_dir)
        assert_no_stored_outcome(held)
        return list(
            power.held_out_episodes_by_arrival(held, population=power.POPULATION_BRIEFED).values()
        )
    panel = conf._held_out_episodes(labels_dir, briefs_dir, with_signals=False)
    assert_no_stored_outcome(panel)
    return [int(count) for count in panel.groupby("arrival").size().to_numpy()]


def structure_and_scale(
    power: Any,
    labels_dir: Path,
    briefs_dir: Path,
    *,
    conf: Any = None,
    structure: str = DEFAULT_STRUCTURE,
) -> dict[str, Any]:
    """Burnt scale and ICC, held-out cluster sizes, burnt signal rows.

    One read, used by both modes, so the two tables in section 10 of the memo describe one panel.
    """
    burnt = power.burnt_panel(labels_dir, briefs_dir, population=power.POPULATION_BRIEFED)
    sizes = cluster_sizes_from(
        source=structure, power=power, conf=conf, labels_dir=labels_dir, briefs_dir=briefs_dir
    )
    outcomes = burnt[power.OUTCOME].astype(float).to_numpy()
    return {
        "burnt_episodes": len(burnt),
        "structure": structure,
        "cluster_sizes": sizes,
        "sd_y": float(np.std(outcomes)),
        "icc": power.estimate_icc_local(outcomes, burnt["arrival"].to_numpy()),
        "effects": power.standardised_effects(burnt),
        "signals": np.column_stack(
            [power._z(burnt[column].astype(float).to_numpy()) for column in power.SIGNALS.values()]
        ),
    }


def _slopes_at(
    *, power: Any, conf: Any, shared: dict[str, Any], rho: float, sims: int, seed: int
) -> np.ndarray:
    """One slope per simulated panel at a true ATR effect of `rho`."""
    injected = inject_effects(
        shared["effects"], rho=rho, sd_y=shared["sd_y"], order=tuple(power.SIGNALS)
    )
    columns = [conf.ATR, *conf.COVARIATES]
    rng = np.random.default_rng(seed)
    out = np.empty(sims)
    for index in range(sims):
        outcome, design, clusters = power.simulate_panel(
            burnt_signals=shared["signals"],
            cluster_sizes=shared["cluster_sizes"],
            effects=injected,
            sd_y=shared["sd_y"],
            icc=shared["icc"],
            rng=rng,
        )
        out[index] = conf.atr_slope(
            simulated_frame(
                outcome=outcome,
                design=design,
                clusters=clusters,
                outcome_name=conf.OUTCOME,
                signal_columns=columns,
            )
        )
    return out


def _report_slopes(power: Any, conf: Any, shared: dict[str, Any], *, sims: int, seed: int) -> None:
    null = _slopes_at(power=power, conf=conf, shared=shared, rho=0.0, sims=sims, seed=seed)
    se = float(np.std(null, ddof=1))
    boundary = -ONE_SIDED_95_Z * se
    print(f"\nstandard error of the registered slope: {se:.4f}  ({sims} panels, seed {seed})")
    print(
        f"the figure section 4 publishes:         {PUBLISHED_IMPLIED_SE:.4f}  "
        f"-> ratio {se / PUBLISHED_IMPLIED_SE:.2f}"
    )
    print(f"one-sided 0.05 significance boundary:   {boundary:.4f}")
    binds = boundary > -conf.DELTA
    print(
        f"the FLOOR binds (boundary closer to zero than -{conf.DELTA}): {'yes' if binds else 'NO'}"
    )
    if not binds:
        # `classify_tilt` grades the promote arm as `beta <= -DELTA` alone, which
        # is the registered rule ONLY while the floor binds. Printing the grid
        # anyway would publish a table that overstates P(promote) by the whole
        # p-value leg, in the same format as a valid one. Everything else in this
        # file refuses rather than prints a wrong number.
        raise FloorDoesNotBindError(
            f"the one-sided boundary is {boundary:.4f}, past the floor of -{conf.DELTA}, so "
            f"the promote arm is no longer the floor alone and this grid would overstate it. "
            f"The standard error above stands; the table does not."
        )
    print(f"\n{'true |rho|':>10} {'confirmed':>12} {'unchanged':>12} {'RETIRED':>12}")
    for rho in SLOPE_GRID:
        slopes = _slopes_at(
            power=power,
            conf=conf,
            shared=shared,
            rho=rho,
            sims=sims,
            seed=seed + 1 + SLOPE_GRID.index(rho),
        )
        arms = [classify_tilt(float(beta), conf.DELTA) for beta in slopes]
        shares = {
            arm: arms.count(arm) / float(sims) for arm in ("confirmed", "unchanged", "retired")
        }
        print(
            f"{rho:10.3f} "
            f"{100 * shares['confirmed']:11.1f}% "
            f"{100 * shares['unchanged']:11.1f}% "
            f"{100 * shares['retired']:11.1f}%"
        )


def _report_arms(power: Any, conf: Any, shared: dict[str, Any], *, sims: int, seed: int) -> None:
    columns = [conf.ATR, *conf.COVARIATES]
    print(f"\nregistered interval: CI_BOOT {conf.CI_BOOT}, CI_LEVEL {conf.CI_LEVEL}")
    print("section 5 discloses the evidence-against arm at about 13% under a true zero")
    for rho in ARM_RHOS:
        injected = inject_effects(
            shared["effects"], rho=rho, sd_y=shared["sd_y"], order=tuple(power.SIGNALS)
        )
        rng = np.random.default_rng(seed + round(rho * 1000))
        inside = 0
        arms: list[str] = []
        widths: list[float] = []
        betas: list[float] = []
        firing: list[float] = []
        for index in range(sims):
            outcome, design, clusters = power.simulate_panel(
                burnt_signals=shared["signals"],
                cluster_sizes=shared["cluster_sizes"],
                effects=injected,
                sd_y=shared["sd_y"],
                icc=shared["icc"],
                rng=rng,
            )
            panel = simulated_frame(
                outcome=outcome,
                design=design,
                clusters=clusters,
                outcome_name=conf.OUTCOME,
                signal_columns=columns,
            )
            beta = conf.atr_slope(panel)
            lo, hi = conf.slope_ci(panel, seed=seed + CI_SEED_OFFSET + index)
            betas.append(beta)
            widths.append(hi - lo)
            arms.append(classify_tilt(beta, conf.DELTA))
            if is_evidence_against(lo, hi, conf.DELTA):
                inside += 1
                # The registered interval is an equal-tail percentile interval
                # over resampled clusters and is NOT symmetric about the point
                # estimate, so "the arm needs |beta| below DELTA - half-width"
                # is an approximation. Record the estimates that actually fired
                # it, so the memo can quote a measured range instead.
                firing.append(beta)
            if (index + 1) % ARM_PROGRESS_EVERY == 0:
                # This mode runs for over an hour at the recorded settings and
                # prints nothing until a rho finishes, which reads as a hang.
                # A print cannot move a result: every draw comes from `rng` and
                # `slope_ci`'s own seeded generator.
                print(
                    f"   ... rho {rho:.2f}: {index + 1}/{sims} panels, "
                    f"evidence-against so far {100 * inside / (index + 1):.1f}%",
                    flush=True,
                )
        share = inside / float(sims)
        half = float(np.mean(widths)) / 2.0
        print(f"\ntrue |rho| {rho:.2f}, {sims} panels, seed {seed + round(rho * 1000)}")
        print(
            f"   evidence-against arm fires {100 * share:.1f}% "
            f"+-{100 * monte_carlo_error(share, sims):.1f}pp  ({inside}/{sims})"
        )
        print(
            f"   mean {int(conf.CI_LEVEL * 100)}% interval half-width {half:.4f}; "
            f"a symmetric reading would need |slope| below {conf.DELTA - half:+.4f}"
        )
        if firing:
            print(
                f"   MEASURED: the arm fired on slopes in [{min(firing):+.4f}, {max(firing):+.4f}]"
            )
        else:
            print("   MEASURED: the arm never fired, so it has no firing range")
        print(f"   sd of the slope {float(np.std(betas, ddof=1)):.4f}")
        for arm in ("confirmed", "unchanged", "retired"):
            frequency = arms.count(arm) / float(sims)
            print(
                f"   {arm:>10}: {100 * frequency:5.1f}% "
                f"+-{100 * monte_carlo_error(frequency, sims):.1f}pp"
            )


def main(argv: list[str] | None = None) -> int:
    import argparse

    home = Path.home() / ".alphalens"
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--mode", choices=("slopes", "arms"), required=True)
    parser.add_argument("--structure", choices=STRUCTURE_SOURCES, default=DEFAULT_STRUCTURE)
    parser.add_argument("--labels-dir", type=Path, default=home / "selection_labels")
    parser.add_argument("--briefs-dir", type=Path, default=home / "thematic_briefs")
    parser.add_argument("--sims", type=int, default=None)
    parser.add_argument("--seed", type=int, default=None)
    args = parser.parse_args(argv)

    power = _load_sibling("a20_power", "_a20_power_for_opchar")
    conf = _load_sibling("2026_09_a20_atr_confirmation", "_a20_confirmation_for_opchar")
    shared = structure_and_scale(
        power, args.labels_dir, args.briefs_dir, conf=conf, structure=args.structure
    )

    sizes = shared["cluster_sizes"]
    print(
        f"burnt episodes {shared['burnt_episodes']} | "
        f"held-out structure {sum(sizes)} episodes in {len(sizes)} clusters "
        f"(source: {shared['structure']})"
    )
    print(
        f"sd(y) {shared['sd_y']:.4f} | icc {shared['icc']:.3f} | burnt standardised effects "
        + ", ".join(f"{name} {shared['effects'][name]:+.3f}" for name in power.SIGNALS)
    )
    print(f"registered rule: p_one_sided < {conf.ALPHA} AND slope <= -{conf.DELTA}")
    print(
        f"nuisance signals held at {int(NUISANCE_SHRINKAGE * 100)}% of their burnt effects, "
        f"the power gate's own shrinkage"
    )

    if args.mode == "slopes":
        _report_slopes(
            power,
            conf,
            shared,
            sims=SLOPE_SIMS if args.sims is None else args.sims,
            seed=SLOPE_SEED if args.seed is None else args.seed,
        )
    else:
        _report_arms(
            power,
            conf,
            shared,
            sims=ARM_SIMS if args.sims is None else args.sims,
            seed=ARM_SEED if args.seed is None else args.seed,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
