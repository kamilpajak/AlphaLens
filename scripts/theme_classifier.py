"""Theme classifier for survivorship probe — improved filters per Perplexity research.

Input: Polygon ticker details (sic_code, sic_description, name, composite_figi).
Output: theme label ('quantum'|'ai'|'biotech'|'semis'|None) + confidence.

Usage:
    from theme_classifier import classify_theme
    theme, confidence = classify_theme(sic_code="3674", sic_desc="SEMICONDUCTORS & RELATED DEVICES", name="Intel Corp")
"""

from __future__ import annotations

from dataclasses import dataclass

# SIC -> primary theme mapping. Based on Perplexity research + real US small/mid cap landscape.
# Semiconductors — tight set; 3674 is canonical.
SEMIS_SIC = {"3674", "3679", "3559", "3827"}
SEMIS_DESC_SUBSTRINGS = (
    "semiconductor",
    "electronic component",
    "special industry machinery",
    "optical instruments",
    "integrated circuit",
)
SEMIS_NAME_KEYWORDS = (
    "semiconductor",
    "semi ",
    " semi",
    "silicon",
    "wafer",
    "photomask",
    "photon",  # caveat: photon can be biomed — we cross-check with SIC
)

# AI — mostly software; 7372 prepackaged software dominates.
AI_SIC = {"7372", "7371", "7374", "7370", "3571"}
AI_DESC_SUBSTRINGS = (
    "prepackaged software",
    "computer programming",
    "data processing",
    "electronic computers",
)
AI_NAME_KEYWORDS = (
    " ai ",
    " ai,",
    "ai)",
    "artificial intel",
    "machine lear",
    "deep lear",
    "neural",
    "cogniti",
    "autonom",
    "robot",
    "soundhound",
    "upstart",
    "pegasyst",
    "palantir",
    "c3.ai",
    "bigbear",
    "innodata",
    "applied digital",
    "core scient",
)

# Quantum — too niche for SIC alone; keyword-driven.
QUANTUM_NAME_KEYWORDS = (
    "quantum comput",
    "quantum corp",
    " qubit",
    "ionq",
    "rigetti",
    "d-wave",
    "atomera",
    "formfactor",  # semicap-quantum adjacent
)

# Biotech — pharma + biologics + diagnostics. Therapeutics/Biosciences name tag is gold.
BIOTECH_SIC = {"2834", "2836", "2835", "2833", "8731"}
BIOTECH_DESC_SUBSTRINGS = (
    "biological products",
    "pharmaceutical preparations",
    "in vitro",
    "diagnostic substances",
    "medicinal chemicals",
    "commercial physical & biological research",
)
BIOTECH_NAME_KEYWORDS = (
    "therapeutics",
    "therapeutic ",
    "biosciences",
    "bioscience",
    "pharmaceutic",
    "biopharm",
    "bio-",
    "genomics",
    "gene therap",
    "immuno",
    "oncolog",
    "crispr",
    "gene editing",
    "rna ",
    "mrna",
)

# Hard excludes — SICs we never want matched, even if name has keywords.
EXCLUDED_SIC = {
    "7389",  # Services-Business Services NEC — too broad
    "3670",  # broad Electronics
    "5065",  # Electronic Parts Distribution
    "6770",  # SPACs / blank check
    "6199",  # Finance services
    "6159",  # Federal credit agencies
}

EXCLUDED_NAME_KEYWORDS = (
    "spac",
    "acquisition corp",
    "holdings",  # SPACs
    "trust",
    "fund",
    "reit",  # funds/REITs
    "royalty",  # royalty companies
)


@dataclass(frozen=True)
class ThemeMatch:
    theme: str | None
    confidence: str  # "high" | "medium" | "low" | "none"
    reason: str


def _norm(s: str | None) -> str:
    return (s or "").lower()


