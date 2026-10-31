"""Unit tests for the pure market-state classifier (PR-1).

``classify_state`` composes the PR-0 primitives into the 4-state trend×vol label
(+ ``unknown``) at the last bar, per the design memo
(docs/research/market_state_signal_design_2026_07_05.md §1.3). It is a pure
function of assembled trailing SPY OHLC + VIX series — no store, no network. The
I/O wrapper (``classify`` / ``enrich``) is tested separately once added.

Fixtures build ~300-bar trailing series with a SHRINKING intraday range so the
ATR% quantile stays low: that pins the vol axis on VIX (the OR's other leg),
letting each state be selected deterministically.
"""

import datetime as dt
import re
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd


def _ohlc(
    n: int = 300,
    *,
    drift: float = 0.0005,
    spread_start: float = 4.0,
    spread_end: float = 0.3,
    base: float = 100.0,
):
    """Trending close with a shrinking high/low range → recent ATR% is low."""
    idx = pd.date_range("2019-01-01", periods=n, freq="B")
    close = base * (1.0 + drift) ** np.arange(n)
    spread = np.linspace(spread_start, spread_end, n)
    high = close + spread / 2.0
    low = close - spread / 2.0
    return (
        pd.Series(close, index=idx),
        pd.Series(high, index=idx),
        pd.Series(low, index=idx),
    )


def _ohlc_high_atr(n: int = 300, *, drift: float = 0.0008):
    """The mirror of :func:`_ohlc`: an EXPANDING range, so recent ATR% sits at the
    top of its own trailing distribution (measured ``atr_pct_q == 1.0``).

    This is the fixture that exercises the decided half of the OR: with the
    realized leg above ``ATR_HIGH_Q`` the vol axis is 'high' whatever the VIX is,
    or is not."""
    return _ohlc(n=n, drift=drift, spread_start=0.3, spread_end=4.0)


def _no_vix():
    """The VIX series a caller has when FRED could not supply one."""
    return pd.Series([], dtype=float)


def _vix(n: int = 300, *, last: float = 10.0, body: float = 15.0):
    idx = pd.date_range("2019-01-01", periods=n, freq="B")
    v = np.full(n, body)
    v[-1] = last
    return pd.Series(v, index=idx)


class TestClassifyStateFourStates(unittest.TestCase):
    def test_uptrend_low_vol_is_bull_quiet(self):
        from alphalens_pipeline.market.market_state import classify_state

        close, high, low = _ohlc(drift=0.0008)
        out = classify_state(close=close, high=high, low=low, vix=_vix(last=10.0))

        self.assertEqual(out["market_state"], "bull_quiet")

    def test_uptrend_high_vix_is_bull_volatile(self):
        from alphalens_pipeline.market.market_state import classify_state

        close, high, low = _ohlc(drift=0.0008)
        out = classify_state(close=close, high=high, low=low, vix=_vix(last=30.0))

        self.assertEqual(out["market_state"], "bull_volatile")

    def test_downtrend_low_vol_is_bear_quiet(self):
        from alphalens_pipeline.market.market_state import classify_state

        close, high, low = _ohlc(drift=-0.0008)
        out = classify_state(close=close, high=high, low=low, vix=_vix(last=10.0))

        self.assertEqual(out["market_state"], "bear_quiet")

    def test_downtrend_high_vix_is_bear_volatile(self):
        from alphalens_pipeline.market.market_state import classify_state

        close, high, low = _ohlc(drift=-0.0008)
        out = classify_state(close=close, high=high, low=low, vix=_vix(last=30.0))

        self.assertEqual(out["market_state"], "bear_volatile")


class TestClassifyStateVolBoundary(unittest.TestCase):
    def test_vix_just_below_25_is_low_vol(self):
        from alphalens_pipeline.market.market_state import classify_state

        close, high, low = _ohlc(drift=0.0008)
        out = classify_state(close=close, high=high, low=low, vix=_vix(last=24.99))

        self.assertEqual(out["market_state"], "bull_quiet")

    def test_vix_just_above_25_is_high_vol(self):
        from alphalens_pipeline.market.market_state import classify_state

        close, high, low = _ohlc(drift=0.0008)
        out = classify_state(close=close, high=high, low=low, vix=_vix(last=25.01))

        self.assertEqual(out["market_state"], "bull_volatile")


class TestClassifyStateJustEitherSideOfTheSMA200(unittest.TestCase):
    """A close a HAIR either side of its SMA200 still reads by the sign.

    Renamed from ``TestClassifyStateNeutralFold`` on 2026-10-30. These two cases
    were written when a ±2% ``DIST_FLAT_BAND`` deadband sent a near-flat series
    to a ``neutral`` arm that the caller then folded by ``sign(dist200)``. Both
    the deadband and the fold are gone — the trend word is now ``dist200 >= 0``
    outright — so the old name and the "≤ band" comments described a rule the
    code no longer runs, while the assertions stayed green. A green test that
    quietly lost its subject is worse than a red one.

    What they still earn their place for: ±0.5% is the small-magnitude end of
    the sign rule, which :class:`TestTrendWordIsTheSignOfDist200` covers only at
    exactly zero and at large magnitudes. This is the region the deleted
    deadband used to swallow.
    """

    def _flat_then_last(self, last_close: float):
        # ~flat series, so |dist200| lands near zero. Shrinking spread keeps
        # ATR% low (the vol axis is not the point here; the VIX settles it).
        close, high, low = _ohlc(n=300, drift=0.0, spread_start=4.0, spread_end=0.3)
        close.iloc[-1] = last_close
        high.iloc[-1] = last_close + 0.15
        low.iloc[-1] = last_close - 0.15
        return close, high, low

    def test_a_hair_above_the_sma200_reads_bull(self):
        from alphalens_pipeline.market.market_state import classify_state

        close, high, low = self._flat_then_last(100.5)  # dist200 ≈ +0.5%
        out = classify_state(close=close, high=high, low=low, vix=_vix(last=10.0))

        self.assertGreater(out["market_state_dist200"], 0.0)
        self.assertIn(out["market_state"], {"bull_quiet", "bull_volatile"})

    def test_a_hair_below_the_sma200_reads_bear(self):
        from alphalens_pipeline.market.market_state import classify_state

        close, high, low = self._flat_then_last(99.5)  # dist200 ≈ −0.5%
        out = classify_state(close=close, high=high, low=low, vix=_vix(last=10.0))

        self.assertLess(out["market_state_dist200"], 0.0)
        self.assertIn(out["market_state"], {"bear_quiet", "bear_volatile"})


