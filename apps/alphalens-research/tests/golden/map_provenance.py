"""Machine-readable provenance for one map-themes recording.

A characterization fixture is only auditable if it says where it came from. The
committed cassette and projection show WHAT was approved; this file records what
it was recorded FROM — the event, the prompt version, the model and sampling
config, the cassette key, the digest of every frozen surface, which of those
surfaces were hand-authored rather than captured, the day it was captured, and
who approved it.

One document per RECORDING, at ``golden/<version>/provenance.json``, because
those facts belong to the recording and not to the fixture: a re-baseline adds a
new version directory with its own provenance and leaves the old one untouched,
so the two can be diffed. The frozen surfaces are shared across a fixture's
versions, so each manifest is a snapshot of them AT CAPTURE TIME — that is what
makes a deliberate full re-capture visible instead of silent.

This module is the single source of truth for the document: ``build_provenance``
writes it from the artifacts (never from prose), and
``tests/golden/test_golden_map_provenance.py`` checks it with the same field
list, so the recorder and the guard cannot drift.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path
from typing import Any

import pandas as pd
from alphalens_pipeline.thematic.mapping import catalyst_resolver

from tests.golden.map_fixtures import MapFixture

PROVENANCE_FILENAME = "provenance.json"

# Bump when the document's shape changes, so an old file fails loud instead of
# being read with a field silently absent.
# v2: added ``seeded_surfaces`` — the frozen surfaces whose content was written
# by hand rather than captured.
PROVENANCE_SCHEMA_VERSION = 2

# Re-recording a characterization golden is a reviewed operation, so the
# document names the human who approved the execution it pins.
APPROVER = "Kamil Pajak"

#: Dotted paths that MUST be present and non-empty in every provenance file.
#: ``None`` / ``""`` / ``{}`` / ``[]`` count as absent; ``0`` and ``0.0`` do not
#: (``sampling.temperature`` is legitimately ``0.0``).
REQUIRED_FIELDS: tuple[str, ...] = (
    "schema_version",
    "fixture",
    "recording",
    "event.event_id",
    "event.url",
    "event.headline",
    "event.event_type",
    "event.published_at",
    "event.theme",
    "event.asof",
    "prompt.mapper_config_version",
    "prompt.schema",
    "prompt.prompt_sha",
    "prompt.schema_sha",
    "prompt.system_message_sha",
    "model",
    "sampling.temperature",
    "sampling.max_tokens",
    "sampling.response_format",
    "cassette_key",
    "frozen_surfaces",
    "recorded_date",
    "provenance_written",
    "recorded_by",
    "approved_by",
)

#: Fixture-root subtrees whose every file is a frozen external surface. These
#: are SHARED across the fixture's recording versions; the per-version LLM
#: cassette and golden are deliberately NOT here — they are the recording, not
#: the evidence it was recorded against.
SURFACE_DIRS: tuple[str, ...] = (
    "cassettes_vendor",
    "events",
    "news",
    "tenk_cache",
    "form4_parquet",
)

#: Single frozen-surface files at the fixture root.
SURFACE_FILES: tuple[str, ...] = ("mcap.json",)

_SHA_PREFIX = 12


def recording_versions(fixture: MapFixture) -> tuple[str, ...]:
    """Every recording version present under the fixture's ``golden/`` tree.

    Globbed on purpose — unlike the CURRENT recording, which is read explicitly
    from the descriptor so a replay can never pick up "whatever is on disk".
    Here the point is the opposite: a version directory that was added without
    a provenance file must be found, not skipped.
    """
    root = fixture.root / "golden"
    if not root.is_dir():
        return ()
    return tuple(sorted(p.name for p in root.iterdir() if (p / "projection.json").exists()))


def provenance_path(fixture: MapFixture, version: str | None = None) -> Path:
    return fixture.golden_dir(version) / PROVENANCE_FILENAME


def load_provenance(fixture: MapFixture, version: str | None = None) -> dict[str, Any]:
    return json.loads(provenance_path(fixture, version).read_text())


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:_SHA_PREFIX]


def surface_manifest(fixture: MapFixture) -> dict[str, str]:
    """``{path relative to the fixture root: sha256}`` for every frozen surface."""
    manifest: dict[str, str] = {}
    for name in SURFACE_FILES:
        path = fixture.root / name
        if path.exists():
            manifest[name] = sha256_file(path)
    for subdir in SURFACE_DIRS:
        root = fixture.root / subdir
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if path.is_file():
                manifest[str(path.relative_to(fixture.root))] = sha256_file(path)
    return dict(sorted(manifest.items()))


def seeded_surfaces(fixture: MapFixture) -> dict[str, str]:
    """``{path relative to the fixture root: why it was hand-authored}``.

    Read off the fixture descriptor, which is where the fixture author declares
    it. Everything else in the document is re-derived from artifacts; this one
    cannot be, because "was this row typed or ingested?" is not recoverable
    from the row. Writing it through the recorder is what stops the disclosure
    from living only in a memo that the next capture forgets to update.
    """
    return dict(sorted(fixture.seeded_surfaces))


def cassette_record(fixture: MapFixture, version: str | None = None) -> dict[str, Any]:
    """The one recorded LLM request/response for a recording version.

    ``map_themes`` makes exactly one Pro call per theme and every fixture pins
    one theme, so more than one cassette in a version directory means two
    recordings were mixed — refuse rather than pick.
    """
    directory = fixture.llm_cassette_dir(version)
    cassettes = sorted(directory.glob("*.json"))
    if len(cassettes) != 1:
        raise ValueError(
            f"expected exactly 1 LLM cassette in {directory}, found {len(cassettes)} — "
            "a recording version holds one request, so this is two recordings mixed"
        )
    return json.loads(cassettes[0].read_text())


def resolve_event(fixture: MapFixture) -> dict[str, Any]:
    """Re-resolve the trigger event from the fixture's OWN frozen event window.

    Reads the identity off the artifacts rather than taking it from prose, so a
    provenance file cannot claim an event the replay does not actually use. The
    resolver returns the catalyst payload (which carries the URL, not the store
    id), so the news window supplies the ``news_id`` that identifies the event
    in the thematic store.
    """
    payload = catalyst_resolver.find_trigger_event(
        theme=fixture.theme,
        asof=fixture.asof,
        events_dir=fixture.events_dir,
        news_dir=fixture.news_dir,
    )
    if payload is None:
        raise ValueError(
            f"{fixture.name}: the frozen event window resolves no trigger event for "
            f"{fixture.theme} at {fixture.asof} — the fixture cannot be described"
        )
    return {
        "event_id": _news_id_for_url(fixture, payload.url),
        "url": payload.url,
        "headline": payload.title,
        "event_type": payload.event_type,
        "published_at": str(payload.published_at),
        "theme": fixture.theme,
        "asof": fixture.asof.isoformat(),
    }


def _news_id_for_url(fixture: MapFixture, url: str) -> str:
    for path in sorted(fixture.news_dir.glob("*.parquet")):
        frame = pd.read_parquet(path)
        hit = frame[frame["url"] == url]
        if not hit.empty:
            return str(hit.iloc[0]["id"])
    raise ValueError(f"{fixture.name}: no frozen news row carries the trigger URL {url}")


def _field(doc: dict[str, Any], dotted: str) -> Any:
    node: Any = doc
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def missing_fields(doc: dict[str, Any]) -> list[str]:
    """Required dotted paths that are absent or empty, in declaration order."""
    missing = []
    for dotted in REQUIRED_FIELDS:
        value = _field(doc, dotted)
        # Explicitly NOT `if not value`: temperature 0.0 is a real value.
        if value is None or value in ("", {}, []):
            missing.append(dotted)
    return missing


def audit_surfaces(fixture: MapFixture, doc: dict[str, Any]) -> list[str]:
    """Differences between the document's manifest and the fixture tree on disk."""
    listed: dict[str, str] = doc.get("frozen_surfaces") or {}
    actual = surface_manifest(fixture)
    problems = []
    for rel in sorted(set(listed) | set(actual)):
        if rel not in actual:
            problems.append(f"frozen_surfaces lists {rel}, which is not in the fixture tree")
        elif rel not in listed:
            problems.append(f"{rel} is in the fixture tree but not listed in frozen_surfaces")
        elif listed[rel] != actual[rel]:
            problems.append(f"{rel} digest {listed[rel]} does not match the file ({actual[rel]})")
    return problems


