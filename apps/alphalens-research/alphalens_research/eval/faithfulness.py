"""T6 brief-faithfulness scorer (v1) — deterministic-first, NO LLM-judge.

Scores the generated brief's four SCHEMA output fields (``tldr``,
``supply_chain_reasoning``, ``bear_summary``, ``catalyst_failure_exit``) against
the injected ``<facts>`` block. Implements the LOCKED design memo
``docs/research/reading_quality_eval_design_2026_07_11.md`` §6.

Public pure functions (memo §8):

* :func:`parse_facts_index` — typed-facts JSON (preferred) or rendered ``<facts>``
  string (documented fallback) → a normalized fact index.
* :func:`extract_atoms` — one brief field's text → list of checkable atoms.
* :func:`score_brief` — fact index + brief fields → :class:`FaithfulnessResult`.

**v1 gating scope (memo §6.6):** ONLY ``fabricated_numeric_date_atoms`` and
``characterization_violations`` are gating. Entity/product atoms with no
fact-index coverage → ``DEFERRED`` (non-gating, Phase-2). Free prose mechanism
→ ``OUT_OF_SCOPE``. The numeric/date matcher runs inside EVERY field, including
``supply_chain_reasoning`` (memo §6.2 gate-blind-spot correction), so a bare
fabricated number in a mechanism sentence is still caught.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

# --- v1 scorer version (poolability key, memo §9) ----------------------------
# Bump whenever the matcher logic OR the forbidden-characterization lexicon OR
# the corpus fact-index adapter shape changes; rates are partitioned by this and
# never pooled across a change.
#   v1.1 -> v1.2: the corpus adapter (measurement.py::_COLUMN_TO_FACT_KEY) now
#   maps the three sector-percentile columns (valuation_composite /
#   insider_score / fcff_yield) to ``*_pct`` fact keys. They are rendered into
#   the <facts> block but were previously unmapped, so a brief citing "NN%"
#   false-fired FABRICATED. The fact-index SHAPE changed and the reported corpus
#   rates moved (any-fabricated 0.1246 -> 0.1103, ~10 atoms reclassified
#   FABRICATED -> DISTORTED), so per memo §9 the rates are not poolable with a
#   pre-fix report — hence the bump. The GATING cassette path is unaffected
#   (cassettes carry brief_template_facts_json / rendered <facts>, not raw
#   columns), so no golden re-record is required.
#   v1.2 -> v1.3: added ONE targeted academic-refusal pattern binding a
#   lack-lexeme (lacks/absent/insufficient/fails to) to the bargain/cheap lexeme
#   so "the stock lacks fundamental support to signal a bargain" no longer
#   false-fires a characterization VIOLATION. Done as a BOUND pattern (not by
#   widening the shared _NEGATION_CUES) — a comma is not a clause boundary, so a
#   shared 'insufficient'/'lacks' cue would wrongly suppress a real affirmative
#   'cheap'/'on sale' violation in "insufficient growth makes it cheap". The
#   characterization matcher PATH changed → poolability bump. No golden re-record
#   needed: no cassette OUTPUT carries an affirmative cheap/on-sale/bargain/
#   promotion, and the "lacks"/"fails to" cassette occurrences precede no
#   forbidden phrase, so the new pattern flips nothing on the golden set.
FAITHFULNESS_SCORER_VERSION = "t6-v1.3-2026-07-11"

# --- Forbidden-characterization lexicon (memo §6.1 / §6.4) -------------------
# Derived VERBATIM from prompts.py ~L231-235 (Pro) / ~L260-262 (Flash):
#   'Do NOT label a large 52w drawdown as "cheap", "on sale", or "promotion".'
#   '... requires ... corroboration.' (bargain)
# and the next_earnings_date forecast ban ~L235-237 / L262:
#   'Do NOT forecast, predict, or speculate ... ("expecting a beat" /
#    "investors are anticipating").'
_FORBIDDEN_CHAR_PHRASES: tuple[str, ...] = (
    "cheap",
    "on sale",
    "bargain",
    "promotion",
)
# Forecast verbs / phrases that must not appear near next_earnings_date.
_FORECAST_PHRASES: tuple[str, ...] = (
    "expecting a beat",
    "investors are anticipating",
    "anticipating",
    "forecast",
    "predict",
    "speculate",
)

# Negation cues that, when they precede a forbidden phrase in the SAME clause,
# flip the verdict to compliant (memo §6.4 "not a bargain" / "do not treat ...
# as cheap"). Matched as WHOLE WORDS (\b…\b) so an ordinary word that merely
# CONTAINS a cue substring does NOT suppress a genuine violation ("now" ⊃ "no",
# "economy" ⊃ "no", "announce" ⊃ "no"). Multi-word cues are matched literally.
_NEGATION_CUES: tuple[str, ...] = (
    "not",
    "no",
    "never",
    "isn't",
    "aren't",
    "wasn't",
    "weren't",
    "n't",
    "hardly",
    "far from",
    "rather than",
    "instead of",
    "avoid",
    "without",
)
# Pre-built whole-word / literal-phrase matchers for the negation cues.
_NEGATION_CUE_RES: tuple[re.Pattern, ...] = tuple(
    re.compile(r"\b" + re.escape(cue) + r"\b" if cue.isalpha() else re.escape(cue))
    for cue in _NEGATION_CUES
)

# Compliant ACADEMIC-REFUSAL constructions per prompts.py: the model is allowed
# to say the bargain label is NOT warranted ("bargain conclusion requires
# fundamental and insider corroboration", "failing to corroborate a bargain
# thesis"). These are targeted patterns bound to the bargain lexeme — NOT the
# bare substrings 'requires' / 'fail' / 'corroborat', which over-suppressed any
# violation near those tokens (memo §6.4 correctness fix). Matched against the
# whole field text, not a char window.
_ACADEMIC_REFUSAL_RES: tuple[re.Pattern, ...] = (
    re.compile(r"fail(?:ing|s|ed)?\s+to\s+corroborat", re.IGNORECASE),
    re.compile(r"(?:does\s+not|doesn't|do\s+not|don't)\s+corroborat", re.IGNORECASE),
    re.compile(r"\b(?:bargain|cheap|on sale|promotion)\b[^.;]{0,40}?requires?\b", re.IGNORECASE),
    re.compile(r"\brequires?\b[^.;]{0,40}?corroborat", re.IGNORECASE),
    # "lacks/absent/insufficient/fails to ... [support] to signal a bargain" — a
    # compliant refusal where the bargain/cheap label is the OBJECT the lack-lexeme
    # denies. BOUND to the bargain lexeme (not a widened shared negation cue), and
    # the gap must NOT contain a finite CAUSAL/COPULA verb (make/makes/making,
    # render(ing), leave(s)/leaving, keep(s)/keeping, look(s/ing), is/are/was/were,
    # trades/appears/seems/remains) — those turn "cheap" into an affirmative
    # predicate, so "insufficient growth makes/making it cheap" still FIRES while
    # "lacks fundamental support to signal a bargain" is suppressed. The gap window
    # (bounded to 60 chars so a slightly longer real refusal still matches) excludes
    # those verbs as whole words via a tempered negative lookahead.
    re.compile(
        r"\b(?:lack(?:s|ing|ed)?|absent|insufficient|fail(?:s|ing|ed)?\s+to)\b"
        r"(?:(?!\b(?:mak(?:es?|ing)|render(?:s|ing)?|leav(?:es?|ing)|keep(?:s|ing)?"
        r"|look(?:s|ing)?|is|are|was|were|trades?|appears?|seems?|remains?)\b)[^.;]){0,60}?"
        r"\b(?:bargain|cheap|on sale|promotion)\b",
        re.IGNORECASE,
    ),
)

# Clause boundary characters — the negation window is the clause CONTAINING the
# forbidden phrase (text back to the previous ., ;, or : within the field), not
# a raw fixed-width char window (memo §6.4 correctness fix).
_CLAUSE_BOUNDARY = ".;:\n"
# Window (chars) around next_earnings_date scanned for a forecast verb.
_EARNINGS_WINDOW = 120

# --- Numeric matching policy (memo §6.4) -------------------------------------
# The GROUNDED / DISTORTED / FABRICATED boundary is entirely this policy.
#
# GROUNDED  — equal after rounding the fact to the brief's stated precision
#             (sign stripped for directional facts).
# DISTORTED — not equal after rounding, but the brief value is close enough to a
#             real same-kind fact to be a paraphrase/rounding of it: within
#             _DISTORTED_REL_BAND relative (memo pin: brief "50%" vs fact -39.2%
#             ≈ 27% relative → DISTORTED). This is a real distortion of an
#             existing fact, not an invention.
# FABRICATED — no fact within the DISTORTED band covers it (gating in v1).
_DISTORTED_REL_BAND = 0.40  # ≤40% relative from a same-kind fact → DISTORTED

# The fact-index keys whose values are directional distances: a brief may supply
# its own direction word ("50% drawdown", "21% below MA200"), so the sign is
# stripped before comparison (memo §6.4 step 1).
_DIRECTIONAL_FACT_KEYS: frozenset[str] = frozenset(
    {
        "technical_pct_off_52w_high",
        "technical_pct_off_52w_low",
        "technical_ma200_distance_pct",
        "technical_ma50_distance_pct",
        "technical_ma200_slope_pct_per_day",
    }
)

_SCHEMA_FIELDS: tuple[str, ...] = (
    "tldr",
    "supply_chain_reasoning",
    "bear_summary",
    "catalyst_failure_exit",
)

# --- Unit-kind matching (memo §6.4 step 3, correctness fix) ------------------
# The matcher is unit-AWARE: a numeric atom only matches a fact whose unit-kind
# is compatible, so a "$7.5 billion" claim can never ground against a P/S ratio
# and a "4.2x sales" multiple can never ground against a % fact. Fact unit-kind
# is inferred from the key name (the typed source and the rendered-facts fallback
# share these key names by construction).
#
# Atom unit → the fact unit-kind(s) it may match:
#   "%"     — percentage atom            → only "%" facts
#   "$"     — dollar atom ($, $Nk, $Nbn) → only "$" facts
#   "x"     — multiple atom (4.2x)       → only "ratio" facts (P/S, EV/Rev, ...)
#   ""      — bare number (7.5, 53, 0.34)→ only "ratio" facts (a bare number is a
#             ratio/index like P/S or RSI, never a % or $ without its symbol)
#   "count" — magnitude word, no $ sign  → "$" or "ratio" (ambiguous magnitude)
_ATOM_UNIT_TO_FACT_KINDS: dict[str, frozenset[str]] = {
    "%": frozenset({"%"}),
    "$": frozenset({"$"}),
    "x": frozenset({"ratio"}),
    "": frozenset({"ratio"}),
    "count": frozenset({"$", "ratio"}),
}


def _fact_unit_kind(key: str) -> str:
    """Infer a fact's unit-kind (``%`` / ``$`` / ``ratio``) from its key name."""
    low = key.lower()
    # All % facts carry "pct" in the key (technical_*_pct, *_pct_per_day,
    # fcff_yield_pct). "yield" is a defensive alias in case a future yield key
    # drops the _pct suffix.
    if "pct" in low or "yield" in low:
        return "%"
    if low == "market_cap" or low.endswith(("_usd", "_dollars")):
        return "$"
    return "ratio"