class TestTrendWordIsTheSignOfDist200(unittest.TestCase):
    """The trend word is EXACTLY ``dist200 >= 0``, and nothing else.

    ``_trend_axis`` used to return {up, down, neutral} off four facts — price vs
    SMA200, the 50/200 cross, the SMA50 slope and a +-2% deadband — and the
    caller then folded ``neutral`` back to up/down by ``sign(dist200)``. That
    fold is what made the other three inert: above the SMA200 the strict-up
    branch and the fold both say "up", below it both say "down", and the cross,
    the slope and the deadband can only route the answer to ``neutral``. So the
    bull/bear word was already ``dist200 >= 0``, measured over 990 combinations
    of (dist200, sma50, slope) straddling every boundary with zero
    counterexamples, and both ways a counterexample could exist are unreachable
    by construction: a strict "up" needs ``c > sma200`` which forces
    ``dist200 > 0``, because ``dist200`` is DERIVED as
    ``(c - sma200) / sma200`` and is NaN unless ``sma200 > 0``.

    This class pins that equivalence permanently, so a future session cannot
    reintroduce a slope or cross term believing it moves the label. Every
    assertion goes through :func:`classify_state`, never through a trend helper
    called with a freely-chosen ``dist200``: sweeping ``dist200`` independently
    of ``c`` and ``sma200`` produces states the classifier cannot build (the
    sign of ``dist200`` contradicting ``c - sma200``), and a test written that
    way fails on correct code.
    """

    def _series(self, closes: list[float]):
        """OHLC with a tight, constant intraday range (the vol axis is not the
        point here; the VIX leg settles it)."""
        n = len(closes)
        idx = pd.date_range("2019-01-01", periods=n, freq="B")
        close = pd.Series(closes, index=idx, dtype=float)
        return close, close + 0.15, close - 0.15

    def _death_cross_above_ma200(self):
        """A REAL death cross with price back above the SMA200 AND a FALLING
        SMA50: a long steep decline pulls SMA50 under SMA200 and keeps it
        falling, then a 3-session spike lifts the close far above SMA200.

        The move is deliberately violent. A gentler rally lifts the SMA50 too,
        which makes ``sign(slope50)`` agree with ``sign(dist200)`` and costs the
        fixture its power to catch a reintroduced slope term — measured: a
        20-session rally gives slope50 +0.114, this one gives -0.011.
        """
        return self._series(
            list(np.linspace(220.0, 100.0, 297)) + list(np.linspace(105.0, 320.0, 3))
        )

    def _golden_cross_below_ma200(self):
        """The mirror: a long rally leaves SMA50 above SMA200 and still RISING,
        then a 5-session crash puts the close far below SMA200. Same reason for
        the violence — measured slope50 +0.002 against dist200 -0.749."""
        return self._series(list(np.linspace(60.0, 180.0, 295)) + list(np.linspace(170.0, 35.0, 5)))

    def _slope50(self, close) -> float:
        """The SMA50 slope the DELETED rule used to read, at the last bar.

        The arithmetic is written out here rather than imported: it reproduces
        the removed ``normalized_slope(sma(close, 50), window=20)``, which this
        change deleted along with the rule that consulted it. Spelling it out
        keeps the pin self-contained instead of keeping a production primitive
        alive for a test.
        """
        from alphalens_pipeline.market.primitives import sma

        sma50 = sma(close, 50)
        return float((sma50.iloc[-1] - sma50.iloc[-1 - 20]) / sma50.iloc[-1])

    def _cross_facts(self, close):
        """(close, sma50, sma200) at the last bar — so each test PROVES its
        fixture really holds the cross it is named for, instead of asserting it."""
        from alphalens_pipeline.market.primitives import sma

        return (
            float(close.iloc[-1]),
            float(sma(close, 50).iloc[-1]),
            float(sma(close, 200).iloc[-1]),
        )

    def test_death_cross_above_the_ma200_still_reads_bull(self):
        from alphalens_pipeline.market.market_state import classify_state

        close, high, low = self._death_cross_above_ma200()
        c, sma50, sma200 = self._cross_facts(close)
        # The fixture is only evidence if it HOLDS the cross it claims.
        self.assertLess(sma50, sma200, "fixture does not hold a death cross")
        self.assertGreater(c, sma200, "fixture is not above its SMA200")
        # and the SMA50 slope points the OTHER way, so a rule that consulted it
        # would answer bear here. That contradiction is the fixture's power.
        self.assertLess(self._slope50(close), 0.0, "fixture's SMA50 is not falling")

        out = classify_state(close=close, high=high, low=low, vix=_vix(last=10.0))

        self.assertTrue(
            out["market_state"].startswith("bull"),
            f"a death cross above the SMA200 must still read bull; got {out['market_state']}",
        )

    def test_golden_cross_below_the_ma200_still_reads_bear(self):
        from alphalens_pipeline.market.market_state import classify_state

        close, high, low = self._golden_cross_below_ma200()
        c, sma50, sma200 = self._cross_facts(close)
        self.assertGreater(sma50, sma200, "fixture does not hold a golden cross")
        self.assertLess(c, sma200, "fixture is not below its SMA200")
        self.assertGreater(self._slope50(close), 0.0, "fixture's SMA50 is not rising")

        out = classify_state(close=close, high=high, low=low, vix=_vix(last=10.0))

        self.assertTrue(
            out["market_state"].startswith("bear"),
            f"a golden cross below the SMA200 must still read bear; got {out['market_state']}",
        )

    def test_dist200_exactly_zero_reads_bull(self):
        # The boundary itself: a perfectly flat series puts the last close ON its
        # own SMA200, so dist200 is exactly 0.0 and `>= 0` must take the bull arm.
        from alphalens_pipeline.market.market_state import classify_state

        close, high, low = self._series([100.0] * 300)

        out = classify_state(close=close, high=high, low=low, vix=_vix(last=10.0))

        self.assertEqual(out["market_state_dist200"], 0.0)
        self.assertTrue(out["market_state"].startswith("bull"))

    def _corpus(self):
        """Fixtures that between them span both sides of the SMA200, both cross
        directions and the exact boundary."""
        return {
            "death cross above ma200": self._death_cross_above_ma200(),
            "golden cross below ma200": self._golden_cross_below_ma200(),
            "dist200 exactly zero": self._series([100.0] * 300),
            "steady rally": self._series(list(np.linspace(80.0, 130.0, 300))),
            "steady decline": self._series(list(np.linspace(130.0, 80.0, 300))),
        }

    def test_the_word_is_the_sign_of_dist200_on_every_fixture(self):
        from alphalens_pipeline.market.market_state import classify_state

        for name, (close, high, low) in self._corpus().items():
            with self.subTest(fixture=name):
                out = classify_state(close=close, high=high, low=low, vix=_vix(last=10.0))
                self.assertNotEqual(out["market_state"], "unknown", "fixture decided nothing")

                expected = "bull" if out["market_state_dist200"] >= 0 else "bear"

                self.assertTrue(
                    out["market_state"].startswith(expected),
                    f"{name}: dist200={out['market_state_dist200']:+.4f} "
                    f"demands {expected}, got {out['market_state']}",
                )

    def test_positive_control_the_corpus_can_refute_a_wrong_trend_rule(self):
        # A pass above proves nothing unless these fixtures could have FAILED.
        # The most plausible wrong rule is folding by the SMA50 slope's sign
        # instead of dist200's. On this corpus the two rules must DISAGREE
        # somewhere, or the fixtures lack the power to catch a reintroduced
        # slope term and the class above is decoration.
        from alphalens_pipeline.market.market_state import classify_state

        disagreements = 0
        for close, high, low in self._corpus().values():
            out = classify_state(close=close, high=high, low=low, vix=_vix(last=10.0))
            by_dist = "bull" if out["market_state_dist200"] >= 0 else "bear"
            by_slope = "bull" if self._slope50(close) >= 0 else "bear"
            if by_dist != by_slope:
                disagreements += 1

        self.assertGreater(
            disagreements,
            0,
            "the corpus cannot tell sign(dist200) from sign(slope50), so it "
            "would not notice a reintroduced slope term",
        )