def classify_theme(
    sic_code: str | None,
    sic_desc: str | None,
    name: str | None,
) -> ThemeMatch:
    """Return best theme match for a ticker.

    Policy:
    - SIC in EXCLUDED_SIC → never matched.
    - SIC match + name match → high confidence.
    - SIC match only → medium.
    - Name match only → low (risky — could be false positive).
    """
    sic = _norm(sic_code)
    desc = _norm(sic_desc)
    n = _norm(name)

    # Hard excludes first
    if sic in EXCLUDED_SIC:
        return ThemeMatch(None, "none", f"excluded SIC {sic}")
    for kw in EXCLUDED_NAME_KEYWORDS:
        if kw in n and sic not in BIOTECH_SIC and sic not in SEMIS_SIC:
            # Allow "holdings" in semis/biotech (e.g. "Alnylam Holdings") but not generic SPACs
            return ThemeMatch(None, "none", f"excluded name keyword '{kw}'")

    # Quantum — keyword-driven. Check first because it's narrow.
    if any(kw in n for kw in QUANTUM_NAME_KEYWORDS):
        # High if SIC is tech/semi, medium otherwise
        if sic in AI_SIC | SEMIS_SIC:
            return ThemeMatch("quantum", "high", "quantum keyword + tech SIC")
        return ThemeMatch("quantum", "medium", "quantum keyword, SIC weak")

    # Biotech — SIC is strongest signal in this space
    sic_biotech = sic in BIOTECH_SIC or any(s in desc for s in BIOTECH_DESC_SUBSTRINGS)
    name_biotech = any(kw in n for kw in BIOTECH_NAME_KEYWORDS)
    if sic_biotech and name_biotech:
        return ThemeMatch("biotech", "high", "biotech SIC + name")
    if sic_biotech:
        return ThemeMatch("biotech", "medium", "biotech SIC only")
    if name_biotech and not sic:
        # Name-only when SIC is null (common for delisted)
        return ThemeMatch("biotech", "low", "biotech name, SIC missing")

    # Semis — SIC dominates
    sic_semis = sic in SEMIS_SIC or any(s in desc for s in SEMIS_DESC_SUBSTRINGS)
    name_semis = any(kw in n for kw in SEMIS_NAME_KEYWORDS)
    if sic_semis and name_semis:
        return ThemeMatch("semis", "high", "semis SIC + name")
    if sic_semis:
        return ThemeMatch("semis", "medium", "semis SIC only")
    if name_semis and not sic:
        return ThemeMatch("semis", "low", "semis name, SIC missing")

    # AI — trickiest; software SIC is noisy
    sic_ai = sic in AI_SIC or any(s in desc for s in AI_DESC_SUBSTRINGS)
    name_ai = any(kw in n for kw in AI_NAME_KEYWORDS)
    if sic_ai and name_ai:
        return ThemeMatch("ai", "high", "AI SIC + strong name keyword")
    if name_ai:
        # Name-driven AI match — low confidence because "AI" keyword is overloaded
        return ThemeMatch("ai", "low", "AI name only, SIC weak")

    return ThemeMatch(None, "none", f"no match (sic={sic!r} name={n[:30]!r})")


if __name__ == "__main__":
    # Self-test with known examples.
    cases = [
        ("3674", "SEMICONDUCTORS & RELATED DEVICES", "Intel Corp", "semis", "high"),
        ("2836", "PHARMACEUTICAL PREPARATIONS", "CRISPR Therapeutics", "biotech", "high"),
        ("7372", "SERVICES-PREPACKAGED SOFTWARE", "C3.ai, Inc.", "ai", "high"),
        (None, None, "Quantum Computing Inc", "quantum", "medium"),
        ("6770", None, "Digital World Acquisition Corp", None, "none"),
        ("2836", "BIOLOGICAL PRODUCTS", "Taysha Gene Therapies", "biotech", "high"),
        (
            "7372",
            "SERVICES-PREPACKAGED SOFTWARE",
            "Salesforce Inc",
            "ai",
            None,
        ),  # false positive risk
        ("3679", "ELECTRONIC COMPONENTS, NEC", "Vishay Intertechnology", "semis", "high"),
    ]
    for sic, desc, name, expected_theme, expected_conf in cases:
        m = classify_theme(sic, desc, name)
        status = "OK" if (m.theme == expected_theme) else "MISMATCH"
        print(f"  [{status}] {name:40s} sic={sic!r:8} → {m.theme!r:9} ({m.confidence}) {m.reason}")