def _atom_unit(atom: Atom) -> str:
    """The unit token carried on a numeric atom's canonical ``extracted_value``.

    ``extracted_value`` is ``f"{value:g}{unit}"`` — recover the trailing unit
    (``%`` / ``$`` / ``x`` / ``count`` / ``""``)."""
    ev = atom.extracted_value
    for unit in ("count", "%", "$", "x"):
        if ev.endswith(unit):
            return unit
    return ""


@dataclass(frozen=True)
class Atom:
    """One extracted checkable claim from a brief field (memo §6.3)."""

    field: str
    span: str
    kind: str  # numeric | date | entity | product | characterization
    extracted_value: str
    verdict: str  # GROUNDED | FABRICATED | DISTORTED | VIOLATION | OUT_OF_SCOPE | DEFERRED
    gating: bool
    matched_fact: str | None = None


@dataclass
class FaithfulnessResult:
    """Aggregate T6 verdict for one brief (memo §6.6)."""

    atoms: list[Atom] = field(default_factory=list)

    # --- PRIMARY (gating, target 0) ---
    @property
    def fabricated_numeric_date_atoms(self) -> int:
        return sum(
            1
            for a in self.atoms
            if a.gating and a.kind in ("numeric", "date") and a.verdict == "FABRICATED"
        )

    @property
    def characterization_violations(self) -> int:
        # Tie the count to the verdict (not just gating+kind) so a future
        # non-VIOLATION characterization atom cannot inflate the gate.
        return sum(
            1 for a in self.atoms if a.kind == "characterization" and a.verdict == "VIOLATION"
        )

    # --- SECONDARY (measurement / diagnostic) ---
    @property
    def distorted_atoms(self) -> int:
        return sum(1 for a in self.atoms if a.kind == "numeric" and a.verdict == "DISTORTED")

    @property
    def deferred_entity_atoms(self) -> int:
        """Count of DEFERRED (non-gating) entity/product atoms.

        STRUCTURALLY 0 in v1: entity/product extraction is Phase-2 (memo §11),
        so no atom ever carries ``verdict=DEFERRED`` yet. A 0 here means
        "entities not scanned in v1", NOT "no ungrounded entities found".
        """
        return sum(1 for a in self.atoms if a.verdict == "DEFERRED")

    @property
    def checkable_atoms(self) -> int:
        # "Checkable in principle" = everything except OUT_OF_SCOPE prose. This
        # deliberately INCLUDES DEFERRED (entity/product) atoms, so it can differ
        # from the groundedness_rate denominator (which uses only resolved
        # GROUNDED/DISTORTED/FABRICATED). Harmless in v1 (DEFERRED is structurally
        # 0); revisit the split when Phase-2 starts emitting DEFERRED atoms.
        return sum(1 for a in self.atoms if a.verdict != "OUT_OF_SCOPE")

    @property
    def out_of_scope_atoms(self) -> int:
        return sum(1 for a in self.atoms if a.verdict == "OUT_OF_SCOPE")

    @property
    def groundedness_rate(self) -> float | None:
        """DIAGNOSTIC ONLY — never a headline pass/fail (memo §6.6 / §10).

        Green-while-broken by construction: OUT_OF_SCOPE atoms leave the
        denominator, so this can only trend to 100%. Always paired with
        ``checkable_coverage`` + ``out_of_scope_atoms``.
        """
        checkable = [a for a in self.atoms if a.verdict in ("GROUNDED", "DISTORTED", "FABRICATED")]
        if not checkable:
            return None
        return sum(1 for a in checkable if a.verdict == "GROUNDED") / len(checkable)

    @property
    def checkable_coverage(self) -> float | None:
        """(checkable atoms) / (checkable + OUT_OF_SCOPE) — makes a shrinking
        checkable denominator visible (memo §6.6)."""
        denom = self.checkable_atoms + self.out_of_scope_atoms
        if denom == 0:
            return None
        return self.checkable_atoms / denom

    @property
    def is_clean(self) -> bool:
        """v1 gate verdict: both gating metrics at target 0."""
        return self.fabricated_numeric_date_atoms == 0 and self.characterization_violations == 0