class TestTheTwoLookbacksAreWiredSeparately(unittest.TestCase):
    """``ATR_QUANTILE_LOOKBACK`` and ``VIX_DECILE_LOOKBACK`` each drive ONE
    stamped quantity, and each read site reads its own constant.

    One constant used to serve both: the ATR axis (which feeds
    ``market_state_atr_pct_q`` and, through the vol axis, the label) and the
    ``market_state_vix_decile`` telemetry column. Editing it to retune the ATR
    axis would silently redefine the published VIX decile. The split holds the
    value at 252 on both sides, so no row moved.

    These tests exist because a mutation check found the split UNPINNED. Measured
    2026-10-30 on this module's 45 tests: retuning `ATR_QUANTILE_LOOKBACK` reddens
    the emission-boundary test, but retuning `VIX_DECILE_LOOKBACK` reddened
    NOTHING, because the VIX expectation was computed from the very constant it
    was meant to pin. Both windows are now asserted as literals.
    """

    def test_the_vix_decile_is_ranked_over_its_own_lookback(self):
        from alphalens_pipeline.market.market_state import (
            VIX_DECILE_LOOKBACK,
            classify_state,
        )
        from alphalens_pipeline.market.primitives import rolling_quantile_rank

        close, high, low = _ohlc(drift=0.0005)
        # A high-vol PAST, a calm middle, a mild recent rise — so the last
        # print's percentile depends on how far back the window reaches. A
        # monotonic ramp will NOT do: it ranks 1.0 under every lookback, which
        # is how the first version of this test passed its own positive control
        # vacuously. Measured on this shape: 0.397 over 252, 1.0 over 60.
        idx = pd.date_range("2019-01-01", periods=300, freq="B")
        vix = pd.Series(
            np.concatenate(
                [
                    np.full(200, 55.0),  # a crisis long ago
                    np.full(95, 10.0),  # calm since
                    np.linspace(12.0, 20.0, 5),  # a mild recent rise
                ]
            ),
            index=idx,
        )

        out = classify_state(close=close, high=high, low=low, vix=vix)

        # The window is written as the LITERAL 252, not read from the constant.
        # Computing the expectation from `VIX_DECILE_LOOKBACK` made the oracle
        # follow the mutation: measured 2026-10-30, retuning it to 120 left all
        # 45 tests in this module green, so the split was pinned by NAME and not
        # by VALUE — cosmetic on exactly the half it was made to protect.
        self.assertEqual(VIX_DECILE_LOOKBACK, 252, "the published VIX decile window moved")
        expected = float(rolling_quantile_rank(vix, lookback=252).iloc[-1])
        self.assertAlmostEqual(out["market_state_vix_decile"], expected, places=12)
        # Positive control: a DIFFERENT window must give a different answer, or
        # the assertion above could pass under any window.
        other = float(rolling_quantile_rank(vix, lookback=60).iloc[-1])
        self.assertNotAlmostEqual(expected, other, places=6)

    def test_252_is_the_binding_window_for_emitting_a_label(self):
        # The ATR% quantile needs 252 trailing bars where the SMA200 needs 200,
        # so 252 — not 200 — is what gates emission. Pinning the boundary pins
        # ATR_QUANTILE_LOOKBACK to its read site: shortening it would let n=251
        # decide. This is also the measurement the `decision_inputs` comment
        # cites, kept executable instead of only written down.
        from alphalens_pipeline.market.market_state import classify_state

        at_251 = classify_state(
            close=_ohlc(n=251)[0],
            high=_ohlc(n=251)[1],
            low=_ohlc(n=251)[2],
            vix=_vix(n=251, last=10.0),
        )
        at_252 = classify_state(
            close=_ohlc(n=252)[0],
            high=_ohlc(n=252)[1],
            low=_ohlc(n=252)[2],
            vix=_vix(n=252, last=10.0),
        )

        self.assertEqual(at_251["market_state"], "unknown", "251 bars must not decide")
        self.assertNotEqual(at_252["market_state"], "unknown", "252 bars must decide")