def audit_recording(fixture: MapFixture, version: str, doc: dict[str, Any]) -> list[str]:
    """Ways the document contradicts the artifacts it claims to describe.

    Every expectation is re-read from an artifact — the fixture descriptor, the
    recorded cassette, the frozen event window, or the candidates parquet the
    recording produced. The three prompt fingerprints are the one exception:
    they are checked against the frozen ``mapper_config_version`` token they are
    supposed to summarise, which for a recording whose parquet carries that
    token is in turn pinned to the parquet.

    KNOWN LIMIT — a recording whose parquet predates the
    ``mapper_config_version`` column has NO artifact to pin that token against,
    so a wrong-but-internally-consistent token would pass every check here.
    That case cannot be closed after the fact (the parquet is frozen), so it is
    made visible instead: such a recording must disclose the gap in ``notes``,
    and this function reports it when it does not.
    """
    record = cassette_record(fixture, version)
    config = record.get("config") or {}
    event = resolve_event(fixture)
    config_version = _parsed_config_version(doc)
    stamped = stamped_config_version(fixture, version)

    expected: list[tuple[str, Any]] = [
        ("schema_version", PROVENANCE_SCHEMA_VERSION),
        ("fixture", fixture.name),
        ("recording", version),
        ("approved_by", APPROVER),
        ("cassette_key", record["key"]),
        ("model", record["model"]),
        ("sampling.temperature", config.get("temperature")),
        ("sampling.max_tokens", config.get("max_tokens")),
        ("sampling.response_format", config.get("response_format")),
        ("prompt.system_message_sha", sha256_text(config.get("system_message") or "")),
        *[(f"event.{key}", value) for key, value in event.items()],
        # The three prompt fingerprints must be the ones inside the frozen
        # mapper_config_version token, not free-typed beside it.
        ("prompt.schema", config_version.get("schema")),
        ("prompt.prompt_sha", config_version.get("prompt_sha")),
        ("prompt.schema_sha", config_version.get("schema_sha")),
        ("model", config_version.get("model")),
        # Hand-authored surfaces are declared on the descriptor; the document
        # must repeat the declaration verbatim, so neither side can drift into
        # presenting a seeded input as a captured one.
        ("seeded_surfaces", seeded_surfaces(fixture)),
    ]
    if stamped is not None:
        expected.append(("prompt.mapper_config_version", stamped))
    problems = []
    for dotted, want in expected:
        got = _field(doc, dotted)
        if got != want:
            problems.append(f"{dotted} is {got!r}, artifacts say {want!r}")
    if stamped is None and not str(doc.get("notes") or "").strip():
        problems.append(
            f"{fixture.name}/{version} stamps no mapper_config_version in its parquet, so "
            "the token is pinned by nothing — notes must say where it came from"
        )
    return problems