# ---------------------------------------------------------------------------
# Step 1 — build the fact index
# ---------------------------------------------------------------------------

# Rendered-facts lines the fallback parser reads (memo §6.4). Keys mirror the
# score-stage typed-fact dict names so the typed source and the fallback produce
# the SAME index shape.
_FACTS_OPEN_RE = re.compile(r"<facts>\s*\n(ticker:.*?)</facts>", re.DOTALL)

# A signed decimal immediately followed by a percent sign, used to pull the
# numeric out of several rendered-facts lines ("4.8%", "-39.2 %"). The possessive
# ``++`` removes all backtracking without changing the match set: neither ``\s``
# nor ``%`` is in ``[\d.]``, so giving back a matched ``[\d.]`` char can never
# help the trailing ``\s*%`` match. Shared constant so the identical literal is
# not duplicated (Sonar S1192). NOSONAR: SonarPython's regex engine predates the
# possessive-quantifier syntax and misreads ``[\d.]++`` as a nested quantifier,
# false-flagging S8786 — the pattern is provably linear (verified 0 backtracking).
_PERCENT_RE = re.compile(r"([-+]?[\d.]++)\s*%")  # NOSONAR


def _to_float(token: str) -> float | None:
    try:
        return float(token.replace(",", ""))
    except ValueError:
        return None