class TestEachLookbackReadSiteNamesItsOwnConstant(unittest.TestCase):
    """A SOURCE gate, because no behavioural test can catch this one.

    ``ATR_QUANTILE_LOOKBACK`` and ``VIX_DECILE_LOOKBACK`` both hold 252, so
    wiring the VIX decile to the ATR constant is observationally identical today
    — measured: that mutation left all 42 behavioural tests in this module
    green. The defect would surface only once someone changed one value, which
    is exactly the silent coupling the split exists to prevent. So the wiring is
    pinned at the source, by name (the AST-gate-over-source idiom this
    repository already uses where seeded state cannot refute a claim).
    """

    def _lookback_calls(self):
        """``[(first positional arg name, lookback keyword's name), ...]`` for
        every ``rolling_quantile_rank`` call in the classifier module."""
        import ast

        from alphalens_pipeline.market import market_state

        tree = ast.parse(Path(market_state.__file__).read_text())
        found = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if not (isinstance(fn, ast.Name) and fn.id == "rolling_quantile_rank"):
                continue
            series = node.args[0].id if node.args and isinstance(node.args[0], ast.Name) else "?"
            kw = next(
                (
                    k.value.id
                    for k in node.keywords
                    if k.arg == "lookback" and isinstance(k.value, ast.Name)
                ),
                "?",
            )
            found.append((series, kw))
        return found

    def test_both_call_sites_are_found(self):
        # Positive control: if the walk finds nothing, the assertions below pass
        # vacuously and the gate is decoration.
        self.assertEqual(
            len(self._lookback_calls()),
            2,
            f"expected exactly 2 rolling_quantile_rank calls, got "
            f"{self._lookback_calls()}. The gate, not the code, needs updating.",
        )

    def test_the_vix_decile_names_the_vix_constant(self):
        calls = dict(self._lookback_calls())

        self.assertEqual(
            calls.get("vix"),
            "VIX_DECILE_LOOKBACK",
            "the VIX decile must read VIX_DECILE_LOOKBACK. Both constants are "
            "252, so wiring it to ATR_QUANTILE_LOOKBACK changes nothing today "
            "and silently redefines the published decile the day that value moves.",
        )

    def test_the_atr_axis_names_the_atr_constant(self):
        calls = dict(self._lookback_calls())

        self.assertEqual(
            calls.get("atr_pct_series"),
            "ATR_QUANTILE_LOOKBACK",
            "the ATR axis must read ATR_QUANTILE_LOOKBACK",
        )


class TestConfigVersionOwesNoBumpForThisSimplification(unittest.TestCase):
    """Removing a provably inert input owes NO version bump.

    #1559 set the governing rule: a version string has to name exactly one
    emission rule, so a changed emission rule forces a bump even when no
    threshold moves and no label changes. Dropping ``sma50_now`` and
    ``slope_now`` from the finiteness guard changes WHICH values must be finite,
    which is why that rule had to be checked rather than argued.

    It was checked, and the emission rule is unchanged in effect. The binding
    window is 252 (``ATR_QUANTILE_LOOKBACK``), not 200: measured over every
    series length 0..320, both the old six-term guard and the four-term guard
    first emit a label at n=252, and no length emits under one and blanks under
    the other. ``sma50`` is finite from n=50 and the slope from n=70, both
    strictly before ``sma200`` (n=200) and well before ``atr_pct_q`` (n=252), so
    neither deleted term can ever be the input that blanks a label. Holding a
    NaN at each of 300 positions and 3000 randomized poisoned series produced
    zero divergences.

    So the version string stays, and this test says why — a bump would orphan
    the live cohort for a simplification that changed no published row.
    """

    def test_the_version_is_pinned_once_and_elsewhere(self):
        """The string itself is pinned by ``TestConfigVersion`` in this file; this
        class exists for the REASONING, not a second copy of the assertion.

        A duplicate literal adds no detection power and makes a legitimate future
        bump a two-place edit — and missing one place would redden the suite in a
        way that reads like a real violation. What is asserted here instead is the
        part a reader gets wrong: the version did not move, and the 'sma50x200'
        token in it is an opaque poolability key, NOT a statement about which
        inputs the classifier reads. Deleting the SMA50 cross did not falsify the
        token, which is why no bump is owed.
        """
        from alphalens_pipeline.market import market_state

        # Assert on the DEFINITION, not the mention: line 80 of the module names
        # all four deleted constants in the note recording their removal, and
        # that note is the point. A bare substring check would forbid it.
        source = Path(market_state.__file__).read_text()
        self.assertIn("MARKET_STATE_CONFIG_VERSION", source)
        for dead in ("SMA_FAST", "SLOPE_WIN", "SLOPE_EPS", "DIST_FLAT_BAND"):
            # re.MULTILINE is load-bearing: assertNotRegex uses re.search, so a
            # bare `^` anchors to the START OF THE FILE and the guard cannot
            # fire. Measured 2026-10-30: without it, reinserting `SMA_FAST = 50`
            # at module level left all 45 tests green.
            self.assertNotRegex(
                source,
                re.compile(rf"^{dead}\s*=", re.MULTILINE),
                f"{dead} was deleted as provably inert; redefining it would make "
                "the version token's claim about the inputs false again",
            )


