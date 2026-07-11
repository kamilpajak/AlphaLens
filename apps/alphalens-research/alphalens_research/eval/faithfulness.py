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
# Bump whenever the matcher logic OR the forbidden-characterization lexicon
# changes; rates are partitioned by this and never pooled across a change.
FAITHFULNESS_SCORER_VERSION = "t6-v1.1-2026-07-11"

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
        return sum(1 for a in self.atoms if a.gating and a.kind == "characterization")

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


def _to_float(token: str) -> float | None:
    try:
        return float(token.replace(",", ""))
    except ValueError:
        return None


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

    def _line_after(prefix: str) -> str | None:
        for line in body.splitlines():
            stripped = line.strip()
            if stripped.startswith(prefix):
                return stripped[len(prefix) :].strip()
        return None

    ticker = _line_after("ticker:")
    if ticker:
        index["ticker"] = ticker
    company = _line_after("company:")
    if company is not None:
        index["company"] = company
    theme = _line_after("theme:")
    if theme is not None:
        index["theme"] = theme

    mcap = _line_after("market_cap:")
    if mcap is not None:
        # "$8.20B" → 8.2e9
        m = re.search(r"\$?([\d.]+)\s*([BMK])?", mcap)
        if m and m.group(1):
            val = float(m.group(1))
            mult = {"B": 1e9, "M": 1e6, "K": 1e3, None: 1.0}[m.group(2)]
            index["market_cap"] = val * mult

    # valuation line: "P/S 7.5, EV/Rev 7.2, FCF margin 0.34, composite sector pctile 45"
    val_line = _line_after("- valuation:")
    if val_line:
        for label, key in (
            ("P/S", "valuation_ps"),
            ("EV/Rev", "valuation_ev_rev"),
            ("FCF margin", "valuation_fcf_margin"),
        ):
            m = re.search(re.escape(label) + r"\s+([-+]?[\d.]+)", val_line)
            if m:
                index[key] = float(m.group(1))

    # FCFF yield: "- FCFF yield: 4.8%, sector percentile 39"
    fcff = _line_after("- FCFF yield:")
    if fcff:
        m = re.search(r"([-+]?[\d.]+)\s*%", fcff)
        if m:
            index["fcff_yield_pct"] = float(m.group(1))

    # insider dollars: "- insider opportunistic buys (180d, buy-only): $0k, sector percentile 96"
    ins = _line_after("- insider opportunistic buys")
    if ins:
        m = re.search(r"\$([-+]?[\d.]+)k", ins)
        if m:
            index["insider_score_usd"] = float(m.group(1)) * 1000

    # directional distances
    hi = _line_after("- 52w high distance:")
    if hi:
        m = re.search(r"([-+]?[\d.]+)\s*%", hi)
        if m:
            index["technical_pct_off_52w_high"] = float(m.group(1))
        m2 = re.search(r"52w low distance:\s*([-+]?[\d.]+)\s*%", hi)
        if m2:
            index["technical_pct_off_52w_low"] = float(m2.group(1))
    ma = _line_after("- MA200 distance:")
    if ma:
        m = re.search(r"([-+]?[\d.]+)\s*%", ma)
        if m:
            index["technical_ma200_distance_pct"] = float(m.group(1))
        m2 = re.search(r"MA200 slope:\s*([-+]?[\d.]+)\s*%", ma)
        if m2:
            index["technical_ma200_slope_pct_per_day"] = float(m2.group(1))

    # technicals line: "RSI 53 / MA50 +2.4% / MA200 -18.3% (...) / 52w high
    # -39.2% / 52w low +14.7% / ATR 4.6% / volZ -1.3"
    tech = _line_after("- technicals:")
    if tech:
        for label, key in (
            ("RSI", "technical_rsi"),
            ("MA50", "technical_ma50_distance_pct"),
            ("ATR", "technical_atr_pct"),
            ("volZ", "technical_volz"),
        ):
            m = re.search(re.escape(label) + r"\s+([-+]?[\d.]+)", tech)
            if m:
                index[key] = float(m.group(1))

    # catalyst title + published date (for entity / date grounding)
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("title:"):
            index["source_event_title"] = stripped[len("title:") :].strip()
        elif stripped.startswith("published:"):
            index["source_event_published_at"] = stripped[len("published:") :].strip()

    # next_earnings_date if present
    ned = _line_after("next_earnings_date:")
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
# The magnitude word (bn|billion|million) is allowed OPTIONAL whitespace before
# it: briefs write "$7.5 billion" far more often than "$7.5B", so the suffix
# group must consume a spaced magnitude word or the billion/million is silently
# dropped (a $-billion claim would then false-ground against an unrelated
# same-digit ratio fact — memo §6.4 correctness fix).
_ATOM_NUM_RE = re.compile(
    r"(?P<dollar>\$)?"
    r"(?P<sign>[-+])?"
    r"(?P<num>\d[\d,]*\.\d+|\d[\d,]*)"
    r"(?:(?P<glued>%|x|B|M|k)|\s*(?P<word>bn|billion|million|trillion))?",
    re.IGNORECASE,
)
_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")