def _make_line_after(body: str):
    """Return a ``line_after(prefix)`` reader over the ``<facts>`` body: the text
    after the first stripped line starting with ``prefix``, or ``None``."""

    def _line_after(prefix: str) -> str | None:
        for line in body.splitlines():
            stripped = line.strip()
            if stripped.startswith(prefix):
                return stripped[len(prefix) :].strip()
        return None

    return _line_after


def _parse_text_facts(line_after, index: dict) -> None:
    """ticker / company / theme text fields."""
    ticker = line_after("ticker:")
    if ticker:
        index["ticker"] = ticker
    company = line_after("company:")
    if company is not None:
        index["company"] = company
    theme = line_after("theme:")
    if theme is not None:
        index["theme"] = theme


def _parse_market_cap(line_after, index: dict) -> None:
    """market_cap line: "$8.20B" → 8.2e9."""
    mcap = line_after("market_cap:")
    if mcap is None:
        return
    m = re.search(r"\$?([\d.]+)\s*([BMK])?", mcap)
    if m and m.group(1):
        val = float(m.group(1))
        mult = {"B": 1e9, "M": 1e6, "K": 1e3, None: 1.0}[m.group(2)]
        index["market_cap"] = val * mult


def _parse_labeled_numbers(line: str, index: dict, mapping: tuple[tuple[str, str], ...]) -> None:
    """For each (label, key), pull the signed number after the label on ``line``."""
    for label, key in mapping:
        m = re.search(re.escape(label) + r"\s+([-+]?[\d.]+)", line)
        if m:
            index[key] = float(m.group(1))


def _parse_valuation(line_after, index: dict) -> None:
    """valuation line: P/S 7.5, EV/Rev 7.2, FCF margin 0.34, composite pctile."""
    val_line = line_after("- valuation:")
    if val_line:
        _parse_labeled_numbers(
            val_line,
            index,
            (
                ("P/S", "valuation_ps"),
                ("EV/Rev", "valuation_ev_rev"),
                ("FCF margin", "valuation_fcf_margin"),
            ),
        )


def _parse_fcff(line_after, index: dict) -> None:
    """FCFF yield: "- FCFF yield: 4.8%, sector percentile 39"."""
    fcff = line_after("- FCFF yield:")
    if fcff:
        m = _PERCENT_RE.search(fcff)
        if m:
            index["fcff_yield_pct"] = float(m.group(1))


def _parse_insider(line_after, index: dict) -> None:
    """insider dollars: - insider opportunistic buys (...): $0k, sector pctile."""
    ins = line_after("- insider opportunistic buys")
    if ins:
        m = re.search(r"\$([-+]?[\d.]+)k", ins)
        if m:
            index["insider_score_usd"] = float(m.group(1)) * 1000


def _parse_directional_distances(line_after, index: dict) -> None:
    """52w-high/low distance + MA200 distance/slope directional lines."""
    hi = line_after("- 52w high distance:")
    if hi:
        m = _PERCENT_RE.search(hi)
        if m:
            index["technical_pct_off_52w_high"] = float(m.group(1))
        m2 = re.search(r"52w low distance:\s*([-+]?[\d.]+)\s*%", hi)
        if m2:
            index["technical_pct_off_52w_low"] = float(m2.group(1))
    ma = line_after("- MA200 distance:")
    if ma:
        m = _PERCENT_RE.search(ma)
        if m:
            index["technical_ma200_distance_pct"] = float(m.group(1))
        m2 = re.search(r"MA200 slope:\s*([-+]?[\d.]+)\s*%", ma)
        if m2:
            index["technical_ma200_slope_pct_per_day"] = float(m2.group(1))


def _parse_technicals(line_after, index: dict) -> None:
    """technicals line: "RSI 53 / MA50 +2.4% / ... / ATR 4.6% / volZ -1.3"."""
    tech = line_after("- technicals:")
    if tech:
        _parse_labeled_numbers(
            tech,
            index,
            (
                ("RSI", "technical_rsi"),
                ("MA50", "technical_ma50_distance_pct"),
                ("ATR", "technical_atr_pct"),
                ("volZ", "technical_volz"),
            ),
        )


def _parse_durability(line_after, index: dict) -> None:
    """durability (Buffett quant): "ROIC 12.3% (3y avg 11.0%), owner-earnings
    yield 4.5%, DCF margin of safety -8.0%" (prompts.py _format_durability_line).
    Keys carry the ``_pct`` suffix so ``_fact_unit_kind`` classifies them as %
    facts. Absent sub-fields render as "n/a" and simply do not match."""
    dur = line_after("- durability (Buffett quant):")
    if not dur:
        return
    for pattern, key in (
        (r"ROIC\s+([-+]?[\d.]+)\s*%", "buffett_roic_pct"),
        (r"3y avg\s+([-+]?[\d.]+)\s*%", "buffett_roic_3y_avg_pct"),
        (r"owner-earnings yield\s+([-+]?[\d.]+)\s*%", "buffett_owner_earnings_yield_pct"),
        (r"DCF margin of safety\s+([-+]?[\d.]+)\s*%", "buffett_margin_of_safety_pct"),
    ):
        m = re.search(pattern, dur)
        if m:
            index[key] = float(m.group(1))


def _parse_catalyst_lines(body: str, index: dict) -> None:
    """catalyst title + published date (for entity / date grounding)."""
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("title:"):
            index["source_event_title"] = stripped[len("title:") :].strip()
        elif stripped.startswith("published:"):
            index["source_event_published_at"] = stripped[len("published:") :].strip()