class TestClassifyStateUnknown(unittest.TestCase):
    def test_insufficient_history_is_unknown(self):
        from alphalens_pipeline.market.market_state import classify_state

        close, high, low = _ohlc(n=150)  # < 252 → ATR% quantile undefined
        out = classify_state(close=close, high=high, low=low, vix=_vix(n=150, last=10.0))

        self.assertEqual(out["market_state"], "unknown")

    def test_missing_vix_with_low_realized_vol_is_unknown(self):
        # The one branch a missing VIX genuinely leaves undetermined (#1559): the
        # realized leg is low, so a VIX at or above 25 would have flipped the OR.
        from alphalens_pipeline.market.market_state import classify_state

        close, high, low = _ohlc(drift=0.0008)
        vix = _vix(last=float("nan"))  # no VIX at asof
        out = classify_state(close=close, high=high, low=low, vix=vix)

        self.assertEqual(out["market_state"], "unknown")

    def test_empty_bars_with_present_vix_is_unknown(self):
        from alphalens_pipeline.market.market_state import classify_state

        empty = pd.Series([], dtype=float)
        out = classify_state(close=empty, high=empty, low=empty, vix=_vix(last=10.0))

        self.assertEqual(out["market_state"], "unknown")


class TestClassifyStateTelemetry(unittest.TestCase):
    def test_returns_all_columns_with_sane_ranges(self):
        from alphalens_pipeline.market.market_state import (
            MARKET_STATE_COLUMNS,
            classify_state,
        )

        close, high, low = _ohlc(drift=0.0008)
        out = classify_state(close=close, high=high, low=low, vix=_vix(last=10.0))

        # every stamped column except the enrich-added config_version is present
        for col in MARKET_STATE_COLUMNS:
            if col == "market_state_config_version":
                continue
            self.assertIn(col, out)

        self.assertGreaterEqual(out["market_state_atr_pct_q"], 0.0)
        self.assertLessEqual(out["market_state_atr_pct_q"], 1.0)
        self.assertLess(out["market_state_atr_pct_q"], 0.70)  # low-vol fixture
        self.assertAlmostEqual(out["market_state_vix"], 10.0, places=6)
        self.assertIsInstance(out["market_state_squeeze_on"], bool)


class TestConfigVersion(unittest.TestCase):
    def test_config_version_is_unvalidated_poolability_key(self):
        from alphalens_pipeline.market.market_state import MARKET_STATE_CONFIG_VERSION

        self.assertTrue(MARKET_STATE_CONFIG_VERSION.startswith("mstate-v1"))
        self.assertTrue(MARKET_STATE_CONFIG_VERSION.endswith("UNVALIDATED"))

    def test_the_version_is_the_one_bumped_for_the_stale_vix_defect(self):
        # Pinned literally, not by prefix. This key partitions a pre-registered
        # forward study, so a silent edit is a data-integrity change, not a
        # refactor. v1 -> v1.1 on 2026-09-24 (#1524): rows before it carry a VIX
        # frozen at the 2026-07-01 print. v1.1 -> v1.2 on 2026-09-25 (#1559): the
        # label mapping is unchanged, but a version string must name exactly one
        # EMISSION rule and v1.2 emits a label where v1.1 emitted 'unknown'.
        from alphalens_pipeline.market.market_state import MARKET_STATE_CONFIG_VERSION

        self.assertEqual(
            MARKET_STATE_CONFIG_VERSION,
            "mstate-v1.2-spy-sma50x200-atrq70-vix15_25-UNVALIDATED",
        )

    def test_columns_include_label_and_config_version(self):
        from alphalens_pipeline.market.market_state import MARKET_STATE_COLUMNS

        self.assertIn("market_state", MARKET_STATE_COLUMNS)
        self.assertIn("market_state_config_version", MARKET_STATE_COLUMNS)


def _seed_store(
    root: Path,
    *,
    n: int = 300,
    drift: float = 0.0008,
    spread_start: float = 4.0,
    spread_end: float = 0.3,
):
    """Populate a temp grouped-daily store with SPY (+ a NOISE ticker) bars.

    The default range SHRINKS, so recent ATR% sits at the bottom of its own
    trailing distribution and the vol axis is pinned on the VIX leg. Reverse the
    two spreads (:func:`_seed_store_high_atr`) to pin it on the realized leg.
    """
    from alphalens_pipeline.data.rs_history import write_grouped_day_atomic

    dates = pd.bdate_range("2019-01-01", periods=n)
    closes = 100.0 * (1.0 + drift) ** np.arange(n)
    spreads = np.linspace(spread_start, spread_end, n)
    for d, c, sp in zip(dates, closes, spreads, strict=True):
        spy = {"t": 0, "o": c, "h": c + sp / 2, "l": c - sp / 2, "c": c, "v": 1000, "vw": c}
        noise = {"t": 0, "o": 1, "h": 1, "l": 1, "c": 1, "v": 1, "vw": 1}
        write_grouped_day_atomic(root, d.date(), {"SPY": spy, "NOISE": noise})
    return dates