def stamped_config_version(fixture: MapFixture, version: str) -> str | None:
    """The ``mapper_config_version`` the recording stamped into its parquet.

    ``None`` when the recording predates that freeze column (the earliest
    recording does) — then there is no artifact to pin the token against and
    the document's own value stands, as recorded in its ``notes``.
    """
    parquet = fixture.golden_dir(version) / f"{fixture.asof.isoformat()}.parquet"
    if not parquet.exists():
        return None
    frame = pd.read_parquet(parquet)
    if "mapper_config_version" not in frame.columns or frame.empty:
        return None
    stamped = sorted(set(frame["mapper_config_version"].astype(str)))
    if len(stamped) != 1:
        raise ValueError(
            f"{fixture.name}/{version} parquet carries {len(stamped)} distinct "
            "mapper_config_version values — one recording is one config"
        )
    return stamped[0]


def _parsed_config_version(doc: dict[str, Any]) -> dict[str, Any]:
    raw = _field(doc, "prompt.mapper_config_version")
    if not isinstance(raw, str):
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def build_provenance(
    fixture: MapFixture,
    *,
    version: str,
    mapper_config_version: str,
    recorded_by: str,
    recorded_date: dt.date,
    provenance_written: dt.date | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """Assemble the document from the artifacts of one recording.

    ``mapper_config_version`` is passed in rather than read from the live
    ``theme_mapper`` because a document describes the prompt the RECORDING was
    made under, which is not necessarily the one in the working tree.

    ``recorded_date`` is when the capture ran; ``provenance_written`` defaults
    to it and differs only when the document was reconstructed later from the
    committed artifacts. Keeping both visible is what makes a backfilled record
    legible as backfilled instead of passing as contemporaneous.
    """
    record = cassette_record(fixture, version)
    config = record.get("config") or {}
    parsed = json.loads(mapper_config_version)
    doc: dict[str, Any] = {
        "schema_version": PROVENANCE_SCHEMA_VERSION,
        "fixture": fixture.name,
        "recording": version,
        "event": resolve_event(fixture),
        "prompt": {
            "mapper_config_version": mapper_config_version,
            "schema": parsed["schema"],
            "prompt_sha": parsed["prompt_sha"],
            "schema_sha": parsed["schema_sha"],
            "system_message_sha": sha256_text(config.get("system_message") or ""),
        },
        "model": record["model"],
        "sampling": {
            "temperature": config.get("temperature"),
            "max_tokens": config.get("max_tokens"),
            "response_format": config.get("response_format"),
        },
        "cassette_key": record["key"],
        "frozen_surfaces": surface_manifest(fixture),
        "seeded_surfaces": seeded_surfaces(fixture),
        "recorded_date": recorded_date.isoformat(),
        "provenance_written": (provenance_written or recorded_date).isoformat(),
        "recorded_by": recorded_by,
        "approved_by": APPROVER,
    }
    if notes:
        doc["notes"] = notes
    return doc


def write_provenance(fixture: MapFixture, doc: dict[str, Any]) -> Path:
    path = provenance_path(fixture, doc["recording"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
    return path


__all__ = [
    "APPROVER",
    "PROVENANCE_FILENAME",
    "PROVENANCE_SCHEMA_VERSION",
    "REQUIRED_FIELDS",
    "audit_recording",
    "audit_surfaces",
    "build_provenance",
    "cassette_record",
    "load_provenance",
    "missing_fields",
    "provenance_path",
    "recording_versions",
    "resolve_event",
    "seeded_surfaces",
    "sha256_file",
    "sha256_text",
    "stamped_config_version",
    "surface_manifest",
    "write_provenance",
]