def _parse_rendered_facts(contents: str) -> dict:
    """Parse the numeric/date/text fact index from a rendered ``<facts>`` block.

    Selects the ``<facts>...</facts>`` pair whose body starts with ``ticker:``
    (the anti-injection preamble also mentions ``<facts>`` in prose, memo §7).
    """
    match = _FACTS_OPEN_RE.search(contents)
    if not match:
        raise ValueError("no <facts> block starting with 'ticker:' found in contents")
    body = match.group(1)
    index: dict = {}
    line_after = _make_line_after(body)

    _parse_text_facts(line_after, index)
    _parse_market_cap(line_after, index)
    _parse_valuation(line_after, index)
    _parse_fcff(line_after, index)
    _parse_insider(line_after, index)
    _parse_directional_distances(line_after, index)
    _parse_technicals(line_after, index)
    _parse_durability(line_after, index)
    _parse_catalyst_lines(body, index)

    # next_earnings_date if present
    ned = line_after("next_earnings_date:")
    if ned:
        index["next_earnings_date"] = ned

    return index


def parse_facts_index(typed_facts_json: str | dict | None) -> dict:
    """Normalized fact index from the typed source (preferred) or the fallback.

    * ``dict`` or a typed ``brief_template_facts_json`` string → used directly
      (the typed source the pipeline injected — isolates model behaviour from
      display-string regex drift, memo §6.4 step 1).
    * a rendered ``<facts>`` prompt string (contains ``<facts>``) → parsed via
      the documented fallback for cassette-only runs.

    The golden parquet's ``brief_template_facts_json`` is NULL for all 4 rows
    (verified 2026-07-11), so the golden gate takes the fallback path.
    """
    if typed_facts_json is None:
        raise ValueError("parse_facts_index requires typed facts or a <facts> string")
    if isinstance(typed_facts_json, dict):
        return dict(typed_facts_json)
    text = typed_facts_json
    stripped = text.strip()
    # A raw JSON object → typed source.
    if stripped.startswith("{"):
        try:
            return dict(json.loads(stripped))
        except json.JSONDecodeError:
            pass
    if "<facts>" in text:
        return _parse_rendered_facts(text)
    raise ValueError("unrecognized facts source: not JSON, not a <facts> block")


# ---------------------------------------------------------------------------
# Step 2 — extract atoms
# ---------------------------------------------------------------------------

# A numeric span: optional $, a number, optional %, optional 'x'/'B'/'M'/'k'
# suffix, optional 'k'/'B'/'M' before a $ prefix handled separately.
# The (?P<num>...) alternation requires a digit AFTER a decimal point, so a
# trailing sentence period ("RSI 53. The") is NOT swallowed into "53." as a
# decimal — it matches the integer "53".
#
# The magnitude word (bn|billion|million|trillion + the bare abbreviations
# b|m|k) is allowed OPTIONAL whitespace before it: briefs write "$7.5 billion"
# or "$500 M" far more often than "$7.5B", so the suffix group must consume a
# spaced magnitude word or the magnitude is silently dropped (a $-magnitude
# claim would then false-ground against an unrelated same-digit ratio fact —
# memo §6.4 correctness fix). A trailing \b bounds the bare abbreviations so a
# following word is not mis-read as a suffix ("53 breakout" → "53", not 53e9;
# "500 kW" → "500", not 5e5).
_ATOM_NUM_RE = re.compile(
    r"(?P<dollar>\$)?"
    r"(?P<sign>[-+])?"
    r"(?P<num>\d[\d,]*\.\d+|\d[\d,]*)"
    r"(?:(?P<glued>%|x|B|M|k)|\s*(?P<word>bn|billion|million|trillion|b|m|k)\b)?",
    re.IGNORECASE,
)
_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")

# Label tokens where a glued integer is a STRUCTURAL REFERENCE, not a claimed
# quantitative value (memo §6.2 "score only checkable atoms"). Regexes match the
# whole label so its digits are masked out before atom extraction:
#   MA200 / MA50, 52w / 52-week, 200-day, S-1, 10-K / 8-K, EX-99.1 exhibit refs,
#   180 days / 180d, "N weeks/months/days" durations, "1st/2nd/3rd" ordinals,
#   "N)" list markers.
# These are world-knowledge / boilerplate labels; the CHECKABLE data atoms the
# gate scores are the percentages ($, %, x, and bare ratios like P/S 4052.9).
_LABEL_MASK_RES: tuple[re.Pattern, ...] = (
    re.compile(r"\bMA\s?\d+\b", re.IGNORECASE),  # MA200, MA50, MA 200
    re.compile(r"\b\d+\s?w(?:eek)?s?\b", re.IGNORECASE),  # 52w, 2 weeks, 52-week (w)
    re.compile(r"\b\d+-?week\b", re.IGNORECASE),  # 52-week
    re.compile(r"\b\d+-day\b", re.IGNORECASE),  # 200-day
    re.compile(r"\b\d+\s?(?:days?|months?|weeks?)\b", re.IGNORECASE),  # 180 days, 2 weeks
    re.compile(r"\b\d+d\b", re.IGNORECASE),  # 180d
    re.compile(r"\bS-\d+\b", re.IGNORECASE),  # Form S-1
    re.compile(r"\b\d+-[KQ]\b", re.IGNORECASE),  # 10-K, 8-K, 10-Q
    # SEC exhibit label "EX-99.1" / "EX-10.1" — the "-99.1" is NOT a number.
    # No leading \b (a preceding CJK char is a word char, so \b would miss the
    # Chinese-brief case); a lookbehind blocks matching inside a word (REX-99).
    re.compile(r"(?<![A-Z])EX-\d+(?:\.\d+)?", re.IGNORECASE),  # EX-99.1, EX-10.1
    re.compile(r"\b\d+(?:st|nd|rd|th)\b", re.IGNORECASE),  # 1st percentile, 200th
    re.compile(r"(?<!\d)\d\)"),  # "1)" "2)" list markers
)