def _fake_fred(dates, *, last: float = 10.0, body: float = 15.0):
    v = np.full(len(dates), body)
    v[-1] = last
    series = pd.Series(v, index=pd.DatetimeIndex([pd.Timestamp(d) for d in dates]))

    class _Fred:
        """Records `through` so a test can prove the freshness requirement is passed."""

        def __init__(self):
            self.through_seen = []

        def fetch_series(self, series_id, *, through=None):
            assert series_id == "VIXCLS"
            self.through_seen.append(through)
            return series

    return _Fred()


class TestEnrichBroadcast(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name)
        cls.dates = _seed_store(cls.root, n=300)
        cls.asof = cls.dates[-1].date()

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_broadcasts_same_state_to_every_row(self):
        from alphalens_pipeline.market.market_state import (
            MARKET_STATE_COLUMNS,
            MARKET_STATE_CONFIG_VERSION,
            enrich,
        )

        frame = pd.DataFrame({"ticker": ["AAA", "BBB", "CCC"]})
        out = enrich(
            frame,
            asof=self.asof,
            grouped_root=self.root,
            fred_client=_fake_fred(self.dates, last=10.0),
        )

        for col in MARKET_STATE_COLUMNS:
            self.assertIn(col, out.columns)
        self.assertEqual(len(out), 3)
        self.assertIn("ticker", out.columns)  # original columns preserved
        self.assertEqual(out["market_state"].nunique(), 1)  # index-level: same value
        self.assertEqual(out["market_state"].iloc[0], "bull_quiet")
        self.assertTrue((out["market_state_config_version"] == MARKET_STATE_CONFIG_VERSION).all())

    def test_high_vix_flips_broadcast_to_bull_volatile(self):
        from alphalens_pipeline.market.market_state import enrich

        out = enrich(
            pd.DataFrame({"ticker": ["AAA"]}),
            asof=self.asof,
            grouped_root=self.root,
            fred_client=_fake_fred(self.dates, last=30.0),
        )

        self.assertEqual(out["market_state"].iloc[0], "bull_volatile")

    def test_failsoft_on_fred_error_stamps_unknown(self):
        from alphalens_pipeline.market.market_state import MARKET_STATE_COLUMNS, enrich

        class _BoomFred:
            def fetch_series(self, series_id, *, through=None):
                raise RuntimeError("FRED down")

        # A store/FRED hiccup must degrade to 'unknown', never abort the score stage.
        out = enrich(
            pd.DataFrame({"ticker": ["AAA", "BBB"]}),
            asof=self.asof,
            grouped_root=self.root,
            fred_client=_BoomFred(),
        )

        for col in MARKET_STATE_COLUMNS:
            self.assertIn(col, out.columns)
        self.assertTrue((out["market_state"] == "unknown").all())

    def test_the_vix_read_demands_a_series_reaching_the_asof(self):
        # #1524: without this the PIT truncation `vix[vix.index <= asof]` is a
        # no-op on a series that ENDS BEFORE asof, and a frozen cache is
        # indistinguishable from a quiet market. Asserted on what the client was
        # ASKED for, because the classifier cannot tell the difference itself.
        from alphalens_pipeline.market.market_state import enrich

        fred = _fake_fred(self.dates, last=10.0)
        enrich(
            pd.DataFrame({"ticker": ["AAA"]}),
            asof=self.asof,
            grouped_root=self.root,
            fred_client=fred,
        )
        self.assertEqual(fred.through_seen, [self.asof])

    def test_a_stale_series_on_a_quiet_tape_is_unknown_and_does_not_abort(self):
        # This store is the LOW-ATR one, so the realized leg cannot settle the OR
        # and a missing VIX genuinely leaves the label undetermined (#1559). The
        # score stage must still keep running and stamp the first-class token.
        from alphalens_pipeline.data.macro.fred_client import FREDStaleError
        from alphalens_pipeline.market.market_state import enrich

        class _StaleFred:
            def fetch_series(self, series_id, *, through=None):
                raise FREDStaleError("series ends 2026-07-01, 2026-09-24 requested")

        out = enrich(
            pd.DataFrame({"ticker": ["AAA", "BBB"]}),
            asof=self.asof,
            grouped_root=self.root,
            fred_client=_StaleFred(),
        )
        self.assertTrue((out["market_state"] == "unknown").all())
        self.assertEqual(len(out), 2)
        # ...but the drivers that WERE computable must survive. Before #1559 the
        # exception escaped classify() and enrich() blanked every column.
        self.assertTrue(np.isfinite(out["market_state_atr_pct_q"]).all())
        self.assertTrue(np.isfinite(out["market_state_dist200"]).all())

    def test_broadcast_squeeze_column_is_nullable_boolean(self):
        from alphalens_pipeline.market.market_state import enrich

        # populated-frame dtype must match the empty-frame schema (nullable bool)
        out = enrich(
            pd.DataFrame({"ticker": ["AAA"]}),
            asof=self.asof,
            grouped_root=self.root,
            fred_client=_fake_fred(self.dates, last=10.0),
        )

        self.assertEqual(out["market_state_squeeze_on"].dtype, pd.BooleanDtype())


