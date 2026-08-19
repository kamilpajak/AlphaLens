#!/usr/bin/env python
"""Phase-1 blind labeling for the Stage-1 retro gate-increment study.

Replays the FROZEN production Stage-1 event-conditioned mapper over every
(theme, source_event) pair of the old-cohort brief rows and assigns each pair a
majority label, per `docs/research/stage1_retro_gate_increment_prereg_2026_08_19.md`
(§4-§6, §11.1). Measurement only — nothing here feeds selection, ordering, or
the brief.

Locked parameters (pre-registered; do not change without a memo amendment):

* **Instrument** — `stage1_frozen_v2.propose_candidates_frozen`, a byte copy of the
  mapper-freeze-v2 proposal call (the live `theme_mapper` moved to v3 on
  2026-08-19 and no longer has this shape).
  The run asserts `mapper_config_version(market_cap_range=(500e6, 10e9))`
  equals the pre-registered ``FROZEN_MCV`` string byte-for-byte, and that the
  OpenRouter routing block is the exact pinned-provider block
  (`{"order": ["Alibaba"], "allow_fallbacks": False, "quantizations": ["fp8"],
  "require_parameters": True}`) before any call.
* **Input construction** — `CatalystPayload` from the brief row's STORED
  stamped catalyst fields joined to stored event records (all frozen into the
  input parquet). Today's `catalyst_resolver` is never re-run.
* **k = 5** calls per pair; per-call label is `THEME_REFUSED` on a
  `DECLINED` outcome, else `KEPT`. Pair label: `THEME_REFUSED` if >= 3/5 calls
  decline, else `KEPT`. Majority proposal set: tickers proposed in >= 3/5
  calls. Row label: `KEPT_TICKER_PROPOSED` iff the row's ticker is in the
  majority proposal set, else `KEPT_TICKER_ABSENT`.
* **Failures** — empty / malformed / failed calls are FAILURES, never refusal
  labels (#982): each k-slot retries with exponential backoff; a pair that
  cannot complete 5 valid calls is `INSTRUMENT_FAILURE`, excluded from the
  contrast and counted. A call served by any provider other than the pinned
  one is a FAILURE (raised inside the provenance wrapper -> `CALL_FAILED` ->
  retried), never a label.
* **Concurrency** — max 3 threads (10 threads drove the role-classifier
  empty-rate 25%->57%).
* **Blinding** — this module must never read outcome stores or outcome
  columns. Enforced by `tests/test_stage1_retro_label_pairs.py`, which scans
  this file's source for forbidden references.

Usage::

    python apps/alphalens-research/scripts/stage1_retro_label_pairs.py \\
        --input  <inputs.parquet> \\
        --out    <labels.parquet> \\
        --raw-dir <raw_calls/> \\
        --calls-log <phase1_calls.jsonl> \\
        --provenance-log <phase1_provenance.jsonl> \\
        --summary <phase1_summary.json>

The calls log is append-only and doubles as a resume journal: completed
(pair, slot) records are skipped on restart, so an interrupted run never
re-pays for finished calls.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import threading
import time
from collections import Counter
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd
from alphalens_pipeline.data.alt_data.openrouter_client import OpenRouterClient
from alphalens_pipeline.thematic.mapping.catalyst_contract import CatalystPayload
from alphalens_pipeline.thematic.mapping.theme_mapper import MapperOutcome
from alphalens_research.retrospective_audit import stage1_frozen_v2

# --- Locked constants (pre-reg §4-§6, §11.1) --------------------------------

K = 5  # calls per pair
MAJORITY = 3  # strict majority of K=5, for both refusal votes and proposal set
THREADS = 3
MAX_ATTEMPTS_PER_SLOT = 4  # each attempt retries EMPTY once inside propose_candidates
RETRY_BACKOFF_BASE_S = 3.0

PINNED_PROVIDER = "Alibaba"
PINNED_ROUTING = {
    "order": [PINNED_PROVIDER],
    "allow_fallbacks": False,
    "quantizations": ["fp8"],
    "require_parameters": True,
}

# Pre-registered frozen instrument identity (pre-reg §2; sha256 of this string
# is 54704ae415f8e4e1fcc8669b0928fb2803354d90d3fc53489167fb9f1c54d263).
FROZEN_MCV = (
    '{"block_tag":"untrusted_event","field_constants":{"entities_max":10,'
    '"field_max_chars":80,"headline_max_chars":200,"implication_max_chars":240,'
    '"implications_max":5,"unavailable":"(none)"},"max_candidates":15,'
    '"max_output_tokens":8000,"mcap_range":[500000000,10000000000],'
    '"model":"deepseek/deepseek-v4-pro","prompt_sha":"52b12550f344",'
    '"schema":"mapper-freeze-v2","schema_sha":"ec5d56e9d13a","temperature":0.0}'
)

LABEL_THEME_REFUSED = "THEME_REFUSED"
LABEL_KEPT = "KEPT"
LABEL_KEPT_PROPOSED = "KEPT_TICKER_PROPOSED"
LABEL_KEPT_ABSENT = "KEPT_TICKER_ABSENT"
LABEL_INSTRUMENT_FAILURE = "INSTRUMENT_FAILURE"
LABEL_NO_SOURCE_EVENT = "NO_SOURCE_EVENT"  # row has no stored source_event_url

_log_lock = threading.Lock()
_task_ctx = threading.local()


# --- Pure core (unit-tested) ------------------------------------------------


def pair_key(theme: str, source_event_url: str) -> str:
    """The (theme, source_event) pair identity: ``theme|source_event_url``."""
    return f"{theme}|{source_event_url}"


def call_label(outcome_value: str) -> str:
    """Per-call two-level label from a valid MapperOutcome value."""
    return LABEL_THEME_REFUSED if outcome_value == "declined" else LABEL_KEPT


def majority_pair_label(call_labels: Sequence[str]) -> str:
    """Pair-level majority label from exactly K valid per-call labels.

    ``THEME_REFUSED`` iff >= MAJORITY of the K calls declined; ``KEPT``
    otherwise. Fewer than K valid calls is an ``INSTRUMENT_FAILURE``.
    """
    if len(call_labels) != K:
        return LABEL_INSTRUMENT_FAILURE
    refused = sum(1 for label in call_labels if label == LABEL_THEME_REFUSED)
    return LABEL_THEME_REFUSED if refused >= MAJORITY else LABEL_KEPT


def majority_proposal_set(call_proposals: Sequence[Sequence[str]]) -> set[str]:
    """Tickers proposed in >= MAJORITY of the K calls.

    Declined calls contribute empty proposal lists; duplicate mentions within
    one call count once.
    """
    counts = Counter(t for proposals in call_proposals for t in set(proposals))
    return {t for t, c in counts.items() if c >= MAJORITY}


def derive_row_label(pair_label: str, majority_set: set[str], ticker: str) -> str:
    """Row-level label from the pair label + the pair's majority proposal set."""
    if pair_label in (LABEL_THEME_REFUSED, LABEL_INSTRUMENT_FAILURE, LABEL_NO_SOURCE_EVENT):
        return pair_label
    return LABEL_KEPT_PROPOSED if ticker in majority_set else LABEL_KEPT_ABSENT