def _mask_labels(text: str) -> str:
    """Blank out structural label tokens so their digits are not atomized."""
    out = text
    for pattern in _LABEL_MASK_RES:
        out = pattern.sub(lambda mm: " " * len(mm.group(0)), out)
    return out


def _is_checkable_span(match: re.Match, unit: str) -> bool:
    """A numeric span is a CHECKABLE data atom only if it carries a unit
    (``%``/``$``/``x``/magnitude) OR is a bare decimal ratio (has a fractional
    part, e.g. P/S ``4052.9``). Bare integers with no unit are references /
    counts, not quoted facts, so they are not gated (memo §6.2)."""
    if unit in ("%", "$", "x", "count"):
        return True
    return "." in match.group("num")


# Magnitude-word / abbreviation suffixes → scale multiplier. A suffix present
# here scales the number and yields a "$"/"count" unit (dollar-aware); "%" and
# "x" are unit-only suffixes handled separately.
_MAGNITUDE_MULTIPLIERS: dict[str, float] = {
    "b": 1e9,
    "bn": 1e9,
    "billion": 1e9,
    "trillion": 1e12,
    "m": 1e6,
    "million": 1e6,
    "k": 1e3,
}


def _resolve_numeric_suffix(suffix: str, dollar: bool) -> tuple[float, str]:
    """Map a numeric suffix to a (value scale, unit) pair.

    ``%``/``x`` are unit-only (scale 1.0); magnitude words scale the value and
    yield ``$`` when a leading dollar sign is present, else ``count``. An empty
    or unknown suffix keeps the base value and the dollar-aware default unit.
    """
    if suffix == "%":
        return 1.0, "%"
    if suffix == "x":
        return 1.0, "x"
    mult = _MAGNITUDE_MULTIPLIERS.get(suffix)
    if mult is not None:
        return mult, "$" if dollar else "count"
    return 1.0, "$" if dollar else ""


def _canonical_numeric(match: re.Match) -> tuple[float, str, str] | None:
    """Return (value, unit, span) for a numeric atom, or None to skip.

    The magnitude suffix may be GLUED to the number (``$7.5B``) or a SPACED
    word (``$7.5 billion``); both resolve to the same value+unit here so a
    spaced magnitude is never dropped (memo §6.4).
    """
    num = _to_float(match.group("num"))
    if num is None:
        return None
    sign = match.group("sign") or ""
    dollar = bool(match.group("dollar"))
    suffix = (match.group("glued") or match.group("word") or "").lower()
    scale, unit = _resolve_numeric_suffix(suffix, dollar)
    value = num * scale
    if sign == "-":
        value = -value
    return value, unit, match.group(0)


def extract_atoms(field: str, text: str) -> list[Atom]:
    """Extract checkable NUMERIC + DATE atoms from one brief field's text.

    Entity/product extraction is Phase-2 (memo §11); v1 returns numeric + date
    atoms only, with a placeholder ``verdict=""`` filled in by the matcher in
    :func:`score_brief`. Characterization is handled at brief level (needs the
    fact index for the earnings-window check), not here.
    """
    atoms: list[Atom] = []
    if not text:
        return atoms

    # Dates first, then mask them so the year (e.g. 2026) is not re-extracted as
    # a bare number.
    for m in _DATE_RE.finditer(text):
        atoms.append(
            Atom(
                field=field,
                span=m.group(1),
                kind="date",
                extracted_value=m.group(1),
                verdict="",
                gating=True,
            )
        )
    # Mask dates, then structural label tokens (MA200, 52w, 200-day, S-1, 10-K,
    # 180 days, ordinals, list markers) so their digits are not atomized.
    masked = _DATE_RE.sub(lambda mm: " " * len(mm.group(0)), text)
    masked = _mask_labels(masked)

    for m in _ATOM_NUM_RE.finditer(masked):
        canon = _canonical_numeric(m)
        if canon is None:
            continue
        value, unit, span = canon
        if not _is_checkable_span(m, unit):
            continue
        atoms.append(
            Atom(
                field=field,
                span=span,
                kind="numeric",
                extracted_value=f"{value:g}{unit}",
                verdict="",
                gating=True,
            )
        )
    return atoms


# ---------------------------------------------------------------------------
# Step 3 — match
# ---------------------------------------------------------------------------


def _brief_precision(span: str) -> int:
    """Number of decimal places stated in the brief span (0 for an integer)."""
    m = re.search(r"\.(\d+)", span)
    return len(m.group(1)) if m else 0


def _numeric_fact_candidates(facts: dict) -> list[tuple[str, float]]:
    """(key, value) numeric facts eligible for matching."""
    out: list[tuple[str, float]] = []
    for key, val in facts.items():
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            out.append((key, float(val)))
    return out


def _fact_values_for(key: str, fact_val: float) -> list[float]:
    """The comparable value(s) for a fact.

    Directional facts also expose ``abs(fact_val)`` so a brief that supplies its
    own direction word (memo §6.4 step 1) compares magnitude-to-magnitude.
    """
    if key in _DIRECTIONAL_FACT_KEYS:
        return [fact_val, abs(fact_val)]
    return [fact_val]