# Label tokens where a glued integer is a STRUCTURAL REFERENCE, not a claimed
# quantitative value (memo §6.2 "score only checkable atoms"). Regexes match the
# whole label so its digits are masked out before atom extraction:
#   MA200 / MA50, 52w / 52-week, 200-day, S-1, 10-K / 8-K, 180 days / 180d,
#   "N weeks/months/days" durations, "1st/2nd/3rd" ordinals, "N)" list markers.
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
    dollar = match.group("dollar") or ""
    suffix = (match.group("glued") or match.group("word") or "").lower()
    value = num
    unit = ""
    if dollar:
        unit = "$"
    if suffix in ("%",):
        unit = "%"
    elif suffix == "x":
        unit = "x"
    elif suffix in ("b", "bn", "billion"):
        value = num * 1e9
        unit = "$" if dollar else "count"
    elif suffix == "trillion":
        value = num * 1e12
        unit = "$" if dollar else "count"
    elif suffix in ("m", "million"):
        value = num * 1e6
        unit = "$" if dollar else "count"
    elif suffix == "k":
        value = num * 1e3
        unit = "$" if dollar else "count"
    if sign == "-":
        value = -value
    span = match.group(0)
    return value, unit, span


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


def _match_numeric(atom: Atom, value: float, facts: dict) -> Atom:
    """Classify a numeric atom against the fact index (memo §6.4 step 3).

    Unit-AWARE: only facts whose inferred unit-kind is compatible with the
    atom's unit are candidates, so a ``$``-magnitude claim can never ground a
    ratio fact and an ``x``/bare multiple can never ground a ``%`` fact.
    """
    span = atom.span
    precision = _brief_precision(span)
    allowed_kinds = _ATOM_UNIT_TO_FACT_KINDS.get(_atom_unit(atom), frozenset({"ratio"}))
    best_grounded: tuple[str, float] | None = None
    best_distorted: tuple[str, float] | None = None

    for key, fact_val in _numeric_fact_candidates(facts):
        if _fact_unit_kind(key) not in allowed_kinds:
            continue
        candidates = [fact_val]
        # Sign-strip for directional facts when the brief supplies its own
        # direction word (memo §6.4 step 1): compare |brief| to |fact|.
        if key in _DIRECTIONAL_FACT_KEYS:
            candidates.append(abs(fact_val))
        for fv in candidates:
            # Rounding tolerance: round the FACT to the brief's stated precision.
            # Keep the CLOSEST grounded fact (honest attribution: prefer the
            # exact-value fact over one that only collides after rounding, e.g.
            # insider $0k over fcf_margin 0.34 → 0).
            if round(fv, precision) == round(value, precision):
                if best_grounded is None or abs(value - fv) < abs(value - best_grounded[1]):
                    best_grounded = (key, fv)
                continue
            # DISTORTED band: within one-unit-of-last-place OR a relative
            # tolerance of a same-kind fact (memo pin "50%" vs -39.2%). The
            # relative band is scaled to the FACT magnitude ONLY (not
            # max(fact, brief)) so tolerance stays symmetric around the true
            # value: a brief that overstates by >40% of the fact is FABRICATED,
            # not DISTORTED (memo §6.4 correctness fix — an asymmetric band
            # widened as the brief overstated).
            step = 10.0 ** (-precision) if precision > 0 else 1.0
            abs_band = 2.0 * step
            rel_band = _DISTORTED_REL_BAND * max(abs(fv), 1e-9)
            within_band = abs(value - fv) <= max(abs_band, rel_band)
            closer = best_distorted is None or abs(value - fv) < abs(value - best_distorted[1])
            if within_band and closer:
                best_distorted = (key, fv)

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


def _characterization_atoms(field_name: str, text: str, facts: dict) -> list[Atom]:
    """Detect forbidden-lexicon characterization violations in one field.

    Fires VIOLATION only on the affirmative, un-negated, un-quoted match
    (memo §6.4). ``next_earnings_date`` forecast verbs fire only when a forecast
    phrase is adjacent to the earnings date reference.
    """
    atoms: list[Atom] = []
    if not text:
        return atoms
    low = text.lower()

    # Drawdown/valuation framing lexicon.
    for phrase in _FORBIDDEN_CHAR_PHRASES:
        for m in re.finditer(r"\b" + re.escape(phrase) + r"\b", low):
            start = m.start()
            if _is_negated(low, start):
                continue
            if _is_academic_refusal(text, start, len(phrase)):
                continue
            if _is_quoted(text, start, len(phrase)):
                continue
            atoms.append(
                Atom(
                    field=field_name,
                    span=text[max(0, start - 15) : start + len(phrase) + 15].strip(),
                    kind="characterization",
                    extracted_value=phrase,
                    verdict="VIOLATION",
                    gating=True,
                )
            )

    # Forecast-verb-near-earnings check: fires if the field references the
    # earnings event by the WORD 'earnings' OR by the next_earnings_date value
    # itself (a date-only reference like "a strong print on 2026-06-15" still
    # anchors — the ban is on forecasting the outcome, not on the literal word,
    # memo §6.4 correctness fix).
    earnings_date = str(facts.get("next_earnings_date") or "").strip()
    anchors = [m.start() for m in re.finditer(r"earnings", low)]
    if earnings_date:
        anchors += [m.start() for m in re.finditer(re.escape(earnings_date.lower()), low)]
    if earnings_date and anchors:
        for phrase in _FORECAST_PHRASES:
            for m in re.finditer(r"\b" + re.escape(phrase) + r"\b", low):
                start = m.start()
                if _is_quoted(text, start, len(phrase)):
                    continue
                near = any(abs(start - a) <= _EARNINGS_WINDOW for a in anchors)
                if not near:
                    continue
                atoms.append(
                    Atom(
                        field=field_name,
                        span=text[max(0, start - 15) : start + len(phrase) + 15].strip(),
                        kind="characterization",
                        extracted_value=phrase,
                        verdict="VIOLATION",
                        gating=True,
                    )
                )
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