def vote_counts(call_labels: Sequence[str]) -> tuple[int, int]:
    """(n_refused, n_kept) over the valid per-call labels."""
    refused = sum(1 for label in call_labels if label == LABEL_THEME_REFUSED)
    return refused, len(call_labels) - refused


# --- Payload construction (stored stamped fields only) ----------------------


def payload_from_row(row: pd.Series) -> CatalystPayload:
    def _s(v):
        return None if v is None or (isinstance(v, float) and pd.isna(v)) else str(v)

    def _lst(v):
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return []
        return [str(x) for x in list(v)]

    conf = row.get("catalyst_confidence")
    conf = float(conf) if conf is not None and not pd.isna(conf) else None
    return CatalystPayload(
        url=str(row["source_event_url"]),
        title=str(row["source_event_title"] or ""),
        published_at=str(row["source_event_published_at"] or ""),
        event_type=_s(row.get("catalyst_event_type")),
        primary_entities=_lst(row.get("event_primary_entities")),
        confidence=conf,
        second_order_implications=_lst(row.get("event_second_order_implications")),
        # echo_count / is_amplified are NOT stamped on the frozen brief rows
        # (verified: inputs parquet carries no such columns), so the replay
        # uses the non-amplified single-trigger defaults for every pair.
        echo_count=1,
        trigger_url=str(row["source_event_url"]),
        trigger_published_at=str(row["source_event_published_at"] or ""),
        is_amplified=False,
        template_id=_s(row.get("catalyst_template_id")),
        template_facts=(
            json.loads(row["catalyst_template_facts_json"])
            if isinstance(row.get("catalyst_template_facts_json"), str)
            and row["catalyst_template_facts_json"]
            else None
        ),
    )


# --- Call machinery ---------------------------------------------------------