def _is_distorted(value: float, fv: float, precision: int) -> bool:
    """DISTORTED band test: brief ``value`` within an absolute (2 ULP) or a
    relative (``_DISTORTED_REL_BAND`` of the fact magnitude) tolerance of a
    same-kind fact (memo §6.4 correctness fix — the band is scaled to the FACT
    only, so a brief overstating by >40% of the fact is FABRICATED)."""
    step = 10.0 ** (-precision) if precision > 0 else 1.0
    abs_band = 2.0 * step
    rel_band = _DISTORTED_REL_BAND * max(abs(fv), 1e-9)
    return abs(value - fv) <= max(abs_band, rel_band)


def _closer(value: float, fv: float, best: tuple[str, float] | None) -> bool:
    """True if fact value ``fv`` is a strictly better (closer to ``value``)
    candidate than the current ``best`` (``None`` = no candidate held yet)."""
    return best is None or abs(value - fv) < abs(value - best[1])


def _best_numeric_match(
    value: float, precision: int, allowed_kinds: frozenset[str], facts: dict
) -> tuple[tuple[str, float] | None, tuple[str, float] | None]:
    """Scan the fact index for the closest GROUNDED and closest DISTORTED fact.

    Returns ``(best_grounded, best_distorted)`` as ``(key, fact_value)`` pairs
    (or ``None``). "Closest" prefers the exact-value fact over one that only
    collides after rounding (honest attribution, memo §6.4 step 3). A fact that
    rounds to the brief value can only ground it (never counts as distorted).
    """
    best_grounded: tuple[str, float] | None = None
    best_distorted: tuple[str, float] | None = None
    for key, fact_val in _numeric_fact_candidates(facts):
        if _fact_unit_kind(key) not in allowed_kinds:
            continue
        for fv in _fact_values_for(key, fact_val):
            grounded = round(fv, precision) == round(value, precision)
            if grounded and _closer(value, fv, best_grounded):
                best_grounded = (key, fv)
            elif (
                not grounded
                and _closer(value, fv, best_distorted)
                and _is_distorted(value, fv, precision)
            ):
                best_distorted = (key, fv)
    return best_grounded, best_distorted


def _match_numeric(atom: Atom, value: float, facts: dict) -> Atom:
    """Classify a numeric atom against the fact index (memo §6.4 step 3).

    Unit-AWARE: only facts whose inferred unit-kind is compatible with the
    atom's unit are candidates, so a ``$``-magnitude claim can never ground a
    ratio fact and an ``x``/bare multiple can never ground a ``%`` fact.
    """
    span = atom.span
    precision = _brief_precision(span)
    allowed_kinds = _ATOM_UNIT_TO_FACT_KINDS.get(_atom_unit(atom), frozenset({"ratio"}))
    best_grounded, best_distorted = _best_numeric_match(value, precision, allowed_kinds, facts)

    if best_grounded is not None:
        return Atom(
            field=atom.field,
            span=span,
            kind="numeric",
            extracted_value=atom.extracted_value,
            verdict="GROUNDED",
            gating=False,
            matched_fact=best_grounded[0],
        )
    if best_distorted is not None:
        return Atom(
            field=atom.field,
            span=span,
            kind="numeric",
            extracted_value=atom.extracted_value,
            verdict="DISTORTED",
            gating=True,
            matched_fact=best_distorted[0],
        )
    # No fact within tolerance → FABRICATED (gating in v1).
    return Atom(
        field=atom.field,
        span=span,
        kind="numeric",
        extracted_value=atom.extracted_value,
        verdict="FABRICATED",
        gating=True,
    )


def _match_date(atom: Atom, facts: dict) -> Atom:
    """Classify a date atom: present in fact index → GROUNDED, else FABRICATED."""
    fact_dates = {
        str(v) for v in facts.values() if isinstance(v, str) and _DATE_RE.fullmatch(v.strip())
    }
    # Also scan free-text fact values (catalyst title / published) for the date,
    # matched with WORD BOUNDARIES via the date regex — a fabricated date that is
    # merely an incidental substring of a longer digit run ("2026-05-2410") does
    # NOT ground (memo §6.4 correctness fix).
    haystack = " ".join(str(v) for v in facts.values() if isinstance(v, str))
    haystack_dates = {m.group(1) for m in _DATE_RE.finditer(haystack)}
    if atom.extracted_value in fact_dates or atom.extracted_value in haystack_dates:
        return Atom(
            field=atom.field,
            span=atom.span,
            kind="date",
            extracted_value=atom.extracted_value,
            verdict="GROUNDED",
            gating=False,
            matched_fact="date",
        )
    return Atom(
        field=atom.field,
        span=atom.span,
        kind="date",
        extracted_value=atom.extracted_value,
        verdict="FABRICATED",
        gating=True,
    )


def _clause_before(text_lower: str, phrase_start: int) -> str:
    """Text of the clause containing the forbidden phrase, up to its start.

    Bounded to the left by the previous clause boundary (``.`` ``;`` ``:`` or a
    newline), so a negation in an EARLIER sentence never suppresses a violation
    in this one."""
    lo = 0
    for i in range(phrase_start - 1, -1, -1):
        if text_lower[i] in _CLAUSE_BOUNDARY:
            lo = i + 1
            break
    return text_lower[lo:phrase_start]


def _is_negated(text_lower: str, phrase_start: int) -> bool:
    """True if a whole-word negation cue sits in the phrase's clause before it."""
    clause = _clause_before(text_lower, phrase_start)
    return any(cue_re.search(clause) for cue_re in _NEGATION_CUE_RES)