class TestEnrichEmptyAndUnknown(unittest.TestCase):
    def test_empty_frame_adds_columns_with_stable_dtypes(self):
        from alphalens_pipeline.market.market_state import MARKET_STATE_COLUMNS, enrich

        # empty frame short-circuits before any I/O (store/fred untouched)
        out = enrich(pd.DataFrame(), asof=dt.date(2020, 1, 1), grouped_root=None, fred_client=None)

        for col in MARKET_STATE_COLUMNS:
            self.assertIn(col, out.columns)
        self.assertEqual(len(out), 0)
        self.assertEqual(out["market_state"].dtype, object)
        self.assertEqual(out["market_state_atr_pct"].dtype, np.dtype("float64"))

    def test_unknown_broadcast_when_store_too_short(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            dates = _seed_store(root, n=60)  # < 252 → ATR% quantile undefined → unknown
            from alphalens_pipeline.market.market_state import enrich

            out = enrich(
                pd.DataFrame({"ticker": ["AAA", "BBB"]}),
                asof=dates[-1].date(),
                grouped_root=root,
                fred_client=_fake_fred(dates, last=10.0),
            )

            self.assertTrue((out["market_state"] == "unknown").all())


class TestTheVolAxisDecidesWhatTheDisjunctionCanDecide(unittest.TestCase):
    """#1559. The vol axis is an OR, and an OR does not always need both operands.

    With the realized leg at or above ``ATR_HIGH_Q`` the answer is 'high' whatever
    the VIX is, so blanking the whole label discards a decided answer. Only the
    other branch is genuinely undetermined.
    """

    def test_high_realized_vol_decides_the_label_without_a_vix(self):
        from alphalens_pipeline.market.market_state import classify_state

        close, high, low = _ohlc_high_atr()
        out = classify_state(close=close, high=high, low=low, vix=_no_vix())

        self.assertEqual(out["market_state"], "bull_volatile")
        # the drivers that were computable are still reported
        self.assertGreaterEqual(out["market_state_atr_pct_q"], 0.70)
        self.assertTrue(np.isnan(out["market_state_vix"]))

    def test_low_realized_vol_without_a_vix_stays_undetermined(self):
        # A VIX at or above 25 would have flipped this, so there is no answer to
        # keep. Anything other than 'unknown' here is an invented label.
        from alphalens_pipeline.market.market_state import classify_state

        close, high, low = _ohlc(drift=0.0008)
        out = classify_state(close=close, high=high, low=low, vix=_no_vix())

        self.assertEqual(out["market_state"], "unknown")

    def test_the_realized_boundary_is_unmoved_by_this_change(self):
        # ATR_HIGH_Q and VIX_HIGH are frozen a-priori. #1559 changes WHEN a label
        # is emitted, never where the thresholds sit.
        from alphalens_pipeline.market.market_state import (
            ATR_HIGH_Q,
            VIX_HIGH,
            _vol_axis,
        )

        self.assertEqual(_vol_axis(ATR_HIGH_Q, 10.0), "high")
        self.assertEqual(_vol_axis(ATR_HIGH_Q - 1e-9, 10.0), "low")
        self.assertEqual(_vol_axis(0.1, VIX_HIGH), "high")
        self.assertEqual(_vol_axis(0.1, VIX_HIGH - 1e-9), "low")

    def test_the_axis_returns_none_only_on_the_undetermined_branch(self):
        from alphalens_pipeline.market.market_state import ATR_HIGH_Q, _vol_axis

        nan = float("nan")
        self.assertEqual(_vol_axis(ATR_HIGH_Q, nan), "high")
        self.assertIsNone(_vol_axis(ATR_HIGH_Q - 1e-9, nan))


def _seed_store_high_atr(root: Path, *, n: int = 300, drift: float = 0.0008):
    """:func:`_seed_store` with an EXPANDING range, so the realized leg is high."""
    return _seed_store(root, n=n, drift=drift, spread_start=0.3, spread_end=4.0)


class TestAnAbsentVixReachesTheClassifierInsteadOfBlankingEverything(unittest.TestCase):
    """The half of #1559 that the issue's own proposal would have missed.

    In production an unavailable VIX arrives as an EXCEPTION, not as an empty
    series: ``classify`` calls ``fetch_series(through=asof)``, the client raises
    ``FREDStaleError``, and before this change ``enrich``'s broad ``except``
    stamped ``_unknown_result()`` — blanking every column, ATR telemetry with it.
    A fix confined to ``classify_state`` would have been dead code.
    """

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name)
        cls.dates = _seed_store_high_atr(cls.root, n=300)
        cls.asof = cls.dates[-1].date()

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_a_stale_series_still_yields_the_label_the_realized_leg_decided(self):
        from alphalens_pipeline.data.macro.fred_client import FREDStaleError
        from alphalens_pipeline.market.market_state import enrich

        class _StaleFred:
            def fetch_series(self, series_id, *, through=None):
                raise FREDStaleError("series ends 2026-07-01, 2026-09-24 requested")

        out = enrich(
            pd.DataFrame({"ticker": ["AAA", "BBB"]}),
            asof=self.asof,
            grouped_root=self.root,
            fred_client=_StaleFred(),
        )

        self.assertTrue((out["market_state"] == "bull_volatile").all())
        self.assertTrue(np.isfinite(out["market_state_atr_pct"]).all())
        self.assertTrue(out["market_state_vix"].isna().all())

    def test_the_unfresh_series_is_never_read_as_a_fallback(self):
        # The tempting "helpful" edit is to retry without `through=` when the
        # freshness check refuses. That is the frozen-cache defect of #1524 in
        # another dress. This client hands back a stale QUIET series on the
        # unfresh call, over a LOW-ATR tape: a fallback implementation answers
        # 'bull_quiet', a correct one answers 'unknown'.
        from alphalens_pipeline.data.macro.fred_client import FREDStaleError
        from alphalens_pipeline.market.market_state import enrich

        with tempfile.TemporaryDirectory() as d:
            quiet_root = Path(d)
            quiet_dates = _seed_store(quiet_root, n=300)

            class _StaleFredWithAFallback:
                def fetch_series(self, series_id, *, through=None):
                    if through is not None:
                        raise FREDStaleError("too far behind")
                    return _fake_fred(quiet_dates, last=10.0).fetch_series(series_id)

            out = enrich(
                pd.DataFrame({"ticker": ["AAA"]}),
                asof=quiet_dates[-1].date(),
                grouped_root=quiet_root,
                fred_client=_StaleFredWithAFallback(),
            )

            self.assertEqual(out["market_state"].iloc[0], "unknown")

    def test_a_plain_fred_error_still_blanks_everything(self):
        # The narrow catch is the point. A revoked FRED_API_KEY surfaces as a
        # bare FREDError (FRED answers 400), and it is INDEFINITE: degrading it
        # to a realized-leg-only label would keep the banner looking healthy for
        # weeks. Widening the catch to the base class turns this red.
        from alphalens_pipeline.data.macro.fred_client import FREDError
        from alphalens_pipeline.market.market_state import enrich

        class _RevokedKeyFred:
            def fetch_series(self, series_id, *, through=None):
                raise FREDError("FRED returned 400 for series VIXCLS")

        out = enrich(
            pd.DataFrame({"ticker": ["AAA"]}),
            asof=self.asof,
            grouped_root=self.root,
            fred_client=_RevokedKeyFred(),
        )

        self.assertEqual(out["market_state"].iloc[0], "unknown")
        self.assertTrue(out["market_state_atr_pct"].isna().all())

    def test_the_vix_telemetry_is_the_partition_key_for_the_forward_study(self):
        # `market_state_vix` is NaN exactly when the implied leg took no part, so
        # a study can separate the two kinds of decided label without a new
        # column. Both halves asserted, or the claim is only half tested.
        from alphalens_pipeline.data.macro.fred_client import FREDStaleError
        from alphalens_pipeline.market.market_state import enrich

        class _StaleFred:
            def fetch_series(self, series_id, *, through=None):
                raise FREDStaleError("too far behind")

        without = enrich(
            pd.DataFrame({"ticker": ["AAA"]}),
            asof=self.asof,
            grouped_root=self.root,
            fred_client=_StaleFred(),
        )
        with_vix = enrich(
            pd.DataFrame({"ticker": ["AAA"]}),
            asof=self.asof,
            grouped_root=self.root,
            fred_client=_fake_fred(self.dates, last=10.0),
        )

        self.assertTrue(without["market_state_vix"].isna().all())
        self.assertTrue(np.isfinite(with_vix["market_state_vix"]).all())
        # Same decided label either way — the realized leg settled both.
        self.assertEqual(without["market_state"].iloc[0], with_vix["market_state"].iloc[0])
        # ...and both rows were DECIDED, which is the qualifier the partition
        # needs: a fail-soft blank NaNs this column too, so a study that does not
        # exclude 'unknown' first mixes "decided without the implied leg" with
        # "decided nothing at all".
        self.assertNotEqual(without["market_state"].iloc[0], "unknown")

    def test_a_blanked_row_also_has_a_nan_vix_so_the_partition_needs_the_label(self):
        # The trap the docstring now names. This row is NaN-vix and carries no
        # decision at all; only `market_state != 'unknown'` tells the two apart.
        from alphalens_pipeline.market.market_state import enrich

        class _BoomFred:
            def fetch_series(self, series_id, *, through=None):
                raise RuntimeError("not a FRED failure at all")

        blanked = enrich(
            pd.DataFrame({"ticker": ["AAA"]}),
            asof=self.asof,
            grouped_root=self.root,
            fred_client=_BoomFred(),
        )

        self.assertEqual(blanked["market_state"].iloc[0], "unknown")
        self.assertTrue(blanked["market_state_vix"].isna().all())