def _wrap_provenance(client: OpenRouterClient, prov_log: Path, raw_dir: Path) -> None:
    """Log provider/served_model/generation_id and persist the raw response
    text for EVERY underlying call; raise (=> CALL_FAILED => retry) if a call
    is served off-pin."""
    inner = client.generate_content
    seq = {"n": 0}

    def logged(**kwargs):
        resp = inner(**kwargs)
        with _log_lock:
            seq["n"] += 1
            n = seq["n"]
        task = getattr(_task_ctx, "task", "untracked")
        rec = {
            "seq": n,
            "ts": dt.datetime.now(dt.UTC).isoformat(),
            "task": task,
            "provider": resp.provider,
            "served_model": resp.served_model,
            "generation_id": resp.generation_id,
            "text_len": len(resp.text or ""),
        }
        raw = {**rec, "text": resp.text or ""}
        raw_path = raw_dir / f"call_{n:05d}.json"
        raw_path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        with _log_lock, prov_log.open("a") as fh:
            fh.write(json.dumps(rec) + "\n")
        if resp.provider != PINNED_PROVIDER:
            raise RuntimeError(f"off-pin call: provider={resp.provider!r} != {PINNED_PROVIDER!r}")
        return resp

    client.generate_content = logged  # type: ignore[method-assign]


def one_slot(
    client: OpenRouterClient,
    calls_log: Path,
    *,
    pair_id: str,
    slot: int,
    theme: str,
    catalyst: CatalystPayload,
) -> dict:
    """One k-slot: retries FAILURE outcomes with backoff; returns a record."""
    _task_ctx.task = f"{pair_id}#{slot}"
    delay = RETRY_BACKOFF_BASE_S
    for attempt in range(1, MAX_ATTEMPTS_PER_SLOT + 1):
        result = stage1_frozen_v2.propose_candidates_frozen(
            theme=theme, catalyst=catalyst, llm_client=client
        )
        outcome: MapperOutcome = result["outcome"]
        if outcome in (MapperOutcome.SUCCESS, MapperOutcome.DECLINED):
            rec = {
                "pair_id": pair_id,
                "slot": slot,
                "attempt": attempt,
                "ts": dt.datetime.now(dt.UTC).isoformat(),
                "theme": theme,
                "outcome": outcome.value,
                "call_label": call_label(outcome.value),
                "proposed_tickers": [c["ticker"] for c in result["candidates"]],
                "no_candidates_reason": result["no_candidates_reason"],
                "candidates": result["candidates"],
            }
            with _log_lock, calls_log.open("a") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            return rec
        print(
            f"[{pair_id}#{slot}] attempt {attempt}: FAILURE outcome={outcome.value}, retrying",
            file=sys.stderr,
        )
        time.sleep(delay)
        delay *= 2
    rec = {
        "pair_id": pair_id,
        "slot": slot,
        "attempt": MAX_ATTEMPTS_PER_SLOT,
        "ts": dt.datetime.now(dt.UTC).isoformat(),
        "theme": theme,
        "outcome": "UNRESOLVED_FAILURE",
        "call_label": None,
    }
    with _log_lock, calls_log.open("a") as fh:
        fh.write(json.dumps(rec) + "\n")
    return rec


def _completed_slots(calls_log: Path) -> dict[tuple[str, int], dict]:
    """Valid completed (pair_id, slot) records from a prior interrupted run."""
    done: dict[tuple[str, int], dict] = {}
    if not calls_log.exists():
        return done
    for line in calls_log.read_text().splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        if rec.get("call_label") is not None:
            done[(rec["pair_id"], int(rec["slot"]))] = rec
    return done


# --- Aggregation ------------------------------------------------------------


def aggregate_pair(records: Sequence[dict]) -> dict:
    """Pair-level aggregate from this pair's valid slot records."""
    labels = [r["call_label"] for r in records]
    proposals = [r.get("proposed_tickers", []) for r in records]
    pair_label = majority_pair_label(labels)
    refused, kept = vote_counts(labels)
    return {
        "pair_label": pair_label,
        "majority_proposal_set": sorted(majority_proposal_set(proposals))
        if pair_label == LABEL_KEPT
        else [],
        "n_valid_calls": len(labels),
        "n_refused_votes": refused,
        "n_kept_votes": kept,
        "unanimous": len(labels) == K and K in {refused, kept},
    }