def _is_academic_refusal(text: str, phrase_start: int, phrase_len: int) -> bool:
    """True if the forbidden phrase is inside a compliant academic-refusal
    construction (e.g. "failing to corroborate a bargain thesis"). The refusal
    pattern must OVERLAP the phrase span so an unrelated 'requires'/'fail'
    elsewhere in the field cannot suppress a real violation."""
    phrase_end = phrase_start + phrase_len
    for refusal_re in _ACADEMIC_REFUSAL_RES:
        for m in refusal_re.finditer(text):
            # Overlap OR immediate adjacency (within the same short clause window).
            if m.start() <= phrase_end and m.end() >= phrase_start - 40:
                return True
    return False


def _is_quoted(text: str, phrase_start: int, phrase_len: int) -> bool:
    """True if the forbidden phrase sits inside a quote (a cite of the guidance)."""
    before = text.rfind('"', 0, phrase_start)
    after = text.find('"', phrase_start + phrase_len)
    if before == -1 or after == -1:
        return False
    # Odd number of quotes before → inside an open quote.
    return text.count('"', 0, phrase_start) % 2 == 1


def _char_violation_atom(field_name: str, text: str, start: int, phrase: str) -> Atom:
    """Build one characterization VIOLATION atom for a matched forbidden phrase."""
    return Atom(
        field=field_name,
        span=text[max(0, start - 15) : start + len(phrase) + 15].strip(),
        kind="characterization",
        extracted_value=phrase,
        verdict="VIOLATION",
        gating=True,
    )


def _framing_violations(field_name: str, text: str, low: str) -> list[Atom]:
    """Drawdown/valuation framing-lexicon violations (affirmative, un-negated,
    un-quoted, not an academic refusal — memo §6.4)."""
    atoms: list[Atom] = []
    for phrase in _FORBIDDEN_CHAR_PHRASES:
        for m in re.finditer(r"\b" + re.escape(phrase) + r"\b", low):
            start = m.start()
            if _is_negated(low, start):
                continue
            if _is_academic_refusal(text, start, len(phrase)):
                continue
            if _is_quoted(text, start, len(phrase)):
                continue
            atoms.append(_char_violation_atom(field_name, text, start, phrase))
    return atoms


def _earnings_anchors(low: str, earnings_date: str) -> list[int]:
    """Start offsets that anchor the earnings event: the WORD 'earnings' plus
    the ``next_earnings_date`` value itself (a date-only reference still anchors,
    memo §6.4 correctness fix)."""
    anchors = [m.start() for m in re.finditer(r"earnings", low)]
    if earnings_date:
        anchors += [m.start() for m in re.finditer(re.escape(earnings_date.lower()), low)]
    return anchors


def _forecast_violations(field_name: str, text: str, low: str, facts: dict) -> list[Atom]:
    """Forecast-verb-near-earnings violations: a forecast phrase fires only when
    it sits within ``_EARNINGS_WINDOW`` chars of an earnings anchor and is not
    quoted."""
    earnings_date = str(facts.get("next_earnings_date") or "").strip()
    if not earnings_date:
        return []
    anchors = _earnings_anchors(low, earnings_date)
    if not anchors:
        return []
    atoms: list[Atom] = []
    for phrase in _FORECAST_PHRASES:
        for m in re.finditer(r"\b" + re.escape(phrase) + r"\b", low):
            start = m.start()
            if _is_quoted(text, start, len(phrase)):
                continue
            if not any(abs(start - a) <= _EARNINGS_WINDOW for a in anchors):
                continue
            atoms.append(_char_violation_atom(field_name, text, start, phrase))
    return atoms


def _characterization_atoms(field_name: str, text: str, facts: dict) -> list[Atom]:
    """Detect forbidden-lexicon characterization violations in one field.

    Fires VIOLATION only on the affirmative, un-negated, un-quoted match
    (memo §6.4). ``next_earnings_date`` forecast verbs fire only when a forecast
    phrase is adjacent to the earnings date reference.
    """
    if not text:
        return []
    low = text.lower()
    atoms = _framing_violations(field_name, text, low)
    atoms.extend(_forecast_violations(field_name, text, low, facts))
    return atoms


def score_brief(facts_index: dict, brief_fields: dict) -> FaithfulnessResult:
    """Score one brief's four fields against the fact index.

    The numeric/date matcher runs inside EVERY field, including
    ``supply_chain_reasoning`` (memo §6.2). Free prose without a checkable atom
    is simply absent from the atom list — it is neither GROUNDED nor gated
    (OUT_OF_SCOPE by construction; the gate is blind to prose mechanism, memo
    §6.7 / §10).
    """
    result = FaithfulnessResult()
    for field_name in _SCHEMA_FIELDS:
        text = brief_fields.get(field_name)
        if not text:
            continue
        for atom in extract_atoms(field_name, text):
            if atom.kind == "numeric":
                # Re-derive the signed numeric magnitude from the literal span
                # (extracted_value is already canonical value+unit).
                span_val = _canonical_numeric_from_span(atom.span)
                result.atoms.append(_match_numeric(atom, span_val, facts_index))
            elif atom.kind == "date":
                result.atoms.append(_match_date(atom, facts_index))
        result.atoms.extend(_characterization_atoms(field_name, text, facts_index))
    return result


def _canonical_numeric_from_span(span: str) -> float:
    """Signed magnitude of a numeric span (unit suffixes applied)."""
    m = _ATOM_NUM_RE.match(span) or _ATOM_NUM_RE.search(span)
    if m is None:
        return float("nan")
    canon = _canonical_numeric(m)
    return canon[0] if canon else float("nan")


__all__ = [
    "FAITHFULNESS_SCORER_VERSION",
    "Atom",
    "FaithfulnessResult",
    "extract_atoms",
    "parse_facts_index",
    "score_brief",
]