class TestTheLedgerRecordsTheVersionTheCodeStamps(unittest.TestCase):
    """The pre-registration is the frozen record; it must not be the stale copy.

    #1524 bumped the code to v1.1 and never touched the ledger, so the two
    disagreed from 2026-09-24 until #1559. Nothing checked it, which is why.
    """

    LEDGER = (
        Path(__file__).resolve().parents[3]
        / "docs"
        / "research"
        / "preregistration"
        / "ledger.json"
    )
    ENTRY_CLASS = "market_regime_signals_2026_07"

    def _frozen_version(self) -> str:
        import json

        ledger = json.loads(self.LEDGER.read_text())
        # `params_frozen` is a dict on some entries and a free-text string on
        # others, so the isinstance guard is load-bearing, not defensive noise.
        matches = [
            frozen["version"]
            for entry in ledger["entries"]
            for frozen in [entry.get("params_frozen")]
            if isinstance(frozen, dict) and frozen.get("class") == self.ENTRY_CLASS
        ]
        self.assertEqual(
            1,
            len(matches),
            f"expected exactly one ledger entry of class {self.ENTRY_CLASS}, "
            f"found {len(matches)}. The parser, not the pin, needs updating.",
        )
        return matches[0]

    def test_the_ledger_version_equals_the_code_constant(self):
        from alphalens_pipeline.market.market_state import MARKET_STATE_CONFIG_VERSION

        self.assertEqual(
            MARKET_STATE_CONFIG_VERSION,
            self._frozen_version(),
            "the emitter and its pre-registration name different versions. Bump "
            "both in one commit, and record what the old one orphaned.",
        )

    def test_positive_control_a_diverging_version_is_detected(self):
        # A fabricated drift MUST be caught, or this pin is a no-op.
        self.assertNotEqual(self._frozen_version(), "mstate-vX-never-shipped")


if __name__ == "__main__":
    unittest.main()