def build_label_table(inputs: pd.DataFrame, pair_aggs: dict[str, dict]) -> pd.DataFrame:
    """Row-level label table: one output row per input brief row."""
    out_rows = []
    for _, row in inputs.iterrows():
        pid = row["pair_id"]
        if pid is None or (isinstance(pid, float) and pd.isna(pid)):
            agg = {
                "pair_label": LABEL_NO_SOURCE_EVENT,
                "majority_proposal_set": [],
                "n_valid_calls": 0,
                "n_refused_votes": 0,
                "n_kept_votes": 0,
                "unanimous": False,
            }
            pid = None
        else:
            agg = pair_aggs[pid]
        mset = set(agg["majority_proposal_set"])
        out_rows.append(
            {
                "brief_date": row["brief_date"],
                "ticker": row["ticker"],
                "theme": row["theme"],
                "pair_id": pid,
                "window": row["window"],
                "source_event_url": row.get("source_event_url"),
                "pair_label": agg["pair_label"],
                "row_label": derive_row_label(agg["pair_label"], mset, str(row["ticker"])),
                "majority_proposal_set": json.dumps(agg["majority_proposal_set"]),
                "n_valid_calls": agg["n_valid_calls"],
                "n_refused_votes": agg["n_refused_votes"],
                "n_kept_votes": agg["n_kept_votes"],
                "unanimous": agg["unanimous"],
            }
        )
    return pd.DataFrame(out_rows)


# --- Entry point ------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--raw-dir", required=True, type=Path)
    ap.add_argument("--calls-log", required=True, type=Path)
    ap.add_argument("--provenance-log", required=True, type=Path)
    ap.add_argument("--summary", required=True, type=Path)
    ap.add_argument("--pairs-limit", type=int, default=None, help="smoke runs only")
    args = ap.parse_args()

    # Frozen-instrument + pinned-routing assertions BEFORE any call (pre-reg §2, §6).
    mcv = stage1_frozen_v2.frozen_mapper_config_version(
        market_cap_range=(500_000_000, 10_000_000_000)
    )
    assert mcv == FROZEN_MCV, "the frozen instrument drifted from the pre-registered one"
    client = OpenRouterClient.from_env()
    routing = client._provider_routing
    # Compare only the pre-registered keys: the client may grow harmless extra
    # routing keys, but the pinned values themselves must match byte-for-byte.
    pinned_view = {k: routing.get(k) for k in PINNED_ROUTING}
    assert pinned_view == PINNED_ROUTING, f"replay env not pinned as pre-registered: {routing}"

    args.raw_dir.mkdir(parents=True, exist_ok=True)
    _wrap_provenance(client, args.provenance_log, args.raw_dir)

    inputs = pd.read_parquet(args.input)
    labelable = inputs[inputs["pair_id"].notna()]
    pair_ids = sorted(labelable["pair_id"].unique())
    if args.pairs_limit is not None:
        pair_ids = pair_ids[: args.pairs_limit]

    done = _completed_slots(args.calls_log)
    jobs = []
    for pid in pair_ids:
        rows = labelable[labelable["pair_id"] == pid].sort_values(["brief_date", "rank_in_day"])
        row = rows.iloc[0]
        catalyst = payload_from_row(row)
        theme = str(row["theme"])
        for slot in range(K):
            if (pid, slot) in done:
                continue
            jobs.append((pid, slot, theme, catalyst))
    print(
        f"phase1: {len(pair_ids)} pairs, {len(jobs)} calls to run ({len(done)} already complete)",
        file=sys.stderr,
    )

    with ThreadPoolExecutor(max_workers=THREADS) as ex:
        futs = [
            ex.submit(one_slot, client, args.calls_log, pair_id=p, slot=s, theme=t, catalyst=c)
            for p, s, t, c in jobs
        ]
        for i, fut in enumerate(as_completed(futs), 1):
            fut.result()
            if i % 25 == 0:
                print(f"phase1 progress {i}/{len(futs)}", file=sys.stderr)

    all_done = _completed_slots(args.calls_log)
    pair_aggs = {
        pid: aggregate_pair([all_done[(pid, s)] for s in range(K) if (pid, s) in all_done])
        for pid in pair_ids
    }
    if args.pairs_limit is not None:
        inputs = inputs[inputs["pair_id"].isin(pair_ids)]
    labels = build_label_table(inputs, pair_aggs)
    labels.to_parquet(args.out, index=False)

    pair_label_dist = Counter(a["pair_label"] for a in pair_aggs.values())
    summary = {
        "n_pairs": len(pair_ids),
        "n_rows": len(labels),
        "pair_label_distribution": dict(pair_label_dist),
        "row_label_distribution": labels["row_label"].value_counts().to_dict(),
        "n_unanimous_pairs": sum(1 for a in pair_aggs.values() if a["unanimous"]),
        "refused_vote_histogram": dict(Counter(a["n_refused_votes"] for a in pair_aggs.values())),
        "n_instrument_failure_pairs": pair_label_dist.get(LABEL_INSTRUMENT_FAILURE, 0),
        "generated_at": dt.datetime.now(dt.UTC).isoformat(),
    }
    args.summary.write_text(json.dumps(summary, indent=2, sort_keys=True))
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
